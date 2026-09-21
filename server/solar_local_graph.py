"""Packaged solar edits published through the existing graph commit rail."""
import copy
import hashlib
import importlib.util
import math
from functools import lru_cache
from pathlib import Path

import write_loop
import store
from solar_design_graph import GraphValidationError, _bounded_json
from solar_graph_context import resolve_graph_context
from solar_sizing_client import digest
from solar_solve_results import publish_version

ADAPTER_KIND = "local-graph-commit"
RESULT_SCHEMA = "leaf.solar-graph-commit.v1"
LOCAL_GRAPH_TOOLS = ("solar-settings", "solar-correct-string")


@lru_cache(maxsize=2)
def _load_builtin(tool):
    filenames = {"solar-settings": "solar_settings", "solar-correct-string": "solar_correct_string"}
    if tool not in LOCAL_GRAPH_TOOLS:
        raise GraphValidationError("UNKNOWN_LOCAL_GRAPH_TOOL")
    name = filenames[tool]
    spec = importlib.util.spec_from_file_location(
        "_local_graph_" + name, Path(__file__).resolve().parent / "builtins" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def request_digest(tool, drawing_id, source_version, builtin_params):
    return digest({"tool": tool, "drawing_id": drawing_id,
                   "source_version": source_version, "params": builtin_params})


def stable_numbers(value):
    """True when every float in a JSON value survives a JSONB round trip with the same spelling.

    Each container is walked once, so a cyclic or shared value terminates."""
    pending = [value]
    seen = set()
    while pending:
        item = pending.pop()
        if isinstance(item, float):
            if (not math.isfinite(item) or abs(item) >= 1e15
                    or (item == 0 and math.copysign(1, item) < 0)):
                return False
        elif isinstance(item, (dict, list, tuple)):
            if id(item) in seen:
                continue
            seen.add(id(item))
            pending.extend(item.values() if isinstance(item, dict) else item)
    return True


def graph_commit_provenance(result, params, tenant_id, job_id, tool, source_version, *, backend=None):
    """Bind a terminal receipt to its durable request and immutable stored version."""
    try:
        if (not isinstance(result, dict) or result["schema_version"] != RESULT_SCHEMA
                or result["adapter"] != ADAPTER_KIND or tool not in LOCAL_GRAPH_TOOLS
                or result["tool"] != tool or result["tenant_id"] != tenant_id
                or not isinstance(job_id, str) or not job_id or result["job_id"] != job_id
                or not isinstance(params, dict) or not isinstance(params["drawing_id"], str)
                or not stable_numbers(params)
                or result["drawing_id"] != params["drawing_id"]
                or type(source_version) is not int or source_version < 1):
            raise ValueError()
        builtin_params = copy.deepcopy(params)
        drawing_id = builtin_params.pop("drawing_id")
        request_sha256 = request_digest(tool, drawing_id, source_version, builtin_params)
        version = result["new_version"]["version"]
        if (result["request_sha256"] != request_sha256
                or type(version) is not int or version <= source_version
                or result["new_version"] != {"drawing_id": drawing_id, "version": version,
                                             "parent": source_version}
                or result["drawing_changed"] is not True
                or type(result["before_rev"]) is not int or type(result["after_rev"]) is not int
                or result["after_rev"] != result["before_rev"] + 1):
            raise ValueError()
        if backend is None:
            backend = write_loop.backend_for_tenant(tenant_id, aps_live=False, da=None)
        _, key, entry = store.resolve_version_entry(backend, tenant_id, drawing_id, version)
        if (entry["v"] != version or entry["parent"] != source_version
                or entry["workitem_id"] != "solar-graph:" + job_id
                or entry["note"] != "solar-graph-commit:" + request_sha256
                or entry["sha256"] != result["intake_sha256"]
                or hashlib.sha256(backend.get(key)).hexdigest() != result["intake_sha256"]):
            raise ValueError()
        context = resolve_graph_context(backend, tenant_id, drawing_id, version)
        if (context["representation"] != "intake"
                or context["graph_sha256"] != result["graph_sha256"]):
            raise ValueError()
        # The revisions and the before digest are read from the store, never trusted from the receipt.
        if context["graph"]["rev"] != result["after_rev"]:
            raise ValueError()
        parent = resolve_graph_context(backend, tenant_id, drawing_id, source_version)
        if (parent["representation"] != "intake"
                or parent["graph"]["rev"] != result["before_rev"]
                or parent["graph_sha256"] != result["before_graph_sha256"]):
            raise ValueError()
        return {"execution_mode": "local_graph_commit", "adapter": ADAPTER_KIND,
                "request_sha256": request_sha256, "graph_sha256": result["graph_sha256"],
                "intake_sha256": result["intake_sha256"], "source_version": source_version,
                "new_version": version}
    except (KeyError, AttributeError, TypeError, ValueError, OSError, RecursionError, RuntimeError):
        raise ValueError("graph commit terminal proof rejected") from None


def run_local_graph_commit(backend, tenant_id, tool, params, *, drawing_id, source_version,
                           holder, fence, job_id, project_id=None):
    if tool not in LOCAL_GRAPH_TOOLS:
        raise GraphValidationError("UNKNOWN_LOCAL_GRAPH_TOOL")
    _bounded_json(params)
    if type(params) is not dict:
        raise GraphValidationError("INVALID_SETTINGS_REQUEST" if tool == "solar-settings"
                                   else "INVALID_CORRECTION")
    if not stable_numbers(params):
        raise GraphValidationError("INVALID_NUMERIC_PARAM")
    if type(source_version) is not int or source_version < 1:
        raise GraphValidationError("INVALID_PARENT_VERSION")
    if "drawing_id" in params and (type(params["drawing_id"]) is not str
                                   or params["drawing_id"] != drawing_id):
        raise GraphValidationError("DRAWING_ID_CONFLICT")
    builtin_params = copy.deepcopy(params)
    builtin_params.pop("drawing_id", None)
    context = resolve_graph_context(backend, tenant_id, drawing_id, source_version,
                                    project_id=project_id)
    if context["representation"] == "dwg-bundle":
        raise GraphValidationError("LICENSED_GRAPH_COMMIT_REQUIRED")
    after = _load_builtin(tool).run(copy.deepcopy(context["graph"]), builtin_params)
    if builtin_params.get("cancel") is True:
        raise GraphValidationError("GRAPH_COMMIT_CANCELLED")
    request_sha256 = request_digest(tool, drawing_id, source_version, builtin_params)
    receipt = publish_version(
        backend, tenant_id, drawing_id, parent_version=source_version,
        before=context["graph"], after=after, holder=holder, fence=fence,
        job_id=job_id, request_sha256=request_sha256)
    try:
        reopened = resolve_graph_context(backend, tenant_id, drawing_id, receipt["version"])
        _, key = store.resolve_version(backend, tenant_id, drawing_id, receipt["version"])
        stored_sha = hashlib.sha256(backend.get(key)).hexdigest()
        if (reopened["representation"] != "intake"
                or reopened["graph_sha256"] != receipt["graph_sha256"]
                or reopened["resolved_version"] != receipt["version"]
                or stored_sha != receipt["intake_sha256"]):
            raise GraphValidationError("GRAPH_COMMIT_READBACK_FAILED")
    except (GraphValidationError, KeyError, ValueError, TypeError, OSError, RecursionError):
        raise GraphValidationError("GRAPH_COMMIT_READBACK_FAILED") from None
    return {"schema_version": RESULT_SCHEMA, "adapter": ADAPTER_KIND,
            "tenant_id": tenant_id, "job_id": job_id, "tool": tool,
            "project_id": context["project_id"], "drawing_id": drawing_id,
            "request_sha256": request_sha256,
            "new_version": {"drawing_id": drawing_id, "version": receipt["version"],
                            "parent": receipt["parent_version"]},
            "before_graph_sha256": context["graph_sha256"],
            "graph_sha256": receipt["graph_sha256"], "intake_sha256": receipt["intake_sha256"],
            "before_rev": context["graph"]["rev"], "after_rev": after["rev"],
            "drawing_changed": True, "replayed": receipt["replayed"]}
