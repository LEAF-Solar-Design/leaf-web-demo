#!/usr/bin/env python3
"""Checkout entry point for the image-shipped storage collector."""
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from cost_meter.collect_storage_main import main  # noqa: E402


if __name__ == "__main__":
    sys.exit(main())
