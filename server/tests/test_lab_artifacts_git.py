"""CAS mirror behavior against a fake harness and both ledger authorities."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import threading
import urllib.error
import uuid

import pytest

import emf_metrics
import lab_artifacts as ledger
import lab_artifacts_git as mirror

GRAPH = {"schema": "leaf.lab-graph.v1", "blocks": [], "pipes": [], "outputs": {}}
WIDGET = json.loads((Path(__file__).resolve().parents[2] /
    "contract/fixtures/widget/parity/basic.json").read_text(encoding="utf-8"))["doc"]


class Harness:
    def __init__(self):
        self.head = None
        self.remote = None
        self.commits = []
        self.documents = {}
        self.calls = []
        self.fail = None
        self.diverge = False
        self.refused = False
        self.lose_commit = False
        self.lose_publish = False

    def post(self, tenant, endpoint, body):
        if endpoint == 'generation':
            return {'generation': None, 'generation_oid': None, 'head': self.head}
        self.calls.append((tenant, endpoint, body))
        if self.refused:
            raise mirror.MirrorError("lab_repository_refused")
        if endpoint == self.fail:
            raise mirror.MirrorError("unknown")
        if endpoint == "commit":
            if body["expected_sha"] != self.head:
                raise mirror.MirrorError("lab_ref_conflict")
            commit = f"{len(self.commits) + 1:040x}"
            self.commits.insert(0, {"commit": commit, "parent": self.head, "message": body["message"]})
            self.head = commit
            for doc in body["documents"]:
                self.documents[doc["path"]] = doc["content"]
            if self.lose_commit:
                self.lose_commit = False
                raise mirror.MirrorError("unknown")
            return {"ref": mirror.REF, "commit": commit}
        if endpoint == "publish":
            if body["expected_remote_sha"] != self.remote:
                raise mirror.MirrorError("lab_publish_conflict", self.remote)
            self.remote = self.head
            if self.lose_publish:
                self.lose_publish = False
                raise mirror.MirrorError("unknown")
            return {"ref": mirror.REF, "published": self.remote}
        assert endpoint == "inspect"
        documents = [{"path": path, "blob": "a" * 40,
                      "sha256": hashlib.sha256(content.encode()).hexdigest(),
                      "bytes": len(content.encode())} for path, content in self.documents.items()]
        if self.diverge and documents:
            documents[0]["sha256"] = "f" * 64
        return {"ref": mirror.REF, "head": self.head,
                "commits": self.commits[:body["limit"]], "documents": documents}


@pytest.fixture(params=["sqlite", "postgres"])
def world(request, monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "test")
    monkeypatch.setenv("LEAF_PLATFORM_POSTGRES_REQUIRED", "0")
    monkeypatch.setenv("LEAF_LAB_STORE", request.param)
    monkeypatch.setenv("LEAF_LAB_DB", str(tmp_path / "mirror.db"))
    if request.param == "postgres":
        if not os.environ.get("DATABASE_URL"):
            pytest.skip("platform DB unreachable: DATABASE_URL unset")
        import platform_link
        platform_link.platform_db().apply_migration()
    tenant = "mirror-" + uuid.uuid4().hex
    monkeypatch.setenv("LEAF_LAB_GIT_TENANTS", tenant)
    harness = Harness()
    monkeypatch.setattr(mirror, "_post", harness.post)
    events = []
    monkeypatch.setattr(emf_metrics, "emit_lab_replay_divergence", lambda: events.append("divergence"))
    monkeypatch.setattr(emf_metrics, "emit_lab_commit_unknown", lambda: events.append("unknown"))
    return tenant, harness, events


def save(tenant, doc="x", expected=None, kind="graph"):
    return ledger.save(tenant, doc, GRAPH if kind == "graph" else WIDGET,
                       expected, "private-author@example.test", kind=kind)


def test_unlisted_tenant_never_contacts_harness(world, monkeypatch):
    tenant, harness, _ = world
    monkeypatch.setenv("LEAF_LAB_GIT_TENANTS", ",".join([tenant] + [f"other-{n}" for n in range(100)]))
    with pytest.raises(mirror.MirrorError, match="lab_mirror_ineligible"):
        mirror.enable_mirror(tenant, "operator")
    assert harness.calls == []
    monkeypatch.delenv("LEAF_LAB_GIT_TENANTS")
    save(tenant)
    ledger.restore(tenant, "x", 1, 1, "member")
    ledger.publish(tenant, "x", 2, "member")
    assert mirror.mirror_status(tenant)["mode"] == "ledger"
    assert harness.calls == []
    with pytest.raises(mirror.MirrorError, match="lab_mirror_ineligible"):
        mirror.enable_mirror(tenant, "operator")
    assert harness.calls == []


def test_replay_has_canonical_bytes_trailers_order_and_one_publish(world):
    tenant, harness, _ = world
    first = save(tenant)
    second = save(tenant, "widget", kind="widget")
    third = save(tenant, expected=1)
    ledger.publish(tenant, "x", 2, "member")
    state = mirror.enable_mirror(tenant, "operator")
    assert state["mode"] == "mirror" and not state["frozen"]
    assert state["local_sha"] == state["remote_sha"] == harness.head
    assert state["receipt"]["schema"] == "leaf.lab-replay.v1"
    assert state["receipt"]["watermark"] == {"x": 2, "widget": 1}
    assert state["receipt"]["ordinal"] == 3
    assert [endpoint for _, endpoint, _ in harness.calls] == ["inspect", "commit", "commit", "commit", "publish", "inspect"]
    commits = [body for _, endpoint, body in harness.calls if endpoint == "commit"]
    for row, body in zip((first, second, third), commits):
        assert body["documents"][0]["content"] == row["canonical_text"]
        assert f'Lab-Doc: {row["doc_id"]}' in body["message"]
        assert f'Lab-Revision: {row["revision"]}' in body["message"]
        assert f'Lab-Kind: {row["kind"]}' in body["message"]
        assert f'Lab-Sha256: {row["sha256"]}' in body["message"]
        assert "private-author@example.test" not in body["message"]
        assert hashlib.sha256(row["author"].encode()).hexdigest() in body["message"]
        created = next(line.split(": ", 1)[1] for line in body["message"].splitlines() if line.startswith("Lab-Created-At:"))
        assert datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() == pytest.approx(row["created_at"])
        assert "publish" not in body
    assert "Lab-Published: true" in commits[-1]["message"]
    assert commits[1]["documents"][0]["path"] == "lab/widgets/widget.json"
    assert commits[0]["expected_sha"] is None
    assert commits[1]["expected_sha"] == harness.commits[-1]["commit"]


@pytest.mark.parametrize("action", ["save", "restore", "publish"])
def test_freeze_refuses_ledger_mutations_inside_transaction(world, action):
    tenant, _, _ = world
    save(tenant)
    ledger._run(tenant, lambda sql: mirror._update(sql, tenant, frozen=True)
                if mirror._state(sql, tenant) else None, write=True)
    with pytest.raises(ledger.StoreMigrating, match="lab_store_migrating"):
        if action == "save":
            save(tenant, expected=1)
        elif action == "restore":
            ledger.restore(tenant, "x", 1, 1, "member")
        else:
            ledger.publish(tenant, "x", 1, "member")
    assert ledger.get(tenant, "x")["revision"] == 1


def test_replay_revision_cap_precedes_freeze_and_commits(world, monkeypatch):
    tenant, harness, _ = world
    save(tenant)
    save(tenant, expected=1)
    assert mirror.MAX_REPLAY_REVISIONS == 2000
    monkeypatch.setattr(mirror, "MAX_REPLAY_REVISIONS", 1)
    with pytest.raises(mirror.MirrorError, match="lab_replay_too_large"):
        mirror.enable_mirror(tenant, "operator")
    assert not mirror.mirror_status(tenant)["frozen"]
    assert not harness.commits


def crash_replay(tenant, harness, monkeypatch):
    original = harness.post
    def interrupted(t, endpoint, body):
        if endpoint == "commit" and len(harness.commits) == 1:
            raise SystemExit("process crash after durable commit 1")
        return original(t, endpoint, body)
    monkeypatch.setattr(mirror, "_post", interrupted)
    with pytest.raises(SystemExit):
        mirror.enable_mirror(tenant, "operator")
    monkeypatch.setattr(mirror, "_post", original)


def test_crash_resumes_after_durable_receipt(world, monkeypatch):
    tenant, harness, _ = world
    save(tenant)
    save(tenant, expected=1)
    crash_replay(tenant, harness, monkeypatch)
    state = mirror.mirror_status(tenant)
    assert state["receipt"]["ordinal"] == 1 and state["frozen"]
    assert state["receipt"]["base_head"] is None
    with pytest.raises(ledger.StoreMigrating):
        save(tenant, "blocked")
    assert mirror.enable_mirror(tenant, "operator")["mode"] == "mirror"
    assert mirror.mirror_status(tenant)["receipt"]["base_head"] is None
    assert len(harness.commits) == 2


def test_resume_refuses_changed_inspect_head(world, monkeypatch):
    tenant, harness, events = world
    save(tenant)
    save(tenant, expected=1)
    crash_replay(tenant, harness, monkeypatch)
    harness.head = "d" * 40
    with pytest.raises(mirror.MirrorError, match="lab_replay_divergence"):
        mirror.enable_mirror(tenant, "operator")
    assert mirror.mirror_status(tenant)["mode"] == "ledger"
    assert not mirror.mirror_status(tenant)["frozen"]
    assert events == ["divergence"]


def test_divergent_documents_deduplicate_alerts(world, caplog):
    tenant, harness, events = world
    save(tenant)
    harness.diverge = True
    with pytest.raises(mirror.MirrorError, match="lab_replay_divergence"):
        mirror.enable_mirror(tenant, "operator")
    state = mirror.mirror_status(tenant)
    assert state["mode"] == "ledger" and not state["frozen"]
    assert state["receipt"]["status"] == "divergent"
    mirror._notify(tenant, "lab_replay_divergence", None)
    assert events == ["divergence"]
    records = [r for r in caplog.records if r.name == "lab_artifacts_git"]
    assert len(records) == 1
    assert "private-author" not in str(records[0].__dict__)
    assert "canonical_text" not in str(records[0].__dict__)


def test_mirror_save_restore_and_publish(world):
    tenant, harness, _ = world
    save(tenant)
    mirror.enable_mirror(tenant, "operator")
    save(tenant, expected=1)
    ledger.restore(tenant, "x", 1, 2, "restorer")
    ledger.publish(tenant, "x", 3, "publisher")
    assert len(harness.commits) == 4
    assert "Lab-Published: true" in harness.commits[0]["message"]
    state = mirror.mirror_status(tenant)
    assert state["local_sha"] == state["remote_sha"] == harness.head


def test_commit_failure_keeps_ledger_and_catch_up_fills_gap(world):
    tenant, harness, events = world
    save(tenant)
    mirror.enable_mirror(tenant, "operator")
    harness.fail = "commit"
    assert save(tenant, expected=1)["revision"] == 2
    mirror.catch_up(tenant)
    assert ledger.get(tenant, "x")["revision"] == 2
    assert events == ["unknown"]
    assert mirror.mirror_status(tenant)["receipt"]["lagging"]
    harness.fail = None
    mirror.catch_up(tenant)
    assert len(harness.commits) == 2
    assert mirror.mirror_status(tenant)["remote_sha"] == harness.head
    assert not mirror.mirror_status(tenant)["receipt"]["lagging"]


def test_publication_retry_never_recommits(world):
    tenant, harness, events = world
    save(tenant)
    mirror.enable_mirror(tenant, "operator")
    harness.fail = "publish"
    save(tenant, expected=1)
    state = mirror.mirror_status(tenant)
    assert state["local_sha"] != state["remote_sha"]
    assert len(harness.commits) == 2 and events == ["unknown"]
    harness.fail = None
    mirror.catch_up(tenant)
    assert len(harness.commits) == 2
    assert mirror.mirror_status(tenant)["remote_sha"] == harness.head


def test_replay_publication_retry_resumes_without_commit(world):
    tenant, harness, _ = world
    save(tenant)
    harness.fail = "publish"
    with pytest.raises(mirror.MirrorError, match="unknown"):
        mirror.enable_mirror(tenant, "operator")
    assert len(harness.commits) == 1
    harness.fail = None
    assert mirror.enable_mirror(tenant, "operator")["mode"] == "mirror"
    assert len(harness.commits) == 1


@pytest.mark.parametrize("endpoint", ["commit", "publish"])
def test_lost_success_response_is_reconciled_on_retry(world, endpoint):
    tenant, harness, events = world
    save(tenant)
    mirror.enable_mirror(tenant, "operator")
    setattr(harness, "lose_" + endpoint, True)
    save(tenant, expected=1)
    assert events == ["unknown"]
    mirror.catch_up(tenant)
    assert len(harness.commits) == 2
    assert mirror.mirror_status(tenant)["remote_sha"] == harness.head
    recorded = ledger._run(tenant, lambda sql: sql.execute(
        'SELECT "commit" FROM lab_git_mirror WHERE tenant_id=? AND doc_id=? AND revision=?',
        (tenant, "x", 2)).fetchone()["commit"])
    assert recorded == harness.head
    # Exercise the recorded-commit proof independently of the verified head.
    state = mirror.mirror_status(tenant)
    ledger._run(tenant, lambda sql: mirror._update(sql, tenant, receipt={
        key: value for key, value in state["receipt"].items() if key != "verified_head"
    }), write=True)
    assert mirror.flip_back(tenant, "operator")["mode"] == "ledger"


def test_two_document_race_preserves_ledger_order(world):
    tenant, harness, _ = world
    mirror.enable_mirror(tenant, "operator")
    barrier = threading.Barrier(2)
    def writer(doc):
        barrier.wait(timeout=10)
        return save(tenant, doc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(writer, ("one", "two")))
    expected = sorted(rows, key=lambda row: (row["created_at"], row["doc_id"], row["revision"]))
    assert len(harness.commits) == 2
    assert [c["message"].split("Lab-Doc: ")[1].splitlines()[0]
            for c in reversed(harness.commits)] == [row["doc_id"] for row in expected]
    assert set(harness.documents) == {"lab/graphs/one.json", "lab/graphs/two.json"}


def test_flip_back_requires_equal_documents_and_known_revisions(world):
    tenant, harness, events = world
    save(tenant)
    mirror.enable_mirror(tenant, "operator")
    assert mirror.flip_back(tenant, "operator")["mode"] == "ledger"
    harness.commits[0]["message"] = harness.commits[0]["message"].replace("Lab-Revision: 1", "Lab-Revision: 99")
    for _ in range(2):
        with pytest.raises(mirror.MirrorError, match="lab_flip_back_refused"):
            mirror.flip_back(tenant, "operator")
    assert events == ["divergence"]


@pytest.mark.parametrize("valid_trailers", [False, True])
def test_flip_back_refuses_unrecorded_commit_with_equal_documents(world, valid_trailers):
    tenant, harness, events = world
    save(tenant)
    mirror.enable_mirror(tenant, "operator")
    message = harness.commits[0]["message"] if valid_trailers else "Git writer change"
    harness.post(tenant, "commit", {"expected_sha": harness.head,
        "documents": [], "message": message})
    for _ in range(2):
        with pytest.raises(mirror.MirrorError, match="lab_flip_back_refused"):
            mirror.flip_back(tenant, "operator")
    assert mirror.mirror_status(tenant)["mode"] == "mirror"
    assert events == ["divergence"]


@pytest.mark.parametrize("trailer_change", ["doc", "revision", "duplicate_doc", "duplicate_revision"])
def test_flip_back_recorded_commit_requires_matching_unique_trailers(world, trailer_change):
    tenant, harness, events = world
    save(tenant)
    save(tenant, "other")
    save(tenant, expected=1)
    state = mirror.enable_mirror(tenant, "operator")
    ledger._run(tenant, lambda sql: mirror._update(sql, tenant, receipt={
        key: value for key, value in state["receipt"].items() if key != "verified_head"
    }), write=True)
    message = harness.commits[0]["message"]
    if trailer_change == "doc":
        message = message.replace("Lab-Doc: x", "Lab-Doc: other")
    elif trailer_change == "revision":
        message = message.replace("Lab-Revision: 2", "Lab-Revision: 1")
    else:
        message += "\n" + ("Lab-Doc: x" if trailer_change == "duplicate_doc" else "Lab-Revision: 2")
    harness.commits[0]["message"] = message
    with pytest.raises(mirror.MirrorError, match="lab_flip_back_refused"):
        mirror.flip_back(tenant, "operator")
    assert events == ["divergence"]


@pytest.mark.parametrize("initial_commit", [False, True])
@pytest.mark.parametrize("verified", [False, True])
def test_flip_back_accepts_mirror_from_empty_or_untrailed_base(world, initial_commit, verified):
    tenant, harness, events = world
    if initial_commit:
        harness.post(tenant, "commit", {"expected_sha": None,
            "documents": [], "message": "Initial repository commit"})
    base_head = harness.head
    save(tenant)
    state = mirror.enable_mirror(tenant, "operator")
    assert state["receipt"]["base_head"] == base_head
    if not verified:
        ledger._run(tenant, lambda sql: mirror._update(sql, tenant, receipt={
            key: value for key, value in state["receipt"].items() if key != "verified_head"
        }), write=True)
    assert mirror.flip_back(tenant, "operator")["mode"] == "ledger"
    assert events == []


@pytest.mark.parametrize("verified", [False, True])
def test_flip_back_legacy_receipt_requires_verified_head(world, verified):
    tenant, harness, events = world
    save(tenant)
    state = mirror.enable_mirror(tenant, "operator")
    receipt = {key: value for key, value in state["receipt"].items() if key != "base_head"}
    if not verified:
        receipt["verified_head"] = None
    ledger._run(tenant, lambda sql: mirror._update(sql, tenant, receipt=receipt), write=True)
    if verified:
        assert receipt["verified_head"] == harness.head
        assert mirror.flip_back(tenant, "operator")["mode"] == "ledger"
        assert events == []
    else:
        with pytest.raises(mirror.MirrorError, match="lab_flip_back_refused"):
            mirror.flip_back(tenant, "operator")
        assert mirror.mirror_status(tenant)["mode"] == "mirror"
        assert events == ["divergence"]


@pytest.mark.parametrize("head_already_mirrored", [False, True])
def test_catch_up_published_pointer_precedes_newer_head(world, monkeypatch, head_already_mirrored):
    tenant, harness, _ = world
    save(tenant, "widget", kind="widget")
    mirror.enable_mirror(tenant, "operator")
    changed = json.loads(json.dumps(WIDGET))
    changed["formats"]["la"]["radius"] += 1
    # Defer both hooks to model a publish followed by a save while the mirror
    # worker is lagging. The newer document bytes must win after catch-up.
    with monkeypatch.context() as paused:
        paused.setattr(mirror, "after_write", lambda tenant: None)
        ledger.publish(tenant, "widget", 1, "publisher")
        head = ledger.save(tenant, "widget", changed, 1, "member", kind="widget")
    if head_already_mirrored:
        # Another worker can commit the newer head before the worker handling
        # the delayed publication notices the older pointer.
        def mirror_head(sql):
            row = mirror._rows(sql, tenant)[-1]
            mirror._commit(sql, tenant, mirror._state(sql, tenant), row)
        ledger._run(tenant, mirror_head, write=True)
    mirror.catch_up(tenant)
    assert len(harness.commits) == (4 if head_already_mirrored else 3)
    assert "Lab-Published: true" in harness.commits[1]["message"]
    assert "Lab-Revision: 2" in harness.commits[0]["message"]
    assert harness.documents["lab/widgets/widget.json"] == head["canonical_text"]
    assert mirror.mirror_status(tenant)["local_sha"] == harness.remote


def test_ineligible_repository_stays_ledger(world):
    tenant, harness, _ = world
    harness.refused = True
    with pytest.raises(mirror.MirrorError, match="lab_repository_refused"):
        mirror.enable_mirror(tenant, "operator")
    assert mirror.mirror_status(tenant)["mode"] == "ledger"
    count = len(harness.calls)
    save(tenant)
    assert len(harness.calls) == count


def test_harness_client_headers_timeout_and_fixed_errors(monkeypatch):
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "https://harness.test/")
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "private-secret")
    calls = []
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return json.dumps({"ref": mirror.REF, "head": None, "commits": [], "documents": []}).encode()
    def open_request(request, timeout):
        calls.append((request, timeout))
        return Response()
    monkeypatch.setattr(mirror.urllib.request, "urlopen", open_request)
    mirror._inspect("tenant-a")
    request, timeout = calls[0]
    assert timeout == 10 and request.full_url == "https://harness.test/lab/inspect"
    headers = {key.lower(): value for key, value in request.header_items()}
    assert headers["x-harness-secret"] == "private-secret" and headers["x-tenant-id"] == "tenant-a"
    def unavailable(*args, **kwargs):
        raise urllib.error.HTTPError("private-url", 503, "private-secret", {}, io.BytesIO(b"private-body"))
    monkeypatch.setattr(mirror.urllib.request, "urlopen", unavailable)
    with pytest.raises(mirror.MirrorError, match="^unknown$"):
        mirror._inspect("tenant-a")
    def publication_conflict(*args, **kwargs):
        body = {"error": {"code": "lab_publish_conflict",
                          "expected_sha": None, "observed_sha": "a" * 40}}
        raise urllib.error.HTTPError("private-url", 409, "private-secret", {},
                                     io.BytesIO(json.dumps(body).encode()))
    monkeypatch.setattr(mirror.urllib.request, "urlopen", publication_conflict)
    with pytest.raises(mirror.MirrorError, match="^lab_publish_conflict$") as failure:
        mirror._post("tenant-a", "publish", {"expected_remote_sha": None})
    assert failure.value.observed == "a" * 40


def test_emf_alert_dimensions_are_bounded_and_guarded(monkeypatch):
    emitted = []
    monkeypatch.setattr(emf_metrics, "_DISABLED", False)
    monkeypatch.setattr(emf_metrics, "_emit", lambda directives, root: emitted.append((directives, root)))
    emf_metrics.emit_lab_replay_divergence()
    emf_metrics.emit_lab_commit_unknown()
    emf_metrics._emit_lab_mirror_event("tenant-controlled-event")
    assert len(emitted) == 2
    for directives, root in emitted:
        assert directives[0]["Dimensions"] == [["event"]]
        assert set(root) == {"event", "LabMirrorEvent"}
    monkeypatch.setattr(emf_metrics, "_emit", lambda *args: (_ for _ in ()).throw(RuntimeError("sink down")))
    emf_metrics.emit_lab_commit_unknown()
