#!/usr/bin/env python3
"""
PyInstaller entry point for the quick-support executable.

Double-clicking it starts a support session directly -- no subcommand, no
arguments, nothing for the person to configure. The relay URL and enrollment
key are baked in by build-support-exe.ps1.

Any argument passed on the command line still works, so the same binary can
be used as a full agent by an administrator:
    AegisDesk-Support.exe status
"""
import multiprocessing
import sys

from aegis_agent.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    argv = sys.argv[1:]
    if not argv:
        argv = ["support"]
    sys.exit(main(argv))
