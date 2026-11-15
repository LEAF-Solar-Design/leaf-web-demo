"""Seeded failure causes and drawing-state preservation for X02."""

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path
import time
from urllib.parse import quote

from xbench_core import Step, utc_now, write_json_atomic
import xbench_session
from xbench_studio import StudioClient, StudioError, disposable_project, new_idempotency_key, parse_error


LIMITS = "X02 slice 1: cases 3, 4a, 4b, 7, 8, 9 seeded live; cases 1, 2, 5, 6, 10 and the UI text check (step 6) are not graded yet and score UNREADABLE."
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
            "10": "not seeded here: deterministic_fallback run record"}
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
    state_before: tuple | None = None
    state_after: tuple | None = None
    note: str = ""


def _transport(page, *, origin=xbench_session.STAGING_ORIGIN):
    def request(method, path, **kw):
        status, headers, body = xbench_session.api(page, method, path, origin=origin, **kw)
        content_type = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
        return status, json.loads(body) if "json" in content_type.lower() else body
    return request


def run_cases(client, transport, *, project, drawing, sleep=time.sleep, clock=time.time):
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

    def finish(response):
        status, body = response
        if isinstance(body, dict) and body.get("job_id"):
            body = client.wait_job(body["job_id"])
            if body.get("status") == "failed":
                raise parse_error(422, body)
        if not 200 <= status < 300:
            raise parse_error(status, body)
        return status, body

    def call(tool, params, *, cap=None):
        return finish(client.run(tool, org_id=project["org_id"], project_id=pid,
                                 version_id=version, params=params, capability=cap,
                                 idempotency_key=new_idempotency_key("x02")))

    def capture(case, action):
        result = CaseResult(case, seeded=True, state_before=state())
        try:
            result.status, body = action()
            result.ok_body = isinstance(body, dict) and body.get("ok") is True
            error = parse_error(result.status, body)
        except StudioError as exc:
            error = exc
            result.status = exc.status
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
        nonlocal version, rev
        _, body = call("solar-settings", settings(changes, initialize), cap=capability)
        output = body.get("result", body)
        version = output["output_version_id"]
        rev += 1
        graph = context().get("intake", {}).get("solar_design_graph", {})
        rev = graph.get("rev", rev)

    def unavailable(cases, exc):
        note = safe(f"setup failed: {type(exc).__name__}: {exc}")
        for case in cases:
            results[case] = CaseResult(case, note=note)

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
            setup({"panels_in_sequence": 10}, seed("in"))
        except Exception as exc:
            unavailable(("4a", "4b", "9", "8"), exc)
            return [results[case] for case in CASE_IDS]
        capture("4a", lambda: call("solar-panels-from-drawing",
                                   {"drawing_id": did, "expected_rev": rev}, cap=capability))
        try:
            setup({"panel_layer_contains": OBJECTS["4b"]})
        except Exception as exc:
            unavailable(("4b", "9", "8"), exc)
            return [results[case] for case in CASE_IDS]
        capture("4b", lambda: call("solar-panels-from-drawing",
                                   {"drawing_id": did, "expected_rev": rev}, cap=capability))
        path = "/api/projects/{}/drawings/{}/versions/{}/imports/solaredge-pdf".format(
            *(quote(str(value), safe="") for value in (pid, did, version)))
        capture("9", lambda: finish(transport("POST", path, headers={"Content-Type": "application/pdf"}, data=PDF)))
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
                if row.status is not None and 200 <= row.status < 300:
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
    steps.extend([Step(4, "UNREADABLE", "case 10: UNREADABLE; engine provenance and deterministic_fallback not seeded yet"),
                  Step(5, "UNREADABLE", "case 6: UNREADABLE; unknown-submission retry reconciliation not seeded yet"),
                  Step(6, "UNREADABLE", "all cases: UNREADABLE; UI text check not built yet")])
    return steps


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
                                    drawing={**drawing, "checkout_capability": capability})
            finally:
                try:
                    client.release(project["project_id"], drawing["drawing_id"], capability)
                except Exception:
                    pass
            write_json_atomic(Path(ctx.out_dir) / "cases.json", [asdict(row) for row in results])
            return score(results)
