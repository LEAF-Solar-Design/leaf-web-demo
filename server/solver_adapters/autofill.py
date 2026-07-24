"""Isolated adapter for the existing ``autofill-solver`` target solver.

The API process never imports solver code.  A worker invokes the solver in a
separate Python process with a JSON-only boundary and records input/result hashes
plus the exact source revision used for the attempt.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_SOLVER_ROOT = Path(__file__).resolve().parents[3] / "autofill-solver"
TOOL_NAME = "string-autofill-opt"
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _validated_input(params: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("autofill params must be an object")
    groups = params.get("groups")
    if not isinstance(groups, list) or not groups or any(not isinstance(g, dict) for g in groups):
        raise ValueError("autofill groups must be a non-empty array of objects")
    panels_per_string = params.get("panelsPerString")
    if not isinstance(panels_per_string, int) or isinstance(panels_per_string, bool) \
            or panels_per_string < 3:
        raise ValueError("panelsPerString must be an integer greater than or equal to 3")
    options = params.get("options", {})
    if not isinstance(options, dict):
        raise ValueError("autofill options must be an object")
    # JSON round-trip both proves the process-boundary contract and prevents the
    # solver from mutating a caller-owned object.
    return json.loads(json.dumps({
        "groups": groups,
        "panelsPerString": panels_per_string,
        "options": options,
        "panelAngle": params.get("panelAngle", 0.0),
        "panelWidth": params.get("panelWidth", 0.0),
        "panelHeight": params.get("panelHeight", 0.0),
    }, allow_nan=False))


def _git_source_revision(root: Path) -> Optional[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
            text=True, timeout=5, check=True)
        revision = completed.stdout.strip()
        if _COMMIT_RE.fullmatch(revision):
            return revision
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _image_source_revision(root: Path) -> Optional[str]:
    marker = root / ".leaf-source-revision"
    if not marker.is_file():
        return None
    revision = marker.read_text(encoding="utf-8").strip()
    if not _COMMIT_RE.fullmatch(revision):
        raise RuntimeError("autofill solver image contains an invalid source revision marker")
    return revision


def _source_revision(root: Path) -> str:
    configured = os.environ.get("AUTOFILL_SOLVER_REVISION", "").strip()
    image_revision = _image_source_revision(root)
    observed = _git_source_revision(root)
    if configured:
        if not _COMMIT_RE.fullmatch(configured):
            raise RuntimeError(
                "AUTOFILL_SOLVER_REVISION must be an exact lowercase 40-character commit")
        if image_revision is not None and image_revision != configured:
            raise RuntimeError(
                "AUTOFILL_SOLVER_REVISION does not match the worker image")
        if observed is not None and observed != configured:
            raise RuntimeError(
                "AUTOFILL_SOLVER_REVISION does not match the solver checkout")
        return configured
    if image_revision is not None:
        if observed is not None and observed != image_revision:
            raise RuntimeError("worker image revision does not match the solver checkout")
        return image_revision
    if observed is not None:
        return observed
    solver_file = root / "solver.py"
    if not solver_file.is_file():
        raise RuntimeError(f"autofill solver source not found: {solver_file}")
    return "sha256:" + hashlib.sha256(solver_file.read_bytes()).hexdigest()


def _source_sha256(root: Path) -> str:
    """Digest the exact Python source tree available to the isolated solver."""
    digest = hashlib.sha256()
    files = sorted(path for path in root.rglob("*.py")
                   if "__pycache__" not in path.parts)
    if not files:
        raise RuntimeError(f"autofill solver source not found: {root}")
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        body = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(body).to_bytes(8, "big"))
        digest.update(body)
    return digest.hexdigest()


def descriptor(*, solver_root: Optional[Path] = None) -> Dict[str, str]:
    root = Path(solver_root or os.environ.get("AUTOFILL_SOLVER_ROOT")
                or DEFAULT_SOLVER_ROOT).resolve()
    if not (root / "solver.py").is_file():
        raise RuntimeError(f"autofill solver repository is unavailable: {root}")
    return {"tool_name": TOOL_NAME, "source_revision": _source_revision(root),
            "source_sha256": _source_sha256(root),
            "runtime": f"python-{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"}


def run(params: Dict[str, Any], *, solver_root: Optional[Path] = None,
        timeout_s: float = 60.0) -> Dict[str, Any]:
    """Run the real target solver through a deterministic JSON subprocess seam."""
    body = _validated_input(params)
    root = Path(solver_root or os.environ.get("AUTOFILL_SOLVER_ROOT")
                or DEFAULT_SOLVER_ROOT).resolve()
    if not (root / "solver.py").is_file():
        raise RuntimeError(f"autofill solver repository is unavailable: {root}")
    source = descriptor(solver_root=root)
    script = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import solver
body = json.load(sys.stdin)
result = solver.solve_targets(
    body['groups'], body['panelsPerString'], body['options'],
    panel_angle=body['panelAngle'], panel_width=body['panelWidth'],
    panel_height=body['panelHeight'])
if not isinstance(result, dict):
    raise TypeError('autofill solver returned a non-object result')
sys.stdout.write(json.dumps(result, sort_keys=True, separators=(',', ':'), allow_nan=False))
"""
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script, str(root)],
        input=_canonical_bytes(body), capture_output=True, timeout=timeout_s, env=env,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"autofill solver failed with exit {completed.returncode}: {detail[-1000:]}")
    try:
        result = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("autofill solver returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise RuntimeError("autofill solver returned a non-object result")
    source_after = descriptor(solver_root=root)
    if source_after != source:
        raise RuntimeError("autofill solver source or runtime changed during execution")
    return {
        "solver": TOOL_NAME,
        "solver_input": body,
        "request_sha256": _sha256(params),
        "solver_result": result,
        "input_sha256": _sha256(body),
        "result_sha256": _sha256(result),
        "solver_revision": source["source_revision"],
        "source_sha256": source["source_sha256"],
        "runtime": source["runtime"],
    }
