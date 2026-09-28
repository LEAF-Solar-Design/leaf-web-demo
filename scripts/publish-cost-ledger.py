#!/usr/bin/env python3
"""Publish the cost ledger through the image-shipped cost_meter entry point."""
from pathlib import Path
import sys

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from cost_meter.publish_main import main  # noqa: E402


if __name__ == "__main__":
    sys.exit(main())
