"""X03 normal-route natural-language authoring benchmark, one prompt at a time."""
from contextlib import ExitStack, contextmanager
from collections import Counter
import csv
import hashlib
import json
import math
from pathlib import Path
import re
import time
from uuid import uuid4

from xbench_core import Step, write_json_atomic, utc_now
import xbench_session


LIMITS = "50 frozen prompts; sequential execution (one in flight); 600 seconds per prompt; staging only; ordinary supported agent sign-in; no template credit."
TRADES = {"solar": 30, "structural": 10, "mechanical": 10}


def content_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def matches(actual, expected, rule):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and actual.keys() == expected.keys() and all(
            matches(actual[key], value, rule) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(
            matches(a, b, rule) for a, b in zip(actual, expected))
    if type(expected) in (int, float):
        if type(actual) not in (int, float) or not math.isfinite(actual):
            return False
        return (abs(actual - expected) <= rule["tolerance"]
                if rule["mode"] == "absolute" else actual == expected)
    return type(actual) is type(expected) and actual == expected


def validate_prompt_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"X03-(?:0[1-9]|[1-4][0-9]|50)", value):
        raise ValueError("invalid frozen prompt ID")


def load_prompts(manifest):
    fixture = next(f for f in manifest["fixtures"] if f["id"] == "x03-prompts")
    raw = Path(fixture["path"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != fixture.get("sha256"):
        raise ValueError("frozen prompt SHA-256 mismatch")
    payload = json.loads(raw.decode("utf-8-sig"))
    rows = payload["rows"]
    if payload.get("schema") != "leaf.xbench.x03-prompts.v1" or not isinstance(rows, list) or len(rows) != 50:
        raise ValueError("invalid frozen prompt file")
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid frozen prompt row")
        validate_prompt_id(row.get("id"))
    if len({r["id"] for r in rows}) != 50 or Counter(r["trade"] for r in rows) != Counter(TRADES):
        raise ValueError("invalid prompt IDs or trade split")
    drawings = {f["id"] for f in manifest["fixtures"] if f.get("role") == "drawing"}
    for row in rows:
        if row["drawing"] not in drawings or not row.get("prompt") or not row.get("oracle_method"):
            raise ValueError("unbound prompt")
        compare = row["compare"]
        if compare.get("mode") not in ("exact", "absolute"):
            raise ValueError("invalid comparison mode")
        if compare["mode"] == "absolute" and (type(compare.get("tolerance")) not in (int, float)
                or not math.isfinite(compare["tolerance"]) or compare["tolerance"] < 0):
            raise ValueError("invalid tolerance")
    review = manifest.get("oracle_review", {})
    if (not review.get("evidence") or len(set(review.get("row_ids", []))) < 10
            or not set(review["row_ids"]).issubset({r["id"] for r in rows})):
        raise ValueError("ten hand-reviewed oracle rows are required before scoring")
    evidence = next((f for f in manifest["fixtures"] if f["path"] == review["evidence"]), None)
    if evidence is None:
        raise ValueError("oracle review evidence must be hash-pinned")
    return rows, fixture["sha256"]


def transport_for(page, base):
    def request(method, path, **kwargs):
        status, headers, body = xbench_session.api(page, method, path, origin=base, **kwargs)
        return status, json.loads(body) if "json" in headers.get("content-type", "") else body
    return request


def find_model(body):
    if not isinstance(body, dict):
        return None
    for key in ("model_id", "model", "author_model"):
        if isinstance(body.get(key), str) and body[key]:
            return body[key]
    for key in ("provenance", "authoring", "tool", "receipt", "result"):
        value = find_model(body.get(key))
        if value:
            return value
    return None


class PromptFailure(RuntimeError):
    pass


@contextmanager
def author_authority(transport, row, project, drawing, evidence_dir, *, deadline=None):
    """Use AuthorPanel's ordinary app-owned active-turn setup, never a grant."""
    session_id = turn_id = None
    events = []
    def request(path, payload):
        remaining = 60 if deadline is None else deadline - time.monotonic()
        if remaining <= 0:
            raise PromptFailure("per-prompt 600 second ceiling reached during author setup")
        status, body = transport("POST", path, json_body=payload, timeout_s=min(60, remaining))
        events.append({"path": path, "status": status,
                       "reason_code": body.get("reason_code") if isinstance(body, dict) else None})
        if not 200 <= status < 300:
            raise PromptFailure("normal author context refused: HTTP " + str(status))
        return body
    try:
        session = request("/api/sessions", {"drawing_id": drawing["drawing_id"],
                                           "project_id": project["project_id"]})
        session_id = session.get("session_id")
        if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
            raise PromptFailure("invalid author session identity")
        started = request(f"/api/sessions/{session_id}/messages", {
            "text": row["prompt"], "classifier_hint": {"source": "author_panel", "purpose": "stage_authority"},
            "request_id": str(uuid4()), "project_context": {
                "project_id": project["project_id"], "input_version_id": drawing["version_id"]}})
        turn_id = started.get("turn_id")
        if (not isinstance(turn_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", turn_id)
                or started.get("status") in ("queued", "completed")):
            turn_id = None
            raise PromptFailure("normal author context did not start an active turn")
        yield {"X-Authority-Session-Id": session_id, "X-Authority-Turn-Id": turn_id}
    finally:
        if session_id and turn_id:
            path = f"/api/sessions/{session_id}/turns/{turn_id}/cancel"
            try:
                status, _ = transport("POST", path, json_body={}, timeout_s=30)
                events.append({"path": path, "status": status, "owned_turn_cleanup": True})
            except Exception as exc:
                events.append({"path": path, "cleanup_error": type(exc).__name__})
        write_json_atomic(evidence_dir / (row["id"] + "-authority.json"), events)


def evaluate_prompt(row, *, transport, client, project, drawing, source_sha,
                    evidence_dir, run_id, clock=time.monotonic, sleep=time.sleep,
                    authority_headers=None, deadline_at=None):
    validate_prompt_id(row.get("id"))
    record = {"id": row["id"], "trade": row["trade"], "drawing": row["drawing"],
              "source_sha": source_sha, "model_id": None, "tool_name": None,
              "version": None, "tool_source_hash": None, "output_hash": None,
              "score": 0, "failing_stage": "author", "author_submitted": False,
              "started_at": utc_now()}
    deadline = clock() + 600 if deadline_at is None else deadline_at
    trace = []

    def remaining():
        left = deadline - clock()
        if left <= 0:
            raise PromptFailure("per-prompt 600 second ceiling reached")
        return left

    def call(method, path, **kwargs):
        timeout = kwargs.pop("timeout_s", 60)
        status, body = transport(method, path, timeout_s=min(remaining(), timeout), **kwargs)
        # Store route and bounded public error codes; never headers or raw auth responses.
        trace.append({"method": method, "path": path, "status": status})
        if not 200 <= status < 300:
            if status == 401 or path == "/api/health":
                record["unreadable"] = True
            error = body.get("error", {}) if isinstance(body, dict) else {}
            record["error_code"] = (error.get("reason_code") or error.get("error_code")
                                    or body.get("reason_code")) if isinstance(body, dict) and isinstance(error, dict) else None
            raise PromptFailure(f"HTTP {status}, code {record.get('error_code')}")
        return status, body

    try:
        # Health at each prompt binds observations to the actual release being evaluated.
        _, health = call("GET", "/api/health")
        if health.get("source_sha") != source_sha:
            record["failing_stage"] = "source_drift"
            raise PromptFailure("staging source changed")
        target = f"x03-{run_id}-{row['id'].lower()}"
        record["author_submitted"] = True
        _, staged = call("POST", "/api/author", json_body={
            "description": row["prompt"], "mode": "build", "target_tool_name": target},
            headers={"Idempotency-Key": target, **(authority_headers or {})}, timeout_s=remaining())
        change_id = staged.get("change_set_id") if staged.get("contract") == "leaf.customization-stage-job.v1" else None
        while staged.get("contract") == "leaf.customization-stage-job.v1":
            if (not isinstance(change_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", change_id)
                    or staged.get("change_set_id") != change_id):
                raise PromptFailure("invalid stage change-set identity")
            poll = staged.get("poll_url") or "/api/author/stages/" + change_id
            if poll != "/api/author/stages/" + change_id:
                raise PromptFailure("invalid stage polling address")
            sleep(min(2, remaining()))
            _, staged = call("GET", poll)
            if staged.get("status") in ("failed", "cancelled"):
                record["error_code"] = staged.get("error", {}).get("reason_code") if isinstance(staged.get("error"), dict) else None
                raise PromptFailure("author stage failed")
        record["model_id"] = find_model(staged)
        receipt = staged.get("receipt", {})
        if change_id is not None and receipt.get("change_set_id") != change_id:
            raise PromptFailure("staged receipt belongs to another change set")
        tool = staged.get("tool")
        preview = str(staged.get("preview", "")).lower()
        if (not isinstance(tool, dict) or not tool.get("name")
                or receipt.get("state") != "staged" or not receipt.get("staged_commit")
                or "template" in str(staged.get("source", "")).lower()
                or "templated" in preview or "template fallback" in preview):
            raise PromptFailure("no generated tool with a staged source receipt")
        record.update(tool_name=tool["name"], version=tool.get("version") or receipt["staged_commit"],
                      tool_source_hash=receipt["staged_commit"])
        record["failing_stage"] = "publication"
        _, publication = call("POST", "/api/author/publication-requests",
                              json_body={"change_set_id": receipt["change_set_id"]})
        if publication.get("status") != "published":
            record["error_code"] = publication.get("status")
            raise PromptFailure("normal publication rail did not publish")
        _, catalog = call("GET", "/api/tools")
        catalog_row = next((item for item in catalog.get("tools", []) if item.get("name") == tool["name"]), None)
        if not catalog_row or not catalog_row.get("catalog_digest"):
            raise PromptFailure("published tool missing from effective catalog")
        # Compare identities that the actual producer reports. Per-tool catalog_digest
        # is a different object from the receipt's whole-catalog digest.
        conflict = any(key in tool and key in catalog_row and tool[key] != catalog_row[key]
                       for key in ("version", "source_commit", "source_sha256", "entry", "entrypoint", "code"))
        conflict |= any(key in catalog_row and catalog_row[key] != receipt.get(receipt_key)
                        for key, receipt_key in (("catalog_commit", "staged_commit"),
                                                 ("effective_catalog_digest", "catalog_digest")))
        if conflict:
            record["error_code"] = "CATALOG_PROVENANCE_MISMATCH"
            raise PromptFailure("catalog tool conflicts with staged source")
        record["failing_stage"] = "execution"
        status, result = call("POST", "/api/run", headers={"X-Org-Id": project["org_id"],
            "X-Project-Id": project["project_id"], "Idempotency-Key": target + "-run"},
            json_body={"tool": tool["name"], "catalog_digest": catalog_row["catalog_digest"],
                       "dwg": drawing["version_id"], "params": {}})
        if status == 202:
            job_path = "/api/jobs/" + result["job_id"]
            while result.get("status") not in ("complete", "failed", "cancelled"):
                sleep(min(2, remaining()))
                _, result = call("GET", job_path)
        if result.get("status") == "cancelled":
            record["error_code"] = "JOB_CANCELLED"
            raise PromptFailure("tool execution cancelled")
        if result.get("status") == "failed" or result.get("ok") is False:
            raise PromptFailure("tool execution failed")
        output = result.get("result", result.get("output", result))
        record["output_hash"] = content_hash(output)
        record["failing_stage"] = "comparison"
        write_json_atomic(evidence_dir / (row["id"] + "-output.json"), output)
        if not matches(output, row["oracle"], row["compare"]):
            raise PromptFailure("output differs from frozen oracle")
        record["failing_stage"] = "model_provenance"
        if not record["model_id"]:
            raise PromptFailure("author model identity was not reported")
        _, health = call("GET", "/api/health")
        if health.get("source_sha") != source_sha:
            record["failing_stage"] = "source_drift"
            raise PromptFailure("staging source changed during prompt")
        remaining()
        record.update(score=1, failing_stage=None)
    except Exception as exc:
        # Error messages may echo provider configuration, so retain type and our own bounded diagnostics only.
        record["failure"] = str(exc) if isinstance(exc, PromptFailure) else type(exc).__name__
        if not isinstance(exc, PromptFailure):
            record["unreadable"] = True
    record["finished_at"] = utc_now()
    write_json_atomic(evidence_dir / (row["id"] + ".json"), {"record": record, "routes": trace})
    return record


def rates(records):
    def rate(rows):
        return sum(r["score"] for r in rows) / len(rows) if rows else None
    return {"overall": rate(records),
            "trades": {trade: rate([r for r in records if r["trade"] == trade]) for trade in TRADES},
            "models": {model: {
                "overall": rate([r for r in records if (r.get("model_id") or "unreported") == model]),
                "trades": {trade: rate([r for r in records if r["trade"] == trade and
                           (r.get("model_id") or "unreported") == model]) for trade in TRADES}}
                for model in sorted({r.get("model_id") or "unreported" for r in records})}}


def validate_baseline(baseline, prompt_sha256):
    if (not isinstance(baseline, dict)
            or baseline.get("schema") != "leaf.xbench.x03-scores.v1"
            or baseline.get("complete") is not True):
        raise ValueError("baseline must have the complete X03 scores schema")
    if not isinstance(baseline.get("source_sha"), str) or not re.fullmatch(r"[0-9a-f]{40}", baseline["source_sha"]):
        raise ValueError("baseline source identity is invalid")
    if baseline.get("prompt_sha256") != prompt_sha256:
        raise ValueError("baseline belongs to a different frozen prompt set")
    trades = baseline.get("trades")
    if not isinstance(trades, dict) or set(trades) != set(TRADES):
        raise ValueError("baseline must contain each frozen trade")
    for rate in (baseline.get("overall"), *trades.values()):
        if type(rate) not in (int, float) or not math.isfinite(rate) or not 0 <= rate <= 1:
            raise ValueError("baseline rates must be finite numbers between zero and one")


def grade_records(records, *, baseline, prompt_sha256, source_sha, out_dir):
    seen = set()
    for record in records:
        validate_prompt_id(record.get("id"))
        if record["id"] in seen:
            raise ValueError("duplicate prompt record ID")
        seen.add(record["id"])
        score, stage = record.get("score"), record.get("failing_stage")
        if type(score) is not int or score not in (0, 1):
            raise ValueError("prompt score must be binary")
        if ((score == 1 and (stage is not None or record.get("unreadable")))
                or (score == 0 and stage not in {"author", "publication", "execution",
                    "comparison", "model_provenance", "source_drift"})):
            raise ValueError("prompt score contradicts its failing stage")
    if baseline is not None:
        validate_baseline(baseline, prompt_sha256)
    complete = (len(records) == 50 and len({r["id"] for r in records}) == 50
                and Counter(r["trade"] for r in records) == Counter(TRADES)
                and all(r.get("author_submitted") and r["source_sha"] == source_sha for r in records)
                and not any(r.get("unreadable") for r in records)
                and not any(r.get("failing_stage") == "source_drift" for r in records))
    report = {"schema": "leaf.xbench.x03-scores.v1", "source_sha": source_sha,
              "prompt_sha256": prompt_sha256, "complete": complete, **rates(records),
              "failing_stages": dict(Counter(r["failing_stage"] for r in records if not r["score"]))}
    report["REGRESSION"] = bool(baseline and complete and (
        baseline["overall"] - report["overall"] > 0.05 + 1e-12 or any(
            baseline["trades"][t] - report["trades"][t] > 0.10 + 1e-12 for t in TRADES)))
    write_json_atomic(out_dir / "summary.json", report)
    fields = ["id", "trade", "drawing", "source_sha", "model_id", "tool_name", "version",
              "tool_source_hash", "output_hash", "score", "failing_stage", "author_submitted"]
    with (out_dir / "scores.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    provenance = complete and all(r.get("model_id") for r in records)
    threshold = complete and report["overall"] >= 0.70 and all(report["trades"][t] >= 0.50 for t in TRADES)
    evidence = str(out_dir / "summary.json")
    steps = [
        Step(1, "PASS" if provenance else "FAIL" if complete else "UNREADABLE", evidence,
             observed={"submitted": sum(bool(r.get("author_submitted")) for r in records), "model_reported": sum(bool(r.get("model_id")) for r in records)}, expected=50),
        Step(2, "PASS" if complete and all(not r["score"] or all(r.get(k) for k in (
            "tool_name", "version", "tool_source_hash", "output_hash", "model_id")) for r in records)
             else "FAIL" if complete else "UNREADABLE", evidence,
             observed=report["failing_stages"], expected="generated, published and oracle-matching executions"),
        Step(3, "PASS" if complete else "UNREADABLE", str(out_dir / "scores.csv"),
             observed=len(records), expected=50),
        Step(4, "PASS" if complete else "UNREADABLE", evidence,
             observed={k: report[k] for k in ("overall", "trades", "models")}),
        Step(5, "FAIL" if report["REGRESSION"] else "PASS" if complete else "UNREADABLE", evidence,
             observed="REGRESSION" if report["REGRESSION"] else "baseline" if baseline is None else "within margins"),
        Step(6, "PASS" if threshold and not report["REGRESSION"] else "FAIL" if complete else "UNREADABLE", evidence,
             observed=report["overall"], expected={"overall": 0.70, "per_trade": 0.50}),
    ]
    return steps, report


def run(ctx):
    from xbench_studio import StudioClient, disposable_project
    rows, prompt_sha = load_prompts(ctx.manifest)
    ctx.out_dir.mkdir(parents=True, exist_ok=True)
    baseline_path = Path(ctx.manifest["baseline_path"])
    baseline = None
    if baseline_path.exists():
        baseline = json.loads(baseline_path.read_text())
        validate_baseline(baseline, prompt_sha)
    fixtures = {f["id"]: f for f in ctx.manifest["fixtures"] if f.get("role") == "drawing"}
    records = []
    repo = Path(__file__).resolve().parents[3]
    run_id = uuid4().hex[:10]
    with xbench_session.signed_in(repo, ctx.source_sha, origin=ctx.base) as (page, info):
        write_json_atomic(ctx.out_dir / "session.json", info)
        transport = transport_for(page, ctx.base)
        client = StudioClient(transport)
        with ExitStack() as stack:
            projects = {trade: stack.enter_context(disposable_project(
                client, f"X03 {trade} {run_id}")) for trade in TRADES}
            drawings = {}
            for fixture_id in dict.fromkeys(r["drawing"] for r in rows):
                fixture = fixtures[fixture_id]
                trade = next(r["trade"] for r in rows if r["drawing"] == fixture_id)
                # Anonymous upload names prevent leaking local customer filenames into harness logs.
                name = fixture_id + Path(fixture["path"]).suffix
                uploaded = client.upload_drawing(name, Path(fixture["path"]).read_bytes())
                client.wait_upload(uploaded, timeout_s=600)
                drawings[fixture_id] = client.adopt_drawing(projects[trade]["project_id"], uploaded, name)
            write_json_atomic(ctx.out_dir / "projects.json", {"projects": projects, "drawings": drawings})
            for row in rows:
                deadline = time.monotonic() + 600
                with author_authority(transport, row, projects[row["trade"]],
                                      drawings[row["drawing"]], ctx.out_dir, deadline=deadline) as authority:
                    record = evaluate_prompt(row, transport=transport, client=client,
                        project=projects[row["trade"]], drawing=drawings[row["drawing"]],
                        source_sha=ctx.source_sha, evidence_dir=ctx.out_dir, run_id=run_id,
                        authority_headers=authority, deadline_at=deadline)
                records.append(record)
                write_json_atomic(ctx.out_dir / "records.json", records)
                print(f"{row['id']} score={record['score']} stage={record['failing_stage']}", flush=True)
                if record.get("failing_stage") == "source_drift":
                    break
    steps, report = grade_records(records, baseline=baseline, prompt_sha256=prompt_sha,
                                  source_sha=ctx.source_sha, out_dir=ctx.out_dir)
    if report["complete"] and baseline is None:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        # One immutable baseline; a later run can never silently replace it.
        with baseline_path.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
    return steps
