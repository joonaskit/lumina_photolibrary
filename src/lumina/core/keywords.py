"""Keyword names. A keyword is a path of names from the top, `Places > Finland > Helsinki`;
a photo given the last one is found under each of the others too.

Names are compared without regard to case, and kept as first written. A name cannot hold
the separator: a `>` typed inside one is read as a level, and the empty ones are dropped.
"""

SEPARATOR = " > "
_PARTS = (">", "|")


def parse(text):
    """The keyword `text` names as its normal path ("places>Finland" is "places > Finland"),
    or None when it holds no name."""
    if not isinstance(text, str):
        return None
    for mark in _PARTS[1:]:
        text = text.replace(mark, _PARTS[0])
    names = [" ".join(part.split()) for part in text.split(_PARTS[0])]
    names = [name for name in names if name]
    return SEPARATOR.join(names) if names else None


def levels(path):
    """The names of a keyword path, from the top."""
    return path.split(SEPARATOR)


def leaf(path):
    return levels(path)[-1]


def parent(path):
    """The path one level up, or None for a top keyword."""
    names = levels(path)
    return SEPARATOR.join(names[:-1]) if len(names) > 1 else None


def ancestors(path):
    """The paths above `path`, the top one first."""
    names = levels(path)
    return [SEPARATOR.join(names[:n]) for n in range(1, len(names))]


def within(path, top):
    """Whether `path` is `top` or below it (without regard to case)."""
    path, top = path.casefold(), top.casefold()
    return path == top or path.startswith(top + SEPARATOR.casefold())
