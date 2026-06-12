"""Entry point: `python -m orchestrator.main` (alias of the CLI)."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
