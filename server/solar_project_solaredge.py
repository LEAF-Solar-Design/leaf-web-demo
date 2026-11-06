"""SolarEdge sources, reports and tracking in one exact canonical project graph.

The PDF and matching kernels are shared with the legacy drawing route. UUID
provenance stays in this adapter and never crosses its integer-version contract.
"""
from copy import deepcopy
from hashlib import sha256
import json
import math
from uuid import UUID

import entitlements
import solar_artifacts
import solar_import_sources as sources
import solar_local_read as local_read
import solar_project_admission as policy
import solar_project_artifacts as artifacts
import solar_project_context as project
import solar_project_graph as graphs
import solar_solaredge_report as reports
import solar_solaredge_tracking as tracking
import store
import write_loop
from envelopes import with_envelope_fields
from leaf_cloud_client import canonical_bytes
from leaf_platform import entitlements as stored
from solar_design_graph import GraphValidationError, require_revision
from solar_sizing_client import advance

SOURCE_SCHEMA = "leaf.solar-project-import-source.v1"
REPORT_SCHEMA = "leaf.solar-project-solaredge-report.v1"
RESULT_SCHEMA = "leaf.solar-project-solaredge-report-result.v1"
STORE_SCHEMA = "leaf.solar-project-solaredge-import.v1"
READ_SCHEMA = "leaf.solar-project-solaredge-tracking-read.v1"
READ_TOOL = "solar-solaredge-tracking-read"
ACCEPT_TOOL = "solar-solaredge-accept"
_IDENTITY = ("organization_id", "project_id", "drawing_id", "source_version_id",
             "source_intake_sha256", "graph_sha256", "graph_project_id")
_REPORT_KEYS = frozenset(("schema", *_IDENTITY, "source", "request", "row_angle",
                         "counts", "matches", "strings", "unassigned_panel_refs"))
_PRODUCERS = {
    sources.SOURCE_TOOL: {"schema": "leaf.solar-project-solaredge-producer.v1",
        "name": sources.SOURCE_TOOL, "version": "1.0.0", "output_schema": SOURCE_SCHEMA,
        "implementation": "solar_project_solaredge.import_source"},
    reports.REPORT_TOOL: {"schema": "leaf.solar-project-solaredge-producer.v1",
        "name": reports.REPORT_TOOL, "version": "1.0.0", "output_schema": REPORT_SCHEMA,
        "implementation": "solar_solaredge_report.report_from_matrices"},
}


def _fail(code):
    raise GraphValidationError(code)


def _check(condition, code=tracking.REPORT_INVALID):
    if not condition:
        _fail(code)


def producer_digest(tool):
    return "sha256:" + sha256(canonical_bytes(_PRODUCERS[tool])).hexdigest()


def _identity(context):
    return {"organization_id": str(context.organization_id),
        "project_id": str(context.project_id), "drawing_id": str(context.drawing_id),
        "source_version_id": str(context.parent_version_id),
        "source_intake_sha256": context.intake_sha256,
        "graph_sha256": context.graph_sha256, "graph_project_id": context.graph_project_id}


def _result_scope(context):
    result = _identity(context)
    result["input_version_id"] = result.pop("source_version_id")
    return result


def _authorized(tenant, project_id, drawing_id, version_id, capability):
    if not policy.project_runs_enabled():
        _fail("project_execution_disabled")
    tier = entitlements.resolve_tier(tenant)
    roles, elevated = entitlements.resolve_roles(tenant)
    try:
        allowed = entitlements.entitlements_for(tier, roles, elevated).get(capability, False)
    except entitlements.EntitlementsError:
        return None, entitlements.policy_unavailable_response(capability, tier)
    if not allowed:
        return None, entitlements.entitlement_denied_response(capability, tier)
    verified = project.resolve_context(tenant, project_id, version_id,
        drawing_id=drawing_id, write=capability == "upload")
    context = graphs.resolve_project_graph_context(verified)
    if context.graph is None:
        _fail("GRAPH_NOT_EMBEDDED")
    graphs.require_project_sizing(context)
    denial, _org = stored.stored_job_entitlement_verdict(context.organization_id, "extract")
    return context, denial


def _recheck(tenant, context, *, write=False):
    verified = project.resolve_context(tenant, context.project_id, context.parent_version_id,
        drawing_id=context.drawing_id, write=write)
    final = graphs.resolve_project_graph_context(verified)
    if _identity(final) != _identity(context):
        _fail("SIP_R6_ARTIFACT_SOURCE_MISMATCH")


def _sink(context, tool, request_sha):
    return artifacts.ProjectArtifactSink(None, artifacts.project_artifact_binding(
        context, tool, producer_digest(tool), request_sha))


def _ref(meta):
    ref = {key: meta[key] for key in artifacts._REF_KEYS}
    ref.update(schema=artifacts.REF_SCHEMA, download=(
        f"/api/projects/{meta['project_id']}/drawings/{meta['drawing_id']}/"
        f"solar-artifacts/{meta['artifact_id']}"))
    return ref


def _read(context, artifact_id, tool, media_type, filename):
    if not solar_artifacts._hex(artifact_id):
        _fail("ARTIFACT_ID_INVALID")
    backend = artifacts._backend(context.organization_id)
    scope = {key: _identity(context)[key] for key in
             ("organization_id", "project_id", "drawing_id")}
    try:
        raw = backend.get(artifacts._key(scope, artifact_id))
    except KeyError:
        _fail("ARTIFACT_NOT_FOUND")
    except Exception:
        _fail("ARTIFACT_STORE_UNAVAILABLE")
    meta = artifacts._metadata(raw, scope, artifact_id)
    if (meta["tool"] != tool or meta["tool_manifest_sha256"] != producer_digest(tool)
            or meta["media_type"] != media_type or meta["filename"] != filename):
        _fail(tracking.REPORT_KIND_MISMATCH)
    if any(meta[key] != value for key, value in _identity(context).items()
           if key != "graph_project_id"):
        _fail(tracking.REPORT_STALE)
    try:
        content = backend.get(artifacts._key(scope, meta["content_sha256"], blob=True))
    except KeyError:
        _fail("ARTIFACT_CORRUPT")
    except Exception:
        _fail("ARTIFACT_STORE_UNAVAILABLE")
    if (type(content) is not bytes or len(content) != meta["byte_length"]
            or sha256(content).hexdigest() != meta["content_sha256"]):
        _fail("ARTIFACT_CORRUPT")
    return meta, content


def _reserve(context, content_sha):
    backend = artifacts._backend(context.organization_id)
    prefix = (f"tenants/{context.organization_id}/projects/{context.project_id}/"
        f"drawings/{context.drawing_id}/versions/{context.parent_version_id}/imports/solaredge/")
    mine = canonical_bytes({"schema": "leaf.solar-project-import-slot.v1",
        "kind": sources.SOURCE_KIND, "content_sha256": content_sha})
    for n in range(sources.MAX_SOURCES_PER_DRAWING):
        key = prefix + f"slot-{n}.json"
        try:
            try:
                existing = backend.get(key)
            except KeyError:
                try:
                    backend.put_if_absent_or_verify(key, mine)
                    return
                except store.ImmutableConflict:
                    existing = backend.get(key)
            if existing == mine:
                return
        except (OSError, RuntimeError, ValueError):
            _fail("IMPORT_STORE_UNAVAILABLE")
    _fail("IMPORT_QUOTA_EXCEEDED")


def import_source(tenant, project_id, drawing_id, input_version_id, data):
    context, denial = _authorized(tenant, project_id, drawing_id, input_version_id, "upload")
    if denial is not None:
        return denial
    if write_loop.drawing_mutations_refusal() is not None:
        _fail("IMPORT_WRITES_DRAINED")
    try:
        page_count = sources.inspect_solaredge_pdf(data)
    except sources.ImportSourceError as exc:
        _fail(exc.code)
    content_sha = sha256(data).hexdigest()
    sink = _sink(context, sources.SOURCE_TOOL, content_sha)
    prepared = sink.prepare(solar_artifacts.ArtifactOutput(
        {"page_count": page_count}, sources.SOURCE_MEDIA_TYPE, sources.SOURCE_FILENAME, data))
    _recheck(tenant, context, write=True)
    # Conditional reservations survive store failures. Same bytes reuse the slot.
    _reserve(context, content_sha)
    sink.finish(prepared)
    ref = sink.commit()
    return with_envelope_fields({"schema": SOURCE_SCHEMA, "kind": sources.SOURCE_KIND,
        **_result_scope(context), "page_count": page_count, "source": ref})


def _report_request(request):
    if (type(request) is not dict or set(request) != {
            "source_artifact_id", "alignment_tolerance", "selection_order"}):
        _fail("REPORT_REQUEST_INVALID")
    try:
        normalized = reports.validate_request(request)
    except reports.SolarEdgeReportError as exc:
        _fail(exc.code)
    normalized.pop("project_id")
    return normalized


def _request_sha(req, source_meta):
    return sha256(canonical_bytes({"schema": "leaf.solar-project-solaredge-report-request.v1",
        **req, "source_content_sha256": source_meta["content_sha256"]})).hexdigest()


def build_report(tenant, project_id, drawing_id, input_version_id, request):
    context, denial = _authorized(tenant, project_id, drawing_id, input_version_id, "run_read")
    if denial is not None:
        return denial
    if write_loop.drawing_mutations_refusal() is not None:
        _fail("REPORT_WRITES_DRAINED")
    req = _report_request(request)
    source_meta, content = _read(context, req["source_artifact_id"], sources.SOURCE_TOOL,
        sources.SOURCE_MEDIA_TYPE, sources.SOURCE_FILENAME)
    if source_meta["request_sha256"] != source_meta["content_sha256"]:
        _fail("IMPORT_SOURCE_CORRUPT")
    sink = _sink(context, reports.REPORT_TOOL, _request_sha(req, source_meta))
    artifact_id = sha256(canonical_bytes(sink.binding)).hexdigest()
    try:
        meta, report = _load_report(context, artifact_id)
    except GraphValidationError as exc:
        if exc.code != "ARTIFACT_NOT_FOUND":
            raise
        if not reports._PARSE_SLOTS.acquire(blocking=False):
            _fail("REPORT_BUSY")
        try:
            try:
                matrices = reports.parse_source(content)
            except reports.SolarEdgeReportError as error:
                _fail(error.code)
        finally:
            reports._PARSE_SLOTS.release()
        try:
            core = reports.report_from_matrices(context.graph, matrices,
                alignment_tolerance=req["alignment_tolerance"], selection_order=req["selection_order"])
        except reports.SolarEdgeReportError as error:
            _fail(error.code)
        report = {"schema": REPORT_SCHEMA, **_identity(context),
            "source": {key: source_meta[key] for key in
                       ("artifact_id", "content_sha256", "byte_length")},
            "request": {key: req[key] for key in ("alignment_tolerance", "selection_order")}, **core}
        prepared = sink.prepare(solar_artifacts.ArtifactOutput(report["counts"],
            reports.REPORT_MEDIA_TYPE, reports.REPORT_FILENAME, canonical_bytes(report)))
        _record(context, prepared.meta, report)
        _recheck(tenant, context)
        sink.finish(prepared)
        sink.commit()
        meta = prepared.meta
    else:
        _recheck(tenant, context)
    return with_envelope_fields({"schema": RESULT_SCHEMA, "kind": reports.REPORT_KIND,
        **_result_scope(context), "source_artifact_id": req["source_artifact_id"],
        "counts": report["counts"], "report": _ref(meta)})


def _load_report(context, artifact_id):
    meta, content = _read(context, artifact_id, reports.REPORT_TOOL,
        reports.REPORT_MEDIA_TYPE, reports.REPORT_FILENAME)
    try:
        report = json.loads(content.decode("utf-8"), object_pairs_hook=project._object_pairs,
            parse_constant=project._nonfinite, parse_float=project._json_float)
        if canonical_bytes(report) != content:
            raise ValueError()
        source_meta, _ = _read(context, report["source"]["artifact_id"], sources.SOURCE_TOOL,
            sources.SOURCE_MEDIA_TYPE, sources.SOURCE_FILENAME)
        if (report["source"] != {key: source_meta[key] for key in
                ("artifact_id", "content_sha256", "byte_length")}
                or source_meta["request_sha256"] != source_meta["content_sha256"]):
            _fail(tracking.REPORT_CORRUPT)
        req = _report_request({"source_artifact_id": source_meta["artifact_id"], **report["request"]})
        if meta["request_sha256"] != _request_sha(req, source_meta):
            _fail(tracking.REPORT_CORRUPT)
        _record(context, meta, report)
    except GraphValidationError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError):
        _fail(tracking.REPORT_CORRUPT)
    return meta, report


def _record(context, meta, report):
    """Validate canonical provenance and every matching association, without coercion."""
    try:
        return _validated_record(context, meta, report)
    except GraphValidationError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError):
        _fail(tracking.REPORT_INVALID)


def _validated_record(context, meta, report):
    _check(type(report) is dict and set(report) == _REPORT_KEYS and report["schema"] == REPORT_SCHEMA)
    _check(all(report[key] == value for key, value in _identity(context).items()), tracking.REPORT_STALE)
    graph = context.graph
    _check(type(report["row_angle"]) in (int, float)
        and -tracking.MAX_LABEL <= report["row_angle"] <= tracking.MAX_LABEL
        and math.isfinite(report["row_angle"]))
    counts = report["counts"]
    _check(type(counts) is dict and set(counts) == tracking._COUNT_KEYS
        and all(tracking._int(value, 0, tracking.MAX_LABEL) for value in counts.values()))
    req = _report_request({"source_artifact_id": report["source"]["artifact_id"], **report["request"]})
    _check(report["request"] == {key: req[key] for key in ("alignment_tolerance", "selection_order")})
    frames = graph["frames"]
    _check(0 < len(frames) <= tracking.MAX_FRAMES)
    frame_ids = {frame["id"] for frame in frames}
    handle_of = {p["id"]: p["provenance"].get("source_handle") for p in graph["panels"]}
    frame_of = {ref: frame["id"] for frame in frames for ref in frame["panel_refs"]}
    _check(len(frame_of) <= tracking.MAX_PANELS)
    matches = report["matches"]
    _check(type(matches) is list and 0 < len(matches) <= len(frames))
    matched, frame_rows = set(), []
    for match in matches:
        _check(type(match) is dict and set(match) == tracking._MATCH_KEYS)
        ref, candidates = match["frame_ref"], match["candidates"]
        _check(tracking._text(ref, 100) and ref in frame_ids and ref not in matched)
        _check(tracking._int(match["pdf_grid"], 0, tracking.MAX_INDEX)
            and tracking._int(match["pdf_matrix"], 0, tracking.MAX_INDEX)
            and tracking._int(match["pdf_sub_grid"], -1, tracking.MAX_INDEX))
        _check(type(candidates) is list and 0 < len(candidates) <= tracking.MAX_CANDIDATES
            and all(tracking._int(c, 0, tracking.MAX_INDEX) for c in candidates)
            and match["pdf_grid"] in candidates
            and tracking._int(match["candidate_count"], len(candidates), tracking.MAX_INDEX))
        if match["candidate_count"] > 1 and req["selection_order"] != "recorded":
            _fail(tracking.REPORT_AMBIGUOUS)
        matched.add(ref)
        frame_rows.append({key: match[key] for key in ("frame_ref", "pdf_matrix", "pdf_grid", "pdf_sub_grid")})
    strings = report["strings"]
    _check(type(strings) is list and len(strings) <= tracking.MAX_PANELS)
    covered, rows = set(), []
    for position, row in enumerate(strings):
        _check(type(row) is dict and set(row) == tracking._STRING_KEYS
            and type(row["index"]) is int and row["index"] == position)
        kind, ref = row["source"], row["frame_ref"]
        _check(type(kind) is str and ((kind == "group" and ref in matched)
            or (kind == "bridge" and ref is None)))
        _check(tracking._int(row["pdf_matrix"], 0, tracking.MAX_INDEX)
            and tracking._int(row["pdf_inverter_id"], -1, tracking.MAX_LABEL)
            and tracking._int(row["pdf_string_input"], 0, tracking.MAX_LABEL)
            and type(row["partial"]) is bool)
        refs, handles = row["panel_refs"], row["panel_handles"]
        _check(type(refs) is list and type(handles) is list
            and 0 < len(refs) == len(handles) <= tracking.MAX_PANELS)
        for panel_ref, handle in zip(refs, handles):
            _check(tracking._text(panel_ref, 100) and panel_ref in frame_of and panel_ref not in covered
                and (kind == "bridge" or frame_of[panel_ref] == ref)
                and type(handle_of[panel_ref]) is str and handle == handle_of[panel_ref].upper())
            covered.add(panel_ref)
        rows.append({key: deepcopy(row[key]) for key in row if key != "panel_handles"})
    unassigned = report["unassigned_panel_refs"]
    _check(type(unassigned) is list and len(unassigned) <= tracking.MAX_PANELS)
    for ref in unassigned:
        _check(tracking._text(ref, 100) and ref in frame_of and ref not in covered)
        covered.add(ref)
    _check(len(covered) == len(frame_of))
    expected_counts = {"frames": len(frames), "matched_frames": len(matches),
        "strings": len(strings), "assigned_panels": sum(len(row["panel_refs"]) for row in strings),
        "unassigned_panels": len(unassigned), "partial_strings": sum(row["partial"] for row in strings),
        "group_strings": sum(row["source"] == "group" for row in strings),
        "bridge_strings": sum(row["source"] == "bridge" for row in strings)}
    _check(all(counts[key] == value for key, value in expected_counts.items()))
    return {"schema": STORE_SCHEMA, "source": deepcopy(report["source"]),
        "report": {**{key: meta[key] for key in ("artifact_id", "content_sha256", "byte_length")},
                   **_identity(context)}, "request": deepcopy(report["request"]),
        "frames": frame_rows, "strings": rows, "unassigned_panel_refs": list(unassigned)}


def accept(context, params):
    tracking.validate_request(params)
    result = require_revision(context.graph, params["expected_rev"])
    meta, report = _load_report(context, params["report_artifact_id"])
    result["extra"][tracking.STORE_KEY] = _record(context, meta, report)
    return advance(result, [], ACCEPT_TOOL)


def read_tracking(tenant, context, params):
    module = local_read._load_builtin(READ_TOOL)
    section, offset = module._request(params)
    record = context.graph["extra"].get(tracking.STORE_KEY)
    if record is None:
        items, page = module._page([], offset)
        return {"schema": READ_SCHEMA, "accepted": False, "labels_only": True,
            "provenance": None, "counts": None, "references": None,
            "section": section, "page": page, "items": items}
    try:
        _check(type(record) is dict and record.get("schema") == STORE_SCHEMA)
        source_id = UUID(record["report"]["source_version_id"])
        verified = project.resolve_context(tenant, context.project_id, source_id,
            drawing_id=context.drawing_id, write=False)
        source = graphs.resolve_project_graph_context(verified)
        _check((source.organization_id, source.project_id, source.drawing_id)
            == (context.organization_id, context.project_id, context.drawing_id))
        meta, report = _load_report(source, record["report"]["artifact_id"])
        _check(record == _record(source, meta, report))
    except (GraphValidationError, project.ProjectContextError, ValueError, TypeError,
            KeyError, IndexError, AttributeError):
        _fail("SOLAREDGE_IMPORT_RECORD_INVALID")
    page_items = {"strings": record["strings"], "frames": record["frames"],
                  "unassigned": record["unassigned_panel_refs"]}[section]
    items, page = module._page(page_items, offset)
    return deepcopy({"schema": READ_SCHEMA, "accepted": True, "labels_only": True,
        "provenance": {key: record[key] for key in ("source", "report", "request")},
        "counts": module._counts(record), "references": module._references(context.graph, record),
        "section": section, "page": page, "items": items})
