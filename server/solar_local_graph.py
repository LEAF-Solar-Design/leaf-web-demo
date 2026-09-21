"""Packaged solar edits published through the existing graph commit rail."""
import copy
import hashlib
import importlib.util
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


def run_local_graph_commit(backend, tenant_id, tool, params, *, drawing_id, source_version,
                           holder, fence, job_id, project_id=None):
    if tool not in LOCAL_GRAPH_TOOLS:
        raise GraphValidationError("UNKNOWN_LOCAL_GRAPH_TOOL")
    _bounded_json(params)
    if type(params) is not dict:
        raise GraphValidationError("INVALID_SETTINGS_REQUEST" if tool == "solar-settings"
                                   else "INVALID_CORRECTION")
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
