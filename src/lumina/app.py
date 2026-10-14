"""Entry point: the arguments, then the application."""

import argparse
import sys

from lumina import APP_ID, __version__


def main(argv=None):
    parser = argparse.ArgumentParser(prog=APP_ID, description="A local photo library.")
    parser.add_argument(
        "folder", nargs="?", help="a folder of photos to add to the library and show"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    # Qt and the window only now: --help and --version need none of it
    from lumina.application import run

    return run(args.folder)


if __name__ == "__main__":
    sys.exit(main())
