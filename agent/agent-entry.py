#!/usr/bin/env python3
"""PyInstaller entry point. Keeps the frozen exe's CLI identical to
`python -m aegis_agent`."""
import multiprocessing
import sys

from aegis_agent.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
