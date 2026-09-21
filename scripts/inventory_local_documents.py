#!/usr/bin/env python3
"""Compatibility CLI for the packaged metadata-only document discovery engine."""
from pathlib import Path
import sys

ENGINE = Path(__file__).resolve().parents[1] / 'distribution/macos-local-memory/engine'
sys.path.insert(0, str(ENGINE))

# Keep the original Python API (including shared os/time modules used by safety
# tests), while the installed application imports the same implementation.
from document_locations import *  # noqa: F401,F403,E402


if __name__ == '__main__':
    raise SystemExit(main())
