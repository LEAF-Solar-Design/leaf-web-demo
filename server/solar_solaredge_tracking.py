"""SolarEdge import tracking accepted into the design graph (import-solaredge-pdf, piece 3 of 3).

A drafter who accepts a stored SolarEdge report (server/solar_solaredge_report.py) commits only
explicit import provenance and tracking associations. They live in the graph's top-level extra at
graph["extra"]["solaredge_import"], schema leaf.solar-solaredge-import.v1, exactly seven keys:

  schema                 the string leaf.solar-solaredge-import.v1
  source                 the stored PDF the report parsed: artifact_id, content_sha256, byte_length
  report                 the accepted report artifact: artifact_id, content_sha256, byte_length,
                         source_version, graph_sha256
  request                the alignment_tolerance and selection_order the report was computed with
  frames                 one row per matched frame, in the report's order: frame_ref, pdf_matrix,
                         pdf_grid, pdf_sub_grid
  strings                one row per PDF string, in the report's order: index, source, frame_ref,
                         pdf_matrix, pdf_inverter_id, pdf_string_input, partial, panel_refs
  unassigned_panel_refs  framed panels no PDF string covers, in the report's order

The PDF's inverter ids and string inputs are labels only. Accepting creates no string, inverter,
device, route or schedule, changes no entity, and never marks Solve complete: top-level extra is
outside every digest a design output binds (solar_solve_results.upstream_basis and
solar_sizing_client.sizing_basis). A later accept replaces the record; undo restores the previous one.

The report reaches the builtin only through the solaredge_report trusted input
(server/solar_local_graph.py calls resolve_report): the stored report artifact bound to the exact
version and graph the commit runs against, so a report computed before the head moved is refused,
never re-applied. Every field is validated again against the graph before anything is written.
Bounds: at most 10,000 frames and 200,000 framed panels (the report's own ceilings), PDF inverter
ids -1..2147483647, string inputs 0..2147483647, PDF matrix and grid indexes 0..1000000, sub grid
ids -1..1000000, at most 16 listed candidates. Linear in the report size, no I/O outside
resolve_report's artifact read, fails closed with a named GraphValidationError.
"""
import json
import math
import re

import solar_artifacts
import solar_solaredge_report as report_module
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError
from solar_sizing_client import digest

STORE_KEY = "solaredge_import"
STORE_SCHEMA = "leaf.solar-solaredge-import.v1"
INVALID = "INVALID_SOLAREDGE_ACCEPT_REQUEST"
REPORT_REQUIRED = "SOLAREDGE_REPORT_REQUIRED"
REPORT_NOT_FOUND = "SOLAREDGE_REPORT_NOT_FOUND"
REPORT_UNAVAILABLE = "SOLAREDGE_REPORT_UNAVAILABLE"
REPORT_CORRUPT = "SOLAREDGE_REPORT_CORRUPT"
REPORT_KIND_MISMATCH = "SOLAREDGE_REPORT_KIND_MISMATCH"
REPORT_STALE = "SOLAREDGE_REPORT_STALE"
REPORT_INVALID = "SOLAREDGE_REPORT_INVALID"
REPORT_AMBIGUOUS = "SOLAREDGE_REPORT_AMBIGUOUS"
MAX_FRAMES = 10_000
MAX_PANELS = 200_000
MAX_INDEX = 1_000_000
MAX_LABEL = 2_147_483_647
MAX_CANDIDATES = 16
_REQUEST_KEYS = frozenset(("expected_rev", "report_artifact_id"))
_REPORT_KEYS = frozenset(("schema", "drawing_id", "project_id", "source_version", "graph_sha256",
                          "source", "request", "row_angle", "counts", "matches", "strings",
                          "unassigned_panel_refs"))
_MATCH_KEYS = frozenset(("frame_ref", "pdf_grid", "pdf_matrix", "pdf_sub_grid", "candidates",
                         "candidate_count"))
_STRING_KEYS = frozenset(("index", "source", "frame_ref", "pdf_matrix", "pdf_inverter_id",
                          "pdf_string_input", "partial", "panel_handles", "panel_refs"))
_COUNT_KEYS = frozenset(("pdf_matrices", "pdf_panels", "matchable_grids", "bridge_grids", "frames",
                         "matched_frames", "group_strings", "bridge_strings", "strings",
                         "assigned_panels", "unassigned_panels", "partial_strings"))
_READ_CODES = {"ARTIFACT_NOT_FOUND": REPORT_NOT_FOUND, "ARTIFACT_STORE_UNAVAILABLE": REPORT_UNAVAILABLE}
_HEX64 = re.compile(r"[0-9a-f]{64}")


def _hex(value):
    return type(value) is str and _HEX64.fullmatch(value) is not None


def _int(value, lower, upper):
    return type(value) is int and lower <= value <= upper


def _text(value, bound):
    return type(value) is str and 0 < len(value) <= bound


def validate_request(params):
    """The accept request: exactly expected_rev and a 64 hex report_artifact_id (drawing_id is
    removed by the adapter before the builtin runs). The revision itself is require_revision's."""
    if (type(params) is not dict or set(params) != _REQUEST_KEYS
            or not _hex(params["report_artifact_id"])):
        raise GraphValidationError(INVALID)


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("nonfinite JSON constant")


def resolve_report(backend, tenant_id, drawing_id, version, graph_sha256, params):
    """The solaredge_report trusted input: {"meta", "report"} for the stored report the request
    names, only when it is bound to exactly this version and graph. Reads one artifact."""
    artifact_id = params.get("report_artifact_id") if type(params) is dict else None
    if not _hex(artifact_id):
        raise GraphValidationError(INVALID)
    try:
        meta, content = solar_artifacts.read_artifact(backend, tenant_id, drawing_id, artifact_id)
    except GraphValidationError as exc:
        raise GraphValidationError(_READ_CODES.get(exc.code, REPORT_CORRUPT)) from None
    if (meta["tool"] != report_module.REPORT_TOOL
            or meta["media_type"] != report_module.REPORT_MEDIA_TYPE
            or meta["filename"] != report_module.REPORT_FILENAME):
        raise GraphValidationError(REPORT_KIND_MISMATCH)
    if meta["source_version"] != version or meta["graph_sha256"] != graph_sha256:
        raise GraphValidationError(REPORT_STALE)
    try:
        report = json.loads(content.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys,
                            parse_constant=_reject_constant)
        if canonical_bytes(report) != content:
            raise ValueError("not canonical")
    except (ValueError, TypeError, RecursionError):
        raise GraphValidationError(REPORT_CORRUPT) from None
    return {"meta": meta, "report": report}


def _check(condition):
    if not condition:
        raise GraphValidationError(REPORT_INVALID)


def import_record(graph, params, resolved):
    """Fail closed with a named refusal when reading malformed stored report content."""
    try:
        return _import_record(graph, params, resolved)
    except GraphValidationError:
        raise
    except (TypeError, KeyError, ValueError, AttributeError, IndexError):
        raise GraphValidationError(REPORT_INVALID) from None


def _import_record(graph, params, resolved):
    """The leaf.solar-solaredge-import.v1 record for a validated graph and the resolved report.

    Refusals, first failure wins: REPORT_INVALID for a malformed input or a report naming another
    artifact, REPORT_STALE when the report was computed against any other graph, REPORT_AMBIGUOUS
    when a frame had more than one candidate grid and the report did not use the recorded
    selection order, REPORT_INVALID for any association that disagrees with the graph."""
    _check(type(resolved) is dict and set(resolved) == {"meta", "report"})
    meta, report = resolved["meta"], resolved["report"]
    _check(type(meta) is dict and meta.get("artifact_id") == params["report_artifact_id"]
           and meta.get("tool") == report_module.REPORT_TOOL)
    _check(_hex(meta["artifact_id"]) and _hex(meta["content_sha256"])
           and _int(meta["byte_length"], 1, solar_artifacts.MAX_ARTIFACT_BYTES)
           and _int(meta["source_version"], 1, 99_999_999) and _hex(meta["graph_sha256"])
           and _text(meta["drawing_id"], 128) and _text(meta["project_id"], 100))
    _check(type(report) is dict and set(report) == _REPORT_KEYS
           and report["schema"] == report_module.REPORT_SCHEMA)
    _check(_hex(report["graph_sha256"]) and _int(report["source_version"], 1, 99_999_999)
           and _text(report["drawing_id"], 128) and _text(report["project_id"], 100))
    graph_sha256 = digest(graph)
    if (report["graph_sha256"] != graph_sha256 or meta.get("graph_sha256") != graph_sha256
            or report["source_version"] != meta.get("source_version")):
        raise GraphValidationError(REPORT_STALE)
    _check(report["drawing_id"] == meta.get("drawing_id")
           and report["project_id"] == meta.get("project_id"))
    if report["project_id"] != graph["project"]["id"]:
        raise GraphValidationError(REPORT_STALE)
    _check(type(report["row_angle"]) in (int, float)
           and -MAX_LABEL <= report["row_angle"] <= MAX_LABEL
           and math.isfinite(report["row_angle"]))
    counts = report["counts"]
    _check(type(counts) is dict and set(counts) == _COUNT_KEYS
           and all(_int(value, 0, MAX_LABEL) for value in counts.values()))
    source, request = report["source"], report["request"]
    _check(type(source) is dict and set(source) == {"artifact_id", "content_sha256", "byte_length"}
           and _hex(source["artifact_id"]) and _hex(source["content_sha256"])
           and _int(source["byte_length"], 1, solar_artifacts.MAX_ARTIFACT_BYTES))
    _check(type(request) is dict and set(request) == {"alignment_tolerance", "selection_order"}
           and type(request["alignment_tolerance"]) is float
           and math.isfinite(request["alignment_tolerance"])
           and report_module.MIN_ALIGNMENT_TOLERANCE <= request["alignment_tolerance"]
           <= report_module.MAX_ALIGNMENT_TOLERANCE
           and type(request["selection_order"]) is str
           and request["selection_order"] in report_module.SELECTION_ORDERS)
    frames = graph["frames"]
    _check(0 < len(frames) <= MAX_FRAMES)
    frame_ids = {frame["id"] for frame in frames}
    handle_of = {p["id"]: p["provenance"].get("source_handle") for p in graph["panels"]}
    frame_of = {}
    for frame in frames:
        for ref in frame["panel_refs"]:
            frame_of[ref] = frame["id"]
    _check(len(frame_of) <= MAX_PANELS)

    matches = report["matches"]
    _check(type(matches) is list and 0 < len(matches) <= len(frames))
    matched, frame_rows = set(), []
    for match in matches:
        _check(type(match) is dict and set(match) == _MATCH_KEYS)
        ref, candidates = match["frame_ref"], match["candidates"]
        _check(_text(ref, 100) and ref in frame_ids and ref not in matched)
        _check(_int(match["pdf_grid"], 0, MAX_INDEX) and _int(match["pdf_matrix"], 0, MAX_INDEX)
               and _int(match["pdf_sub_grid"], -1, MAX_INDEX))
        _check(type(candidates) is list and 0 < len(candidates) <= MAX_CANDIDATES
               and all(_int(c, 0, MAX_INDEX) for c in candidates)
               and match["pdf_grid"] in candidates
               and _int(match["candidate_count"], len(candidates), MAX_INDEX))
        if match["candidate_count"] > 1 and request["selection_order"] != "recorded":
            raise GraphValidationError(REPORT_AMBIGUOUS)
        matched.add(ref)
        frame_rows.append({"frame_ref": ref, "pdf_matrix": match["pdf_matrix"],
                           "pdf_grid": match["pdf_grid"], "pdf_sub_grid": match["pdf_sub_grid"]})

    strings = report["strings"]
    _check(type(strings) is list and len(strings) <= MAX_PANELS)
    covered, string_rows = set(), []
    for position, row in enumerate(strings):
        _check(type(row) is dict and set(row) == _STRING_KEYS and row["index"] == position
               and type(row["index"]) is int)
        kind, ref = row["source"], row["frame_ref"]
        _check(type(kind) is str
               and ((kind == "group" and _text(ref, 100) and ref in matched)
                    or (kind == "bridge" and ref is None)))
        _check(_int(row["pdf_matrix"], 0, MAX_INDEX)
               and _int(row["pdf_inverter_id"], -1, MAX_LABEL)
               and _int(row["pdf_string_input"], 0, MAX_LABEL)
               and type(row["partial"]) is bool)
        refs, handles = row["panel_refs"], row["panel_handles"]
        _check(type(refs) is list and type(handles) is list
               and 0 < len(refs) == len(handles) <= MAX_PANELS)
        for panel_ref, handle in zip(refs, handles):
            _check(_text(panel_ref, 100) and panel_ref in frame_of and panel_ref not in covered
                   and (kind == "bridge" or frame_of[panel_ref] == ref)
                   and _text(handle, 4096) and type(handle_of[panel_ref]) is str
                   and handle == handle_of[panel_ref].upper())
            covered.add(panel_ref)
        string_rows.append({"index": position, "source": kind, "frame_ref": ref,
                            "pdf_matrix": row["pdf_matrix"], "pdf_inverter_id": row["pdf_inverter_id"],
                            "pdf_string_input": row["pdf_string_input"], "partial": row["partial"],
                            "panel_refs": list(refs)})

    unassigned = report["unassigned_panel_refs"]
    _check(type(unassigned) is list and len(unassigned) <= MAX_PANELS)
    for panel_ref in unassigned:
        _check(_text(panel_ref, 100) and panel_ref in frame_of and panel_ref not in covered)
        covered.add(panel_ref)
    _check(len(covered) == len(frame_of))
    return {"schema": STORE_SCHEMA,
            "source": {key: source[key] for key in ("artifact_id", "content_sha256", "byte_length")},
            "report": {key: meta[key] for key in ("artifact_id", "content_sha256", "byte_length",
                                                  "source_version", "graph_sha256")},
            "request": {"alignment_tolerance": request["alignment_tolerance"],
                        "selection_order": request["selection_order"]},
            "frames": frame_rows, "strings": string_rows,
            "unassigned_panel_refs": list(unassigned)}
