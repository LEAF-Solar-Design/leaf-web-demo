"""Seeded failure causes and drawing-state preservation for X02."""

from dataclasses import asdict, dataclass
from datetime import datetime
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import quote

from xbench_core import Step, utc_now, write_json_atomic
import xbench_session
from xbench_studio import StudioClient, StudioError, disposable_project, new_idempotency_key, parse_error


LIMITS = "X02: cases 3, 4a, 4b, 7, 8, 9 and case 10 engine provenance are bound; cases 1, 2, 5, 6 and the UI text check (step 6) are not graded yet and score UNREADABLE."
CASE_IDS = ("1", "2", "3", "4a", "4b", "5", "6", "7", "8", "9", "10")
BOUND_CODES = {
    "1": "SOLAREDGE_GRID_UNMATCHED", "2": "SOLAREDGE_GRID_AMBIGUOUS",
    "3": "LAYER_NOT_FOUND", "4a": "PANEL_LAYER_FILTER_REQUIRED",
    "4b": "NO_PANELS_RECOGNISED", "5": "CLOUD_REQUEST_REJECTED",
    "6": ("SOLVE_TIMEOUT", "solve_outcome_unknown"), "7": "INVALID_SEED_REQUEST",
    "8": "SIP_R1_CHECKOUT_EXPIRED", "9": "IMPORT_PDF_MALFORMED",
}
OBJECTS = {"3": "XBENCH_ABSENT_LAYER", "4a": "panel_layer_contains",
           "4b": "XBENCH_NO_MATCH", "7": "drawing_units", "8": "checkout", "9": "PDF"}
UNSEEDED = {"1": "not seeded here", "2": "not seeded here",
            "5": "not seeded: no staging fault hook", "6": "not seeded: no staging fault hook",
            "10": "not reached: solar-add-all-inverters"}
INVERTER_TOOL = "solar-add-all-inverters"
PDF = b"%PDF-1.7\n%xbench truncated\n1 0 obj\n<<"


@dataclass
class CaseResult:
    case: str
    seeded: bool = False
    status: int | None = None
    code: str | None = None
    message: str = ""
    next_action: str | None = None
    object_named: bool | None = None
    ok_body: bool = False
    async_refusal: bool = False
    state_before: tuple | None = None
    state_after: tuple | None = None
    note: str = ""
    provenance: dict | None = None


def _transport(page, *, origin=xbench_session.STAGING_ORIGIN):
    def request(method, path, **kw):
        status, headers, body = xbench_session.api(page, method, path, origin=origin, **kw)
        content_type = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
        return status, json.loads(body) if "json" in content_type.lower() else body
    return request


def run_cases(client, transport, *, project, drawing, source_sha="unknown", sleep=time.sleep, clock=time.time):
    pid, did = project["project_id"], drawing["drawing_id"]
    version = drawing["version_id"]
    rev = 0
    secrets = []
    results = {case: CaseResult(case, note=UNSEEDED.get(case, "not reached")) for case in CASE_IDS}

    def safe(value):
        if value is None:
            return None
        value = str(value)
        for secret in secrets:
            value = value.replace(secret, "[redacted]")
        return value

    def context():
        return client.drawing_context(pid, version, did)

    def state():
        try:
            ctx = context()["context"]
            head, digest = ctx.get("head_version_id"), ctx.get("intake_sha256")
            return (head, digest) if head is not None and digest is not None else None
        except Exception:
            return None

    def refresh():
        nonlocal version, rev
        version = context()["context"]["head_version_id"]
        graph = context().get("intake", {}).get("solar_design_graph", {})
        rev = graph.get("rev", rev)

    def finish(response, *, write=False, identity=None):
        status, body = response
        if identity is not None and isinstance(body, dict) and isinstance(body.get("error"), dict):
            raise parse_error(status, body)
        if identity is not None and 200 <= status < 300:
            if status != 202 or not isinstance(body, dict) or not isinstance(body.get("job_id"), str) or not body["job_id"]:
                raise ValueError("job_id")
            identity["job_id"] = safe(body["job_id"])
        if status == 202 and isinstance(body, dict) and body.get("job_id"):
            try:
                body = client.wait_job(body["job_id"], timeout_s=600)
            except (AttributeError, KeyError, TypeError) as exc:
                if identity is not None:
                    raise ValueError("job.status") from exc
                raise
            if identity is not None and (not isinstance(body, dict) or body.get("status") not in ("complete", "failed")):
                raise ValueError("job.status")
            if identity is not None and isinstance(body.get("error"), dict):
                raise parse_error(status, body)
            if body.get("status") == "failed":
                error = parse_error(status, body)
                error.async_refusal = True
                raise error
        if not 200 <= status < 300:
            raise parse_error(status, body)
        if write:
            refresh()
        return status, body

    def call(tool, params, *, cap=None, identity=None):
        return finish(client.run(tool, org_id=project["org_id"], project_id=pid,
                                 version_id=version, params=params, capability=cap,
                                 idempotency_key=new_idempotency_key("x02")),
                      write=tool != "count-by-layer", identity=identity)

    def capture(case, action, *, prepare=None):
        result = CaseResult(case, seeded=True, state_before=state())
        try:
            if prepare is not None:
                prepare()
                result.state_before = state()
            result.status, body = action()
            result.ok_body = isinstance(body, dict) and (body.get("ok") is True or
                                                       body.get("status") == "complete")
            error = parse_error(result.status, body)
        except StudioError as exc:
            error = exc
            result.status = exc.status
            result.async_refusal = getattr(exc, "async_refusal", False)
            result.ok_body = isinstance(exc.body, dict) and exc.body.get("ok") is True
        except Exception as exc:
            error = None
            result.note = safe(f"{type(exc).__name__}: {exc}")
        finally:
            result.state_after = state()
        if error is not None:
            result.code = safe(error.reason_code or error.error_code)
            result.message = safe(error.message)
            result.next_action = safe(error.next_action)
        result.object_named = OBJECTS[case].lower() in (
            result.message + " " + (result.next_action or "")).lower()
        results[case] = result

    def seed(units):
        return {"schema_version": 1, "source_intake_sha256": context()["context"]["intake_sha256"],
                "units": {"drawing_units": units,
                          "wcs_to_ucs": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
                          "elevation_datum": "xbench", "crs": None}}

    def settings(changes, initialize=None):
        params = {"drawing_id": did, "expected_rev": rev, "changes": changes}
        if initialize is not None:
            params["initialize"] = initialize
        return params

    def setup(changes, initialize=None):
        call("solar-settings", settings(changes, initialize), cap=capability)

    def unavailable(cases, exc):
        note = safe(f"setup failed: {type(exc).__name__}: {exc}")
        for case in cases:
            results[case] = CaseResult(case, note=note)

    def engine_case():
        row = CaseResult("10")
        results["10"] = row
        try:
            catalog = client.tools()
            if not isinstance(catalog, list) or any(not isinstance(tool, dict) for tool in catalog):
                raise ValueError("tools")
            tool = next((tool for tool in catalog if tool.get("name") == INVERTER_TOOL), None)
            if tool is None:
                row.note = f"{INVERTER_TOOL} not live on {source_sha}"
                return
            proof = row.provenance = {}
            for field in ("version", "catalog_digest"):
                if not isinstance(tool.get(field), str) or not tool[field].strip():
                    raise ValueError(f"tool.{field}")
                proof["tool_version" if field == "version" else field] = safe(tool[field])
            call("solar-settings", {**settings({"use_l2_collectors": True,
                 "panel_layer_contains": "Panels", "panels_in_sequence": 2}),
                 "project_changes": {"name": "X02 rooftop demo", "zip_code": "44224"}}, cap=capability)
            call("solar-panels-from-drawing", {"drawing_id": did, "expected_rev": rev}, cap=capability)
            intake = context()["intake"]
            graph = intake["solar_design_graph"]
            panels = graph.get("panels")
            if not isinstance(panels, list) or not panels:
                raise ValueError("graph.panels")
            outlines = {poly["handle"].upper(): poly["pts"] for poly in intake.get("polylines", [])
                        if isinstance(poly, dict) and isinstance(poly.get("handle"), str)}
            memberships = {}
            for panel in panels:
                memberships.setdefault(round(panel["angle"] % 180, 1), []).append(panel)
            groups = []
            for index, members in enumerate(memberships.values()):
                points = outlines.get(members[0]["provenance"]["source_handle"].upper())
                if not isinstance(points, list) or len(points) < 4:
                    raise ValueError("intake.polylines.pts")
                sides = [math.dist(points[i][:2], points[i + 1][:2]) for i in (0, 1)]
                groups.append({"name": f"X02 fixture group {index + 1}",
                    "panel_refs": [panel["id"] for panel in members], "alignment_tolerance": .5,
                    "module_width_along_row": max(sides), "module_height_across_row": min(sides)})
            # Manual length and geometry-only groups use public handlers and do not
            # invent cloud sizing evidence or require a private grant for this fixture.
            call("solar-size-strings", {"drawing_id": did, "expected_rev": rev,
                                      "mode": "manual-global", "confirm": True}, cap=capability)
            call("solar-panel-groups", {"drawing_id": did, "expected_rev": rev,
                                      "groups": groups, "geometry_only": True}, cap=capability)
            frames = context()["intake"]["solar_design_graph"].get("frames")
            if not isinstance(frames, list) or not frames:
                raise ValueError("graph.frames")
            refs = max(frames, key=lambda frame: len(frame["panel_refs"]))["panel_refs"][:2]
            if len(refs) != 2:
                raise ValueError("graph.frames.panel_refs")
            call("solar-string-add", {"drawing_id": did, "expected_rev": rev,
                 "operation": "add-string", "ordered_panel_refs": refs}, cap=capability)
            parent = version
            row.status, body = call(INVERTER_TOOL, {"drawing_id": did, "expected_rev": rev,
                "placement_strategy": "deterministic_grid", "hardware": {
                    "model": "Central 100", "max_dc_voltage": 1500, "max_ac_power_kw": 100,
                    "mppt_count": 1, "total_dc_inputs": 1, "collector_capacity": 4}},
                cap=capability, identity=proof)
            result = body.get("result")
            if not isinstance(result, dict):
                raise ValueError("result")
            commit = result.get("graph_commit")
            if not isinstance(commit, dict):
                raise ValueError("result.graph_commit")
            # Project only the contract fields; never retain echoed request/grant/auth data.
            proof["malformed_fields"] = []
            for field in ("engine", "engine_backend", "engine_label", "placement_strategy",
                          "request_sha256", "graph_sha256", "parent_version_id", "output_version_id"):
                value = commit.get(field)
                proof[field] = safe(value) if isinstance(value, str) else None
                if field in commit and not isinstance(value, str):
                    proof["malformed_fields"].append(field)
            attempt = commit.get("producing_attempt")
            proof["producing_attempt"] = ({
                "job_id": safe(attempt.get("job_id")) if isinstance(attempt.get("job_id"), str) else None,
                "attempt": attempt.get("attempt") if type(attempt.get("attempt")) is int else None,
            } if isinstance(attempt, dict) else None)
            proof["input_version_id"] = safe(parent)
            proof["head_version_id"] = safe(version)
            row.seeded = True
        except StudioError as exc:
            row.status = exc.status
            row.code = safe(exc.reason_code or exc.error_code)
            row.note = f"refusal: {row.code or 'reason_code unavailable'}"
        except (KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
            row.note = safe(f"malformed {exc}")
        except Exception as exc:
            row.note = f"case 10 unavailable: {type(exc).__name__}"

    capability = drawing.get("checkout_capability")
    if capability:
        secrets.append(capability)
    capture("3", lambda: call("count-by-layer", {"drawing_id": did, "layer": OBJECTS["3"]}))
    try:
        if capability is None:
            capability, _ = client.checkout(pid, did, ttl_s=900)
            secrets.append(capability)
        capture("7", lambda: call("solar-settings", settings({"panels_in_sequence": 10}, seed("unknown")),
                                  cap=capability))
        try:
            setup({"panel_layer_contains": OBJECTS["4b"]}, seed("in"))
        except Exception as exc:
            unavailable(("4a", "4b", "9", "8", "10"), exc)
            return [results[case] for case in CASE_IDS]
        capture("4b", lambda: call("solar-panels-from-drawing",
                                   {"drawing_id": did, "expected_rev": rev}, cap=capability))
        capture("4a", lambda: call("solar-panels-from-drawing",
                                   {"drawing_id": did, "expected_rev": rev}, cap=capability),
                prepare=lambda: setup({"panel_layer_contains": ""}))
        path = "/api/projects/{}/drawings/{}/versions/{}/imports/solaredge-pdf".format(
            *(quote(str(value), safe="") for value in (pid, did, version)))
        capture("9", lambda: finish(transport("POST", path, headers={"Content-Type": "application/pdf"}, data=PDF),
                                    write=True))
        engine_case()
        try:
            client.release(pid, did, capability)
            capability = None
            capability, checkout = client.checkout(pid, did, ttl_s=2)
            secrets.append(capability)
            expires = checkout["expires"]
            deadline = (float(expires) if isinstance(expires, (int, float)) else
                        datetime.fromisoformat(expires.replace("Z", "+00:00")).timestamp()) + 4
            while clock() < deadline:
                sleep(min(deadline - clock(), 60))
            capture("8", lambda: call("solar-settings", settings({"panels_in_sequence": 11}), cap=capability))
        except Exception as exc:
            unavailable(("8",), exc)
    except Exception as exc:
        unavailable(tuple(case for case in OBJECTS if not results[case].seeded), exc)
    finally:
        if capability is not None:
            try:
                client.release(pid, did, capability)
            except Exception:
                pass
    return [results[case] for case in CASE_IDS]


def score(results):
    rows = {result.case: result for result in results}
    missing = any(case not in rows or not rows[case].seeded for case in BOUND_CODES)
    seeded = [row for row in results if row.seeded and row.case in BOUND_CODES]
    steps = []
    for n in (1, 2, 3):
        evidence, failed, unreadable = [], False, missing
        for case in BOUND_CODES:
            row = rows.get(case)
            if row is None or not row.seeded:
                note = (row.note if row else "missing")[:140]
                evidence.append(f"case {case}: UNREADABLE ({note})")
                continue
            reasons = []
            if n == 1:
                bound = BOUND_CODES[case]
                if row.code not in (bound if isinstance(bound, tuple) else (bound,)):
                    reasons.append(f"code {row.code!r}, expected {bound}")
                if not row.next_action or not row.next_action.strip():
                    reasons.append("empty next_action")
                if row.object_named is False:
                    reasons.append("object not named")
            elif n == 2:
                if row.status is not None and 200 <= row.status < 300 and not row.async_refusal:
                    reasons.append(f"2xx status {row.status}")
                if row.ok_body:
                    reasons.append("ok:true")
            else:
                if row.state_before != row.state_after:
                    reasons.append("drawing state changed")
                if row.state_before is None or row.state_after is None:
                    unreadable = True
            failed |= bool(reasons)
            verdict = "FAIL" if reasons else ("UNREADABLE" if n == 3 and
                       (row.state_before is None or row.state_after is None) else "PASS")
            reason = ("; ".join(reasons) or
                      ("state unavailable" if verdict == "UNREADABLE" else "checks satisfied"))[:140]
            evidence.append(f"case {case}: {verdict} ({reason})")
        steps.append(Step(n, "FAIL" if failed else "UNREADABLE" if unreadable else "PASS",
                          "; ".join(evidence)[:1999],
                          observed={row.case: row.code for row in seeded} if n == 1 else None,
                          expected=BOUND_CODES if n == 1 else None))
    steps.extend([_engine_step(rows.get("10")),
                  Step(5, "UNREADABLE", "case 6: UNREADABLE; unknown-submission retry reconciliation not seeded yet"),
                  Step(6, "UNREADABLE", "all cases: UNREADABLE; UI text check not built yet")])
    return steps


def _engine_step(row):
    proof = row.provenance if row and isinstance(row.provenance, dict) else {}
    failed, malformed = [], []
    if row and row.seeded:
        for field, expected in (("engine", "deterministic_fallback"),
                                ("engine_backend", "local_python"),
                                ("engine_label", "Deterministic fallback"),
                                ("placement_strategy", "deterministic_grid")):
            value = proof.get(field)
            if field in proof.get("malformed_fields", []):
                malformed.append(field)
            elif (value is None and field != "placement_strategy") or (isinstance(value, str) and value != expected):
                failed.append(field)
            elif not isinstance(value, str):
                malformed.append(field)
        for field in ("tool_version", "catalog_digest", "job_id", "parent_version_id", "output_version_id"):
            if not isinstance(proof.get(field), str) or not proof[field].strip():
                malformed.append(field)
        for field in ("request_sha256", "graph_sha256"):
            if not isinstance(proof.get(field), str) or not re.fullmatch(r"[0-9a-fA-F]{64}", proof[field]):
                malformed.append(field)
        attempt = proof.get("producing_attempt")
        if not isinstance(attempt, dict):
            malformed.append("producing_attempt")
        else:
            if not isinstance(attempt.get("job_id"), str) or not attempt["job_id"]:
                malformed.append("producing_attempt.job_id")
            elif attempt["job_id"] != proof.get("job_id"):
                failed.append("producing_attempt.job_id")
            if type(attempt.get("attempt")) is not int or attempt["attempt"] < 1:
                malformed.append("producing_attempt.attempt")
        for field, bound in (("parent_version_id", "input_version_id"), ("output_version_id", "head_version_id")):
            if field not in malformed and proof.get(field) != proof.get(bound):
                failed.append(field)
    verdict = "FAIL" if failed else "UNREADABLE" if malformed or not row or not row.seeded else "PASS"
    if failed:
        reason = "invalid " + ", ".join(failed)
    elif malformed:
        reason = "malformed " + ", ".join(malformed)
    else:
        reason = row.note if row and not row.seeded else "engine provenance checked" if row else "missing"
    return Step(4, verdict, f"case 10: {verdict} ({reason}); " + json.dumps(proof, sort_keys=True), observed=proof)


def run(ctx):
    fixture = next((row for row in ctx.fixtures if row["id"] == "rooftop-demo"), None)
    if fixture is None:
        fixture = next(row for row in ctx.manifest["fixtures"] if row["id"] == "rooftop-demo")
    repo = Path(__file__).resolve().parents[3]
    with xbench_session.signed_in(repo, ctx.source_sha, origin=ctx.base) as (page, _):
        transport = _transport(page, origin=ctx.base)
        client = StudioClient(transport)
        with disposable_project(client, f"XBench X02 {ctx.source_sha[:8]} {utc_now()}") as project:
            uploaded = client.upload_drawing("rooftop_demo.dwg", Path(fixture["path"]).read_bytes())
            client.wait_upload(uploaded)
            drawing = client.adopt_drawing(project["project_id"], uploaded, "rooftop_demo.dwg")
            capability, _ = client.checkout(project["project_id"], drawing["drawing_id"], ttl_s=900)
            try:
                results = run_cases(client, transport, project=project,
                                    drawing={**drawing, "checkout_capability": capability}, source_sha=ctx.source_sha)
            finally:
                try:
                    client.release(project["project_id"], drawing["drawing_id"], capability)
                except Exception:
                    pass
            write_json_atomic(Path(ctx.out_dir) / "cases.json", [asdict(row) for row in results])
            return score(results)
