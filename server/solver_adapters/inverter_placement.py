"""Isolated adapter for the ``aws-inverter-placement`` target solver.

Mirrors ``solver_adapters/autofill.py``: the API process never imports solver
code directly.  A worker invokes the real solver in a separate Python process
with a JSON-only boundary and records input/result hashes plus the exact
source revision used for the attempt.

Route decision: local import/subprocess, not HTTP-to-staging.  The target
repository's heavy dependencies (numpy, scipy, shapely, matplotlib,
scikit-learn) are pip-installed and importable in this environment, so the
deterministic local path is practical.  The solver's deps live in user
site-packages, so ``-I`` (which implies ``-s`` and would hide them) is not
usable; instead of relying on a clean stdout pipe, the subprocess writes its
result to an explicit out-FILE, so any interpreter-startup or solver stdout
noise cannot corrupt the JSON boundary regardless of the inherited
environment.  The solver source digest is RECORDED on every run and can be
pinned to an approved value via ``expected_source_sha256`` for fail-closed
provenance in the registration lane.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_SOLVER_ROOT = Path(__file__).resolve().parents[3] / "aws-inverter-placement"
TOOL_NAME = "inverter-placement"
# minimax is the exposed optimizer: it raises NoFeasibleAssignmentError, so the
# adapter can report infeasibility HONESTLY. The legacy solver (inv_optim) only
# prints a warning and returns a result with pairs left unassigned, so exposing
# it would report infeasible-as-success (review finding). Legacy stays a
# documented follow-up until its infeasibility signal is characterised against
# its own tests.
_ALGORITHMS = {"minimax"}
_DEFAULT_ITERATIONS = {"minimax": 15}
_MARKER_FILES = ("minimax_optimizer.py",)
_ALLOWED_KEYS = {"algorithm", "groups", "stringsPerInverter", "iterations", "fixedCentroids"}
_ALLOWED_STRING_KEYS = {"handle", "startPoint", "endPoint"}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _validated_input(params: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("inverter placement params must be an object")

    unknown = set(params) - _ALLOWED_KEYS
    if unknown:
        raise ValueError(f"inverter placement rejects unknown fields: {sorted(unknown)}")

    algorithm = params.get("algorithm", "minimax")
    if algorithm not in _ALGORITHMS:
        raise ValueError(f"algorithm must be one of {sorted(_ALGORITHMS)} "
                         "(legacy is a documented follow-up, not yet exposed)")

    groups = params.get("groups")
    if not isinstance(groups, list) or not groups or any(not isinstance(g, dict) for g in groups):
        raise ValueError("inverter placement groups must be a non-empty array of objects")
    for group in groups:
        strings = group.get("strings")
        if not isinstance(strings, list) or not strings or any(not isinstance(s, dict) for s in strings):
            raise ValueError("each group must have a non-empty 'strings' array of objects")
        # Every string must carry a resolvable start/end endpoint, else the
        # solver crashes deep in geometry with an opaque subprocess failure.
        for s in strings:
            for endpoint in ("startPoint", "endPoint"):
                pt = s.get(endpoint)
                if not isinstance(pt, dict) or not isinstance(pt.get("coordinate"), str) \
                        or not pt["coordinate"].strip():
                    raise ValueError(
                        f"each string needs {endpoint}.coordinate as a non-empty string")

    strings_per_inverter = params.get("stringsPerInverter")
    if not isinstance(strings_per_inverter, int) or isinstance(strings_per_inverter, bool) \
            or strings_per_inverter < 1:
        raise ValueError("stringsPerInverter must be an integer greater than or equal to 1")

    iterations = params.get("iterations", _DEFAULT_ITERATIONS[algorithm])
    if not isinstance(iterations, int) or isinstance(iterations, bool) or iterations < 1:
        raise ValueError("iterations must be an integer greater than or equal to 1")

    fixed_centroids = params.get("fixedCentroids")
    if fixed_centroids is not None and not isinstance(fixed_centroids, list):
        raise ValueError("fixedCentroids must be an array when provided")

    # JSON round-trip both proves the process-boundary contract and prevents the
    # solver from mutating a caller-owned object.
    return json.loads(json.dumps({
        "algorithm": algorithm,
        "groups": groups,
        "stringsPerInverter": strings_per_inverter,
        "iterations": iterations,
        "fixedCentroids": fixed_centroids,
    }, allow_nan=False))


def _require_solver_root(root: Path) -> Path:
    for marker in _MARKER_FILES:
        if not (root / marker).is_file():
            raise RuntimeError(f"inverter placement solver repository is unavailable: {root}")
    return root


def _source_revision(root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
            text=True, timeout=5, check=True)
        revision = completed.stdout.strip()
        if revision:
            return revision
    except (OSError, subprocess.SubprocessError):
        pass
    marker = root / "minimax_optimizer.py"
    if not marker.is_file():
        raise RuntimeError(f"inverter placement solver source not found: {marker}")
    return "sha256:" + hashlib.sha256(marker.read_bytes()).hexdigest()


def _source_sha256(root: Path) -> str:
    """Digest the exact Python source tree available to the isolated solver."""
    digest = hashlib.sha256()
    files = sorted(path for path in root.rglob("*.py")
                   if "__pycache__" not in path.parts)
    if not files:
        raise RuntimeError(f"inverter placement solver source not found: {root}")
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        body = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(body).to_bytes(8, "big"))
        digest.update(body)
    return digest.hexdigest()


def descriptor(*, solver_root: Optional[Path] = None) -> Dict[str, str]:
    root = _require_solver_root(Path(solver_root or os.environ.get("INVERTER_SOLVER_ROOT")
                                      or DEFAULT_SOLVER_ROOT).resolve())
    return {"tool_name": TOOL_NAME, "source_revision": _source_revision(root),
            "source_sha256": _source_sha256(root),
            "runtime": f"python-{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"}


# minimax_optimizer prints progress to stdout during solve, so the ENTIRE
# body -- imports included, before any redirect could start -- plus any
# interpreter startup hook could emit stdout noise. Rather than fight that on
# the stdout pipe, the result JSON is written to an explicit out-FILE (argv[2])
# and stdout is left free to absorb noise harmlessly (same immune boundary the
# combiner adapter uses). Infeasibility is a structured ``{"ok": false, ...}``
# payload, since "no feasible assignment" is an expected outcome, not a crash.
_SUBPROCESS_SCRIPT = r"""
import contextlib, io, json, sys
solver_root, out_path = sys.argv[1], sys.argv[2]
sys.path.insert(0, solver_root)
body = json.load(sys.stdin)
solver_payload = {'groups': body['groups']}
if body.get('fixedCentroids') is not None:
    solver_payload['fixed_centroids'] = body['fixedCentroids']
solver_json = json.dumps(solver_payload)
buffer = io.StringIO()
with contextlib.redirect_stdout(buffer):
    import minimax_optimizer
    try:
        result = minimax_optimizer.run_minimax_optimization(
            solver_json, body['stringsPerInverter'],
            max_iterations=body['iterations'], visualize=False)
        payload = {'ok': True, 'result': result}
    except minimax_optimizer.NoFeasibleAssignmentError as exc:
        payload = {'ok': False, 'error': 'no_feasible_assignment',
                   'message': str(exc), 'details': exc.details}
if payload['ok'] and not isinstance(payload['result'], dict):
    raise TypeError('inverter placement solver returned a non-object result')
with open(out_path, 'w', encoding='utf-8') as fh:
    json.dump(payload, fh, sort_keys=True, separators=(',', ':'), allow_nan=False)
"""


def run(params: Dict[str, Any], *, solver_root: Optional[Path] = None,
        timeout_s: float = 120.0,
        expected_source_sha256: Optional[str] = None) -> Dict[str, Any]:
    """Run the real target solver through a deterministic JSON subprocess seam.

    ``expected_source_sha256`` (optional): when the registration lane pins an
    APPROVED solver digest, pass it here; the run fails closed if the live
    source does not match, so a solver modified out-of-band cannot execute and
    be reported as new provenance. When omitted the digest is still RECORDED in
    the result (not silently trusted as approved).
    """
    import tempfile
    body = _validated_input(params)
    root = _require_solver_root(Path(solver_root or os.environ.get("INVERTER_SOLVER_ROOT")
                                      or DEFAULT_SOLVER_ROOT).resolve())
    source = descriptor(solver_root=root)
    if expected_source_sha256 is not None and source["source_sha256"] != expected_source_sha256:
        raise RuntimeError(
            "inverter placement solver source does not match the approved digest; refusing to run")
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    with tempfile.TemporaryDirectory() as tmp:
        out_path = str(Path(tmp) / "result.json")
        completed = subprocess.run(
            [sys.executable, "-c", _SUBPROCESS_SCRIPT, str(root), out_path],
            input=_canonical_bytes(body), capture_output=True, timeout=timeout_s, env=env,
            cwd=str(root),
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"inverter placement solver failed with exit {completed.returncode}: {detail[-1000:]}")
        try:
            with open(out_path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("inverter placement solver returned invalid JSON") from exc
    if not isinstance(payload, dict) or "ok" not in payload:
        raise RuntimeError("inverter placement solver returned a malformed payload")
    source_after = descriptor(solver_root=root)
    if source_after != source:
        raise RuntimeError("inverter placement solver source or runtime changed during execution")
    return {
        "solver": TOOL_NAME,
        "algorithm": body["algorithm"],
        "solver_input": body,
        "request_sha256": _sha256(params),
        "feasible": bool(payload["ok"]),
        "solver_result": payload.get("result"),
        "infeasible_details": payload.get("details"),
        "input_sha256": _sha256(body),
        "result_sha256": _sha256(payload),
        "solver_revision": source["source_revision"],
        "source_sha256": source["source_sha256"],
        "runtime": source["runtime"],
    }
