"""
The fold lane's source: the multi-round (marathon) runs a tenant owns
(standardization slice 11a).

Layout, read-only, under ``LEAF_MARATHON_RUNS_DIR`` (unset = the lane is
unconfigured and reads as []):

  <dir>/<tenant_id>/<run_id>/state.json         the run's durable state
  <dir>/<tenant_id>/<run_id>/run-manifest.json  optional: title, requested_by
  <dir>/<tenant_id>/<run_id>/promotion.json     optional: a promotion artifact
                                                (prewarm_relay /
                                                app_store_connect_result /
                                                promotion_stage)

Tenant scoping is the directory: a tenant only ever reads its own subtree,
and a tenant id that is not a plain token never touches the filesystem.

HARDENING CONTRACT. Bounded everywhere: at most ``limit`` runs (newest state
first by mtime), every file capped (MAX_STATE_BYTES / MAX_SIDE_BYTES), a run
whose state.json is missing, oversized, not JSON or not an object is SKIPPED
and named in the warnings, never guessed at. Symlinked run directories are
skipped (a link out of the tenant's subtree is not that tenant's run).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DIR_ENV = "LEAF_MARATHON_RUNS_DIR"
MAX_STATE_BYTES = 1024 * 1024
MAX_SIDE_BYTES = 64 * 1024
MAX_RUNS = 200
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._@-]{1,128}$")


def configured() -> bool:
    return bool(os.environ.get(DIR_ENV, "").strip())


def _root() -> Optional[Path]:
    raw = os.environ.get(DIR_ENV, "").strip()
    return Path(raw) if raw else None


def _token(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not _TOKEN_RE.match(value) or set(value) <= {"."}:
        return None
    return value


def _read_json_object(path: Path, cap: int) -> Optional[Dict[str, Any]]:
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > cap:
            return None
        with open(path, "rb") as fh:
            raw = fh.read(cap + 1)
        if len(raw) > cap:
            return None
        body = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001 - unreadable == absent
        return None
    return body if isinstance(body, dict) else None


def list_runs(tenant_id: str, limit: int) -> Tuple[List[Dict[str, Any]], List[str]]:
    """``([{run_id, state, meta}, ...], warnings)`` for the tenant, newest
    first. ``meta`` is what ``build_queue.from_fold_state`` takes."""
    root = _root()
    warnings: List[str] = []
    if root is None:
        return [], warnings
    tenant = _token(tenant_id)
    if tenant is None:
        return [], ["fold: tenant id is not a plain token"]
    tenant_dir = root / tenant
    try:
        if not tenant_dir.is_dir():
            return [], warnings
        entries = [p for p in tenant_dir.iterdir() if p.is_dir() and not p.is_symlink() and _token(p.name)]
        entries.sort(key=lambda p: (p / "state.json").stat().st_mtime if (p / "state.json").is_file() else 0,
                     reverse=True)
    except OSError as exc:
        return [], [f"fold: runs directory unreadable ({type(exc).__name__})"]
    bounded = max(1, min(int(limit), MAX_RUNS))
    out: List[Dict[str, Any]] = []
    for run_dir in entries:
        if len(out) >= bounded:
            break
        state_path = run_dir / "state.json"
        state = _read_json_object(state_path, MAX_STATE_BYTES)
        if state is None:
            warnings.append(f"fold: run {run_dir.name} skipped (state.json missing, oversized or malformed)")
            continue
        try:
            stat = state_path.stat()
            mtime = stat.st_mtime
            started = os.path.getctime(run_dir)
        except OSError:
            mtime, started = None, None
        meta: Dict[str, Any] = {"run_id": run_dir.name, "state_mtime": mtime, "started_at": started}
        manifest = _read_json_object(run_dir / "run-manifest.json", MAX_SIDE_BYTES)
        if manifest is not None:
            for key in ("title", "requested_by"):
                if isinstance(manifest.get(key), str):
                    meta[key] = manifest[key]
            if isinstance(manifest.get("started_at"), (int, float, str)):
                meta["started_at"] = manifest["started_at"]
            if isinstance(manifest.get("receipts"), list):
                meta["receipts"] = manifest["receipts"]
        promotion = _read_json_object(run_dir / "promotion.json", MAX_SIDE_BYTES)
        if promotion is not None:
            for key in ("prewarm_relay", "app_store_connect_result", "promotion_stage"):
                if key in promotion:
                    meta[key] = promotion[key]
        out.append({"run_id": run_dir.name, "state": state, "meta": meta})
    return out, warnings
