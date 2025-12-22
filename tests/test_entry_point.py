#!/usr/bin/env python3
"""
Test script to verify the entry point works correctly.
"""

import sys
from ilha.cli import main

if __name__ == "__main__":
    # Simulate what happens when installed via pip
    sys.argv = ['ilha'] + sys.argv[1:]
    main()
