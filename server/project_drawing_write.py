"""Project drawing.write producer: a registered builtin's pure mutations, committed
as exactly one new immutable project drawing version.

Contract (fails closed before any durable write):
- Support comes from trusted registry metadata plus a server-owned builtin file
  listed in PRODUCERS. The tool record never chooses the code that runs.
- The builtin's pure run(intake, params) sees a deep copy of the exact stored
  input version's intake. Its declared mutations go through the same
  write_loop.apply_mutations the legacy chain uses, and must remove at least one
  entity that exists, so a no-op never becomes a version.
- The output is emitted as DXF and published only through
  project_drawing_editor.save_dxf, which owns project role access, checkout
  custody, the parent compare-and-set and idempotent replay. One confirmation
  (Idempotency-Key) maps to one request id, so a replay with changed params or a
  changed parent is refused by the editor's replay fence instead of publishing.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid5

import product_capability_availability as capability_catalog
import entitlements
import intake_dxf
import project_drawing_editor as drawing_editor
import solar_project_context as project
import write_loop

SCHEMA = "leaf.project-drawing-write.v1"
_SERVER = Path(__file__).resolve().parent
_BUILTINS = (_SERVER / "builtins").resolve()
# Builtins whose run(intake, params) only DECLARES removed/added entities.
# Listing a name here is the only way a write tool gains a project producer.
PRODUCERS = {"delete-marked-panel": "builtins/delete_marked_panel.py"}
_NAMESPACE = UUID("0c01d7a1-5f1e-4b6e-9a51-6f0c3b8e3c1d")
MAX_PARAMS_BYTES = 16 * 1024
MAX_IDEMPOTENCY_KEY = 128


class ProducerRefused(ValueError):
    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status = code, status


def producer_entry(tool):
    """The server-owned builtin file for a trusted project write tool, else None."""
    if not isinstance(tool, dict):
        return None
    name = tool.get("name")
    entry = PRODUCERS.get(name)
    if (entry is None or tool.get("capabilities") != ["drawing.write"]
            or tool.get("kind") != "script" or tool.get("entry") != entry
            or not write_loop.is_write_tool(tool)
            or entitlements.tool_required_capability(tool) != "run_write"
            or tool.get("canonical_only") or "solar" in tool or "graph_input" in tool
            or name in capability_catalog.SOLAR_CAPABILITIES
            or capability_catalog.capability_adapter(name) is not None):
        return None
    return entry


def _builtin(entry):
    path = (_SERVER / entry).resolve()
    if path.parent != _BUILTINS or not path.is_file():
        raise ProducerRefused("PROJECT_WRITE_PRODUCER_UNAVAILABLE", 503)
    spec = importlib.util.spec_from_file_location("leaf_project_write_" + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "run", None)):
        raise ProducerRefused("PROJECT_WRITE_PRODUCER_UNAVAILABLE", 503)
    return module


def _handles(intake):
    return {str(row["handle"]) for key in drawing_editor._GEOMETRY
            for row in (intake.get(key) or []) if isinstance(row, dict) and row.get("handle") is not None}


def request_id_for(org_id, project_id, drawing_id, idempotency_key):
    """One confirmation key is one publication request in this drawing."""
    if (not isinstance(idempotency_key, str) or not idempotency_key
            or len(idempotency_key) > MAX_IDEMPOTENCY_KEY):
        raise ProducerRefused("PROJECT_WRITE_IDEMPOTENCY_REQUIRED", 400)
    return uuid5(_NAMESPACE, f"{org_id}:{project_id}:{drawing_id}:{idempotency_key}")


def commit(tenant, project_id, drawing_id, parent_id, tool, params, *,
           catalog_digest, idempotency_key, capability):
    entry = producer_entry(tool)
    if entry is None:
        raise ProducerRefused("PROJECT_WRITE_PRODUCER_UNAVAILABLE", 409)
    if not isinstance(catalog_digest, str) or not catalog_digest:
        raise ProducerRefused("PROJECT_WRITE_CONFIRMATION_REQUIRED", 409)
    try:
        params_json = json.dumps(params, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise ProducerRefused("PROJECT_WRITE_PARAMS_INVALID", 400) from None
    if not isinstance(params, dict) or len(params_json) > MAX_PARAMS_BYTES:
        raise ProducerRefused("PROJECT_WRITE_PARAMS_INVALID", 400)
    if not all(isinstance(value, UUID) for value in (project_id, drawing_id, parent_id)):
        project.refuse("INVALID_BINDING")
    # Role and tenant gate before any execution; save_dxf repeats it in its transaction.
    org_id, _actor = project._access(tenant, project_id, write=True)
    request_id = request_id_for(org_id, project_id, drawing_id, idempotency_key)
    parent = project.resolve_context(tenant, project_id, parent_id, drawing_id=drawing_id, write=True)
    try:
        result, overlay = _builtin(entry).run(deepcopy(parent.intake), deepcopy(params))
        mutations = result["mutations"]
        removed = [str(handle) for handle in mutations.get("removed") or []]
    except ProducerRefused:
        raise
    except Exception:
        raise ProducerRefused("PROJECT_WRITE_MUTATION_INVALID", 422) from None
    if not removed or not set(removed) <= _handles(parent.intake):
        raise ProducerRefused("PROJECT_WRITE_TARGET_NOT_FOUND", 422)
    try:
        output = intake_dxf.intake_to_dxf(write_loop.apply_mutations(parent.intake, mutations))
    except intake_dxf.IntakeDxfError:
        raise ProducerRefused("PROJECT_WRITE_DRAWING_UNSUPPORTED", 409) from None
    except (ValueError, TypeError, KeyError):
        raise ProducerRefused("PROJECT_WRITE_MUTATION_INVALID", 422) from None
    receipt = drawing_editor.save_dxf(tenant, project_id, drawing_id, parent_id, output,
                                      request_id=request_id, capability=capability)
    producer = {
        "schema": SCHEMA, "tool": tool["name"], "tool_version": tool.get("version"),
        "entry": entry, "catalog_digest": catalog_digest,
        "params_sha256": hashlib.sha256(params_json.encode("utf-8")).hexdigest(),
        "input_version_id": str(parent_id), "input_intake_sha256": parent.intake_sha256,
        "request_id": str(request_id),
    }
    return {"result": result, "overlay": overlay, "receipt": receipt, "producer": producer}
