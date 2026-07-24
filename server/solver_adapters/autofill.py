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
SOURCE_ATTESTATION = ".leaf-source-attestation.json"


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


def _exact_revision(value: str, label: str = "AUTOFILL_SOLVER_REVISION") -> str:
    revision = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise RuntimeError(f"{label} must be an exact Git commit")
    return revision


def _git_revision(root: Path) -> Optional[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
            text=True, timeout=5, check=True)
        return _exact_revision(completed.stdout.strip(), "autofill solver source revision")
    except (OSError, subprocess.SubprocessError):
        return None


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


def attest_source(root: Path, revision: str, manifest_path: Path) -> Dict[str, str]:
    """Verify copied solver bytes against a trusted commit manifest and seal them."""
    root = Path(root).resolve()
    revision = _exact_revision(revision)
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        expected_sha256 = str(manifest[revision]).strip().lower()
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"no trusted autofill source digest for revision {revision}") from exc
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise RuntimeError(f"invalid trusted source digest for revision {revision}")
    actual_sha256 = _source_sha256(root)
    if actual_sha256 != expected_sha256:
        raise RuntimeError(
            f"autofill source does not match revision {revision}: "
            f"expected {expected_sha256}, got {actual_sha256}")
    attestation = {"revision": revision, "source_sha256": actual_sha256}
    (root / SOURCE_ATTESTATION).write_text(
        json.dumps(attestation, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return attestation


def _source_identity(root: Path) -> Dict[str, str]:
    supplied_raw = os.environ.get("AUTOFILL_SOLVER_REVISION", "").strip()
    supplied = _exact_revision(supplied_raw) if supplied_raw else None
    actual_sha256 = _source_sha256(root)
    attestation_path = root / SOURCE_ATTESTATION
    if attestation_path.is_file():
        try:
            attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
            attested_revision = _exact_revision(
                attestation["revision"], "attested autofill solver revision")
            attested_sha256 = str(attestation["source_sha256"]).strip().lower()
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("autofill source attestation is invalid") from exc
        if not re.fullmatch(r"[0-9a-f]{64}", attested_sha256) \
                or attested_sha256 != actual_sha256:
            raise RuntimeError("autofill source bytes do not match their build attestation")
        if supplied is not None and supplied != attested_revision:
            raise RuntimeError(
                "AUTOFILL_SOLVER_REVISION does not match the attested solver source")
        return {"source_revision": attested_revision, "source_sha256": actual_sha256}

    derived = _git_revision(root)
    if supplied is not None:
        if derived is None:
            raise RuntimeError(
                "AUTOFILL_SOLVER_REVISION cannot be verified without Git metadata "
                "or a build attestation")
        if supplied != derived:
            raise RuntimeError(
                "AUTOFILL_SOLVER_REVISION does not match the checked-out solver source")
    revision = derived or f"sha256:{actual_sha256}"
    return {"source_revision": revision, "source_sha256": actual_sha256}


def descriptor(*, solver_root: Optional[Path] = None) -> Dict[str, str]:
    root = Path(solver_root or os.environ.get("AUTOFILL_SOLVER_ROOT")
                or DEFAULT_SOLVER_ROOT).resolve()
    if not (root / "solver.py").is_file():
        raise RuntimeError(f"autofill solver repository is unavailable: {root}")
    source = _source_identity(root)
    return {"tool_name": TOOL_NAME, **source,
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
