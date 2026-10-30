"""Capture sanitized wave-3 staging observations; importing performs no I/O.

Run ``python -P server/capture_lab_wave3.py NAME --sha <40 lowercase hex>
--proof-key <32 lowercase hex> --doc <private id> --out-dir <directory>``.
Reuse the release sha and proof key for all six commands. Credentials come from
LEAF_E2E_STAGING_JWT or --jwt-secret (Secrets Manager key jwt); DATABASE_URL
must address the staging database. relay needs no credentials or database.
ledger --publish and activation --create explicitly perform their named writes.
First use activation --create, then close the editor, observe the custom widget
on /mini, and capture with activation (without --create). Capture requires
--editor-closed-at, --observed-at and --widget-rendered. relay requires
--ops-sha and all six explicit observation flags. rollback performs live pause,
resume and cancel exercises; use a disposable acceptance document and attest to
each documented control with --reviewed <id> after reviewing its procedure. run --cancel
performs only a disposable cancellation and emits no receipt. --timeout defaults
to 600 seconds and cannot exceed 1800. No raw source values reach diagnostics.

HTTP injection: request(method, path, body=None) -> decoded JSON. Database
injection: replay(private_scope), publication_count(private_scope),
jobs(private_scope, run_id). Capture also accepts a clock and sleep for fakes.
Projections below are pure: their common metadata includes the completion time,
never a start time or a timestamp borrowed from another receipt.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


ORIGIN = "https://platform-staging.leafdesign.ai"
IDS = ("worker-disable", "worker-pause", "run-cancel", "git-flip-back",
       "activation-pause", "activation-off", "tool-nodes", "image-downgrade",
       "surface-rail", "relay-gate")
LIVE = {"worker-pause", "run-cancel", "activation-pause"}
RELAY = ("listOk", "detailOk", "intentByIdOk", "opaqueHandlesOnly", "capsHeld", "fallbackPolicyOk")
UUID = r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"
PRIVACY = [re.compile(p, re.I) for p in (
    UUID, r"client_request_id|nonce|approval_id|tenant|subject",
    r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+",
    r"(?:^|[^a-z0-9_-])(?:(?:eyJ[a-z0-9_-]*|e30)\.[a-z0-9_-]+\.[a-z0-9_-]+|[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,})(?:$|[^a-z0-9_-])",
)]


class Refused(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(ok, code):
    if not ok:
        raise Refused(code)


def hex_value(value, width, code):
    require(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{%d}" % width, value), code)
    return value


def integer(value, minimum=1):
    require(type(value) is int and minimum <= value <= 9007199254740991, "measurement_invalid")
    return value


def utc(value, code="time_order"):
    require(isinstance(value, str) and re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z", value), code)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise Refused(code) from None
    # Lexicographically ordered seconds and exact fractional seconds (no float
    # or datetime microsecond truncation for browser observations).
    return value[:19], (value[20:-1] if "." in value else "").rstrip("0")


def published(value):
    return None if value is None else integer(value)


def common(name, sha, proof_key, captured_at):
    hex_value(sha, 40, "sha_invalid")
    hex_value(proof_key, 32, "proof_key_invalid")
    utc(captured_at, "capture_time_invalid")
    return dict(schema=f"leaf.wave3-{name}.v1", sha=sha, proofKey=proof_key,
                capturedAt=captured_at, **{"pass": True})


def replay_receipt(meta, source, doc):
    require(isinstance(source, dict), "replay_absent")
    require(source.get("status") == "complete", "replay_not_complete")
    require(doc in source.get("watermark", {}), "replay_doc_absent")
    return dict(meta, status="complete", revision=integer(source["watermark"][doc]),
                publishedRevision=published(source.get("published", {}).get(doc)),
                headCommit=hex_value(source.get("last_commit"), 40, "commit_invalid"), divergent=False)


def ledger_receipt(meta, head, revision, count):
    require(head["revision"] == revision["revision"], "revision_mismatch")
    return dict(meta, revision=integer(head["revision"]),
                publishedRevision=published(head["published_revision"]),
                digest=hex_value(revision.get("sha256"), 64, "digest_invalid"),
                publicationCount=integer(count, 0))


def run_receipt(meta, run, revision, jobs):
    require(run.get("state") == "finished", "run_not_finished")
    require(bool(jobs), "no_broker_jobs")
    require(all(j.get("status") == "complete" for j in jobs), "broker_incomplete")
    blocks = set()
    for job in jobs:
        key = job.get("idempotency_key", "")
        prefix = "lab:" + run["run_id"] + ":"
        require(key.startswith(prefix) and key.endswith(":1") and bool(key[len(prefix):-2]),
                "broker_incomplete")
        blocks.add(key[len(prefix):-2])
    require(run["graph_revision"] == revision["revision"], "revision_mismatch")
    digest = hex_value(revision.get("sha256"), 64, "digest_invalid")
    broker_digest = hex_value(run.get("graph_sha256"), 64, "digest_invalid")
    require(digest == broker_digest, "digest_mismatch")
    # Server 'finished' means success. Distinct broker key block ids count tool
    # nodes; every matching job must be 'complete' to map to broker 'succeeded'.
    # graph_sha256 is the ledger digest pinned and verified by the worker before
    # broker submission, not a digest invented from a broker response payload.
    return dict(meta, revision=integer(run["graph_revision"]), digest=digest,
                toolNodeCount=len(blocks), runState="succeeded", brokerState="succeeded",
                brokerDigest=broker_digest)


def activation_receipt(meta, activation, count, closed, observed, widget):
    require(activation is not None and activation.get("state") == "active", "activation_absent")
    times = [t["at"] for t in activation.get("transitions", [])
             if t.get("to_state") == "active" and t.get("reason") == "created" and "at" in t]
    require(len(times) == 1, "activation_time_unknown")
    require(widget is True, "widget_unobserved")
    require(utc(times[0]) <= utc(closed) < utc(observed), "time_order")
    return dict(meta, revision=integer(activation["revision"]),
                digest=hex_value(activation.get("graph_sha256"), 64, "digest_invalid"),
                publicationCount=integer(count, 0), activatedAt=times[0],
                editorClosedAt=closed, observedAt=observed,
                widgetRendered=True, widgetSurface="/mini")


def relay_receipt(meta, ops_sha, observations):
    require(all(observations.get(k) is True for k in RELAY), "relay_unobserved")
    return dict(meta, opsSha=hex_value(ops_sha, 40, "ops_sha_invalid"),
                **{key: True for key in RELAY})


def require_reviewed(reviewed):
    documented = set(IDS) - LIVE
    require(not (set(reviewed) - documented), "reviewed_unknown")
    require(documented <= set(reviewed), "documented_unreviewed")


def rollback_receipt(meta, observations, reviewed):
    require_reviewed(reviewed)
    for key in IDS:
        if key in LIVE:
            require(observations.get(key) == (True, True), key)
    return dict(meta, controls=[dict(id=key, method="live" if key in LIVE else "documented",
                                    exercised=key in LIVE, restored=key in LIVE) for key in IDS])


def check_privacy(value):
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            require(not any(p.search(item) for p in PRIVACY), "privacy")
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def write_receipt(directory, name, value):
    check_privacy(value)  # Check before even creating the directory/temp file.
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix=".capture-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, separators=(",", ":"), ensure_ascii=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / f"{name}.json")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def allowed_path(method, path):
    # Fullmatch rejects encoded separators, traversal, arbitrary queries and
    # fragments. Only individually quoted identifiers enter these paths.
    ident = r"[A-Za-z0-9_-]+"
    routes = {
        "GET": [rf"/api/lab/docs/{ident}/head", rf"/api/lab/docs/{ident}/revisions/[1-9][0-9]*",
                rf"/api/lab/runs/{ident}", rf"/api/lab/runs/{ident}/events\?after_seq=[0-9]+&limit=500",
                r"/api/lab/activations"],
        "POST": [rf"/api/lab/docs/{ident}/publish", r"/api/lab/runs",
                 rf"/api/lab/runs/{ident}/cancel", r"/api/lab/runs/worker/(?:pause|resume)",
                 r"/api/lab/activations", rf"/api/lab/activations/{ident}/(?:pause|resume|off)"],
    }
    require(any(re.fullmatch(p, path) for p in routes.get(method, [])), "path_refused")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise Refused("origin_refused")


class HTTP:
    def __init__(self, token, origin=ORIGIN):
        require(origin == ORIGIN and urlsplit(origin).netloc == "platform-staging.leafdesign.ai",
                "origin_refused")
        self.token, self.origin = token, origin
        self.opener = build_opener(NoRedirect())

    def request(self, method, path, body=None):
        allowed_path(method, path)
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(self.origin + path, data=data, method=method, headers={
            "Authorization": "Bearer " + self.token, "Content-Type": "application/json",
            "User-Agent": "leaf-wave3-capture/1"})
        with self.opener.open(request, timeout=30) as response:
            require(200 <= response.status < 300, "http_refused")
            raw = response.read(1024 * 1024 + 1)
        require(len(raw) <= 1024 * 1024, "response_too_large")
        return json.loads(raw)


class Database:
    """Only three parameterized SELECTs; never imports application stores."""
    def __init__(self, url):
        require(bool(url), "database_unavailable")
        self.url = url

    def _read(self, statement, parameters):
        if self.url.startswith("sqlite:///"):
            import sqlite3
            path = Path(self.url[len("sqlite:///"):]).resolve()
            with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
                conn.row_factory = sqlite3.Row
                return [dict(r) for r in conn.execute(statement.replace("%s", "?"), parameters)]
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(self.url, row_factory=dict_row, connect_timeout=30) as conn:
            conn.read_only = True
            with conn.cursor() as cursor:
                cursor.execute(statement, parameters)
                return cursor.fetchall()

    def replay(self, scope):
        rows = self._read("SELECT receipt FROM lab_git_tenants WHERE tenant_id = %s", (scope,))
        value = rows[0]["receipt"] if rows else None
        return json.loads(value) if isinstance(value, str) else value

    def publication_count(self, scope):
        return self._read("SELECT COUNT(*) AS count FROM lab_publish_receipts WHERE tenant_id = %s",
                          (scope,))[0]["count"]

    def jobs(self, scope, run_id):
        # Literal prefix comparison avoids LIKE wildcards in opaque run ids.
        prefix = "lab:" + run_id + ":"
        return self._read("SELECT status, idempotency_key FROM async_jobs WHERE tenant_id = %s "
                          "AND substr(idempotency_key, 1, %s) = %s", (scope, len(prefix), prefix))


def credentials(env, secret):
    token = env.get("LEAF_E2E_STAGING_JWT")
    if not token:
        import boto3
        value = boto3.client("secretsmanager").get_secret_value(SecretId=secret)
        token = json.loads(value["SecretString"])["jwt"]
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        scope = claims["https://leafdesign.ai/tenant_id"]
        require(isinstance(scope, str) and bool(scope), "credentials_invalid")
    except Exception:
        raise Refused("credentials_invalid") from None
    # Routing only: the server verifies the JWT and its subject binding.
    return token, scope


class Capture:
    def __init__(self, http, database, scope, *, now=None, monotonic=None, sleep=None):
        self.http, self.db, self.scope = http, database, scope
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.monotonic, self.sleep = monotonic or time.monotonic, sleep or time.sleep

    def request(self, method, path, body=None):
        allowed_path(method, path)
        return self.http.request(method, path, body)

    def head(self, doc):
        return self.request("GET", f"/api/lab/docs/{quote(doc, safe='')}/head")

    def revision(self, doc, revision):
        integer(revision)
        return self.request("GET", f"/api/lab/docs/{quote(doc, safe='')}/revisions/{revision}")

    def create_run(self, doc):
        head = self.head(doc)
        integer(head["revision"])
        return self.request("POST", "/api/lab/runs", {"doc_id": doc, "graph_revision": head["revision"]})["run_id"]

    def poll(self, run_id, timeout, *, cancelled=False, leave_queued=False):
        deadline = self.monotonic() + timeout
        while True:
            row = self.request("GET", f"/api/lab/runs/{quote(run_id, safe='')}")
            require(row.get("run_id") == run_id, "run_identity_invalid")
            state = row.get("state")
            require(state in {"queued", "running", "finished", "failed", "cancelled"},
                    "run_not_finished")
            if leave_queued and state in {"running", "finished"}:
                return row
            if state in {"finished", "failed", "cancelled"}:
                require(state == ("cancelled" if cancelled else "finished"), "run_not_finished")
                return row
            require(self.monotonic() < deadline, "run_timeout")
            self.sleep(min(5, deadline - self.monotonic()))

    def events(self, run_id):
        after = 0
        for _ in range(50):
            page = self.request("GET", f"/api/lab/runs/{quote(run_id, safe='')}/events?after_seq={after}&limit=500")["events"]
            require(isinstance(page, list) and len(page) <= 500, "events_invalid")
            if not page:
                return
            for event in page:
                require(type(event.get("seq")) is int and event["seq"] > after, "events_invalid")
                after = event["seq"]
        raise Refused("events_incomplete")

    def cancel(self, doc, timeout):
        run_id = self.create_run(doc)
        self.request("POST", f"/api/lab/runs/{quote(run_id, safe='')}/cancel")
        self.poll(run_id, timeout, cancelled=True)
        return True

    def active(self, doc):
        rows = [a for a in self.request("GET", "/api/lab/activations")["activations"]
                if a.get("doc_id") == doc and a.get("state") == "active"]
        require(bool(rows), "activation_absent")
        for row in rows:
            # Server dates may use +00:00; they are read as instants here.
            self.date(row["created_at"])
        return max(rows, key=lambda row: self.date(row["created_at"]))

    @staticmethod
    def date(value):
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(date.tzinfo is not None, "measurement_invalid")
        return date

    def rollback(self, doc, timeout):
        observations = {}
        # Finally blocks attempt recovery even when a stop observation fails.
        try:
            try:
                self.request("POST", "/api/lab/runs/worker/pause", {"reason": "wave3 capture"})
                run_id = self.create_run(doc)
                deadline = self.monotonic() + 30
                while True:
                    row = self.request("GET", f"/api/lab/runs/{quote(run_id, safe='')}")
                    require(row.get("run_id") == run_id and row.get("state") == "queued",
                            "worker-pause")
                    if self.monotonic() >= deadline:
                        break
                    self.sleep(min(5, deadline - self.monotonic()))
            finally:
                self.request("POST", "/api/lab/runs/worker/resume")
            self.poll(run_id, timeout, leave_queued=True)
            observations["worker-pause"] = (True, True)
        except Exception:
            raise Refused("worker-pause") from None
        try:
            self.cancel(doc, timeout)
            self.poll(self.create_run(doc), timeout)
            observations["run-cancel"] = (True, True)
        except Exception:
            raise Refused("run-cancel") from None
        try:
            selected = self.active(doc)
            aid = selected["activation_id"]
            path = f"/api/lab/activations/{quote(aid, safe='')}"
            try:
                self.request("POST", path + "/pause")
                rows = self.request("GET", "/api/lab/activations")["activations"]
                require(any(a.get("activation_id") == aid and a.get("state") == "paused" for a in rows),
                        "activation-pause")
            finally:
                self.request("POST", path + "/resume")
            rows = self.request("GET", "/api/lab/activations")["activations"]
            require(any(a.get("activation_id") == aid and a.get("state") == "active"
                        and self.date(a["next_due_at"]) > self.now() for a in rows), "activation-pause")
            observations["activation-pause"] = (True, True)
        except Exception:
            raise Refused("activation-pause") from None
        return observations

    def capture(self, args):
        require(type(args.timeout) is int and 1 <= args.timeout <= 1800, "timeout_invalid")
        # Validate common inputs before any action; stamp completion afterward.
        common(args.command, args.sha, args.proof_key, self.now().isoformat().replace("+00:00", "Z"))
        name, doc = args.command, args.doc
        if name == "replay":
            source = self.db.replay(self.scope)
            project = lambda meta: replay_receipt(meta, source, doc)
        elif name == "ledger":
            head = self.head(doc)
            if args.publish:
                self.request("POST", f"/api/lab/docs/{quote(doc, safe='')}/publish", {"expected_head": head["revision"]})
                head = self.head(doc)
            revision = self.revision(doc, head["revision"])
            count = self.db.publication_count(self.scope)
            project = lambda meta: ledger_receipt(meta, head, revision, count)
        elif name == "run":
            if args.cancel:
                self.cancel(doc, args.timeout)
                return None
            run_id = self.create_run(doc)
            run = self.poll(run_id, args.timeout)
            require(run.get("run_id") == run_id, "run_identity_invalid")
            self.events(run_id)
            revision = self.revision(doc, run["graph_revision"])
            jobs = self.db.jobs(self.scope, run_id)
            project = lambda meta: run_receipt(meta, run, revision, jobs)
        elif name == "activation":
            if args.create:
                require(args.editor_closed_at is None and args.observed_at is None
                        and not args.widget_rendered, "usage")
                self.request("POST", "/api/lab/activations", {"doc_id": doc, "interval_s": 60})
                return None
            require(args.widget_rendered is True, "widget_unobserved")
            require(utc(args.editor_closed_at) < utc(args.observed_at), "time_order")
            activation = self.active(doc)
            # The server stores +00:00, whereas receipts require UTC Z.
            transitions = []
            for transition in activation.get("transitions", []):
                transition = dict(transition)
                if "at" in transition:
                    # Preserve already-UTC fractions exactly; convert server
                    # offset timestamps only when normalization is necessary.
                    value = transition["at"]
                    transition["at"] = value if value.endswith("Z") else self.date(value).astimezone(
                        timezone.utc).isoformat().replace("+00:00", "Z")
                transitions.append(transition)
            activation = dict(activation, transitions=transitions)
            count = self.db.publication_count(self.scope)
            project = lambda meta: activation_receipt(meta, activation, count, args.editor_closed_at,
                                                       args.observed_at, args.widget_rendered)
        elif name == "relay":
            observations = {key: getattr(args, re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()) for key in RELAY}
            project = lambda meta: relay_receipt(meta, args.ops_sha, observations)
        else:
            require_reviewed(args.reviewed)
            observations = self.rollback(doc, args.timeout)
            project = lambda meta: rollback_receipt(meta, observations, args.reviewed)
        completed = self.now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        result = project(common(name, args.sha, args.proof_key, completed))
        check_privacy(result)
        return result


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refused("arguments_invalid")


def parser():
    root = Parser(description=__doc__, add_help=False)
    commands = root.add_subparsers(dest="command", required=True, parser_class=Parser)
    for name in ("replay", "ledger", "run", "activation", "relay", "rollback"):
        sub = commands.add_parser(name, add_help=False)
        for key in ("sha", "proof-key", "doc", "out-dir"):
            sub.add_argument("--" + key, required=True)
        sub.add_argument("--timeout", type=int, default=600)
        sub.add_argument("--jwt-secret", default="stg/leaf/acceptance-tenant-b")
        if name == "ledger":
            sub.add_argument("--publish", action="store_true")
        if name == "run":
            sub.add_argument("--cancel", action="store_true")
        if name == "rollback":
            sub.add_argument("--reviewed", action="append", default=[])
        if name == "activation":
            sub.add_argument("--create", action="store_true")
            sub.add_argument("--editor-closed-at")
            sub.add_argument("--observed-at")
            sub.add_argument("--widget-rendered", action="store_true")
        if name == "relay":
            sub.add_argument("--ops-sha")
            for flag in ("list-ok", "detail-ok", "intent-by-id-ok", "opaque-handles-only", "caps-held", "fallback-policy-ok"):
                sub.add_argument("--" + flag, action="store_true")
    return root


def main(argv=None, *, capture=None, env=None):
    try:
        # Dependency errors and argparse must never echo private inputs.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            args = parser().parse_args(argv)
            if capture is None:
                if args.command == "relay":
                    capture = Capture(None, None, None)
                else:
                    environment = os.environ if env is None else env
                    token, scope = credentials(environment, args.jwt_secret)
                    capture = Capture(HTTP(token), Database(environment.get("DATABASE_URL")), scope)
            receipt = capture.capture(args)
            if receipt is not None:
                write_receipt(args.out_dir, args.command, receipt)
        print(json.dumps({"outcome": "captured", "receipt": args.command} if receipt is not None
                         else {"outcome": "activation_created" if args.command == "activation" else "cancelled"}))
        return 0
    except (Exception, KeyboardInterrupt, SystemExit) as error:
        code = error.code if isinstance(error, Refused) else "capture_refused"
        print(json.dumps({"outcome": "refused", "reason": code}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
