"""X05 normal-user import and explicitly bound grid coverage, bounded first slice.

The runtime-only xbench_studio must supply the project lifecycle methods used
below. A missing or incompatible client is harness evidence, never product FAIL.
"""

import hashlib
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import re
from uuid import uuid4

from xbench_core import Step, read_health, write_json_atomic


LIMITS = ("Steps 1-2 cover source import and grid correspondence only. "
          "This slice supports Roof and Ground geometry setups. "
          "Module counts must equal independent manifest oracles; grid counts are exact "
          "unless explicitly bound to null-grid match_or_named coverage. "
          "Step 3 requires an independently pinned legend oracle and normal acceptance "
          "followed by canonical saved electrical string counts; report rows are diagnostic only. "
          "Count mismatches can FAIL, but matching counts remain UNREADABLE because "
          "per-string saved optimizer measurements are unavailable. Without an explicit "
          "oracle no acceptance is attempted. Steps 4-7 are unimplemented. Unknown wire "
          "formats, missing fixtures, auth, drift and nonterminal jobs are UNREADABLE.")
REQUIRED = {"id", "drawing_fixture", "report_fixture", "panel_layer_contains",
            "drawing_units", "installation_design", "branch_max_offset",
            "alignment_tolerance", "expected_modules", "expected_grids", "expected_strings"}
MAX_REASON_CODE = 80
PRODUCT_REFUSAL_CODES = frozenset({
    "IMPORT_PDF_INVALID", "IMPORT_PDF_MALFORMED", "IMPORT_PDF_UNSUPPORTED",
    "REPORT_GRID_UNMATCHED", "REPORT_NO_MATCH", "REPORT_AMBIGUOUS_MATCH",
    "REPORT_PDF_UNSUPPORTED",
})
SAFE_REASON_CODES = PRODUCT_REFUSAL_CODES | frozenset({
    "CONTEXT_DRIFT", "FIXTURE_UNVERIFIED", "FIXTURE_CHANGED",
    "FIXTURE_METADATA_UNREADABLE", "PROJECT_METADATA_UNREADABLE",
    "MANIFEST_UNREADABLE", "SETUP_UNREADABLE", "SOURCE_DRIFT",
    "SOURCE_WIRE_UNREADABLE", "REPORT_WIRE_UNREADABLE", "JOB_NOT_COMPLETE",
    "MUTATION_UNREADABLE", "SERVER_REASON_UNRECOGNISED",
    "ORACLE_UNREADABLE", "SAVED_STRINGS_UNREADABLE",
})


def _reason_code(value):
    if value is None:
        return None
    if type(value) is str and len(value) <= MAX_REASON_CODE and value in SAFE_REASON_CODES:
        return value
    return "SERVER_REASON_UNRECOGNISED"


def _fixture_id(value):
    return (type(value) is str and len(value) <= 128
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) is not None)


def _metadata_problem(manifest, verified):
    if not isinstance(manifest, dict):
        return "MANIFEST_UNREADABLE"
    for fixtures in (manifest.get("fixtures", []), verified):
        if not isinstance(fixtures, list):
            return "FIXTURE_METADATA_UNREADABLE"
        ids = set()
        for fixture in fixtures:
            if not isinstance(fixture, dict) or not _fixture_id(fixture.get("id")):
                return "FIXTURE_METADATA_UNREADABLE"
            if fixture["id"] in ids:
                return "FIXTURE_METADATA_UNREADABLE"
            ids.add(fixture["id"])
    projects = manifest.get("projects", [])
    if isinstance(projects, list):
        for project in projects:
            if not isinstance(project, dict):
                return "PROJECT_METADATA_UNREADABLE"
            for key in ("id", "drawing_fixture", "report_fixture"):
                if key in project and not _fixture_id(project[key]):
                    return "PROJECT_METADATA_UNREADABLE"
    return None


def _integer(value, minimum=0):
    # Counts cross a JSON transport; reject integers that cannot round-trip exactly.
    return type(value) is int and minimum <= value <= 2**53 - 1


def _positive_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        return False


def _setup(project):
    return (isinstance(project, dict) and REQUIRED <= project.keys()
            and all(isinstance(project[k], str) and project[k].strip() for k in
                    ("id", "drawing_fixture", "report_fixture", "panel_layer_contains"))
            and project["drawing_units"] in ("m", "mm", "cm", "km", "in", "ft", "yd")
            and type(project["installation_design"]) is str
            and project["installation_design"] in ("Roof", "Ground")
            and all(_positive_number(project[k]) for k in ("branch_max_offset", "alignment_tolerance"))
            and (_integer(project["expected_grids"], 1)
                 or (project["expected_grids"] is None
                     and project.get("grid_count_policy") == "match_or_named"))
            and all(_integer(project[k], 1) for k in
                    ("expected_modules", "expected_strings")))


def _null_grid_score(project, result):
    """Cover saved frames with matches or named causes, without a grid oracle."""
    if not isinstance(result, dict):
        return "UNREADABLE", "report_wire_unreadable"
    if (not _integer(result.get("panel_count"), 1)
            or not _integer(result.get("frame_count"), 1)
            or result["panel_count"] != project["expected_modules"]):
        return "UNREADABLE", "discovery_count_blocker"
    frames = result.get("frame_refs")
    if (not isinstance(frames, list) or len(frames) != result["frame_count"]
            or any(not isinstance(f, str) or not f.strip() for f in frames)
            or len(set(frames)) != len(frames)):
        return "UNREADABLE", "report_wire_unreadable"
    if result.get("report_product_failure") is True:
        if not _setup(project) or result.get("setup_verified") is not True:
            return "UNREADABLE", "setup_or_session_unreadable"
        return "FAIL", "product_report_refusal"
    report = result.get("report")
    if not isinstance(report, dict):
        return "UNREADABLE", "report_wire_unreadable"
    counts, rows = report.get("counts"), report.get("matches")
    if not isinstance(counts, dict) or not isinstance(rows, list):
        return "UNREADABLE", "report_wire_unreadable"
    for key in ("pdf_panels", "matchable_grids", "frames", "matched_frames"):
        if not _integer(counts.get(key)):
            return "UNREADABLE", "report_counts_unreadable"
    if (counts["pdf_panels"] != project["expected_modules"]
            or counts["frames"] != len(frames)):
        return "UNREADABLE", "report_setup_count_blocker"
    seen_frames, seen_grids, causes = [], [], set()
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("frame_ref"), str)
                or not row["frame_ref"].strip()):
            return "UNREADABLE", "report_row_unreadable"
        candidates = row.get("candidates")
        if (not isinstance(candidates, list) or any(not _integer(c) for c in candidates)
                or len(set(candidates)) != len(candidates)
                or not _integer(row.get("candidate_count"))):
            return "UNREADABLE", "report_candidates_unreadable"
        if "status" in row:
            status = row["status"]
            if (status not in ("unmatched", "ambiguous")
                    or any(key not in row or row[key] is not None
                           for key in ("pdf_grid", "pdf_matrix", "pdf_sub_grid"))):
                return "UNREADABLE", "report_row_unreadable"
            if (row["candidate_count"] != len(candidates)
                    or (status == "unmatched" and candidates)
                    or (status == "ambiguous" and not candidates)):
                return "UNREADABLE", "report_candidates_unreadable"
            causes.add("SOLAREDGE_GRID_UNMATCHED" if status == "unmatched"
                       else "SOLAREDGE_GRID_AMBIGUOUS")
        else:
            if (not _integer(row.get("pdf_grid"))
                    or not _integer(row.get("pdf_matrix"))
                    or not _integer(row.get("pdf_sub_grid"), -1)):
                return "UNREADABLE", "report_row_unreadable"
            if (not candidates or row["pdf_grid"] not in candidates
                    or row["candidate_count"] < len(candidates)):
                return "UNREADABLE", "report_candidates_unreadable"
            seen_grids.append(row["pdf_grid"])
        seen_frames.append(row["frame_ref"])
    if counts["matched_frames"] != len(seen_grids):
        return "UNREADABLE", "report_counts_unreadable"
    if len(set(seen_frames)) != len(seen_frames) or len(set(seen_grids)) != len(seen_grids):
        return "FAIL", "product_duplicate_grid_association"
    if len(rows) != len(frames) or set(seen_frames) != set(frames):
        return "FAIL", "product_incomplete_grid_correspondence"
    reason = "match_or_named_grid_coverage"
    if causes:
        reason += "; normalized status causes (not emitted row codes): " + ", ".join(sorted(causes))
    return "PASS", reason


def _grid_score(project, result):
    """Validate nested wire containers before comparing any associations."""
    if project.get("expected_grids") is None and project.get("grid_count_policy") == "match_or_named":
        return _null_grid_score(project, result)
    if not isinstance(result, dict):
        return "UNREADABLE", "report_wire_unreadable"
    if (not _integer(result.get("panel_count"), 1)
            or not _integer(result.get("frame_count"), 1)
            or result["panel_count"] != project["expected_modules"]
            or result["frame_count"] != project["expected_grids"]):
        return "UNREADABLE", "discovery_count_blocker"
    if result.get("report_product_failure") is True:
        return "FAIL", "product_report_refusal"
    report, frames = result.get("report"), result.get("frame_refs")
    if (not isinstance(report, dict) or not isinstance(frames, list)
            or len(frames) != project["expected_grids"]
            or any(not isinstance(f, str) or not f for f in frames)
            or len(set(frames)) != len(frames)):
        return "UNREADABLE", "report_wire_unreadable"
    counts, rows = report.get("counts"), report.get("matches")
    if not isinstance(counts, dict) or not isinstance(rows, list):
        return "UNREADABLE", "report_wire_unreadable"
    for key in ("pdf_panels", "matchable_grids", "frames", "matched_frames"):
        if not _integer(counts.get(key)):
            return "UNREADABLE", "report_counts_unreadable"
    if (counts["pdf_panels"] != project["expected_modules"]
            or counts["matchable_grids"] != project["expected_grids"]
            or counts["frames"] != len(frames)):
        return "UNREADABLE", "report_setup_count_blocker"
    seen_frames, seen_grids = [], []
    for row in rows:
        if not isinstance(row, dict):
            return "UNREADABLE", "report_row_unreadable"
        if row.get("status") in ("unmatched", "ambiguous"):
            return "FAIL", "product_grid_" + row["status"]
        if ("status" in row or not isinstance(row.get("frame_ref"), str)
                or not _integer(row.get("pdf_grid"))
                or not _integer(row.get("pdf_matrix"))
                or not _integer(row.get("pdf_sub_grid"), -1)
                or not isinstance(row.get("candidates"), list)
                or any(not _integer(c) for c in row["candidates"])
                or not _integer(row.get("candidate_count"), 1)):
            return "UNREADABLE", "report_row_unreadable"
        # The product may cap the candidate list; candidate_count is structural,
        # while an explicit unresolved status carries the ambiguity verdict.
        if (not row["candidates"] or len(set(row["candidates"])) != len(row["candidates"])
                or row["pdf_grid"] not in row["candidates"]
                or row["candidate_count"] < len(row["candidates"])):
            return "UNREADABLE", "report_candidates_unreadable"
        seen_frames.append(row["frame_ref"])
        seen_grids.append(row["pdf_grid"])
    if len(set(seen_frames)) != len(seen_frames) or len(set(seen_grids)) != len(seen_grids):
        return "FAIL", "product_duplicate_grid_association"
    if (set(seen_frames) != set(frames)
            or set(seen_grids) != set(range(project["expected_grids"]))
            or counts["matched_frames"] != len(frames)):
        return "FAIL", "product_incomplete_grid_correspondence"
    return "PASS", "exact_grid_correspondence"


def score(projects, results):
    """Aggregate every configured project, including missing/duplicate results.

    Results are internal observations, keyed by manifest project id. Only explicit
    validated source success and complete report mappings can establish a PASS.
    """
    buckets = {1: [], 2: [], 3: []}
    diagnostics = []
    projects = projects if isinstance(projects, list) else []
    results = results if isinstance(results, list) else []
    ids = Counter(p.get("id") for p in projects
                  if isinstance(p, dict) and isinstance(p.get("id"), str))
    indexed = defaultdict(list)
    for result in results:
        if isinstance(result, dict) and isinstance(result.get("id"), str):
            indexed[result["id"]].append(result)
    for project in projects:
        valid = _setup(project) and ids[project["id"]] == 1
        found = indexed.get(project["id"], []) if valid else []
        result = found[0] if valid and len(found) == 1 else {}
        if (not valid or result.get("unreadable") or not result.get("setup_verified")
                or result.get("status") in (401, 403)):
            buckets[1].append(("UNREADABLE", "setup_or_session_unreadable"))
            buckets[2].append(("UNREADABLE", "setup_or_session_unreadable"))
            buckets[3].append(("UNREADABLE", "setup_or_session_unreadable"))
            continue
        imported = result.get("import_verdict")
        buckets[1].append((imported if imported in ("PASS", "FAIL") else "UNREADABLE",
                           "source_import"))
        buckets[2].append(_grid_score(project, result) if imported == "PASS"
                          else ("UNREADABLE", "source_import_unavailable"))
        if "oracle_fixture" not in project or result.get("oracle_readable") is not True:
            buckets[3].append(("UNREADABLE", "independent_oracle_unreadable"))
        elif (result.get("accepted") is not True or result.get("saved_head_read") is not True
              or not _integer(result.get("saved_string_count"))):
            buckets[3].append(("UNREADABLE", "saved_string_observation_unreadable"))
        elif result["saved_string_count"] != project["expected_strings"]:
            buckets[3].append(("FAIL", "product_saved_string_count_mismatch"))
        else:
            buckets[3].append(("UNREADABLE", "saved_optimizer_measurements_unavailable"))
        report = result.get("report")
        if isinstance(report, dict) and isinstance(report.get("strings"), list):
            diagnostics.append({"report_string_rows": len(report["strings"]),
                                "expected_strings": project["expected_strings"]})
    steps = []
    for n in (1, 2):
        rows = buckets[n]
        verdict = ("FAIL" if any(v == "FAIL" for v, _ in rows) else
                   "PASS" if rows and all(v == "PASS" for v, _ in rows) else "UNREADABLE")
        steps.append(Step(n, verdict, "x05-evidence.json; " +
                          ", ".join(sorted({reason for _, reason in rows})),
                          observed={"projects": len(rows)}))
    rows = buckets[3]
    verdict = ("FAIL" if rows and all(v == "FAIL" for v, _ in rows) else "UNREADABLE")
    steps.append(Step(3, verdict, "x05-evidence.json; " +
                      ", ".join(sorted({reason for _, reason in rows})), observed=diagnostics))
    steps.extend(Step(n, "UNREADABLE", "Unimplemented in this slice. " + LIMITS)
                 for n in range(4, 8))
    return steps


class ObservationError(Exception):
    def __init__(self, status=None, code=None, product=False):
        self.status = status if type(status) is int else None
        self.code = _reason_code(code)
        self.product = product is True and self.code in PRODUCT_REFUSAL_CODES


def _fixture(ctx, identifier):
    if not _fixture_id(identifier) or _metadata_problem(ctx.manifest, ctx.fixtures):
        raise ObservationError(code="FIXTURE_METADATA_UNREADABLE")
    fixtures = [f for f in ctx.manifest.get("fixtures", []) if f.get("id") == identifier]
    verified = [f for f in ctx.fixtures if f.get("id") == identifier and f.get("ok") is True]
    if len(fixtures) != 1 or len(verified) != 1:
        raise ObservationError(code="FIXTURE_UNVERIFIED")
    fixture = fixtures[0]
    data = Path(fixture["path"]).read_bytes()
    if len(data) != fixture["bytes"] or hashlib.sha256(data).hexdigest() != fixture["sha256"]:
        raise ObservationError(code="FIXTURE_CHANGED")
    return data


def _oracle(ctx, project, pdf):
    """Consume only a separately pinned, source-bound independent legend result."""
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError
            value[key] = item
        return value

    def finite(raw):
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError
        return value

    try:
        if project["oracle_fixture"] in (project["drawing_fixture"], project["report_fixture"]):
            raise ValueError
        data = _fixture(ctx, project["oracle_fixture"])
        if len(data) > 5 * 1024 * 1024:
            raise ValueError
        oracle = json.loads(data, object_pairs_hook=pairs, parse_float=finite,
                            parse_constant=lambda raw: finite(raw))
        source = {"bytes": len(pdf), "sha256": hashlib.sha256(pdf).hexdigest()}
        if (not isinstance(oracle, dict) or oracle.get("status") != "READABLE"
                or oracle.get("source") != source
                or not isinstance(oracle["source"], dict)
                or not _integer(oracle["source"].get("bytes"), 1)):
            raise ValueError
        totals, rows = oracle.get("totals"), oracle.get("strings")
        if (not isinstance(totals, dict) or not isinstance(rows, list)
                or not 1 <= len(rows) <= 20000
                or any(not _integer(totals.get(k), 1) for k in
                       ("modules", "optimizers", "inverters", "strings"))):
            raise ValueError
        identifiers = set()
        modules = optimizers = 0
        for row in rows:
            if (not isinstance(row, dict) or type(row.get("identifier")) is not str
                    or re.fullmatch(r"[1-9][0-9]{0,8}\.[1-9][0-9]{0,8}", row["identifier"]) is None
                    or row["identifier"] in identifiers
                    or not _integer(row.get("optimizers"), 1)
                    or not _integer(row.get("modules"), 1)):
                raise ValueError
            identifiers.add(row["identifier"])
            modules += row["modules"]
            optimizers += row["optimizers"]
        if (totals["modules"] != modules or totals["optimizers"] != optimizers
                or totals["strings"] != len(rows)
                or totals["inverters"] != len({i.split(".")[0] for i in identifiers})
                or modules != project["expected_modules"]
                or len(rows) != project["expected_strings"]):
            raise ValueError
    except Exception:
        raise ObservationError(code="ORACLE_UNREADABLE") from None


def _project(ctx, client, request, project, result):
    drawing = _fixture(ctx, project["drawing_fixture"])
    pdf = _fixture(ctx, project["report_fixture"])
    if "oracle_fixture" in project:
        _oracle(ctx, project, pdf)
        result["oracle_readable"] = True
    created = client.create_project(name="xbench-x05-" + uuid4().hex)
    project_id = created["project_id"]
    capability = None
    drawing_id = version = None

    def context():
        nonlocal version
        current = client.drawing_context(project_id, version, drawing_id)
        version = current["context"]["head_version_id"]
        current = client.drawing_context(project_id, version, drawing_id)
        identity = current["context"]
        if (not identity["is_head"] or identity["organization_id"] != org_id
                or identity["project_id"] != project_id or identity["drawing_id"] != drawing_id):
            raise ObservationError(code="CONTEXT_DRIFT")
        return current

    def mutate(tool, params):
        status, body = client.run(tool, org_id=org_id, project_id=project_id,
                                  version_id=version, params={"drawing_id": drawing_id, **params},
                                  capability=capability,
                                  idempotency_key="xbench-x05-" + uuid4().hex)
        if status == 202 and isinstance(body, dict) and isinstance(body.get("job_id"), str):
            terminal = client.wait_job(body["job_id"], timeout_s=300)
            if not isinstance(terminal, dict) or terminal.get("status") != "complete":
                raise ObservationError(code="JOB_NOT_COMPLETE")
            if tool == "solar-solaredge-accept" and "error" in terminal:
                raise ObservationError(code="SAVED_STRINGS_UNREADABLE")
        elif status != 200:
            raise ObservationError(status, "MUTATION_UNREADABLE")
        if tool == "solar-solaredge-accept" and (
                not isinstance(body, dict) or "error" in body
                or (status == 200 and not isinstance(body.get("output"), dict))):
            raise ObservationError(code="SAVED_STRINGS_UNREADABLE")
        return context()

    try:
        org_id = created["org_id"]
        uploaded = client.upload_drawing("xbench-input.dwg", drawing)
        upload_state = client.wait_upload(uploaded, timeout_s=300)
        if not isinstance(upload_state, dict) or upload_state.get("status") != "ready":
            raise ObservationError(code="SOURCE_WIRE_UNREADABLE")
        adopted = client.adopt_drawing(project_id, uploaded, "xbench-input", version=1)
        drawing_id, version = adopted["drawing_id"], adopted["version_id"]
        current = context()
        checkout = client.checkout(project_id, drawing_id, ttl_s=900)
        if (not isinstance(checkout, tuple) or len(checkout) != 2
                or not isinstance(checkout[0], str) or not checkout[0]):
            raise ObservationError(code="CONTEXT_DRIFT")
        capability, _ = checkout
        current = mutate("solar-settings", {"expected_rev": 0, "initialize": {
            "schema_version": 1, "source_intake_sha256": current["context"]["intake_sha256"],
            "units": {"drawing_units": project["drawing_units"],
                      "wcs_to_ucs": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
                      "elevation_datum": "xbench", "crs": None}},
            "changes": {"panel_layer_contains": project["panel_layer_contains"]}})
        result["setup_verified"] = True

        def route():
            return f"/api/projects/{project_id}/drawings/{drawing_id}/versions/{version}/imports/solaredge-pdf"

        source = request("POST", route(), data=pdf, headers={"Content-Type": "application/pdf"},
                         timeout_s=300, product=True)
        if source.get("schema") != "leaf.solar-project-import-source.v1":
            raise ObservationError(code="SOURCE_WIRE_UNREADABLE")
        artifact_id = source["source"]["artifact_id"]
        if not isinstance(artifact_id, str) or not re.fullmatch(r"[0-9a-f]{64}", artifact_id):
            raise ObservationError(code="SOURCE_WIRE_UNREADABLE")
        result["import_verdict"] = "PASS"
        graph = current["intake"]["solar_design_graph"]
        current = mutate("solar-panels-from-drawing", {"expected_rev": graph["rev"]})
        graph = current["intake"]["solar_design_graph"]
        result["panel_count"] = len(graph["panels"])
        if result["panel_count"] != project["expected_modules"]:
            return
        current = mutate("solar-panel-groups", {"expected_rev": graph["rev"], "geometry_only": True,
            "discover": {"BranchMaxOffset": project["branch_max_offset"],
                         "AlignmentTolerance": project["alignment_tolerance"],
                         "InstallationDesign": project["installation_design"]}})
        graph = current["intake"]["solar_design_graph"]
        result["frame_refs"] = [f["id"] for f in graph["frames"]]
        result["frame_count"] = len(graph["frames"])
        if (not _integer(result["frame_count"], 1)
                or (project["expected_grids"] is not None
                    and result["frame_count"] != project["expected_grids"])):
            return
        # Source artifacts bind to their input version. Import again at the
        # current head before generating a report over the populated graph.
        result["phase"] = "report"
        source = request("POST", route(), data=pdf, headers={"Content-Type": "application/pdf"},
                         timeout_s=300, product=True)
        if source.get("schema") != "leaf.solar-project-import-source.v1":
            raise ObservationError(code="SOURCE_WIRE_UNREADABLE")
        artifact_id = source["source"]["artifact_id"]
        if not isinstance(artifact_id, str) or not re.fullmatch(r"[0-9a-f]{64}", artifact_id):
            raise ObservationError(code="SOURCE_WIRE_UNREADABLE")
        report = request("POST", route() + "/report", json_body={"source_artifact_id": artifact_id,
            "alignment_tolerance": project["alignment_tolerance"], "selection_order": "unknown"},
            timeout_s=300, product=True)
        if (not isinstance(report, dict) or not isinstance(report.get("report"), dict)
                or not isinstance(report["report"].get("download"), str)
                or not report["report"]["download"]):
            raise ObservationError(code="REPORT_WIRE_UNREADABLE")
        downloaded = request("GET", report["report"]["download"])
        if not isinstance(downloaded, dict) or downloaded.get("schema") != "leaf.solar-project-solaredge-report.v1":
            raise ObservationError(code="REPORT_WIRE_UNREADABLE")
        result["report"] = downloaded
        if result.get("oracle_readable") is True:
            result["phase"] = "accept"
            if _grid_score(project, result)[0] != "PASS":
                raise ObservationError(code="REPORT_WIRE_UNREADABLE")
            report_id = report["report"].get("artifact_id")
            if type(report_id) is not str or re.fullmatch(r"[0-9a-f]{64}", report_id) is None:
                raise ObservationError(code="REPORT_WIRE_UNREADABLE")
            current = context()
            rev = current["intake"]["solar_design_graph"]["rev"]
            if not _integer(rev):
                raise ObservationError(code="SAVED_STRINGS_UNREADABLE")
            params = {"expected_rev": rev, "report_artifact_id": report_id}
            if (project["expected_grids"] is None
                    and project.get("grid_count_policy") == "match_or_named"
                    and any("status" in row for row in downloaded["matches"])):
                params["accept_matched_only"] = True
            current = mutate("solar-solaredge-accept", params)
            graph = current["intake"]["solar_design_graph"]
            if not _integer(graph.get("rev")) or graph["rev"] <= rev:
                raise ObservationError(code="SAVED_STRINGS_UNREADABLE")
            result["accepted"] = True
            strings = graph.get("strings")
            if (not isinstance(strings, list) or any(
                    not isinstance(row, dict) or type(row.get("id")) is not str
                    or re.fullmatch(r"leaf:string:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",
                                    row["id"]) is None for row in strings)
                    or len({row["id"] for row in strings}) != len(strings)):
                raise ObservationError(code="SAVED_STRINGS_UNREADABLE")
            result.update(saved_head_read=True, saved_string_count=len(strings))
    finally:
        if capability is not None:
            try:
                client.release(project_id, drawing_id, capability)
            except Exception:
                result.setdefault("cleanup_errors", []).append("CHECKOUT_RELEASE_UNREADABLE")
        try:
            client.delete_project(project_id)
        except Exception:
            result.setdefault("cleanup_errors", []).append("PROJECT_DELETE_UNREADABLE")


def run(ctx):
    problem = _metadata_problem(ctx.manifest, ctx.fixtures)
    projects = ctx.manifest.get("projects", []) if isinstance(ctx.manifest, dict) else []
    if problem:
        steps = [Step(n, "UNREADABLE", problem) for n in range(1, 8)]
        write_json_atomic(Path(ctx.out_dir) / "x05-evidence.json", {
            "limits": LIMITS, "reason_code": problem, "results": [],
            "steps": [{"n": s.n, "verdict": s.verdict, "evidence": s.evidence} for s in steps],
        })
        return steps
    if isinstance(projects, list):
        projects = list(projects)
        configured = {p.get("drawing_fixture") for p in projects if isinstance(p, dict)}
        for fixture in ctx.manifest.get("fixtures", []):
            if fixture.get("role") == "drawing" and fixture.get("id") not in configured:
                projects.append({"id": "unconfigured:" + fixture["id"],
                                 "drawing_fixture": fixture["id"]})
    results = []
    health_started = False
    try:
        identifiers = Counter(p.get("id") for p in projects if isinstance(p, dict)) if isinstance(projects, list) else Counter()
        ready = {p["id"] for p in projects if _setup(p) and identifiers[p["id"]] == 1} if isinstance(projects, list) else set()
        if not ready:
            raise ObservationError(code="SETUP_UNREADABLE")
        health_started = True
        if read_health(ctx.base) != ctx.source_sha:
            raise ObservationError(code="SOURCE_DRIFT")
        # Deliberately lazy: the owner supplies the confirmed runtime stack.
        from xbench_studio import StudioClient
        from xbench_session import api, signed_in

        repo = Path(__file__).resolve().parents[3]
        with signed_in(repo, ctx.source_sha, origin=ctx.base) as (page, info):
            if info.get("web_build") not in (None, ctx.source_sha):
                raise ObservationError(code="SOURCE_DRIFT")

            def raw_transport(method, path, **kwargs):
                return api(page, method, path, origin=ctx.base, **kwargs)

            def transport(method, path, *, json_body=None, data=None, headers=None, timeout_s=60.0):
                status, response_headers, body = raw_transport(
                    method, path, json_body=json_body, data=data, headers=headers, timeout_s=timeout_s)
                is_json = "application/json" in response_headers.get("content-type", "").lower()
                payload = json.loads(body) if is_json else body
                return status, payload

            def request(method, path, product=False, **kwargs):
                status, payload = transport(method, path, **kwargs)
                if not 200 <= status < 300:
                    error = payload.get("error", {}) if isinstance(payload, dict) else {}
                    code = _reason_code(error.get("reason_code", error.get("code")))
                    proven = (product and status not in (401, 403, 408, 429, 503, 504)
                              and code in PRODUCT_REFUSAL_CODES)
                    raise ObservationError(status, code, proven)
                return payload

            # Client transport decodes JSON and preserves artifact bytes.
            client = StudioClient(transport)
            for project in projects if isinstance(projects, list) else []:
                result = {"id": project.get("id")} if isinstance(project, dict) else {}
                results.append(result)
                if not _setup(project) or project["id"] not in ready:
                    result["unreadable"] = True
                    continue
                try:
                    _project(ctx, client, request, project, result)
                except ObservationError as exc:
                    result["status"] = exc.status
                    result["reason_code"] = _reason_code(exc.code)
                    if exc.product and result.get("setup_verified") and "import_verdict" not in result:
                        result["import_verdict"] = "FAIL"
                    elif exc.product and result.get("phase") == "report":
                        result["report_product_failure"] = True
                    else:
                        result["reason"] = "operation_unreadable"
                except Exception:
                    result["reason"] = "runtime_contract_unreadable"
    except Exception:
        # Exception strings can include credentials, paths and response bodies.
        for result in results:
            result.update(unreadable=True, reason="session_contract_unreadable")
    finally:
        if health_started:
            try:
                stable = read_health(ctx.base) == ctx.source_sha
            except Exception:
                stable = False
            if not stable:
                for result in results:
                    result.update(unreadable=True, reason="source_identity_unreadable")
    steps = score(projects, results)
    # Persist only numerical diagnostics and fixed scorer reasons, never raw
    # graphs, project identities, checkout capabilities or response envelopes.
    write_json_atomic(Path(ctx.out_dir) / "x05-evidence.json", {
        "limits": LIMITS, "projects_expected": len(projects) if isinstance(projects, list) else 0,
        "results": [{k: r[k] for k in ("status", "reason_code", "reason", "panel_count", "frame_count",
                                       "import_verdict", "report_product_failure", "cleanup_errors",
                                       "oracle_readable", "accepted", "saved_head_read",
                                       "saved_string_count") if k in r} for r in results],
        "steps": [{"n": s.n, "verdict": s.verdict, "evidence": s.evidence,
                   "observed": s.observed} for s in steps]})
    return steps
