"""Entry point: the arguments, then the application."""

import argparse
import os
import sys
from urllib.parse import unquote, urlparse

from lumina import APP_ID, __version__


def local_path(argument):
    """The absolute path an argument names: a path, or the `file://` URL that portals
    and some file managers pass. None for any other URL."""
    if "://" in argument:
        url = urlparse(argument)
        if url.scheme != "file" or url.netloc not in ("", "localhost"):
            return None
        argument = unquote(url.path)
    return os.path.abspath(os.path.expanduser(argument))


def main(argv=None):
    parser = argparse.ArgumentParser(prog=APP_ID, description="A local photo library.")
    parser.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help="a folder of photos to add to the library and show, or photos to open",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    paths = [path for path in map(local_path, args.paths) if path is not None]

    # Qt and the window only now: --help and --version need none of it
    from lumina.application import run

    return run(paths)


if __name__ == "__main__":
    sys.exit(main())
