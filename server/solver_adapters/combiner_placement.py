"""Isolated adapter for the existing ``combiner-placement`` Node solver.

The API process never imports solver code.  A worker invokes the solver in a
separate Node process with a JSON-only boundary and records input/result
hashes plus the exact source revision used for the attempt.  This mirrors
``solver_adapters/autofill.py``'s subprocess-isolation contract, adapted to
the real target solver's CLI: the Node entrypoint
(``aws-combiner-placement/sim/simulate-row-end-optimizer.js``) reads its
input dump and writes its result from/to *files* (``--dump`` / ``--out``),
not stdin/stdout, so the JSON-only boundary here is a pair of temp files
instead of a pipe.  This is the same calling convention the solver's own
production wrapper (``aws-combiner-placement/app.py::solve_dump``) uses, and
it was verified to run locally (Node on PATH) before this adapter committed
to the subprocess route -- see the module docstring's "Route" note below for
the verification command.

Route: verified locally with ``node --version`` (v22.17.1 present on PATH)
and a live run of ``sim/simulate-row-end-optimizer.js`` against a minimal
``{"inputs": {}}`` dump, which produced a well-formed, byte-stable
``solution`` object (only the wrapping ``generatedUtc`` timestamp varies
between runs).  Node subprocess invocation is therefore the adapter's route;
no HTTP-to-staging fallback is implemented because it was not needed.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

# aws-combiner-placement is a sibling checkout of leaf-web-demo, same layout
# convention as autofill.py's DEFAULT_SOLVER_ROOT (parents[3] = the GitHub
# folder that holds every first-party repo as a sibling directory).
DEFAULT_SOLVER_ROOT = Path(__file__).resolve().parents[3] / "aws-combiner-placement"
SOLVER_SCRIPT_RELATIVE = Path("sim") / "simulate-row-end-optimizer.js"
TOOL_NAME = "combiner-placement-row-end"

_ALLOWED_PLACEMENT_STRATEGIES = {"row-end", "lane-sweep"}
_ALLOWED_LOCATION_MODES = {"segment", "chunk"}
# Fail closed on unknown fields: an option this adapter does not wire into the
# solver CLI (e.g. optimizeL2Swaps) must be REJECTED, never silently dropped so
# the caller believes it was applied. These are the only keys the adapter maps.
_ALLOWED_TOP_LEVEL_KEYS = {"dump", "options"}
_ALLOWED_OPTION_KEYS = {"stringsPerInput", "dcInputsPerL2", "placementStrategy", "locationMode"}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _validated_input(params: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("combiner placement params must be an object")
    unknown = set(params) - _ALLOWED_TOP_LEVEL_KEYS
    if unknown:
        raise ValueError(f"combiner placement rejects unknown fields: {sorted(unknown)}")
    dump = params.get("dump")
    if not isinstance(dump, dict):
        raise ValueError("combiner placement dump must be an object")
    inputs = dump.get("inputs")
    if not isinstance(inputs, dict):
        raise ValueError("combiner placement dump.inputs must be an object")
    options = params.get("options", {})
    if not isinstance(options, dict):
        raise ValueError("combiner placement options must be an object")
    # JSON round-trip both proves the process-boundary contract and prevents the
    # solver from mutating a caller-owned object.
    return json.loads(json.dumps({
        "dump": dump,
        "options": options,
    }, allow_nan=False))


def _validated_options(options: Dict[str, Any]) -> Dict[str, Any]:
    unknown = set(options) - _ALLOWED_OPTION_KEYS
    if unknown:
        raise ValueError(
            f"combiner placement rejects unsupported options (not wired to the "
            f"solver CLI): {sorted(unknown)}")
    strings_per_input = options.get("stringsPerInput", 10)
    dc_inputs_per_l2 = options.get("dcInputsPerL2", 36)
    placement_strategy = options.get("placementStrategy", "lane-sweep")
    location_mode = options.get("locationMode", "segment")
    if not isinstance(strings_per_input, int) or isinstance(strings_per_input, bool) \
            or strings_per_input < 1:
        raise ValueError("options.stringsPerInput must be a positive integer")
    if not isinstance(dc_inputs_per_l2, int) or isinstance(dc_inputs_per_l2, bool) \
            or dc_inputs_per_l2 < 1:
        raise ValueError("options.dcInputsPerL2 must be a positive integer")
    if placement_strategy not in _ALLOWED_PLACEMENT_STRATEGIES:
        raise ValueError("options.placementStrategy must be 'row-end' or 'lane-sweep'")
    if location_mode not in _ALLOWED_LOCATION_MODES:
        raise ValueError("options.locationMode must be 'segment' or 'chunk'")
    return {
        "stringsPerInput": strings_per_input,
        "dcInputsPerL2": dc_inputs_per_l2,
        "placementStrategy": placement_strategy,
        "locationMode": location_mode,
    }


def _node_binary() -> str:
    return os.environ.get("NODE_BINARY", "node")


def _node_version(node_binary: str) -> str:
    try:
        completed = subprocess.run(
            [node_binary, "--version"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"node binary is unavailable or not runnable: {node_binary}") from exc
    if completed.returncode != 0:
        raise RuntimeError(f"node binary is unavailable or not runnable: {node_binary}")
    return completed.stdout.strip().lstrip("v")


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
    script = root / SOLVER_SCRIPT_RELATIVE
    if not script.is_file():
        raise RuntimeError(f"combiner placement solver entrypoint not found: {script}")
    return "sha256:" + hashlib.sha256(script.read_bytes()).hexdigest()


def _source_sha256(root: Path) -> str:
    """Digest the exact Node source tree available to the isolated solver."""
    sim_dir = root / "sim"
    digest = hashlib.sha256()
    files = sorted(path for path in sim_dir.rglob("*.js")
                   if "node_modules" not in path.parts)
    if not files:
        raise RuntimeError(f"combiner placement solver source not found: {sim_dir}")
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        body = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(body).to_bytes(8, "big"))
        digest.update(body)
    return digest.hexdigest()


def descriptor(*, solver_root: Optional[Path] = None,
               node_binary: Optional[str] = None) -> Dict[str, str]:
    root = Path(solver_root or os.environ.get("COMBINER_PLACEMENT_SOLVER_ROOT")
                or DEFAULT_SOLVER_ROOT).resolve()
    script = root / SOLVER_SCRIPT_RELATIVE
    if not script.is_file():
        raise RuntimeError(f"combiner placement solver repository is unavailable: {root}")
    binary = node_binary or _node_binary()
    return {"tool_name": TOOL_NAME, "source_revision": _source_revision(root),
            "source_sha256": _source_sha256(root),
            "runtime": f"node-{_node_version(binary)}"}


def run(params: Dict[str, Any], *, solver_root: Optional[Path] = None,
        timeout_s: float = 180.0) -> Dict[str, Any]:
    """Run the real target solver through a deterministic JSON subprocess seam.

    The Node entrypoint's real CLI contract is file-based (``--dump`` input
    file, ``--out`` result file) rather than stdin/stdout, so this adapter
    writes the caller's dump to a temp file and reads the solver's result
    back from another temp file inside a single ``TemporaryDirectory`` that
    is cleaned up before returning.
    """
    body = _validated_input(params)
    options = _validated_options(body["options"])
    root = Path(solver_root or os.environ.get("COMBINER_PLACEMENT_SOLVER_ROOT")
                or DEFAULT_SOLVER_ROOT).resolve()
    script = root / SOLVER_SCRIPT_RELATIVE
    if not script.is_file():
        raise RuntimeError(f"combiner placement solver repository is unavailable: {root}")
    node_binary = _node_binary()
    source = descriptor(solver_root=root, node_binary=node_binary)

    with tempfile.TemporaryDirectory(prefix="combiner-placement-adapter-") as tmp:
        tmp_path = Path(tmp)
        dump_path = tmp_path / "dump.json"
        out_path = tmp_path / "response.json"
        dump_path.write_bytes(_canonical_bytes(body["dump"]))
        cmd = [
            node_binary, str(script),
            "--dump", str(dump_path),
            "--out", str(out_path),
            "--solution-only", "--compact-out",
            "--strings-per-input", str(options["stringsPerInput"]),
            "--dc-inputs", str(options["dcInputsPerL2"]),
            "--placement-strategy", options["placementStrategy"],
            "--location-mode", options["locationMode"],
        ]
        completed = subprocess.run(
            cmd, cwd=str(root), capture_output=True, timeout=timeout_s,
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"combiner placement solver failed with exit {completed.returncode}: {detail[-1000:]}")
        if not out_path.is_file():
            raise RuntimeError("combiner placement solver did not produce an output file")
        try:
            result = json.loads(out_path.read_text("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("combiner placement solver returned invalid JSON") from exc

    if not isinstance(result, dict):
        raise RuntimeError("combiner placement solver returned a non-object result")
    solution = result.get("solution")
    if not isinstance(solution, dict):
        raise RuntimeError("combiner placement solver result missing a solution object")
    source_after = descriptor(solver_root=root, node_binary=node_binary)
    if source_after != source:
        raise RuntimeError("combiner placement solver source or runtime changed during execution")
    return {
        "solver": TOOL_NAME,
        "solver_input": body,
        "request_sha256": _sha256(params),
        # solver_result is the full solver payload (includes a wall-clock
        # generatedUtc timestamp, so it is NOT byte-stable across runs);
        # result_sha256 hashes only the deterministic "solution" object.
        "solver_result": result,
        "input_sha256": _sha256(body),
        "result_sha256": _sha256(solution),
        "solver_revision": source["source_revision"],
        "source_sha256": source["source_sha256"],
        "runtime": source["runtime"],
    }
