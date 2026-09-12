"""Allow ``python -m hotaisle ...`` without installing the console script."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
