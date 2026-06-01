#!/usr/bin/env python3
"""Backward-compatible entry-point shim.

The actual code lives in the memecheck/ package now. You can still run
`python3 memecheck.py <ADDR>` and it will keep working, but the recommended
invocations going forward are:

    python3 -m memecheck <ADDR>     # any working directory
    memecheck <ADDR>                # after `pip install .`
"""

from memecheck.cli import main

if __name__ == "__main__":
    main()
