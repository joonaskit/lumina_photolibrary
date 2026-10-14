import sys

from lumina.app import main

# Guarded: the worker processes' server imports the main module again
if __name__ == "__main__":
    sys.exit(main())
