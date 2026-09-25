"""Keep the trusted module directory independent of the checkout's package name."""

from pathlib import Path
import sys


TRUSTED_MODULES = str(Path(__file__).resolve().parents[1])
if TRUSTED_MODULES not in sys.path:
    sys.path.insert(0, TRUSTED_MODULES)
