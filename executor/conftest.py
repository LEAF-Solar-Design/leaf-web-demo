"""Test harness for the executor tree.

Every test under executor/ imports its subject through the fully-qualified
package path (``from executor.control_plane.jws import ...``), so the REPO
ROOT must be importable. Before this conftest that only happened by accident:
``python -m pytest executor`` from the repo root put the CWD on ``sys.path``,
which also let the repo's ``platform/`` package shadow the stdlib ``platform``
module (the exact hazard platform/tests/conftest.py exists to defuse), and any
other invocation (``pytest executor``, or running from executor/ itself, as
scripts/run-all-gates.py does) failed with ``No module named 'executor'``.

The fix, in order:

  1. Evict any repo-root entries from ``sys.path`` and drop a poisoned
     ``sys.modules['platform']``, exactly like platform/tests/conftest.py, so
     an inherited shadow from a repo-root invocation cannot leak in.
  2. APPEND the repo root — never prepend. ``executor`` still resolves
     (nothing earlier on the path claims that name), while ``import platform``
     keeps finding the stdlib module in the interpreter's own path entries,
     which now all sort ahead of the repo root.

The append also pre-empts pytest's prepend-mode package-root insertion: were
the repo root absent, importing a test module whose package root is the repo
root would re-insert it at the FRONT of ``sys.path`` and re-arm the shadow.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_EXECUTOR_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _EXECUTOR_DIR.parent


def _abs(p: str) -> str:
    return os.path.abspath(p or ".")


sys.path[:] = [p for p in sys.path if _abs(p) != str(_REPO_ROOT)]
_shadow = sys.modules.get("platform")
if _shadow is not None and str(getattr(_shadow, "__file__", "") or "").startswith(
        str(_REPO_ROOT)):
    del sys.modules["platform"]
sys.path.append(str(_REPO_ROOT))
import platform as _stdlib_platform  # noqa: E402,F401  re-resolves to the stdlib
