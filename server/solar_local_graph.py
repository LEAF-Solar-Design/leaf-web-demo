"""Packaged solar edits published through the existing graph commit rail."""
import copy
import hashlib
import importlib.util
import json
import math
import re
from functools import lru_cache
from pathlib import Path

import write_loop
import store
import solar_tools
from solar_design_graph import GraphValidationError, _bounded_json
from solar_graph_context import resolve_graph_context
from solar_graph_seed import new_empty_graph, resolve_seed_context, validate_seed_request
from solar_sizing_client import digest
from solar_solve_results import canonical_bytes, publish_version, version_companion

ADAPTER_KIND = "local-graph-commit"
RESULT_SCHEMA = "leaf.solar-graph-commit.v1"
SEED_RESULT_SCHEMA = "leaf.solar-graph-seed.v1"


def local_graph_tools():
    """Return the local tools from the current validated registry."""
    return tuple(solar_tools.local_graph_tools())


LOCAL_GRAPH_TOOLS = local_graph_tools()


@lru_cache(maxsize=solar_tools.MAX_DECLARATIONS)
def _load_builtin(tool):
    if tool not in local_graph_tools():
        raise GraphValidationError("UNKNOWN_LOCAL_GRAPH_TOOL")
    builtin = solar_tools.get(tool)["builtin"]
    name = Path(builtin).stem
    spec = importlib.util.spec_from_file_location(
        "_local_graph_" + name, Path(__file__).resolve().parent / builtin)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def request_digest(tool, drawing_id, source_version, builtin_params, *, proposal_candidate=None):
    payload = {"tool": tool, "drawing_id": drawing_id,
               "source_version": source_version, "params": builtin_params}
    if proposal_candidate is not None:
        from solar_proposal_candidate import proposal_identity
        payload["trusted_inputs"] = {"proposal_candidate": proposal_identity(proposal_candidate)}
    return digest(payload)


def _source_intake(backend, tenant_id, drawing_id, version, graph_sha256):
    """Resolve immutable intake bytes bound to the graph used by the builtin."""
    try:
        _, key, entry = store.resolve_version_entry(backend, tenant_id, drawing_id, version)
        data = backend.get(key)
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ValueError()
        intake = json.loads(data)
        if (not isinstance(intake, dict) or "solar_design_graph" not in intake
                or "solar_design_graph_sha256" not in intake
                or intake["solar_design_graph_sha256"] != graph_sha256):
            raise ValueError()
        del intake["solar_design_graph"]
        del intake["solar_design_graph_sha256"]
        return intake
    except Exception:
        raise GraphValidationError("SOURCE_INTAKE_UNAVAILABLE") from None


def _proposal_candidate(backend, tenant_id, drawing_id, version, snapshot):
    from solar_proposal_candidate import verify_snapshot
    return verify_snapshot(backend, tenant_id, drawing_id, version, snapshot)["candidate"]


def _solaredge_report(backend, tenant_id, drawing_id, version, graph_sha256, params):
    """The stored SolarEdge report the request names, bound to this exact version and graph."""
    import solar_solaredge_tracking
    return solar_solaredge_tracking.resolve_report(
        backend, tenant_id, drawing_id, version, graph_sha256, params)


def _physical_state(backend, tenant_id, drawing_id, project_id):
    """The drawing's current Ground physical head as {"view", "document"}, both None when no state
    was ever published (solar_physical_head.load_physical_head: at most 13 log reads plus the head
    artifact, read once). Any head refusal is PHYSICAL_STATE_UNAVAILABLE, payload-free."""
    import solar_physical_state as ps  # first: its write_loop import puts da/ (store) on sys.path
    import solar_physical_head as ph
    try:
        view, document = ph.load_physical_head(backend, tenant_id, drawing_id, project_id=project_id)
    except (ps.PhysicalStateError, OSError, RuntimeError, TypeError, KeyError):
        raise GraphValidationError("PHYSICAL_STATE_UNAVAILABLE") from None
    return {"view": view, "document": document}


PVCASE_TOOLS = ("solar-pvcase-convert", "solar-pvcase-solve", "solar-pvcase-export")


def validate_pvcase_request(tool, params, *, adapter=False):
    """Validate builtin parameters before any immutable-source lookup."""
    code = "INVALID_PVCASE_" + tool.rsplit("-", 1)[1].upper() + "_REQUEST"
    keys = ({"expected_rev", "source_artifact_id"} if tool == "solar-pvcase-convert"
            else {"expected_rev"} if tool == "solar-pvcase-solve" else set())
    try:
        _bounded_json(params)
        if adapter and type(params) is dict:
            if "drawing_id" in params and (type(params["drawing_id"]) is not str
                                           or len(params["drawing_id"]) > 128):
                raise ValueError()
            params = {key: value for key, value in params.items() if key != "drawing_id"}
        if type(params) is not dict or set(params) != keys:
            raise ValueError()
        if "expected_rev" in keys and (type(params["expected_rev"]) is not int
                                       or not 0 <= params["expected_rev"] <= 2147483647):
            raise ValueError()
        if "source_artifact_id" in keys and (
                type(params["source_artifact_id"]) is not str
                or re.fullmatch(r"[0-9a-f]{64}", params["source_artifact_id"]) is None):
            raise ValueError()
    except (ValueError, TypeError, RecursionError, ArithmeticError):
        raise GraphValidationError(code, "<root>") from None
    return params.get("expected_rev")


def pvcase_conversion_marker(graph):
    import solar_pvcase_conversion as conversion
    extra = graph.get("extra") if isinstance(graph, dict) else None
    marker = extra.get("pvcase") if isinstance(extra, dict) else None
    return (isinstance(marker, dict) and marker.get("schema") == conversion.INPUT_SCHEMA
            and marker.get("conversion_schema") == conversion.CONVERSION_SCHEMA)


def check_pvcase_source(graph, params, pvcase_source, *, converting=False):
    """Verify exact-byte identity; source-version equality is deliberately not required."""
    import solar_pvcase_sources as sources
    if pvcase_source is None:
        raise GraphValidationError("PVCASE_SOURCE_REQUIRED", "<root>") from None
    try:
        if type(pvcase_source) is not dict or set(pvcase_source) != {"meta", "content", "envelope"}:
            raise ValueError()
        meta, content, envelope = (pvcase_source[k] for k in ("meta", "content", "envelope"))
        _bounded_json(envelope)
        expected = (params["source_artifact_id"] if converting
                    else graph["extra"]["pvcase"]["source_artifact_id"])
        if (type(meta) is not dict or type(content) is not bytes
                or type(expected) is not str or re.fullmatch(r"[0-9a-f]{64}", expected) is None
                or meta["artifact_id"] != expected or meta["project_id"] != graph["project"]["id"]
                or hashlib.sha256(content).hexdigest() != meta["content_sha256"]
                or meta["content_sha256"] != meta["request_sha256"]
                or digest(sources.inspect_pvcase_source(content)) != digest(envelope)
                or (not converting and graph["extra"]["pvcase"]["source_sha256"]
                    != meta["content_sha256"])):
            raise ValueError()
    except (ValueError, TypeError, LookupError, AttributeError, RecursionError, ArithmeticError):
        raise GraphValidationError("PVG_INVALID_SOURCE", "<root>") from None
    return pvcase_source


def check_pvcase_result_source(graph, pvcase_source):
    """The published graph and every imported frame must retain the resolved binding."""
    try:
        meta = pvcase_source["meta"]
        records = [graph["extra"]["pvcase"]] + [f["provenance"]["pvcase"] for f in graph["frames"]]
        if graph["project"]["id"] != meta["project_id"] or not graph["frames"] or any(
                r["source_artifact_id"] != meta["artifact_id"]
                or r["source_sha256"] != meta["content_sha256"] for r in records):
            raise ValueError()
    except (ValueError, TypeError, LookupError, AttributeError):
        raise GraphValidationError("PVG_INVALID_SOURCE", "<root>") from None


def _pvcase_source(backend, tenant_id, drawing_id, version, graph_sha256, params,
                   *, tool, project_id=None):
    import solar_pvcase_sources as sources
    validate_pvcase_request(tool, params)
    context = resolve_graph_context(backend, tenant_id, drawing_id, version, project_id=project_id)
    graph = context["graph"]
    if (context["graph_sha256"] != graph_sha256 or digest(graph) != graph_sha256
            or graph["project"]["id"] != context["project_id"]
            or (project_id is not None and context["project_id"] != project_id)):
        raise GraphValidationError("PVG_INVALID_SOURCE", "<root>") from None
    converting = tool == "solar-pvcase-convert"
    if not converting and not pvcase_conversion_marker(graph):
        raise GraphValidationError("PVCASE_CONVERSION_REQUIRED", "<root>") from None
    try:
        artifact_id = (params["source_artifact_id"] if converting
                       else graph["extra"]["pvcase"]["source_artifact_id"])
    except (KeyError, TypeError):
        raise GraphValidationError("PVG_INVALID_SOURCE", "<root>") from None
    try:
        meta, content, envelope = sources.load_pvcase_source(
            backend, tenant_id, drawing_id, artifact_id, project_id=context["project_id"])
    except (sources.PvcaseSourceError, OSError, RuntimeError, ValueError, TypeError,
            LookupError, AttributeError, ArithmeticError):
        raise GraphValidationError("PVCASE_SOURCE_UNAVAILABLE", "<root>") from None
    value = {"meta": meta, "content": content, "envelope": envelope}
    return check_pvcase_source(graph, params, value, converting=converting)


PHYSICAL_SOURCE_KEYS = frozenset({"head_index", "state_artifact_id", "state_content_sha256"})


def physical_state_source(view):
    """The graph.extra.physical_state record a physical_state tool must write for `view`."""
    return {"head_index": view["index"], "state_artifact_id": view["state"]["artifact_id"],
            "state_content_sha256": view["state"]["content_sha256"]}


def _physical_state_entry(backend, tenant_id, drawing_id, project_id, source):
    """The physical state a committed graph names in graph.extra.physical_state, re-read for the
    replay proof: head-log entry `head_index` must be canonical and chained to a predecessor
    that is a valid log entry at its own index,
    naming that state artifact and content digest for this project, and the artifact must be this drawing's and
    project's physical state. Both are immutable, so a later physical publish never changes the
    answer. Any disagreement raises ValueError (the proof's rejection)."""
    import solar_physical_state as ps  # first: its write_loop import puts da/ (store) on sys.path
    import solar_physical_head as ph
    if type(source) is not dict or set(source) != PHYSICAL_SOURCE_KEYS:
        raise ValueError()
    index, artifact_id = source["head_index"], source["state_artifact_id"]
    content_sha256 = source["state_content_sha256"]
    if type(index) is not int or type(artifact_id) is not str or type(content_sha256) is not str:
        raise ValueError()
    raw = backend.get(ph.entry_key(tenant_id, drawing_id, index))
    entry = json.loads(raw)
    if (type(entry) is not dict
            or raw != ph.entry_bytes(index, project_id, entry.get("parent"), artifact_id, content_sha256)):
        raise ValueError()
    parent = entry["parent"]
    if index == 0:
        if parent is not None:
            raise ValueError()
    elif index > 0:
        try:
            previous = ph._Log(backend, tenant_id, drawing_id).entry(index - 1)
        except ph.PhysicalHeadError:
            raise ValueError() from None
        except ValueError:
            raise
        except Exception:
            raise ValueError() from None
        if type(previous) is not dict or previous.get("state") != parent:
            raise ValueError()
    meta, document = ps.load_physical_state(backend, tenant_id, drawing_id, artifact_id,
                                            project_id=project_id)
    if meta["content_sha256"] != content_sha256:
        raise ValueError()
    view = {"index": index, "state": {"artifact_id": artifact_id, "content_sha256": content_sha256}}
    return {"view": view, "document": document}


_TRUSTED_RESOLVERS = {"source_intake": _source_intake, "proposal_candidate": _proposal_candidate,
                      "solaredge_report": _solaredge_report, "physical_state": _physical_state,
                      "pvcase_source": _pvcase_source}


def _resolve_trusted(tool, backend, tenant_id, drawing_id, version, graph_sha256, snapshot,
                     params=None, *, project_id=None):
    names = solar_tools.get(tool)["trusted_inputs"]
    if snapshot is not None and "proposal_candidate" not in names:
        raise GraphValidationError("INVALID_COMMIT_REQUEST")
    resolved = {}
    for name in names:
        if name == "proposal_candidate":
            resolved["candidate"] = _TRUSTED_RESOLVERS[name](
                backend, tenant_id, drawing_id, version, snapshot)
        elif name == "solaredge_report":
            resolved[name] = _TRUSTED_RESOLVERS[name](
                backend, tenant_id, drawing_id, version, graph_sha256, params)
        elif name == "physical_state":
            resolved[name] = _TRUSTED_RESOLVERS[name](backend, tenant_id, drawing_id, project_id)
        elif name == "pvcase_source":
            resolved[name] = _TRUSTED_RESOLVERS[name](
                backend, tenant_id, drawing_id, version, graph_sha256, params,
                tool=tool, project_id=project_id)
        else:
            resolved[name] = _TRUSTED_RESOLVERS[name](
                backend, tenant_id, drawing_id, version, graph_sha256)
    return resolved


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


def graph_commit_provenance(result, params, tenant_id, job_id, tool, source_version, *, backend=None,
                            proposal_candidate=None):
    """Bind a terminal receipt to its durable request and immutable stored version."""
    try:
        if (proposal_candidate is not None
                and "proposal_candidate" not in (solar_tools.get(tool) or {}).get("trusted_inputs", [])):
            raise ValueError()
        if isinstance(result, dict) and result["schema_version"] == SEED_RESULT_SCHEMA:
            return _seed_provenance(result, params, tenant_id, job_id, tool, source_version,
                                    backend=backend)
        if isinstance(params, dict) and "initialize" in params:
            raise ValueError()
        if (not isinstance(result, dict) or result["schema_version"] != RESULT_SCHEMA
                or result["adapter"] != ADAPTER_KIND or tool not in local_graph_tools()
                or result["tool"] != tool or result["tenant_id"] != tenant_id
                or not isinstance(job_id, str) or not job_id or result["job_id"] != job_id
                or not isinstance(params, dict) or not isinstance(params["drawing_id"], str)
                or not stable_numbers(params)
                or result["drawing_id"] != params["drawing_id"]
                or type(source_version) is not int or source_version < 1):
            raise ValueError()
        builtin_params = copy.deepcopy(params)
        drawing_id = builtin_params.pop("drawing_id")
        request_sha256 = request_digest(tool, drawing_id, source_version, builtin_params,
                                       proposal_candidate=proposal_candidate)
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
        # Publication replaces only the design graph and keeps every other key of the parent's intake, so a
        # receipt that names another project was not produced by this request.
        if (result["project_id"] != context["project_id"]
                or context["project_id"] != parent["project_id"]):
            raise ValueError()
        # Publication writes canonical_bytes(version_companion(parent intake, before, after)), so the proof
        # rebuilds those bytes from the stored parent and the two stored graphs and requires the stored version's
        # digest to equal theirs (the publisher's own replay check). A stored version whose other content differs
        # from its parent's, one the publisher would refuse (a value outside its bounds, even one carried
        # identically in the parent), or one serialized differently is not accepted.
        _, parent_key, _ = store.resolve_version_entry(backend, tenant_id, drawing_id, source_version)
        expected = version_companion(json.loads(backend.get(parent_key)), parent["graph"], context["graph"])
        if hashlib.sha256(canonical_bytes(expected)).hexdigest() != entry["sha256"]:
            raise ValueError()
        trusted_inputs = solar_tools.get(tool)["trusted_inputs"]
        if "proposal_candidate" in trusted_inputs or proposal_candidate is not None:
            resolved_candidate = _resolve_trusted(
                tool, backend, tenant_id, drawing_id, source_version,
                parent["graph_sha256"], proposal_candidate)
            after = _load_builtin(tool).run(copy.deepcopy(parent["graph"]), builtin_params,
                                            **resolved_candidate)
            if digest(after) != result["graph_sha256"]:
                raise ValueError()
        if "source_intake" in trusted_inputs:
            resolved = {name: _TRUSTED_RESOLVERS[name](
                backend, tenant_id, drawing_id, source_version, parent["graph_sha256"])
                for name in trusted_inputs}
            after = _load_builtin(tool).run(copy.deepcopy(parent["graph"]), builtin_params,
                                            **copy.deepcopy(resolved))
            if digest(after) != result["graph_sha256"]:
                raise ValueError()
            intake = _source_intake(backend, tenant_id, drawing_id, version, result["graph_sha256"])
            def canonical(value):
                return json.dumps(value, sort_keys=True, separators=(",", ":"),
                                  ensure_ascii=False, allow_nan=False)
            if canonical(intake) != canonical(resolved["source_intake"]):
                raise ValueError()
        if "solaredge_report" in trusted_inputs:
            resolved = _resolve_trusted(tool, backend, tenant_id, drawing_id, source_version,
                                        parent["graph_sha256"], None, builtin_params)
            after = _load_builtin(tool).run(copy.deepcopy(parent["graph"]), builtin_params,
                                            **resolved)
            if digest(after) != result["graph_sha256"]:
                raise ValueError()
        if "physical_state" in trusted_inputs:
            # The state the commit names, never the head now: a later physical publish moves the head.
            state = _physical_state_entry(backend, tenant_id, drawing_id, parent["project_id"],
                                          context["graph"]["extra"]["physical_state"])
            after = _load_builtin(tool).run(copy.deepcopy(parent["graph"]), builtin_params,
                                            physical_state=state)
            if digest(after) != result["graph_sha256"]:
                raise ValueError()
        if "pvcase_source" in trusted_inputs:
            resolved = _resolve_trusted(tool, backend, tenant_id, drawing_id, source_version,
                                        parent["graph_sha256"], None, builtin_params,
                                        project_id=parent["project_id"])
            after = _load_builtin(tool).run(copy.deepcopy(parent["graph"]), builtin_params,
                                            **copy.deepcopy(resolved))
            check_pvcase_result_source(after, resolved["pvcase_source"])
            if digest(after) != result["graph_sha256"]:
                raise ValueError()
        return {"execution_mode": "local_graph_commit", "adapter": ADAPTER_KIND,
                "request_sha256": request_sha256, "graph_sha256": result["graph_sha256"],
                "intake_sha256": result["intake_sha256"], "source_version": source_version,
                "new_version": version}
    except (LookupError, ArithmeticError, AttributeError, TypeError, ValueError, OSError,
            RecursionError, RuntimeError):
        raise ValueError("graph commit terminal proof rejected") from None


def _seed_provenance(result, params, tenant_id, job_id, tool, source_version, *, backend):
    if (result["adapter"] != ADAPTER_KIND or not (solar_tools.get(tool) or {}).get("seedable")
            or result["tool"] != tool or result["tenant_id"] != tenant_id
            or not isinstance(job_id, str) or not job_id or result["job_id"] != job_id
            or not isinstance(params, dict) or not isinstance(params["drawing_id"], str)
            or not stable_numbers(params) or result["drawing_id"] != params["drawing_id"]
            or type(source_version) is not int or source_version < 1):
        raise ValueError()
    builtin_params = copy.deepcopy(params)
    drawing_id = builtin_params.pop("drawing_id")
    initialize = validate_seed_request(builtin_params["initialize"])
    request_sha256 = request_digest(tool, drawing_id, source_version, builtin_params)
    builtin_params.pop("initialize")
    version = result["new_version"]["version"]
    if (result["request_sha256"] != request_sha256
            or type(version) is not int or version <= source_version
            or result["new_version"] != {"drawing_id": drawing_id, "version": version,
                                         "parent": source_version}
            or result["drawing_changed"] is not True or result["initialized"] is not True
            or type(result["replayed"]) is not bool
            or result["before_rev"] is not None or result["before_graph_sha256"] is not None
            or type(result["seed_base_rev"]) is not int or result["seed_base_rev"] != 0
            or type(result["after_rev"]) is not int or result["after_rev"] != 1):
        raise ValueError()
    if backend is None:
        backend = write_loop.backend_for_tenant(tenant_id, aps_live=False, da=None)
    _, key, entry = store.resolve_version_entry(backend, tenant_id, drawing_id, version)
    data = backend.get(key)
    if (entry["v"] != version or entry["parent"] != source_version
            or entry["workitem_id"] != "solar-graph:" + job_id
            or entry["note"] != "solar-graph-seed:" + request_sha256
            or entry["sha256"] != result["intake_sha256"]
            or hashlib.sha256(data).hexdigest() != result["intake_sha256"]):
        raise ValueError()
    context = resolve_graph_context(backend, tenant_id, drawing_id, version)
    if (context["representation"] != "intake"
            or context["graph_sha256"] != result["graph_sha256"]
            or context["graph"]["project"]["id"] != result["project_id"]
            or context["graph"]["rev"] != 1 or context["graph"]["parent_rev"] != 0):
        raise ValueError()
    ctx = resolve_seed_context(backend, tenant_id, drawing_id, source_version,
                               source_intake_sha256=initialize["source_intake_sha256"])
    if ctx["intake_sha256"] != result["parent_intake_sha256"]:
        raise ValueError()
    base = new_empty_graph(tenant_id=tenant_id, drawing_id=drawing_id,
                           source_hash=ctx["intake_sha256"], units=initialize["units"],
                           created_at=ctx["created"])
    if digest(base) != result["seed_base_graph_sha256"]:
        raise ValueError()
    after = _load_builtin(tool).run(copy.deepcopy(base), builtin_params)
    if digest(after) != result["graph_sha256"]:
        raise ValueError()
    intake = json.loads(data)
    del intake["solar_design_graph"]
    del intake["solar_design_graph_sha256"]
    def canonical(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    if canonical(intake) != canonical(ctx["intake"]):
        raise ValueError()
    return {"execution_mode": "local_graph_commit", "adapter": ADAPTER_KIND,
            "request_sha256": request_sha256, "graph_sha256": result["graph_sha256"],
            "intake_sha256": result["intake_sha256"], "source_version": source_version,
            "new_version": version, "seeded": True}


def run_local_graph_commit(backend, tenant_id, tool, params, *, drawing_id, source_version,
                           holder, fence, job_id, project_id=None, proposal_candidate=None):
    if tool not in local_graph_tools():
        raise GraphValidationError("UNKNOWN_LOCAL_GRAPH_TOOL")
    if proposal_candidate is not None and "proposal_candidate" not in solar_tools.get(tool)["trusted_inputs"]:
        raise GraphValidationError("INVALID_COMMIT_REQUEST")
    if tool in PVCASE_TOOLS:
        validate_pvcase_request(tool, params, adapter=True)
    _bounded_json(params)
    if type(params) is not dict:
        raise GraphValidationError(solar_tools.get(tool)["invalid_request_code"])
    # A malformed revision is the acceptance kernel's STALE_GRAPH_REVISION,
    # including floats whose spelling would otherwise fail the numeric guard.
    numeric_params = ({key: value for key, value in params.items() if key != "expected_rev"}
                      if proposal_candidate is not None else params)
    if not stable_numbers(numeric_params):
        raise GraphValidationError("INVALID_NUMERIC_PARAM")
    if type(source_version) is not int or source_version < 1:
        raise GraphValidationError("INVALID_PARENT_VERSION")
    if "drawing_id" in params and (type(params["drawing_id"]) is not str
                                   or params["drawing_id"] != drawing_id):
        raise GraphValidationError("DRAWING_ID_CONFLICT")
    builtin_params = copy.deepcopy(params)
    builtin_params.pop("drawing_id", None)
    initializing = "initialize" in builtin_params
    if initializing:
        if not solar_tools.get(tool)["seedable"]:
            raise GraphValidationError("INVALID_SEED_REQUEST")
        if project_id is not None:
            raise GraphValidationError("SEED_PROJECT_SCOPE_UNSUPPORTED")
        request_sha256 = request_digest(tool, drawing_id, source_version, builtin_params)
        initialize = validate_seed_request(builtin_params.pop("initialize"))
        ctx = resolve_seed_context(backend, tenant_id, drawing_id, source_version,
                                   source_intake_sha256=initialize["source_intake_sha256"])
        base = new_empty_graph(tenant_id=tenant_id, drawing_id=drawing_id,
                               source_hash=ctx["intake_sha256"], units=initialize["units"],
                               created_at=ctx["created"])
        after = _load_builtin(tool).run(copy.deepcopy(base), builtin_params)
        if builtin_params.get("cancel") is True:
            raise GraphValidationError("GRAPH_COMMIT_CANCELLED")
        receipt = publish_version(
            backend, tenant_id, drawing_id, parent_version=source_version,
            before=None, after=after, holder=holder, fence=fence,
            job_id=job_id, request_sha256=request_sha256)
    else:
        context = resolve_graph_context(backend, tenant_id, drawing_id, source_version,
                                        project_id=project_id)
        if context["representation"] == "dwg-bundle":
            raise GraphValidationError("LICENSED_GRAPH_COMMIT_REQUIRED")
        resolved = _resolve_trusted(tool, backend, tenant_id, drawing_id, source_version,
                                    context["graph_sha256"], proposal_candidate, builtin_params,
                                    project_id=context["project_id"])
        module = _load_builtin(tool)
        run_bound = getattr(module, "run_bound", None)
        if callable(run_bound):
            # A tenant-bound builtin (an outbound service call) sees the job's identity only.
            after = run_bound(copy.deepcopy(context["graph"]), copy.deepcopy(builtin_params),
                              tenant_id=tenant_id, job_id=job_id)
        else:
            after = module.run(copy.deepcopy(context["graph"]), builtin_params, **resolved)
        if builtin_params.get("cancel") is True:
            raise GraphValidationError("GRAPH_COMMIT_CANCELLED")
        if "physical_state" in resolved:
            # The replay proof re-reads the state the graph names, so it must name the head read here.
            view = resolved["physical_state"]["view"]
            extra = after.get("extra") if type(after) is dict else None
            if (view is None or type(extra) is not dict
                    or extra.get("physical_state") != physical_state_source(view)):
                raise GraphValidationError("PHYSICAL_STATE_UNBOUND")
        if "pvcase_source" in resolved:
            check_pvcase_result_source(after, resolved["pvcase_source"])
        request_sha256 = request_digest(tool, drawing_id, source_version, builtin_params,
                                       proposal_candidate=proposal_candidate)
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
    if initializing:
        return {"schema_version": SEED_RESULT_SCHEMA, "adapter": ADAPTER_KIND,
                "tenant_id": tenant_id, "job_id": job_id, "tool": tool,
                "project_id": after["project"]["id"], "drawing_id": drawing_id,
                "request_sha256": request_sha256,
                "new_version": {"drawing_id": drawing_id, "version": receipt["version"],
                                "parent": receipt["parent_version"]},
                "initialized": True, "before_rev": None, "before_graph_sha256": None,
                "seed_base_rev": 0, "seed_base_graph_sha256": digest(base),
                "parent_intake_sha256": ctx["intake_sha256"],
                "graph_sha256": receipt["graph_sha256"], "intake_sha256": receipt["intake_sha256"],
                "after_rev": after["rev"], "drawing_changed": True, "replayed": receipt["replayed"]}
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


def run_project_graph_seed(*args, **kwargs):
    from solar_project_graph import run_project_graph_seed as run
    return run(*args, **kwargs)


def run_project_graph_commit(*args, **kwargs):
    from solar_project_graph import run_project_graph_commit as run
    return run(*args, **kwargs)


def project_graph_commit_provenance(*args, **kwargs):
    from solar_project_graph import project_graph_commit_provenance as prove
    return prove(*args, **kwargs)
