"""Built-in: highlight-panels-near-edge. The file IS the tool.

Delegates to the shared pure-python primitive so the §3 result/overlay stay
byte-identical to the recorded baseline.
"""
from __future__ import annotations

import os
import sys

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import tools_fallback as fb  # noqa: E402


def run(intake, params):
    return fb.op_highlight_near_edge(intake, params or {})
