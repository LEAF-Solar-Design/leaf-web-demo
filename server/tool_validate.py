"""
Authoring oracle for the dynamic tool loader (replaces hand-written mirrors).

Three responsibilities (CONTRACT §3 / hot-script-tier SPEC §8.4 step 1 + §10.2):

  validate_params(tool, params)  -> [errors]   pre-run gate; non-empty => the
      tool BODY MUST NOT RUN (caller returns a BAD_PARAMS envelope).
  validate_envelope(env)         -> [errors]   post-run: the §3 core envelope
      (8 keys) must match engine/envelope_schema.json. The extended
      `degraded_mode` key (ADDENDUM §10) is projected out before validation so
      the frozen §3 schema (additionalProperties:false) still applies.
  static_scan(code_str)          -> [findings] ADVISORY (non-blocking v1): flags
      dangerous imports/calls; findings surface in provenance + author preview.

IMPORTANT: jsonschema is imported LAZILY (inside the functions), never at module
top — importing this module must stay cheap and must not drag in `platform`
(which the repo-root `platform/` package would shadow if the repo root is on
sys.path before `platform` is first imported).
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Dict, List

SERVER_DIR = Path(__file__).resolve().parent
ENGINE_SCHEMA_PATH = SERVER_DIR.parent / "engine" / "envelope_schema.json"

# The frozen §3 envelope keys (engine/envelope_schema.json). The extended body
# additionally carries `degraded_mode` (ADDENDUM §10); it is projected out here.
CORE_KEYS = ("ok", "tool", "version", "result", "overlay", "timing_ms", "cost", "error")

_PY_TYPES = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "object": dict,
    "array": list,
    "null": type(None),
}


# --------------------------------------------------------------------------- #
# 1) params pre-validation
# --------------------------------------------------------------------------- #
def validate_params(tool: Dict[str, Any], params: Dict[str, Any]) -> List[str]:
    """Validate params against the tool's own `params` JSON Schema (§2).

    Empty list == valid. A non-empty list means the caller MUST reject the run
    with a BAD_PARAMS envelope and NEVER execute the tool body.
    """
    schema = (tool or {}).get("params")
    if not isinstance(schema, dict) or not schema:
        return []
    params = params if params is not None else {}
    try:
        import jsonschema  # lazy

        validator = jsonschema.Draft7Validator(schema)
        return [
            f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
            for e in sorted(validator.iter_errors(params), key=lambda e: list(e.path))
        ]
    except ImportError:
        return _structural_params(schema, params)


def _structural_params(schema: Dict[str, Any], params: Any) -> List[str]:
    errs: List[str] = []
    if schema.get("type") == "object" and not isinstance(params, dict):
        return ["<root>: expected object"]
    props = schema.get("properties", {}) or {}
    for req in schema.get("required", []) or []:
        if not isinstance(params, dict) or req not in params:
            errs.append(f"{req}: required property missing")
    if isinstance(params, dict):
        for key, val in params.items():
            spec = props.get(key)
            if isinstance(spec, dict) and "type" in spec:
                py = _PY_TYPES.get(spec["type"])
                # bool is a subclass of int — reject it for number/integer
                if spec["type"] in ("number", "integer") and isinstance(val, bool):
                    errs.append(f"{key}: expected {spec['type']}")
                elif py is not None and not isinstance(val, py):
                    errs.append(f"{key}: expected {spec['type']}")
        if schema.get("additionalProperties") is False:
            for key in params:
                if key not in props:
                    errs.append(f"{key}: additional property not allowed")
    return errs


# --------------------------------------------------------------------------- #
# 2) envelope post-validation (against the FROZEN §3 schema)
# --------------------------------------------------------------------------- #
def core_envelope(env: Dict[str, Any]) -> Dict[str, Any]:
    """Project an extended envelope down to the frozen §3 8-key core."""
    return {k: env[k] for k in CORE_KEYS if k in env}


def _load_engine_schema() -> Any:
    try:
        return json.loads(ENGINE_SCHEMA_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def validate_envelope(env: Dict[str, Any]) -> List[str]:
    """Validate the §3 CORE envelope against engine/envelope_schema.json.

    Applies to SUCCESS envelopes (ok:true, result is an object). Error envelopes
    (result null) are a different shape and are not validated here.
    """
    if not isinstance(env, dict):
        return ["envelope must be an object"]
    core = core_envelope(env)
    schema = _load_engine_schema()
    if schema is not None:
        try:
            import jsonschema  # lazy

            validator = jsonschema.Draft7Validator(schema)
            return [
                f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
                for e in validator.iter_errors(core)
            ]
        except ImportError:
            pass
    return _structural_envelope(core)


def _structural_envelope(env: Dict[str, Any]) -> List[str]:
    errs: List[str] = []
    for k in ("ok", "tool", "version", "result", "timing_ms", "error"):
        if k not in env:
            errs.append(f"missing required key '{k}'")
    if not isinstance(env.get("ok"), bool):
        errs.append("ok must be bool")
    if not isinstance(env.get("tool"), str):
        errs.append("tool must be str")
    if not isinstance(env.get("version"), str):
        errs.append("version must be str")
    if not isinstance(env.get("result"), dict):
        errs.append("result must be object")
    if not isinstance(env.get("timing_ms"), (int, float)):
        errs.append("timing_ms must be number")
    if env.get("overlay") is not None and not isinstance(env.get("overlay"), dict):
        errs.append("overlay must be object or null")
    if env.get("cost") is not None and not isinstance(env.get("cost"), dict):
        errs.append("cost must be object or null")
    if env.get("error") is not None and not isinstance(env.get("error"), str):
        errs.append("error must be str or null")
    extra = set(env) - set(CORE_KEYS)
    if extra:
        errs.append(f"unexpected keys: {sorted(extra)}")
    return errs


# --------------------------------------------------------------------------- #
# 3) advisory static scan (SPEC §10.2) — non-blocking in v1
# --------------------------------------------------------------------------- #
_FLAGGED_MODULES = {
    "os", "subprocess", "socket", "requests", "shutil", "ctypes",
    "importlib", "pickle", "urllib", "http", "sys",
}
_FLAGGED_CALLS = {"eval", "exec", "open", "__import__", "compile", "input"}


def static_scan(code_str: str) -> List[str]:
    """AST walk flagging dangerous imports/builtins. Advisory only (v1)."""
    findings: List[str] = []
    try:
        tree = ast.parse(code_str or "")
    except SyntaxError as exc:
        return [f"syntax error: {exc}"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _FLAGGED_MODULES:
                    findings.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _FLAGGED_MODULES:
                findings.append(f"from {node.module} import ...")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _FLAGGED_CALLS:
                findings.append(f"call {node.func.id}()")
    # de-dupe, preserve order
    seen = set()
    out: List[str] = []
    for f in findings:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out
