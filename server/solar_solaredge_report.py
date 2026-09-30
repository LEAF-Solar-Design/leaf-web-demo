"""Counts, frame matches and tracking rows from a stored SolarEdge PDF.

The report matches the source against the head graph's frames. Its immutable
JSON binding includes tenant, project, drawing, head version, graph digest and
a request digest over source id, source content digest, tolerance and selection
order. Repeated requests reuse that JSON without parsing. Requests are bounded
to 4096 bytes, tolerance to 1e-6 through 1e6, frames to 10,000, panels to 200,000
and displayed match candidates to 16. Parsing runs behind a per-process limit
of MAX_CONCURRENT_PARSES, in a child process (solar_solaredge_parse_worker.py)
under a fixed wall-time deadline of PARSE_DEADLINE_S: at the deadline the child
is killed and reaped before the parse slot is released, and the request is
refused as REPORT_PARSE_TIMEOUT with nothing stored. The report creates no
electrical entity, string, graph revision or Solve state, and fails closed. On
a loaded host the 1 MB fixture's first report took about 9 s (budget 10 s);
reuse took under 1 s (budget 1.5 s).
"""

import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import write_loop
import solar_solaredge_parse_worker as parse_worker
import solar_artifacts
import solar_import_sources
import solar_solaredge_import as si
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError
from solar_graph_context import resolve_graph_context
from solar_sizing_client import digest, units_resolved

REPORT_TOOL = "solar-solaredge-report"
REPORT_SCHEMA = "leaf.solar-solaredge-report.v1"
REQUEST_SCHEMA = "leaf.solar-solaredge-report-request.v1"
RESULT_SCHEMA = "leaf.solar-solaredge-report-result.v1"
REPORT_KIND = "solaredge-report"
REPORT_MEDIA_TYPE = "application/json"
REPORT_FILENAME = "solaredge-report.json"
PARSE_BASE_NAME = parse_worker.PARSE_BASE_NAME
SELECTION_ORDERS = ("unknown", "recorded")
MIN_ALIGNMENT_TOLERANCE = 1e-6
MAX_ALIGNMENT_TOLERANCE = 1e6
MAX_REPORT_FRAMES = 10_000
MAX_REPORT_PANELS = 200_000
MAX_CONCURRENT_PARSES = 2
MAX_REPORT_CANDIDATES = 16
MAX_REPORT_REQUEST_BYTES = 4096
# Wall time from starting the parse child to its reaped verdict. The 1 MB C14 fixture took 6.9 to
# 11.1 s through the child on a fully loaded 20-core host (two and four at once); 60 s is more than
# five times the worst case and leaves half of the 120 s load balancer idle timeout for the rest.
PARSE_DEADLINE_S = 60.0
_REQUEST_KEYS = frozenset(("source_artifact_id", "alignment_tolerance", "selection_order", "project_id"))
_PARSE_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_PARSES)


class SolarEdgeReportError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _artifact_refusal(code):
    return {
        "ARTIFACT_WRITES_DRAINED": "REPORT_WRITES_DRAINED",
        "ARTIFACT_STORE_UNAVAILABLE": "REPORT_STORE_UNAVAILABLE",
        "ARTIFACT_CONFLICT": "REPORT_ARTIFACT_CONFLICT",
        "ARTIFACT_CORRUPT": "REPORT_ARTIFACT_CORRUPT",
    }.get(code, "REPORT_ARTIFACT_INVALID")


def validate_request(request):
    if (type(request) is not dict or not set(request) <= _REQUEST_KEYS
            or not {"source_artifact_id", "alignment_tolerance"} <= set(request)):
        raise SolarEdgeReportError("REPORT_REQUEST_INVALID")
    source = request["source_artifact_id"]
    tolerance = request["alignment_tolerance"]
    order = request.get("selection_order", "unknown")
    project = request.get("project_id")
    # Compare bounds before finiteness so arbitrarily large JSON integers fail
    # without overflowing math.isfinite's conversion to float.
    if (not isinstance(source, str) or not re.fullmatch(r"[0-9a-f]{64}", source)
            or type(tolerance) not in (int, float)
            or not MIN_ALIGNMENT_TOLERANCE <= tolerance <= MAX_ALIGNMENT_TOLERANCE
            or not math.isfinite(tolerance)
            or not isinstance(order, str) or order not in SELECTION_ORDERS
            or (project is not None and (not isinstance(project, str) or not 1 <= len(project) <= 100))):
        raise SolarEdgeReportError("REPORT_REQUEST_INVALID")
    return {"source_artifact_id": source, "alignment_tolerance": float(tolerance),
            "selection_order": order, "project_id": project}


def report_from_matrices(graph, matrices, *, alignment_tolerance, selection_order):
    if not units_resolved(graph):
        raise SolarEdgeReportError("REPORT_UNITS_UNRESOLVED")
    frames = graph["frames"]
    if not frames:
        raise SolarEdgeReportError("REPORT_FRAMES_REQUIRED")
    if len(frames) > MAX_REPORT_FRAMES:
        raise SolarEdgeReportError("REPORT_LIMIT_EXCEEDED")
    panels = {p["id"]: p for p in graph["panels"]}
    ref_of, kernel_panels, groups = {}, [], []
    for frame in frames:
        refs = frame["panel_refs"]
        if not refs:
            raise SolarEdgeReportError("REPORT_FRAME_EMPTY")
        if len(kernel_panels) + len(refs) > MAX_REPORT_PANELS:
            raise SolarEdgeReportError("REPORT_LIMIT_EXCEEDED")
        handles = []
        for ref in refs:
            panel = panels[ref]
            handle = panel["provenance"].get("source_handle")
            if not isinstance(handle, str) or not re.fullmatch(r"[0-9A-Fa-f]{1,32}", handle):
                raise SolarEdgeReportError("REPORT_PANEL_HANDLE_INVALID")
            handle = handle.upper()
            if handle in ref_of:
                raise SolarEdgeReportError("REPORT_PANEL_HANDLE_DUPLICATE")
            ref_of[handle] = ref
            handles.append(handle)
            kernel_panels.append({"handle": handle, "x": float(panel["centre"][0]),
                                  "y": float(panel["centre"][1])})
        groups.append({"block": frame["id"], "panels": handles})
    try:
        row_angle = si.lattice_row_angle([(p["x"], p["y"]) for p in kernel_panels])
        result = si.run_import(matrices, groups, kernel_panels, row_angle=row_angle,
                               alignment_tolerance=alignment_tolerance, selection_order=selection_order)
    except si.SolarEdgeImportError as exc:
        code = {
            "ambiguous-structural-match": "REPORT_AMBIGUOUS_MATCH",
            "no-structural-match": "REPORT_NO_MATCH",
            "bridge-grid-unmatched": "REPORT_BRIDGE_UNRESOLVED",
            "bridge-handle-missing": "REPORT_BRIDGE_UNRESOLVED",
            "duplicate-bridge-seq": "REPORT_BRIDGE_UNRESOLVED",
            "lattice-angle-unavailable": "REPORT_ROW_ANGLE_UNRESOLVED",
            "lattice-angle-ambiguous": "REPORT_ROW_ANGLE_UNRESOLVED",
            "bound-exceeded": "REPORT_LIMIT_EXCEEDED",
            "duplicate-panel": "REPORT_PANEL_HANDLE_DUPLICATE",
        }.get(exc.code, "REPORT_PDF_UNSUPPORTED")
        raise SolarEdgeReportError(code) from None
    except (LookupError, TypeError, ValueError, ArithmeticError, RecursionError):
        raise SolarEdgeReportError("REPORT_PDF_UNSUPPORTED") from None
    matches = [{"frame_ref": m["block"], "pdf_grid": m["grid_index"],
                "pdf_matrix": m["original_index"], "pdf_sub_grid": m["sub_grid_id"],
                "candidates": list(m["candidates"][:MAX_REPORT_CANDIDATES]),
                "candidate_count": len(m["candidates"])} for m in result["matches"]]
    strings = []
    for s in result["strings"] + result["bridge_strings"]:
        source = s["source"]
        group = source["kind"] == "group"
        strings.append({"index": len(strings), "source": source["kind"],
                        "frame_ref": source["block"] if group else None,
                        "pdf_matrix": (result["matches"][source["group_index"]]["original_index"]
                                       if group else source["original_index"]),
                        "pdf_inverter_id": s["inverter_id"], "pdf_string_input": s["string_input_number"],
                        "partial": s["partial"], "panel_handles": list(s["panels"]),
                        "panel_refs": [ref_of[h] for h in s["panels"]]})
    unassigned = [ref_of[h] for h in result["unassigned"]]
    counts = {"pdf_matrices": len(matrices),
              "pdf_panels": sum(cell["Code"] == 1 for matrix in matrices
                                for row in matrix["Rows"] for cell in row["Panels"]),
              "matchable_grids": result["matchable_count"], "bridge_grids": result["bridge_grid_count"],
              "frames": len(frames), "matched_frames": len(matches),
              "group_strings": len(result["strings"]), "bridge_strings": len(result["bridge_strings"]),
              "strings": len(strings), "assigned_panels": sum(len(s["panel_refs"]) for s in strings),
              "unassigned_panels": len(unassigned), "partial_strings": sum(s["partial"] for s in strings)}
    return {"row_angle": row_angle, "counts": counts, "matches": matches,
            "strings": strings, "unassigned_panel_refs": unassigned}


def _worker_argv():
    """The parse child: this interpreter running solar_solaredge_parse_worker.py as a script."""
    return [sys.executable, "-B", os.path.abspath(parse_worker.__file__)]


def parse_source(content):
    """The PDF's matrices, parsed in a child process under a hard wall-time deadline.

    Returns only after the child has exited and been reaped, on every path, so a caller holding a
    parse slot never releases it while a parse is still running. A deadline miss kills the child
    (REPORT_PARSE_TIMEOUT); a child that cannot start is REPORT_PARSE_UNAVAILABLE; any other
    failure, including a crash or a malformed verdict, is REPORT_PDF_UNSUPPORTED.
    """
    if not isinstance(content, (bytes, bytearray)) or len(content) > parse_worker.MAX_INPUT_BYTES:
        raise SolarEdgeReportError("REPORT_PDF_UNSUPPORTED")
    source = tempfile.TemporaryFile()
    try:
        # Windows communicate writes pipe input synchronously before applying its timeout.
        source.write(content)
        source.seek(0)
        try:
            child = subprocess.Popen(_worker_argv(), stdin=source, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)
        except (OSError, ValueError):
            raise SolarEdgeReportError("REPORT_PARSE_UNAVAILABLE") from None
        timed_out = False
        try:
            out, _ = child.communicate(timeout=PARSE_DEADLINE_S)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            if child.returncode is None:
                # A killed child always exits; wait for it (no timeout) so the slot outlives it.
                child.kill()
                child.communicate()
    finally:
        source.close()
    if timed_out:
        raise SolarEdgeReportError("REPORT_PARSE_TIMEOUT")
    matrices = parse_worker.decode_verdict(child.returncode, out)
    if matrices is None:
        raise SolarEdgeReportError("REPORT_PDF_UNSUPPORTED")
    return matrices


def build_solaredge_report(backend, tenant_id, drawing_id, request):
    if write_loop.drawing_mutations_refusal() is not None:
        raise SolarEdgeReportError("REPORT_WRITES_DRAINED")
    req = validate_request(request)
    try:
        context = resolve_graph_context(backend, tenant_id, drawing_id, "head", project_id=req["project_id"])
    except GraphValidationError as exc:
        code = {"PROJECT_MISMATCH": "REPORT_PROJECT_MISMATCH",
                "GRAPH_CONTEXT_UNAVAILABLE": "REPORT_DRAWING_NOT_FOUND"}.get(exc.code, "REPORT_GRAPH_REQUIRED")
        raise SolarEdgeReportError(code) from None
    except (OSError, RuntimeError):
        raise SolarEdgeReportError("REPORT_STORE_UNAVAILABLE") from None
    # Hex spellings among panels sent to the kernel are ambiguous evidence.
    # Refuse before source I/O; numeric ties in the kernel's set ordering would
    # otherwise make immutable report bytes depend on the process hash seed.
    framed_panel_refs = {ref for frame in context["graph"]["frames"] for ref in frame["panel_refs"]}
    handle_numbers = set()
    for panel in context["graph"]["panels"]:
        if panel["id"] not in framed_panel_refs:
            continue
        handle = panel["provenance"].get("source_handle")
        if isinstance(handle, str) and re.fullmatch(r"[0-9A-Fa-f]{1,32}", handle):
            number = int(handle, 16)
            if number in handle_numbers:
                raise SolarEdgeReportError("REPORT_HANDLE_ALIAS")
            handle_numbers.add(number)
    try:
        meta, content = solar_import_sources.load_import_source(
            backend, tenant_id, drawing_id, req["source_artifact_id"], project_id=context["project_id"])
    except solar_import_sources.ImportSourceError as exc:
        raise SolarEdgeReportError(exc.code) from None
    request_sha256 = digest({"schema": REQUEST_SCHEMA, "source_artifact_id": req["source_artifact_id"],
                             "source_content_sha256": meta["content_sha256"],
                             "alignment_tolerance": req["alignment_tolerance"],
                             "selection_order": req["selection_order"]})
    try:
        sink = solar_artifacts.ArtifactSink(backend, tenant_id, drawing_id, context,
                                            REPORT_TOOL, request_sha256, False)
        artifact_id = digest(solar_artifacts.artifact_binding(
            tenant_id, drawing_id, context, REPORT_TOOL, request_sha256))
    except GraphValidationError as exc:
        raise SolarEdgeReportError(_artifact_refusal(exc.code)) from None
    try:
        stored_meta, stored = solar_artifacts.read_artifact(backend, tenant_id, drawing_id, artifact_id)
    except GraphValidationError as exc:
        if exc.code != "ARTIFACT_NOT_FOUND":
            raise SolarEdgeReportError(_artifact_refusal(exc.code)) from None
    else:
        try:
            report = json.loads(stored.decode("utf-8"))
            prepared = sink.prepare(solar_artifacts.ArtifactOutput(
                report["counts"], REPORT_MEDIA_TYPE, REPORT_FILENAME, stored))
            if prepared.meta != stored_meta or canonical_bytes(report) != stored:
                raise ValueError()
        except (ValueError, TypeError, LookupError, RecursionError, GraphValidationError):
            raise SolarEdgeReportError("REPORT_ARTIFACT_CORRUPT") from None
        return _result(drawing_id, context, req, report, prepared.ref)
    if not _PARSE_SLOTS.acquire(blocking=False):
        raise SolarEdgeReportError("REPORT_BUSY")
    try:
        matrices = parse_source(content)
    finally:
        _PARSE_SLOTS.release()
    core = report_from_matrices(context["graph"], matrices,
                                alignment_tolerance=req["alignment_tolerance"], selection_order=req["selection_order"])
    report = {"schema": REPORT_SCHEMA, "drawing_id": drawing_id, "project_id": context["project_id"],
              "source_version": context["resolved_version"], "graph_sha256": context["graph_sha256"],
              "source": {"artifact_id": req["source_artifact_id"], "content_sha256": meta["content_sha256"],
                         "byte_length": meta["byte_length"]},
              "request": {"alignment_tolerance": req["alignment_tolerance"], "selection_order": req["selection_order"]},
              **core}
    try:
        prepared = sink.prepare(solar_artifacts.ArtifactOutput(
            report["counts"], REPORT_MEDIA_TYPE, REPORT_FILENAME, canonical_bytes(report)))
        ref = sink.finish(prepared)
    except GraphValidationError as exc:
        raise SolarEdgeReportError(_artifact_refusal(exc.code)) from None
    return _result(drawing_id, context, req, report, ref)


def _result(drawing_id, context, req, report, ref):
    return {"schema": RESULT_SCHEMA, "kind": REPORT_KIND, "drawing_id": drawing_id,
            "project_id": context["project_id"], "source_version": context["resolved_version"],
            "graph_sha256": context["graph_sha256"], "source_artifact_id": req["source_artifact_id"],
            "counts": report["counts"], "report": ref}
