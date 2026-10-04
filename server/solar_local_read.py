"""Packaged solar reads bound to immutable stored graphs and durable requests."""
import copy
import importlib.util
from functools import lru_cache
from pathlib import Path

import solar_tools
import solar_artifacts
import write_loop  # before solar_physical_head: it puts da/ on sys.path for that module's `import store`
import solar_physical_head
import solar_physical_state
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError, _bounded_json
from solar_graph_context import resolve_graph_context
from solar_local_graph import request_digest, stable_numbers, _resolve_trusted, validate_pvcase_request
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


def _reads_physical_head(module):
    """Only a builtin that declares READS_PHYSICAL_HEAD = True (exactly) receives the head."""
    return getattr(module, "READS_PHYSICAL_HEAD", False) is True


def _current_physical_head(backend, tenant_id, drawing_id, project_id):
    """The drawing's current physical head for a read: None when no state was ever published,
    else {"head": view, "document": document}. One bounded head search (at most 13 log reads)
    and one artifact read; never writes. A physical state or head refusal passes through with
    its own PHYSICAL_* code."""
    try:
        view, document = solar_physical_head.load_physical_head(
            backend, tenant_id, drawing_id, project_id=project_id)
    except solar_physical_state.PhysicalStateError as exc:
        raise GraphValidationError(exc.code) from None
    except (OSError, RuntimeError, ValueError, LookupError, TypeError, AttributeError, ArithmeticError):
        raise GraphValidationError("PHYSICAL_HEAD_STORE_UNAVAILABLE") from None
    return None if view is None else {"head": view, "document": document}


def _recorded_physical_head(backend, tenant_id, drawing_id, project_id, output):
    """The physical head a finished read recorded under output["head"], re-read for its
    terminal proof. The head log is append-only and a later import or operation moves the
    current head, so the proof re-reads the entry the output names: the log entry at the
    recorded index must be byte-equal to the entry the recorded head implies, the state it
    names is loaded by id, and the head view is rebuilt from the stored state's own metadata,
    never copied from the output. The entry must also be chained the way the head reader
    requires: entry 0 names no parent, and any later entry's parent is the state of its
    predecessor, which is read through the head module's own validated entry reader. A log the
    head reader calls corrupt at that index therefore never proves. A recorded None is provable only
    while the head reader finds no head: its search reads the log's contiguous prefix from entry 0,
    so a log whose first entries were removed reads as empty to this proof exactly as it does to
    every reader (an inherited solar_physical_head limitation). At most two entry reads and one
    artifact read (or one head search for None); never writes.
    A malformed or mismatched record raises a ValueError, LookupError, TypeError or
    AttributeError, each of which graph_read_provenance turns into its one rejection."""
    recorded = output["head"]
    if recorded is None:
        if solar_physical_head.physical_head(backend, tenant_id, drawing_id,
                                             project_id=project_id) is not None:
            raise ValueError()
        return None
    index, parent = recorded["index"], recorded["parent"]
    artifact_id = recorded["state"]["artifact_id"]
    log = solar_physical_head._Log(backend, tenant_id, drawing_id)
    if log.get(index) != solar_physical_head.entry_bytes(index, project_id, parent, artifact_id,
                                                         recorded["state"]["content_sha256"]):
        raise ValueError()
    # Byte equality pins the recorded entry; its chain is the head reader's rule.
    if index == 0:
        if parent is not None:
            raise ValueError()
    elif log.entry(index - 1)["state"] != parent:
        raise ValueError()
    meta, document = solar_physical_state.load_physical_state(
        backend, tenant_id, drawing_id, artifact_id, project_id=project_id)
    state = {key: meta[key] for key in ("artifact_id", "media_type", "filename", "byte_length",
                                        "content_sha256", "source_version")}
    state.update(schema=solar_artifacts.REF_SCHEMA,
                 download=f"/api/drawings/{drawing_id}/artifacts/{meta['artifact_id']}")
    view = {"schema": solar_physical_head.HEAD_SCHEMA, "drawing_id": drawing_id,
            "project_id": project_id, "index": index, "parent": parent, "state": state}
    return {"head": view, "document": document}


def _physical_argument(tool, backend, tenant_id, drawing_id, project_id, recorded_output=None,
                       *, proof=False):
    """{} for every builtin that does not declare the physical head need, so its call is
    unchanged; else {"physical_head": ...}: the current head for a read, the recorded head for
    its terminal proof."""
    if not _reads_physical_head(_load_builtin(tool)):
        return {}
    if proof:
        return {"physical_head": _recorded_physical_head(
            backend, tenant_id, drawing_id, project_id, recorded_output)}
    return {"physical_head": _current_physical_head(backend, tenant_id, drawing_id, project_id)}


_UNSET = object()


def _read_output(tool, graph, builtin_params, sink=None, *, version_graph_sha256=None,
                 physical_head=_UNSET, pvcase_source=None):
    try:
        module = _load_builtin(tool)
        extra = {}
        if "pvcase_source" in solar_tools.get(tool)["trusted_inputs"]:
            extra["pvcase_source"] = copy.deepcopy(pvcase_source)
        if _reads_version_history(module):
            extra["version_graph_sha256"] = version_graph_sha256
        if _reads_physical_head(module):
            if physical_head is _UNSET:
                raise GraphValidationError("READ_OUTPUT_INVALID")
            # The document was decoded for this call alone; only the head view is compared below.
            extra["physical_head"] = None if physical_head is None else {
                "head": copy.deepcopy(physical_head["head"]), "document": physical_head["document"]}
        output = module.run(copy.deepcopy(graph), copy.deepcopy(builtin_params), **extra)
        if _reads_physical_head(module):
            # The terminal proof re-reads the head the output names, so the output must name it.
            expected = None if physical_head is None else physical_head["head"]
            physical_output = (output.summary if type(output) is solar_artifacts.ArtifactOutput
                               else output)
            if (type(physical_output) is not dict or "head" not in physical_output
                    or physical_output["head"] != expected):
                raise GraphValidationError("READ_OUTPUT_INVALID")
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
        if _reads_physical_head(module):
            output["head"] = copy.deepcopy(output["summary"]["head"])
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
    if "pvcase_source" in solar_tools.get(tool)["trusted_inputs"]:
        validate_pvcase_request(tool, params, adapter=True)
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
                                **_resolve_trusted(tool, backend, tenant_id, drawing_id,
                                                   source_version, context["graph_sha256"], None,
                                                   builtin_params, project_id=context["project_id"]),
                                **_history_argument(tool, backend, tenant_id, drawing_id),
                                **_physical_argument(tool, backend, tenant_id, drawing_id,
                                                     context["project_id"]))
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
                                    **_resolve_trusted(tool, backend, tenant_id, drawing_id,
                                                       source_version, context["graph_sha256"], None,
                                                       builtin_params, project_id=context["project_id"]),
                                    **_history_argument(tool, backend, tenant_id, drawing_id),
                                    **_physical_argument(tool, backend, tenant_id, drawing_id,
                                                         context["project_id"], result["output"],
                                                         proof=True))
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
