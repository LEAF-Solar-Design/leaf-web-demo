"""Read the accepted SolarEdge tracking record (import-solaredge-pdf, the read after the accept).

The accept (builtins/solar_solaredge_accept.py) stores one leaf.solar-solaredge-import.v1 record at
graph["extra"]["solaredge_import"] (server/solar_solaredge_tracking.py). This read returns that
record's provenance, its counts, a check of its references against the graph being read, and one
bounded page of one section: "strings" (the PDF string rows), "frames" (the frame to PDF grid rows)
or "unassigned" (framed panels no PDF string covers). The PDF inverter ids and string inputs are
labels only: nothing here is a string, inverter, device, route, schedule or Solve state.

Freshness is a reference check, never a digest comparison. record.report.graph_sha256 names the
graph BEFORE the accept, so it never equals the digest of the graph that holds the record; the
check instead counts recorded frames and panels that are gone, recorded panels that left their
frame, and framed panels the record does not name. "current" is true only when all four are zero.

Provenance is bound to THIS drawing. The shared read path (server/solar_local_read.py) passes a
read-only lookup into the version history of the drawing being read, because this module declares
READS_VERSION_HISTORY; record.report.source_version must name a version of that drawing whose graph
digest is exactly record.report.graph_sha256. A record transplanted from another drawing, or one
whose report fields were edited, refuses. A direct call with no lookup skips only that binding.

A drawing with no record reads accepted false with an empty page. A record that is not the exact
stored shape, or not bound to this drawing's history, is SOLAREDGE_IMPORT_RECORD_INVALID; a
malformed request is INVALID_SOLAREDGE_TRACKING_READ_REQUEST. Pages hold at most 1,000 items and
262,144 canonical item bytes (the first item on a page always fits). Linear in the record and graph
size; its only I/O is one bounded history lookup through the caller's read-only lookup.
"""
import copy
import math
import re

import solar_artifacts
import solar_solaredge_report as report_module
import solar_solaredge_tracking as tracking
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError

TOOL = "solar-solaredge-tracking-read"
READS_VERSION_HISTORY = True
OUTPUT_SCHEMA = "leaf.solar-solaredge-tracking-read.v1"
INVALID = "INVALID_SOLAREDGE_TRACKING_READ_REQUEST"
RECORD_INVALID = "SOLAREDGE_IMPORT_RECORD_INVALID"
SECTIONS = ("strings", "frames", "unassigned")
PAGE_ITEMS = 1_000
PAGE_BYTES = 262_144
MAX_REF = 100
MAX_VERSION = 99_999_999
_PARAM_KEYS = frozenset(("section", "offset"))
_RECORD_KEYS = frozenset(("schema", "source", "report", "request", "frames", "strings",
                          "unassigned_panel_refs"))
_SOURCE_KEYS = frozenset(("artifact_id", "content_sha256", "byte_length"))
_REPORT_KEYS = frozenset(("artifact_id", "content_sha256", "byte_length", "source_version",
                          "graph_sha256"))
_FRAME_KEYS = frozenset(("frame_ref", "pdf_matrix", "pdf_grid", "pdf_sub_grid"))
_STRING_KEYS = frozenset(("index", "source", "frame_ref", "pdf_matrix", "pdf_inverter_id",
                          "pdf_string_input", "partial", "panel_refs"))
_HEX64 = re.compile(r"[0-9a-f]{64}")


def _hex(value):
    return type(value) is str and _HEX64.fullmatch(value) is not None


def _int(value, lower, upper):
    return type(value) is int and lower <= value <= upper


def _ref(value):
    return type(value) is str and 0 < len(value) <= MAX_REF


def _check(condition):
    if not condition:
        raise GraphValidationError(RECORD_INVALID)


def _request(params):
    if type(params) is not dict or not set(params) <= _PARAM_KEYS:
        raise GraphValidationError(INVALID)
    section = params.get("section", "strings")
    offset = params.get("offset", 0)
    if (type(section) is not str or section not in SECTIONS
            or not _int(offset, 0, tracking.MAX_PANELS)):
        raise GraphValidationError(INVALID)
    return section, offset


def _validated(record):
    """The stored record, checked field by field against the accept's bounds; fails closed."""
    _check(type(record) is dict and set(record) == _RECORD_KEYS
           and record["schema"] == tracking.STORE_SCHEMA)
    source, report, request = record["source"], record["report"], record["request"]
    _check(type(source) is dict and set(source) == _SOURCE_KEYS
           and _hex(source["artifact_id"]) and _hex(source["content_sha256"])
           and _int(source["byte_length"], 1, solar_artifacts.MAX_ARTIFACT_BYTES))
    _check(type(report) is dict and set(report) == _REPORT_KEYS
           and _hex(report["artifact_id"]) and _hex(report["content_sha256"])
           and _int(report["byte_length"], 1, solar_artifacts.MAX_ARTIFACT_BYTES)
           and _int(report["source_version"], 1, MAX_VERSION) and _hex(report["graph_sha256"]))
    tolerance = request.get("alignment_tolerance") if type(request) is dict else None
    _check(type(request) is dict and set(request) == {"alignment_tolerance", "selection_order"}
           and type(tolerance) is float and math.isfinite(tolerance)
           and report_module.MIN_ALIGNMENT_TOLERANCE <= tolerance
           <= report_module.MAX_ALIGNMENT_TOLERANCE
           and type(request["selection_order"]) is str
           and request["selection_order"] in report_module.SELECTION_ORDERS)
    frames, strings = record["frames"], record["strings"]
    unassigned = record["unassigned_panel_refs"]
    _check(type(frames) is list and 0 < len(frames) <= tracking.MAX_FRAMES)
    frame_refs = set()
    for row in frames:
        _check(type(row) is dict and set(row) == _FRAME_KEYS and _ref(row["frame_ref"])
               and row["frame_ref"] not in frame_refs
               and _int(row["pdf_matrix"], 0, tracking.MAX_INDEX)
               and _int(row["pdf_grid"], 0, tracking.MAX_INDEX)
               and _int(row["pdf_sub_grid"], -1, tracking.MAX_INDEX))
        frame_refs.add(row["frame_ref"])
    _check(type(strings) is list and len(strings) <= tracking.MAX_PANELS
           and type(unassigned) is list and len(unassigned) <= tracking.MAX_PANELS)
    seen = set()
    for position, row in enumerate(strings):
        _check(type(row) is dict and set(row) == _STRING_KEYS
               and type(row["index"]) is int and row["index"] == position)
        kind, ref = row["source"], row["frame_ref"]
        _check(type(kind) is str and ((kind == "group" and _ref(ref) and ref in frame_refs)
                                      or (kind == "bridge" and ref is None)))
        _check(_int(row["pdf_matrix"], 0, tracking.MAX_INDEX)
               and _int(row["pdf_inverter_id"], -1, tracking.MAX_LABEL)
               and _int(row["pdf_string_input"], 0, tracking.MAX_LABEL)
               and type(row["partial"]) is bool)
        refs = row["panel_refs"]
        _check(type(refs) is list and 0 < len(refs) <= tracking.MAX_PANELS)
        for panel_ref in refs:
            _check(_ref(panel_ref) and panel_ref not in seen)
            seen.add(panel_ref)
        _check(len(seen) <= tracking.MAX_PANELS)
    for panel_ref in unassigned:
        _check(_ref(panel_ref) and panel_ref not in seen)
        seen.add(panel_ref)
    _check(len(seen) <= tracking.MAX_PANELS)
    return record


def _bound(record, version_graph_sha256):
    """The report names a version of THIS drawing whose graph digest it carries; fails closed.

    Called with the shared read path's lookup by the read and by its terminal proof alike, so both
    answer the same. With no lookup (a direct call) the binding is not checkable and is skipped.
    """
    if version_graph_sha256 is None:
        return
    report = record["report"]
    if version_graph_sha256(report["source_version"]) != report["graph_sha256"]:
        raise GraphValidationError(
            RECORD_INVALID, "report.source_version and report.graph_sha256 are not a version of "
                            "this drawing")


def _counts(record):
    strings = record["strings"]
    group = sum(1 for row in strings if row["source"] == "group")
    return {"frames": len(record["frames"]), "strings": len(strings), "group_strings": group,
            "bridge_strings": len(strings) - group,
            "assigned_panels": sum(len(row["panel_refs"]) for row in strings),
            "unassigned_panels": len(record["unassigned_panel_refs"]),
            "partial_strings": sum(1 for row in strings if row["partial"]),
            "distinct_labels": len({(row["pdf_inverter_id"], row["pdf_string_input"])
                                    for row in strings})}


def _references(graph, record):
    """Which recorded references still hold on this graph, by identity, never by digest."""
    frame_of = {}
    for frame in graph["frames"]:
        for panel_ref in frame["panel_refs"]:
            frame_of[panel_ref] = frame["id"]
    frame_ids = {frame["id"] for frame in graph["frames"]}
    panel_ids = {panel["id"] for panel in graph["panels"]}
    missing_frames = sum(1 for row in record["frames"] if row["frame_ref"] not in frame_ids)
    missing = moved = 0
    recorded = set()
    rows = [(row["source"], row["frame_ref"], row["panel_refs"]) for row in record["strings"]]
    rows.append(("unassigned", None, record["unassigned_panel_refs"]))
    for kind, frame_ref, refs in rows:
        for panel_ref in refs:
            recorded.add(panel_ref)
            if panel_ref not in panel_ids:
                missing += 1
            elif panel_ref not in frame_of or (kind == "group" and frame_of[panel_ref] != frame_ref):
                moved += 1
    unrecorded = sum(1 for panel_ref in frame_of if panel_ref not in recorded)
    return {"current": missing_frames == missing == moved == unrecorded == 0,
            "missing_frames": missing_frames, "missing_panels": missing, "moved_panels": moved,
            "unrecorded_panels": unrecorded}


def _page(items, offset):
    """At most PAGE_ITEMS items and PAGE_BYTES canonical item bytes from offset; the first fits."""
    if offset > len(items):
        raise GraphValidationError(INVALID)
    page, size = [], 0
    for item in items[offset:offset + PAGE_ITEMS]:
        size += len(canonical_bytes(item)) + 1
        if page and size > PAGE_BYTES:
            break
        page.append(item)
    end = offset + len(page)
    return page, {"offset": offset, "returned": len(page), "total": len(items),
                  "next_offset": end if end < len(items) else None}


def run(graph, params, version_graph_sha256=None):
    section, offset = _request(params)
    extra = graph["extra"]
    if tracking.STORE_KEY not in extra:
        items, page = _page([], offset)
        return {"schema": OUTPUT_SCHEMA, "accepted": False, "labels_only": True,
                "provenance": None, "counts": None, "references": None,
                "section": section, "page": page, "items": items}
    try:
        record = _validated(extra[tracking.STORE_KEY])
    except (TypeError, KeyError, ValueError, AttributeError, IndexError):
        raise GraphValidationError(RECORD_INVALID) from None
    # Outside the shape guard so the binding's named refusal and the lookup's own limit survive.
    _bound(record, version_graph_sha256)
    items = {"strings": record["strings"], "frames": record["frames"],
             "unassigned": record["unassigned_panel_refs"]}[section]
    items, page = _page(items, offset)
    # These nine keys are the fixed output contract. The copy keeps the output independent of the
    # graph's objects; it is bounded by one page plus the three provenance blocks.
    return copy.deepcopy({
        "schema": OUTPUT_SCHEMA, "accepted": True, "labels_only": True,
        "provenance": {key: record[key] for key in ("source", "report", "request")},
        "counts": _counts(record), "references": _references(graph, record),
        "section": section, "page": page, "items": items})
