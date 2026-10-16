"""The XMP sidecar: a `.xmp` file beside a photo (`IMG_0001.CR3.xmp`, so a photo's RAW and
JPEG never share one) holding what Lumina keeps of it in its database: the edits, the flag,
the stars and the keywords.

The standard parts, `xmp:Rating` and `dc:subject`, are for other programs. Lumina reads
back only its own, in a namespace of its own (`lumina:`): there is no promise of `crs:`
compatibility with Adobe's, and an `.xmp` that does not carry our namespace is another
program's: never read as ours, and never written over.

Written to a hidden part file and put in place; the photo itself is never touched.
"""

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from lumina.core import keywords as keywordm
from lumina.core.errors import PhotoError

NAMESPACE = "https://github.com/joonaskit/lumina_photolibrary/ns/1.0/"
VERSION = 1
# A sidecar is a few kilobytes: one far larger is not one of ours
_MOST_BYTES = 8 * 1024 * 1024

_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_XMP = "http://ns.adobe.com/xap/1.0/"
_DC = "http://purl.org/dc/elements/1.1/"
_X = "adobe:ns:meta/"
for _prefix, _uri in (("rdf", _RDF), ("xmp", _XMP), ("dc", _DC), ("x", _X), ("lumina", NAMESPACE)):
    ET.register_namespace(_prefix, _uri)


class SidecarError(PhotoError):
    """A sidecar that could not be written or read; names the file."""


class ForeignSidecar(SidecarError):
    """An `.xmp` another program made is there."""


@dataclass(frozen=True)
class SidecarState:
    # When it last changed in the library, as time.time() says (what the files are
    # compared by: the newer wins)
    updated: float
    flag: int = 0
    rating: int = 0
    # Keyword paths (core/keywords.py)
    keywords: tuple[str, ...] = ()
    # ((copy number, parameters JSON), ...): 0 is the photo's own edit
    edits: tuple[tuple[int, str], ...] = ()
    # (stack key, position in the stack, the top the lowest), or None for no stack
    stack: tuple[str, int] | None = None


def sidecar_path(photo_path):
    return photo_path + ".xmp"


def _q(namespace, name):
    return f"{{{namespace}}}{name}"


def build(state):
    """The sidecar for `state` (a SidecarState) as bytes."""
    root = ET.Element(_q(_X, "xmpmeta"))
    rdf = ET.SubElement(root, _q(_RDF, "RDF"))
    description = ET.SubElement(rdf, _q(_RDF, "Description"))
    description.set(_q(_RDF, "about"), "")
    description.set(_q(_XMP, "Rating"), str(state.rating))
    description.set(_q(NAMESPACE, "Version"), str(VERSION))
    description.set(_q(NAMESPACE, "Updated"), repr(float(state.updated)))
    description.set(_q(NAMESPACE, "Flag"), str(state.flag))
    if state.stack is not None:
        description.set(_q(NAMESPACE, "Stack"), state.stack[0])
        description.set(_q(NAMESPACE, "StackPosition"), str(state.stack[1]))
    if state.keywords:
        # Every name on the way, once, for programs that know no levels
        names = {}
        for path in state.keywords:
            for name in keywordm.levels(path):
                names.setdefault(name.casefold(), name)
        _bag(description, _q(_DC, "subject"), names.values())
        _bag(description, _q(NAMESPACE, "Keywords"), state.keywords)
    if state.edits:
        edits = ET.SubElement(description, _q(NAMESPACE, "Edits"))
        sequence = ET.SubElement(edits, _q(_RDF, "Seq"))
        for copy, params in state.edits:
            item = ET.SubElement(sequence, _q(_RDF, "li"))
            item.set(_q(NAMESPACE, "Copy"), str(copy))
            item.set(_q(NAMESPACE, "Params"), params)
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def _bag(parent, tag, values):
    bag = ET.SubElement(ET.SubElement(parent, tag), _q(_RDF, "Bag"))
    for value in values:
        ET.SubElement(bag, _q(_RDF, "li")).text = value


def is_ours(data):
    """Whether sidecar bytes carry Lumina's namespace (as bytes, not parsed: a half-written
    file of ours is still ours)."""
    return NAMESPACE.encode() in data


def parse(data, path="the sidecar"):
    """The SidecarState in `data`, or a SidecarError naming `path`. A file that is not
    ours is a ForeignSidecar."""
    if not is_ours(data):
        raise ForeignSidecar(path, "It was made by another program")
    # Nothing of ours has a document type, and entities are how an XML file lies about its
    # size
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise SidecarError(path, "It holds a document type, which a sidecar of ours never does")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise SidecarError(path, f"It is not readable XML ({error})") from error
    for description in root.iter(_q(_RDF, "Description")):
        if description.get(_q(NAMESPACE, "Version")) is not None:
            return _state(description, path)
    raise SidecarError(path, "It holds nothing of Lumina's")


def _state(description, path):
    def number(name, kind, default):
        try:
            return kind(description.get(_q(NAMESPACE, name), default))
        except ValueError:
            raise SidecarError(path, f"{name} is not a number") from None

    version = number("Version", int, 0)
    if version > VERSION:
        raise SidecarError(path, f"It is from a newer Lumina (sidecar version {version})")
    keywords = []
    holder = description.find(_q(NAMESPACE, "Keywords"))
    if holder is not None:
        for item in holder.iter(_q(_RDF, "li")):
            found = keywordm.parse(item.text or "")
            if found and found.casefold() not in {k.casefold() for k in keywords}:
                keywords.append(found)
    edits = []
    holder = description.find(_q(NAMESPACE, "Edits"))
    if holder is not None:
        for item in holder.iter(_q(_RDF, "li")):
            try:
                copy = int(item.get(_q(NAMESPACE, "Copy"), ""))
            except ValueError:
                raise SidecarError(path, "An edit has no copy number") from None
            edits.append((copy, item.get(_q(NAMESPACE, "Params"), "")))
    stack_key = description.get(_q(NAMESPACE, "Stack"))
    return SidecarState(
        stack=(stack_key, number("StackPosition", int, 0)) if stack_key else None,
        updated=number("Updated", float, 0.0),
        flag=number("Flag", int, 0),
        rating=_rating(description),
        keywords=tuple(keywords),
        edits=tuple(sorted(edits)),
    )


def _rating(description):
    try:
        return int(description.get(_q(_XMP, "Rating"), "0"))
    except ValueError:
        return 0


def read(photo_path):
    """The state in the sidecar of the photo at `photo_path`, or None when it has none.
    Raises SidecarError (ForeignSidecar for another program's)."""
    path = sidecar_path(photo_path)
    try:
        with open(path, "rb") as handle:
            data = handle.read(_MOST_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise SidecarError(path, error.strerror or str(error)) from error
    if len(data) > _MOST_BYTES:
        raise ForeignSidecar(path, "It is too large to be one of ours")
    return parse(data, path)


def write(photo_path, state):
    """Write the sidecar of the photo at `photo_path` and return the file's time (ns).

    An existing file is replaced only when it is ours; another program's (or an
    unreadable one) raises ForeignSidecar, and any other failure a SidecarError, both
    naming the file. The photo is not touched."""
    path = sidecar_path(photo_path)
    existing = _existing(path)
    data = build(state)
    part = os.path.join(
        os.path.dirname(path), f".lumina-xmp-{os.getpid()}-{os.path.basename(path)}.part"
    )
    try:
        with open(part, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if existing:
            os.replace(part, path)
        else:
            _put_new(part, path)
        return os.stat(path).st_mtime_ns
    except SidecarError:
        raise
    except OSError as error:
        raise SidecarError(path, error.strerror or str(error)) from error
    finally:
        try:
            os.unlink(part)
        except OSError:
            pass


def _existing(path):
    """Whether a sidecar of ours is at `path` (False for none); ForeignSidecar for any
    other file."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(_MOST_BYTES + 1)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise SidecarError(path, error.strerror or str(error)) from error
    if len(data) > _MOST_BYTES or not is_ours(data):
        raise ForeignSidecar(path, "A file from another program is there, and is left as it is")
    return True


def _put_new(part, path):
    # A hard link fails when the name is taken, so a file that appeared since is not written
    # over; a filesystem without links (a card's exFAT) is checked and renamed instead
    try:
        os.link(part, path)
    except FileExistsError:
        raise ForeignSidecar(
            path, "A file appeared there meanwhile, and is left as it is"
        ) from None
    except OSError:
        if os.path.lexists(path):
            raise ForeignSidecar(
                path, "A file appeared there meanwhile, and is left as it is"
            ) from None
        os.replace(part, path)
