#!/usr/bin/env python3
import sys
from pathlib import Path

# Ensure package directory is on sys.path
repo_root = Path(__file__).resolve().parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from sensor_overview.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

