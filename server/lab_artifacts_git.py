"""ledger stays authoritative; git is a CAS-protected mirror for allowlisted, Forge-mapped tenants with main; a mirror failure never fails a ledger write.

Network operations run under the ledger tenant lock. Each accepted commit and
its receipt are persisted before publication, which is independently retryable.
No document content or author identity is sent to the alert path.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.request

import lab_artifacts as ledger
import emf_metrics

LOG = logging.getLogger(__name__)
MAX_REPLAY_REVISIONS = 2000
REF = "refs/heads/lab/main"
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_git_tenants (
 tenant_id TEXT PRIMARY KEY,
 mode TEXT NOT NULL DEFAULT 'ledger' CHECK (mode IN ('ledger','replaying','mirror')),
 frozen BOOLEAN NOT NULL DEFAULT FALSE, remote_sha TEXT, local_sha TEXT,
 receipt TEXT NOT NULL DEFAULT '{}', notified TEXT NOT NULL DEFAULT '[]',
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS lab_git_mirror (
 tenant_id TEXT NOT NULL, doc_id TEXT NOT NULL, revision INTEGER NOT NULL,
 "commit" TEXT NOT NULL, sha256 TEXT NOT NULL,
 PRIMARY KEY (tenant_id, doc_id, revision)
);
"""


class MirrorError(RuntimeError):
    def __init__(self, code, observed=None):
        self.code = code
        self.observed = observed
        super().__init__(code)


def _allowed(tenant):
    ids = [value.strip() for value in os.environ.get("LEAF_LAB_GIT_TENANTS", "").split(",") if value.strip()]
    return len(ids) <= 100 and tenant in ids


def _post(tenant, endpoint, body):
    base = os.environ.get("LEAF_AUTHOR_HARNESS_URL", "").strip().rstrip("/")
    secret = os.environ.get("LEAF_HARNESS_SECRET", "")
    if not base or not secret:
        raise MirrorError("unknown")
    try:
        request = urllib.request.Request(base + "/lab/" + endpoint,
            data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "x-tenant-id": tenant,
                     "X-Harness-Secret": secret})
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                raise MirrorError("unknown")
            result = json.loads(response.read())
        if not isinstance(result, dict) or result.get("ref") != REF:
            raise MirrorError("unknown")
        return result
    except urllib.error.HTTPError as error:
        # Never forward harness text, which may contain sensitive diagnostics.
        codes = {("commit", 409): "lab_ref_conflict",
                 ("commit", 403): "lab_repository_refused",
                 ("inspect", 403): "lab_repository_refused",
                 ("publish", 409): "lab_publish_conflict",
                 ("publish", 403): "lab_publish_refused",
                 ("publish", 422): "lab_publish_invalid"}
        observed = None
        if endpoint == "publish" and error.code == 409:
            try:
                observed = json.loads(error.read())["error"]["observed_sha"]
            except Exception:
                pass
        raise MirrorError(codes.get((endpoint, error.code), "unknown"), observed) from None
    except MirrorError:
        raise
    except Exception:
        raise MirrorError("unknown") from None


def _inspect(tenant):
    result = _post(tenant, "inspect", {"limit": 200})
    head = result.get("head")
    if ((head is not None and (not isinstance(head, str) or not _SHA.fullmatch(head)))
            or not isinstance(result.get("documents"), list)
            or not isinstance(result.get("commits"), list)):
        raise MirrorError("unknown")
    return result


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _state(sql, tenant):
    sql.execute("INSERT INTO lab_git_tenants (tenant_id) VALUES (?) ON CONFLICT DO NOTHING", (tenant,))
    row = dict(sql.execute("SELECT * FROM lab_git_tenants WHERE tenant_id=?", (tenant,)).fetchone())
    row["receipt"] = _json(row["receipt"])
    row["notified"] = _json(row["notified"])
    return row


def _update(sql, tenant, **values):
    assignments, params = [], []
    for key, value in values.items():
        if key in {"receipt", "notified"}:
            value = json.dumps(value)
            assignments.append(key + ("=CAST(? AS jsonb)" if sql.postgres else "=?"))
        else:
            assignments.append(key + "=?")
        params.append(value)
    sql.execute("UPDATE lab_git_tenants SET " + ",".join(assignments)
                + ",updated_at=CURRENT_TIMESTAMP WHERE tenant_id=?", (*params, tenant))


def mirror_status(tenant):
    return ledger._run(tenant, lambda sql: _state(sql, tenant), write=True)


def check_frozen(sql, tenant):
    row = sql.execute("SELECT frozen FROM lab_git_tenants WHERE tenant_id=?", (tenant,)).fetchone()
    if row and row["frozen"]:
        raise ledger.StoreMigrating("lab_store_migrating")


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _path(row):
    return "lab/" + ("widgets" if row["kind"] == "widget" else "graphs") + "/" + row["doc_id"] + ".json"


def _message(row):
    created = datetime.fromtimestamp(row["created_at"], timezone.utc).isoformat().replace("+00:00", "Z")
    trailers = ["Lab-Doc: " + row["doc_id"], "Lab-Revision: " + str(row["revision"]),
                "Lab-Kind: " + row["kind"], "Lab-Sha256: " + row["sha256"],
                "Lab-Created-At: " + created,
                "Lab-Author-Hash: " + hashlib.sha256(row["author"].encode("utf-8")).hexdigest()]
    if row["published_revision"] == row["revision"]:
        trailers.append("Lab-Published: true")
    return "Lab ledger mirror\n\n" + "\n".join(trailers)


def _rows(sql, tenant, watermark=None):
    rows = sql.execute(
        "SELECT r.*,d.kind,d.head,d.published_revision FROM lab_revisions r"
        " JOIN lab_documents d ON d.tenant_id=r.tenant_id AND d.doc_id=r.doc_id"
        " WHERE r.tenant_id=? ORDER BY r.created_at,r.doc_id,r.revision", (tenant,)).fetchall()
    result = []
    for row in rows:
        if watermark is not None and row["revision"] > watermark.get(row["doc_id"], 0):
            continue
        result.append(dict(row))
    return result


def _equal(sql, tenant, inspection):
    heads = sql.execute("SELECT doc_id,head FROM lab_documents WHERE tenant_id=?", (tenant,)).fetchall()
    expected = {}
    for head in heads:
        row = ledger._revision(sql, tenant, head["doc_id"], head["head"])
        expected[_path(row)] = row["sha256"]
    documents = inspection["documents"]
    return (len(documents) == len(expected)
            and {doc.get("path"): doc.get("sha256") for doc in documents} == expected)


def _record(sql, tenant, row, commit):
    sql.execute('INSERT INTO lab_git_mirror (tenant_id,doc_id,revision,"commit",sha256)'
                " VALUES (?,?,?,?,?) ON CONFLICT (tenant_id,doc_id,revision)"
                ' DO UPDATE SET "commit"=excluded."commit",sha256=excluded.sha256',
                (tenant, row["doc_id"], row["revision"], commit, row["sha256"]))


def _known_revisions(sql, tenant, commits):
    for commit in commits:
        pairs = re.findall(r"^(Lab-[A-Za-z0-9-]+): (.*)$", commit["message"], re.MULTILINE)
        trailers = dict(pairs)
        if "Lab-Revision" not in trailers:
            continue
        if any(sum(key == name for key, _ in pairs) != 1 for name in ("Lab-Revision", "Lab-Doc")):
            return False
        try:
            row = ledger._revision(sql, tenant, trailers.get("Lab-Doc"), int(trailers["Lab-Revision"]))
        except (ValueError, ledger.NotFound):
            return False
        if trailers.get("Lab-Sha256") != row["sha256"]:
            return False
    return True


def _commit(sql, tenant, state, row):
    canonical_text = ledger._revision(sql, tenant, row["doc_id"], row["revision"])["canonical_text"]
    try:
        result = _post(tenant, "commit", {"expected_sha": state["local_sha"],
            "documents": [{"path": _path(row), "content": canonical_text}],
            "message": _message(row)})
    except MirrorError as error:
        if error.code != "lab_ref_conflict":
            raise
        # Recover a lost commit response only from an exact parent, trailer
        # message and content digest match. Never overwrite an unrelated head.
        inspected = _inspect(tenant)
        commits = inspected["commits"]
        if (not commits or commits[0].get("parent") != state["local_sha"]
                or commits[0].get("message") != _message(row)
                or commits[0].get("commit") != inspected["head"]
                or not any(doc.get("path") == _path(row) and doc.get("sha256") == row["sha256"]
                           for doc in inspected["documents"])):
            raise
        result = {"commit": inspected["head"]}
    commit = result.get("commit")
    if not isinstance(commit, str) or not _SHA.fullmatch(commit):
        raise MirrorError("unknown")
    _record(sql, tenant, row, commit)
    receipt = state["receipt"]
    receipt = {**receipt, "heads": {**receipt.get("heads", {}), row["doc_id"]: row["revision"]}}
    # A commit SHA binds its entire ancestry. Extend this proof only through
    # our CAS parent, so flip-back can verify histories longer than inspect's
    # 200-commit window without claiming anything about unseen foreign history.
    if "verified_head" in receipt and receipt["verified_head"] == state["local_sha"]:
        receipt = {**receipt, "verified_head": commit}
    if row["published_revision"] == row["revision"]:
        receipt = {**receipt, "published": {**receipt.get("published", {}), row["doc_id"]: row["revision"]}}
    _update(sql, tenant, local_sha=commit, receipt=receipt)
    return commit


def _publish(sql, tenant, state):
    if state["local_sha"] == state["remote_sha"]:
        return
    try:
        result = _post(tenant, "publish", {"expected_remote_sha": state["remote_sha"]})
    except MirrorError as error:
        # A previous unknown publication may already have advanced the remote.
        # This later CAS response is independent evidence of its current SHA.
        if error.code != "lab_publish_conflict" or error.observed != state["local_sha"]:
            raise
        result = {"published": error.observed}
    if result.get("published") != state["local_sha"]:
        raise MirrorError("unknown")
    _update(sql, tenant, remote_sha=state["local_sha"])


def _notify(tenant, event, identity):
    key = json.dumps([tenant, event, identity], separators=(",", ":"))
    def reserve(sql):
        state = _state(sql, tenant)
        if key in state["notified"]:
            return False
        _update(sql, tenant, notified=state["notified"] + [key])
        return True
    if not ledger._run(tenant, reserve, write=True):
        return
    # Read back durable state before publishing either alert signal.
    if key not in mirror_status(tenant)["notified"]:
        return
    getattr(emf_metrics, "emit_" + event)()
    LOG.warning(event, extra={"tenant": tenant, "event": event, "mirror_key": key})


def enable_mirror(tenant, actor):
    if not _allowed(tenant):
        raise MirrorError("lab_mirror_ineligible")
    inspection = None
    def begin(sql):
        nonlocal inspection
        inspection = _inspect(tenant)
        state = _state(sql, tenant)
        receipt = state["receipt"]
        if receipt.get("status") == "in_progress":
            if inspection["head"] != receipt["last_commit"]:
                return "divergence"
            _update(sql, tenant, mode="replaying", frozen=True)
            return receipt
        if state["mode"] != "ledger":
            raise MirrorError("lab_mirror_mode_refused")
        count = sql.execute("SELECT COUNT(*) AS n FROM lab_revisions WHERE tenant_id=?", (tenant,)).fetchone()["n"]
        if count > MAX_REPLAY_REVISIONS:
            raise MirrorError("lab_replay_too_large")
        rows = _rows(sql, tenant)
        watermark = {row["doc_id"]: row["head"] for row in rows}
        receipt = {"schema": "leaf.lab-replay.v1", "tenant": tenant,
                   "watermark": watermark, "ordinal": 0,
                   "base_head": inspection["head"],
                   "last_commit": inspection["head"], "started_at": _now(),
                   "status": "in_progress"}
        commits = inspection["commits"]
        complete = ((inspection["head"] is None and not commits)
                    or (commits and commits[-1].get("parent") is None))
        prior = state["receipt"]
        proven = "verified_head" in prior and prior["verified_head"] == inspection["head"]
        if (complete or proven) and _known_revisions(sql, tenant, commits):
            receipt["verified_head"] = inspection["head"]
        _update(sql, tenant, mode="replaying", frozen=True,
                local_sha=inspection["head"], receipt=receipt)
        return receipt
    receipt = ledger._run(tenant, begin, write=True)
    try:
        if receipt == "divergence":
            raise MirrorError("lab_replay_divergence")
        while True:
            def step(sql):
                state = _state(sql, tenant)
                current = state["receipt"]
                rows = _rows(sql, tenant, current["watermark"])
                ordinal = current["ordinal"]
                if ordinal >= len(rows):
                    return False
                commit = _commit(sql, tenant, state, rows[ordinal])
                current = _state(sql, tenant)["receipt"]
                _update(sql, tenant, receipt={**current, "ordinal": ordinal + 1, "last_commit": commit})
                return True
            if not ledger._run(tenant, step, write=True):
                break
        def finish(sql):
            state = _state(sql, tenant)
            _publish(sql, tenant, state)
            inspected = _inspect(tenant)
            if inspected["head"] != state["local_sha"] or not _equal(sql, tenant, inspected):
                raise MirrorError("lab_replay_divergence")
            _update(sql, tenant, mode="mirror", frozen=False,
                    receipt={**state["receipt"], "status": "complete"})
        ledger._run(tenant, finish, write=True)
    except Exception as error:
        divergent = isinstance(error, MirrorError) and error.code in {
            "lab_replay_divergence", "lab_ref_conflict", "lab_publish_conflict"}
        def fail(sql):
            state = _state(sql, tenant)
            values = {"mode": "ledger", "frozen": False}
            if divergent:
                values["receipt"] = {**state["receipt"], "status": "divergent"}
            _update(sql, tenant, **values)
        ledger._run(tenant, fail, write=True)
        if divergent:
            _notify(tenant, "lab_replay_divergence", inspection["head"])
        raise
    return mirror_status(tenant)


def catch_up(tenant):
    identity = None
    try:
        while True:
            def step(sql):
                state = _state(sql, tenant)
                if state["mode"] != "mirror" or not _allowed(tenant):
                    return None
                for row in _rows(sql, tenant):
                    mirrored = sql.execute("SELECT revision FROM lab_git_mirror"
                        " WHERE tenant_id=? AND doc_id=? AND revision=?",
                        (tenant, row["doc_id"], row["revision"])).fetchone()
                    # A publication is metadata on an existing revision. Commit
                    # it before newer pending revisions so it cannot overwrite
                    # their document head with the older published content.
                    unpublished_pointer = (row["published_revision"] == row["revision"]
                        and state["receipt"].get("published", {}).get(row["doc_id"]) != row["revision"])
                    # An older publish pointer may have been mirrored after
                    # this head was already committed by another worker. Restore
                    # the head bytes before publishing the resulting git tree.
                    stale_head = (row["revision"] == row["head"]
                        and state["receipt"].get("heads", {}).get(row["doc_id"]) != row["revision"])
                    if mirrored is None or unpublished_pointer or stale_head:
                        _commit(sql, tenant, state, row)
                        return True
                return False
            pending = ledger._run(tenant, step, write=True)
            if pending is None:
                return
            if not pending:
                break
        def finish(sql):
            state = _state(sql, tenant)
            _publish(sql, tenant, state)
            _update(sql, tenant, receipt={**state["receipt"], "lagging": False})
        ledger._run(tenant, finish, write=True)
    except Exception:
        def lag(sql):
            state = _state(sql, tenant)
            _update(sql, tenant, receipt={**state["receipt"], "lagging": True})
            return state["local_sha"]
        identity = ledger._run(tenant, lag, write=True)
        _notify(tenant, "lab_commit_unknown", identity)


def after_write(tenant):
    # Keep this guard cheap and do not consult the harness for ledger tenants.
    try:
        if _allowed(tenant) and mirror_status(tenant)["mode"] == "mirror":
            catch_up(tenant)
    except Exception:
        # The ledger transaction has committed. Even an alert/storage failure
        # must not change the result of the original ledger operation.
        pass


def flip_back(tenant, actor):
    def operation(sql):
        state = _state(sql, tenant)
        inspected = _inspect(tenant)
        if not _equal(sql, tenant, inspected):
            return inspected["head"]
        commits = inspected["commits"]
        receipt = state["receipt"]
        proven = "verified_head" in receipt and receipt["verified_head"] == inspected["head"]
        # Unseen ancestry is acceptable only when it was proved before this
        # window and extended exclusively by our CAS commits.
        if commits and commits[-1].get("parent") is not None and not proven:
            return inspected["head"]
        if not proven:
            if "base_head" not in receipt:
                return inspected["head"]
            for commit in commits:
                if commit["commit"] == receipt["base_head"]:
                    break
                row = sql.execute('SELECT doc_id,revision FROM lab_git_mirror'
                    ' WHERE tenant_id=? AND "commit"=?',
                    (tenant, commit["commit"])).fetchone()
                pairs = re.findall(r"^(Lab-[A-Za-z0-9-]+): (.*)$", commit["message"], re.MULTILINE)
                if row is None or any(sum(key == name for key, _ in pairs) != 1
                                      for name in ("Lab-Doc", "Lab-Revision")):
                    return inspected["head"]
                trailers = dict(pairs)
                try:
                    matches = (trailers["Lab-Doc"] == row["doc_id"]
                               and int(trailers["Lab-Revision"]) == row["revision"])
                except ValueError:
                    return inspected["head"]
                if not matches:
                    return inspected["head"]
        if not _known_revisions(sql, tenant, commits):
            return inspected["head"]
        _update(sql, tenant, mode="ledger", frozen=False)
        return False
    try:
        refused = ledger._run(tenant, operation, write=True)
    except Exception:
        # A failed inspection cannot prove that flip-back is safe.
        refused = mirror_status(tenant)["local_sha"]
    if refused is not False:
        _notify(tenant, "lab_replay_divergence", refused)
        raise MirrorError("lab_flip_back_refused")
    return mirror_status(tenant)
