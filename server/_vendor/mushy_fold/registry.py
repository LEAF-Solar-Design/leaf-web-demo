"""The call-time catalog fold: read one consumer repo's registry.json NOW.

Extracted from leaf-web-demo deps.load_tenant_repo_tools. The property that
matters: the fold reads CURRENT repo state at call time — a commit to the repo
is immediately live, and there is no cached catalog to invalidate. Missing
root / file / bad JSON -> [] (never raises): a broken registry must degrade to
"no folded tools", not take the catalog down.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REGISTRY_FILE = "registry.json"


def _default_on_error(reg: Path, exc: Exception) -> None:
    print(f"[mushy-fold] bad registry.json at {reg}: {exc}", file=sys.stderr)


def load_repo_registry_tools(
    root: Optional[Path],
    *,
    on_error: Callable[[Path, Exception], None] = _default_on_error,
) -> List[Dict[str, Any]]:
    """Fold ONE consumer repo's registry.json tools when the repo root resolves.

    Entries keep their ``entry`` RELATIVE to the repo root (e.g.
    ``tools/<name>/tool.py``); ``mushy_fold.entry.resolve_entry`` resolves it
    against the SAME root, so execution never depends on the process cwd and
    consumer A's artifacts never leak into consumer B's catalog.

    ``on_error`` receives the registry path and the exception when the file is
    unreadable/malformed (the fold still degrades to ``[]``). Consumers with an
    established operational log vocabulary inject their own diagnostic here —
    consumer #1 keeps its historical ``[leaf-demo] bad tenant registry.json``
    stderr line this way, so extraction changes no log classification.
    """
    if root is None:
        return []
    reg = Path(root) / REGISTRY_FILE
    if not reg.exists():
        return []
    try:
        data = json.loads(reg.read_text(encoding="utf-8"))
        tools = data.get("tools") if isinstance(data, dict) else None
        if not isinstance(tools, list):
            return []
        return [t for t in tools if isinstance(t, dict) and t.get("name")]
    except Exception as exc:  # pragma: no cover - defensive
        on_error(reg, exc)
        return []
