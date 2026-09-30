"""Packaged solar reads bound to immutable stored graphs and durable requests."""
import copy
import importlib.util
from functools import lru_cache
from pathlib import Path

import solar_tools
import solar_artifacts
import write_loop
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError, _bounded_json
from solar_graph_context import resolve_graph_context
from solar_local_graph import request_digest, stable_numbers
from solar_sizing_client import digest

ADAPTER_KIND = "local-graph-read"
RESULT_SCHEMA = "leaf.solar-graph-read.v1"
MAX_OUTPUT_BYTES = 1_048_576
MAX_HISTORY_LOOKUPS = 2
_RESULT_KEYS = frozenset((
    "schema_version", "adapter", "tenant_id", "job_id", "tool", "project_id", "drawing_id",
    "request_sha256", "source_version", "representation", "graph_sha256", "output",
    "output_sha256", "output_bytes", "drawing_changed",
))


def local_graph_read_tools():
    """Return read tools from the current validated registry."""
    return tuple(solar_tools.local_graph_read_tools())


LOCAL_GRAPH_READ_TOOLS = local_graph_read_tools()


@lru_cache(maxsize=solar_tools.MAX_DECLARATIONS)
def _load_builtin(tool):
    if tool not in local_graph_read_tools():
        raise GraphValidationError("UNKNOWN_LOCAL_GRAPH_READ_TOOL")
    builtin = solar_tools.get(tool)["builtin"]
    stem = Path(builtin).stem
    spec = importlib.util.spec_from_file_location(
        "_local_graph_read_" + stem, Path(__file__).resolve().parent / builtin)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _reads_version_history(module):
    """Only a builtin that declares READS_VERSION_HISTORY = True (exactly) receives the lookup."""
    return getattr(module, "READS_VERSION_HISTORY", False) is True


def _version_history_lookup(backend, tenant_id, drawing_id):
    """A read-only lookup into the version history of the drawing this read resolves.

    lookup(version) returns that version's graph_sha256, or None when the version is not a
    positive int, does not exist or carries no graph. Bounded: at most MAX_HISTORY_LOOKUPS calls
    per read; the next call fails closed with READ_HISTORY_LIMIT_EXCEEDED. It never writes.
    """
    calls = [0]

    def version_graph_sha256(version):
        if calls[0] >= MAX_HISTORY_LOOKUPS:
            raise GraphValidationError("READ_HISTORY_LIMIT_EXCEEDED")
        calls[0] += 1
        if type(version) is not int or version < 1:
            return None
        try:
            return resolve_graph_context(backend, tenant_id, drawing_id, version)["graph_sha256"]
        except GraphValidationError:
            return None

    return version_graph_sha256


def _history_argument(tool, backend, tenant_id, drawing_id):
    """{} for every builtin that does not declare the history need, so its call is unchanged."""
    if not _reads_version_history(_load_builtin(tool)):
        return {}
    return {"version_graph_sha256": _version_history_lookup(backend, tenant_id, drawing_id)}


def _read_output(tool, graph, builtin_params, sink=None, *, version_graph_sha256=None):
    try:
        module = _load_builtin(tool)
        if _reads_version_history(module):
            output = module.run(copy.deepcopy(graph), copy.deepcopy(builtin_params),
                                version_graph_sha256=version_graph_sha256)
        else:
            output = module.run(copy.deepcopy(graph), copy.deepcopy(builtin_params))
    except GraphValidationError:
        raise
    except (LookupError, ArithmeticError, TypeError, ValueError, RecursionError):
        raise GraphValidationError("LOCAL_GRAPH_READ_FAILED") from None
    prepared = None
    if type(output) is solar_artifacts.ArtifactOutput:
        if sink is None:
            raise GraphValidationError("READ_OUTPUT_INVALID")
        prepared = sink.prepare(output)
        output = {"summary": output.summary, "artifact": prepared.ref}
    elif solar_artifacts.artifact_references(output):
        raise GraphValidationError("ARTIFACT_REFERENCE_RESERVED")
    if type(output) is not dict:
        raise GraphValidationError("READ_OUTPUT_INVALID")
    try:
        _bounded_json(output)
        data = canonical_bytes(output)
    except (ValueError, TypeError, RecursionError):
        raise GraphValidationError("READ_OUTPUT_INVALID") from None
    if len(data) > MAX_OUTPUT_BYTES:
        raise GraphValidationError("READ_OUTPUT_LIMIT_EXCEEDED")
    if prepared is not None:
        sink.finish(prepared)
    return output, data


def run_local_graph_read(backend, tenant_id, tool, params, *, drawing_id, source_version,
                         job_id, project_id=None):
    if tool not in local_graph_read_tools():
        raise GraphValidationError("UNKNOWN_LOCAL_GRAPH_READ_TOOL")
    _bounded_json(params)
    if type(params) is not dict:
        raise GraphValidationError(solar_tools.get(tool)["invalid_request_code"])
    if not stable_numbers(params):
        raise GraphValidationError("INVALID_NUMERIC_PARAM")
    if type(source_version) is not int or source_version < 1:
        raise GraphValidationError("INVALID_SOURCE_VERSION")
    if "drawing_id" in params and (type(params["drawing_id"]) is not str
                                   or params["drawing_id"] != drawing_id):
        raise GraphValidationError("DRAWING_ID_CONFLICT")
    builtin_params = copy.deepcopy(params)
    builtin_params.pop("drawing_id", None)
    if "initialize" in builtin_params:
        raise GraphValidationError("READ_SEED_UNSUPPORTED")
    context = resolve_graph_context(backend, tenant_id, drawing_id, source_version,
                                    project_id=project_id)
    request_sha256 = request_digest(tool, drawing_id, source_version, builtin_params)
    sink = solar_artifacts.ArtifactSink(backend, tenant_id, drawing_id, context, tool,
                                        request_sha256, False)
    output, data = _read_output(tool, context["graph"], builtin_params, sink,
                                **_history_argument(tool, backend, tenant_id, drawing_id))
    return {
        "schema_version": RESULT_SCHEMA, "adapter": ADAPTER_KIND,
        "tenant_id": tenant_id, "job_id": job_id, "tool": tool,
        "project_id": context["project_id"], "drawing_id": drawing_id,
        "request_sha256": request_sha256,
        "source_version": context["resolved_version"], "representation": context["representation"],
        "graph_sha256": context["graph_sha256"], "output": output,
        "output_sha256": digest(output), "output_bytes": len(data), "drawing_changed": False,
    }


def graph_read_provenance(result, params, tenant_id, job_id, tool, source_version, *, backend=None):
    """Re-derive the entire read from the durable request and stored source version."""
    try:
        if (not isinstance(result, dict) or set(result) != _RESULT_KEYS
                or result["schema_version"] != RESULT_SCHEMA or result["adapter"] != ADAPTER_KIND
                or tool not in local_graph_read_tools() or result["tool"] != tool
                or result["tenant_id"] != tenant_id
                or not isinstance(job_id, str) or not job_id or result["job_id"] != job_id
                or not isinstance(params, dict) or not isinstance(params["drawing_id"], str)
                or not stable_numbers(params) or "initialize" in params
                or result["drawing_id"] != params["drawing_id"]
                or type(source_version) is not int or source_version < 1
                or type(result["source_version"]) is not int
                or result["source_version"] != source_version
                or result["drawing_changed"] is not False):
            raise ValueError()
        _bounded_json(params)
        builtin_params = copy.deepcopy(params)
        drawing_id = builtin_params.pop("drawing_id")
        request_sha256 = request_digest(tool, drawing_id, source_version, builtin_params)
        if result["request_sha256"] != request_sha256:
            raise ValueError()
        if backend is None:
            backend = write_loop.backend_for_tenant(tenant_id, aps_live=False, da=None)
        context = resolve_graph_context(backend, tenant_id, drawing_id, source_version)
        if (context["resolved_version"] != result["source_version"]
                or context["representation"] != result["representation"]
                or context["graph_sha256"] != result["graph_sha256"]
                or context["project_id"] != result["project_id"]):
            raise ValueError()
        sink = solar_artifacts.ArtifactSink(backend, tenant_id, drawing_id,
                                            context, tool, request_sha256, True)
        for reference in solar_artifacts.artifact_references(result["output"]):
            sink.verify_reference(reference)
        output, data = _read_output(tool, context["graph"], builtin_params, sink,
                                    **_history_argument(tool, backend, tenant_id, drawing_id))
        output_sha256 = digest(output)
        if (type(result["output"]) is not dict or output != result["output"]
                or output_sha256 != result["output_sha256"]
                or type(result["output_bytes"]) is not int or len(data) != result["output_bytes"]):
            raise ValueError()
        return {"execution_mode": "local_graph_read", "adapter": ADAPTER_KIND,
                "request_sha256": request_sha256, "graph_sha256": context["graph_sha256"],
                "source_version": source_version, "output_sha256": output_sha256}
    except (LookupError, ArithmeticError, AttributeError, TypeError, ValueError, OSError,
            RecursionError, RuntimeError):
        raise ValueError("graph read terminal proof rejected") from None
