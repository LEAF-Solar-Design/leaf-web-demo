"""ARLO feeder design through a local JSON-file process boundary.

Canonical project identity comes from the claimed job, never from its params.
This adapter produces proposals only. Native CAD application is a separate,
accepted operation and is not authorized by a successful solve receipt.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable

TOOL_NAME = "arlo-design"
DEFAULT_SOLVER_ROOT = Path(__file__).resolve().parents[3] / "arlo-3dml"
MAX_SOLVE_SECONDS = 180.0
_IDENTITIES = {"organization_id": "org_id", "project_id": "project_id",
               "input_version_id": "input_version_id"}
_FIELDS = {"contract", *_IDENTITIES, "scenario", "catalog_version",
           "requirements_version", "catalog", "requirements", "placement_candidates",
           "native_bindings", "budget", "seed", "objective_version"}


class SolveCancelled(RuntimeError):
    """The worker lost custody or the caller cancelled before completion."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _validated_input(params: dict, job_context: dict) -> dict:
    if not isinstance(params, dict) or not isinstance(job_context, dict):
        raise ValueError("ARLO params and canonical job context must be objects")
    unknown = set(params) - _FIELDS
    if unknown:
        raise ValueError(f"unsupported ARLO request fields: {sorted(unknown)}")
    body = json.loads(_canonical_bytes(params))
    for request_key, context_key in _IDENTITIES.items():
        value = job_context.get(context_key)
        if value is None or not str(value).strip():
            raise ValueError(f"canonical job requires {context_key}")
        body[request_key] = str(value)
    if body.get("contract") != "arlo_design_request_v1":
        raise ValueError("ARLO requires contract arlo_design_request_v1")
    for name in ("catalog_version", "requirements_version"):
        if not isinstance(body.get(name), str) or not body[name].strip():
            raise ValueError(f"{name} must be a nonempty string")
    for name in ("scenario", "requirements"):
        if not isinstance(body.get(name), dict):
            raise ValueError(f"{name} must be an object")
    for name in ("catalog", "placement_candidates"):
        if not isinstance(body.get(name), list) or any(
                not isinstance(item, dict) for item in body[name]):
            raise ValueError(f"{name} must be an array of objects")
    if not body["catalog"]:
        raise ValueError("catalog must not be empty")
    if body.get("native_bindings") is not None and not isinstance(body["native_bindings"], dict):
        raise ValueError("native_bindings must be null or an object")
    budget = body.get("budget", {})
    if not isinstance(budget, dict):
        raise ValueError("budget must be an object")
    seconds = budget.get("timeout_seconds", MAX_SOLVE_SECONDS)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) \
            or not math.isfinite(seconds) or not 0 < seconds <= MAX_SOLVE_SECONDS:
        raise ValueError("budget.timeout_seconds must be greater than 0 and at most 180")
    # Detailed geometry, catalog and engineering validation belongs to the engine.
    # Do not silently change its defaults or duplicate its complete schema here.
    return body


def _root(solver_root: Path | None = None) -> Path:
    root = Path(solver_root or os.environ.get("ARLO_SOLVER_ROOT") or DEFAULT_SOLVER_ROOT).resolve()
    if not (root / "arlo" / "design" / "__main__.py").is_file():
        raise RuntimeError(f"ARLO design entrypoint is unavailable: {root}")
    return root


def _source_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted((root / "arlo").rglob("*.py"))
    for name in ("pyproject.toml", "requirements.txt"):
        if (root / name).is_file():
            files.append(root / name)
    for path in files:
        relative, body = path.relative_to(root).as_posix().encode("utf-8"), path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(body).to_bytes(8, "big"))
        digest.update(body)
    return digest.hexdigest()


def _model_bundle() -> Path | None:
    configured = os.environ.get("ARLO_MODEL_BUNDLE")
    if not configured:
        return None
    path = Path(configured).resolve()
    if not (path / "manifest.json").is_file():
        raise RuntimeError("configured ARLO model bundle requires manifest.json")
    return path


def _bundle_sha256(path: Path) -> str:
    # The engine validates the bundle manifest and each stage's weights itself.
    # Include actual bytes in attempt provenance so weights cannot drift in flight.
    digest = hashlib.sha256()
    for file in sorted(p for p in path.rglob("*") if p.is_file()):
        relative, body = file.relative_to(path).as_posix().encode("utf-8"), file.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(body).to_bytes(8, "big"))
        digest.update(body)
    return digest.hexdigest()


def descriptor(*, solver_root: Path | None = None,
               python_binary: str | None = None) -> dict[str, str]:
    root = _root(solver_root)
    binary = python_binary or os.environ.get("ARLO_PYTHON_EXECUTABLE") or sys.executable
    runtime = subprocess.run(
        [binary, "-I", "-c", "import sys; print(sys.version); print(sys.executable)"],
        capture_output=True, text=True, timeout=10, check=True)
    source_hash = _source_sha256(root)
    bundle = _model_bundle()
    if bundle is not None:
        source_hash = _sha256({"source": source_hash, "model_bundle": _bundle_sha256(bundle)})
    try:
        revision = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        revision = "sha256:" + source_hash
    return {"tool_name": TOOL_NAME, "source_revision": revision,
            "source_sha256": source_hash, "runtime": "python-" + runtime.stdout.strip()}


def _validated_result(value: Any, body: dict) -> dict:
    if not isinstance(value, dict) or value.get("contract") != "arlo_design_result_v1":
        raise RuntimeError("ARLO returned an invalid result contract")
    if value.get("status") not in ("complete", "incomplete", "cancelled"):
        raise RuntimeError("ARLO returned an invalid result status")
    if not isinstance(value.get("proposals"), list) or not isinstance(value.get("trace"), list):
        raise RuntimeError("ARLO result requires proposals and trace arrays")
    if value.get("status") == "complete" and not value["proposals"]:
        raise RuntimeError("ARLO complete result has no proposals")
    if value.get("production_valid") is not False:
        raise RuntimeError("ARLO proposal solve cannot declare production validity")
    if not isinstance(value.get("request_hash"), str) or not re.fullmatch(
            r"[0-9a-f]{64}", value["request_hash"]):
        raise RuntimeError("ARLO result requires a request hash")
    for proposal in value["proposals"]:
        source = proposal.get("source") if isinstance(proposal, dict) else None
        if not isinstance(source, dict) or any(
                source.get(key) != body[key] for key in _IDENTITIES):
            raise RuntimeError("ARLO proposal identity differs from the canonical job")
        if source.get("request_hash") != value["request_hash"]:
            raise RuntimeError("ARLO proposal request hash differs from its result")
    _canonical_bytes(value)  # Reject non-finite JSON numbers before durable hashing.
    return value


def run(params: dict, *, job_context: dict, solver_root: Path | None = None,
        python_binary: str | None = None, timeout_s: float = MAX_SOLVE_SECONDS + 10,
        cancelled: Callable[[], bool] | None = None) -> dict:
    if job_context.get("job_id") is not None:
        # A durable canonical job must consume its registered immutable input.
        # Direct local solver experiments without job IDs keep their old seam.
        import platform_link
        platform_link._load_platform()
        from leaf_platform.arlo_lab import load_registered_request
        source_params = load_registered_request(job_context, params)
    else:
        source_params = params
    body = _validated_input(source_params, job_context)
    if isinstance(timeout_s, bool) or not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("adapter timeout must be positive and finite")
    root = _root(solver_root)
    binary = python_binary or os.environ.get("ARLO_PYTHON_EXECUTABLE") or sys.executable
    source = descriptor(solver_root=root, python_binary=binary)
    should_cancel = cancelled or (lambda: False)
    # -E excludes inherited PYTHONPATH while retaining installed dependencies.
    # CWD is the trusted configured source root, not a caller-supplied location.
    with tempfile.TemporaryDirectory(prefix="arlo-design-") as directory:
        path = Path(directory)
        input_path, output_path = path / "request.json", path / "result.json"
        input_path.write_bytes(_canonical_bytes(body))
        cmd = [binary, "-E", "-B", "-m", "arlo.design", "--input", str(input_path),
               "--output", str(output_path)]
        bundle = _model_bundle()
        if bundle is not None:
            cmd.extend(["--bundle", str(bundle)])
        if should_cancel():
            raise SolveCancelled("ARLO solve cancelled before process start")
        with (path / "stdout.log").open("wb") as stdout, (path / "stderr.log").open("wb") as stderr:
            process = subprocess.Popen(cmd, cwd=root, stdin=subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr)
            deadline = time.monotonic() + min(timeout_s,
                float(body.get("budget", {}).get("timeout_seconds", MAX_SOLVE_SECONDS)) + 10)
            try:
                while process.poll() is None:
                    if should_cancel():
                        raise SolveCancelled("ARLO solve cancelled after custody was lost")
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(cmd, timeout_s)
                    time.sleep(0.05)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
        if should_cancel():
            raise SolveCancelled("ARLO solve cancelled before result acceptance")
        if process.returncode not in (0, 2):
            detail = (path / "stderr.log").read_text("utf-8", errors="replace")[-2000:]
            raise RuntimeError(f"ARLO exited {process.returncode}: {detail}")
        if not output_path.is_file() or output_path.stat().st_size > 32 * 1024 * 1024:
            raise RuntimeError("ARLO output file is missing or exceeds 32 MiB")
        try:
            result = _validated_result(json.loads(output_path.read_text("utf-8")), body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("ARLO returned invalid JSON") from exc
        if process.returncode == 2 and result["status"] == "complete":
            raise RuntimeError("ARLO non-success exit contradicts its complete result")
    if descriptor(solver_root=root, python_binary=binary) != source:
        raise RuntimeError("ARLO source or runtime changed during execution")
    return {"solver": TOOL_NAME, "solver_input": body, "solver_result": result,
            "request_sha256": _sha256(params), "input_sha256": _sha256(body),
            "result_sha256": _sha256(result), "solver_revision": source["source_revision"],
            "source_sha256": source["source_sha256"], "runtime": source["runtime"]}
