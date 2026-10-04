from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest

import customization_store
from customization_models import (
    ChangeSetConflictError, ChangeSetNotFoundError, ChangeState,
    IdempotencyReplayError, InvalidTransitionError,
)
from customization_store import SQLiteCustomizationStore
from tool_record_fields import (
    GRAPH_INPUT_SOLAR_W1 as G, ToolRecordFieldError,
    canonicalize_catalog_record_fields,
)

BASE = "a" * 40
STAGED = "b" * 40
DIGEST = "c" * 64
WORKSPACE = "d" * 64
RAW = {"name": "drape-onto-spheres", "version": "1.0.0", "graph_input": G}
RAW_BYTES = json.dumps(RAW, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True, allow_nan=False).encode("utf-8")
SNAPSHOT = {"schema": "leaf.customization-record-fields.v1", "tools": {
    "drape-onto-spheres": {"version": "1.0.0",
                           "record_sha256": hashlib.sha256(RAW_BYTES).hexdigest(),
                           "graph_input": G},
}}
S = canonicalize_catalog_record_fields(SNAPSHOT)


@pytest.fixture
def store(tmp_path):
    result = SQLiteCustomizationStore(tmp_path / "customization.db")
    result.initialize()
    return result


def reserve(store, *, async_=False, key="request", **metadata):
    kwargs = dict(tenant_id="tenant-a", idempotency_key=key, base_commit=BASE,
                  desired_platform_release="platform@sha256:abc",
                  workspace_contract_digest=WORKSPACE, author_subject="auth0|author")
    kwargs.update(metadata)
    if async_:
        return store.reserve_stage(request_description="exact description",
                                   request_fingerprint="e" * 64, **kwargs)
    return store.create_change_set(**kwargs)


def load(store, row):
    return store.get_change_set(tenant_id=row.tenant_id, change_set_id=row.change_set_id)


def staging(store, row, key="staging"):
    return store.transition(tenant_id=row.tenant_id, change_set_id=row.change_set_id,
                            next_state=ChangeState.STAGING, expected_version=row.version,
                            idempotency_key=key)


def complete(store, row, **overrides):
    kwargs = dict(tenant_id=row.tenant_id, change_set_id=row.change_set_id,
                  expected_version=row.version, idempotency_key="complete",
                  staged_commit=STAGED, catalog_digest=DIGEST,
                  platform_release="platform@sha256:abc", workspace_contract_digest=WORKSPACE)
    kwargs.update(overrides)
    return store.record_staged(**kwargs)


def audit_rows(store):
    with store._connection() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM customization_audit_events").fetchall()]


def persisted(store, row):
    with store._connection() as conn:
        return dict(conn.execute("SELECT * FROM customization_change_sets WHERE change_set_id = ?",
                                 (row.change_set_id,)).fetchone())


def published(store, key="predecessor", tenant="tenant-a", commit=BASE):
    row = reserve(store, key=key, tenant_id=tenant)
    row = complete(store, staging(store, row, key + "-staging"),
                   idempotency_key=key + "-complete", staged_commit=commit)
    for state in (ChangeState.AWAITING_APPROVAL, ChangeState.APPROVED, ChangeState.PUBLISHING):
        row = store.transition(tenant_id=tenant, change_set_id=row.change_set_id,
                               expected_version=row.version, next_state=state,
                               idempotency_key=key + state.value,
                               approver_subject="auth0|approver")
    store.publish(tenant_id=tenant, change_set_id=row.change_set_id,
                  expected_version=row.version, idempotency_key=key + "-publish")
    return load(store, row)


def test_r1_legacy_null_roundtrip(store):
    expected_events = json.loads(
        '[{"approver_subject":null,"author_subject":"auth0|author","base_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","catalog_digest":null,"change_set_id":"<uuid>","contract":"leaf.customization.audit.v1","event_id":"<uuid>","idempotency_key":"False","next_state":"created","platform_release":"platform@sha256:abc","prior_state":null,"reason_code":null,"result":"ok","staged_commit":null,"tenant_id":"tenant-a","ts":"<time>","workspace_contract_digest":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"},{"approver_subject":null,"author_subject":"auth0|author","base_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","catalog_digest":null,"change_set_id":"<uuid>","contract":"leaf.customization.audit.v1","event_id":"<uuid>","idempotency_key":"False-stage","next_state":"staging","platform_release":"platform@sha256:abc","prior_state":"created","reason_code":null,"result":"ok","staged_commit":null,"tenant_id":"tenant-a","ts":"<time>","workspace_contract_digest":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"},{"approver_subject":null,"author_subject":"auth0|author","base_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","catalog_digest":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","change_set_id":"<uuid>","contract":"leaf.customization.audit.v1","event_id":"<uuid>","idempotency_key":"False-complete","next_state":"staged","platform_release":"platform@sha256:abc","prior_state":"staging","reason_code":null,"result":"ok","staged_commit":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","tenant_id":"tenant-a","ts":"<time>","workspace_contract_digest":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"},{"approver_subject":null,"author_subject":"auth0|author","base_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","catalog_digest":null,"change_set_id":"<uuid>","contract":"leaf.customization.audit.v1","event_id":"<uuid>","idempotency_key":"True","next_state":"created","platform_release":"platform@sha256:abc","prior_state":null,"reason_code":null,"result":"ok","staged_commit":null,"tenant_id":"tenant-a","ts":"<time>","workspace_contract_digest":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"},{"approver_subject":null,"author_subject":"auth0|author","base_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","catalog_digest":null,"change_set_id":"<uuid>","contract":"leaf.customization.audit.v1","event_id":"<uuid>","idempotency_key":"True-stage","next_state":"staging","platform_release":"platform@sha256:abc","prior_state":"created","reason_code":null,"result":"ok","staged_commit":null,"tenant_id":"tenant-a","ts":"<time>","workspace_contract_digest":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"},{"approver_subject":null,"author_subject":"auth0|author","base_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","catalog_digest":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","change_set_id":"<uuid>","contract":"leaf.customization.audit.v1","event_id":"<uuid>","idempotency_key":"True-complete","next_state":"staged","platform_release":"platform@sha256:abc","prior_state":"staging","reason_code":null,"result":"ok","staged_commit":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","tenant_id":"tenant-a","ts":"<time>","workspace_contract_digest":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"}]'
    )
    expected_rows = json.loads(
        '[{"approver_subject":null,"author_subject":"auth0|author","authority_session_id":null,"authority_turn_id":null,"base_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","catalog_digest":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","change_kind":"create","desired_platform_release":"platform@sha256:abc","idempotency_key":"False","request_description":null,"request_fingerprint":null,"stage_attempt":0,"stage_error_code":null,"stage_error_message":null,"stage_error_retryable":0,"stage_heartbeat_at":null,"stage_lease_expires_at":null,"stage_lease_owner":null,"stage_next_attempt_at":0,"stage_phase":"staged","stage_started_at":null,"staged_commit":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","state":"staged","target_tool_name":null,"tenant_id":"tenant-a","version":2,"workspace_contract_digest":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"},{"approver_subject":null,"author_subject":"auth0|author","authority_session_id":null,"authority_turn_id":null,"base_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","catalog_digest":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","change_kind":"create","desired_platform_release":"platform@sha256:abc","idempotency_key":"True","request_description":"exact description","request_fingerprint":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","stage_attempt":0,"stage_error_code":null,"stage_error_message":null,"stage_error_retryable":0,"stage_heartbeat_at":null,"stage_lease_expires_at":null,"stage_lease_owner":null,"stage_next_attempt_at":0,"stage_phase":"staged","stage_started_at":null,"staged_commit":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","state":"staged","target_tool_name":null,"tenant_id":"tenant-a","version":2,"workspace_contract_digest":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"}]'
    )

    def masked(value):
        if isinstance(value, str):
            if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", value):
                return "<uuid>"
            if re.match(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T", value):
                return "<time>"
        return value

    for index, async_ in enumerate((False, True)):
        row = reserve(store, async_=async_, key=str(async_))
        row = row[0] if async_ else row
        before = persisted(store, row)
        staged = complete(store, staging(store, row, str(async_) + "-stage"),
                          idempotency_key=str(async_) + "-complete")
        assert (staged.request_graph_input, staged.base_catalog_change_set_id,
                staged.catalog_record_fields_json) == (None, None, None)
        for name in ("base_commit", "request_description", "request_fingerprint",
                     "authority_session_id", "authority_turn_id", "author_subject"):
            assert persisted(store, row)[name] == before[name]
        actual = persisted(store, row)
        assert {name: masked(actual[name]) for name in expected_rows[index]} == expected_rows[index]
        assert all(actual[name] is None for name in (
            "request_graph_input", "base_catalog_change_set_id", "catalog_record_fields_json"
        ))
        for event in audit_rows(store):
            assert json.dumps(json.loads(event["payload_json"]), sort_keys=True) == event["payload_json"]
    assert [
        {name: masked(value) for name, value in json.loads(event["payload_json"]).items()}
        for event in audit_rows(store)
    ] == expected_events


def test_r1_legacy_validation_precedes_database_access(store, monkeypatch):
    row = reserve(store)

    def fail_transaction():
        raise AssertionError("database touched")

    monkeypatch.setattr(store, "_transaction", fail_transaction)
    message = "catalog_digest must be a 64-character lowercase hexadecimal digest"
    with pytest.raises(ValueError, match=message):
        complete(store, row, catalog_digest="X")
    with pytest.raises(ValueError, match=message):
        complete(store, row, catalog_digest="X", tenant_id=[])


def test_r1_present_roundtrip(store):
    row, _ = reserve(store, async_=True, request_graph_input=G)
    row = complete(store, staging(store, row), catalog_record_fields_json=json.dumps(SNAPSHOT))
    reopened = SQLiteCustomizationStore(store.database_path)
    assert load(reopened, row).request_graph_input == G
    assert load(reopened, row).catalog_record_fields_json == S


def test_r1_reservation_exact_replay(store):
    predecessor = published(store)
    for async_ in (False, True):
        kwargs = dict(async_=async_, key=str(async_), request_graph_input=G,
                      base_catalog_change_set_id=predecessor.change_set_id)
        first = reserve(store, **kwargs)
        before = audit_rows(store)
        replay = reserve(store, **kwargs)
        if async_:
            assert first[1] is True and replay[1] is False
            first, replay = first[0], replay[0]
        assert first == replay
        assert audit_rows(store) == before


def replay_refusal(store, original, replay):
    for async_ in (False, True):
        kwargs = dict(async_=async_, key=str(async_))
        row = reserve(store, request_graph_input=original, **kwargs)
        row = row[0] if async_ else row
        before, events = persisted(store, row), audit_rows(store)
        with pytest.raises(IdempotencyReplayError):
            reserve(store, request_graph_input=replay, **kwargs)
        assert persisted(store, row) == before and audit_rows(store) == events


def test_r1_replay_add_declaration(store):
    replay_refusal(store, None, G)


def test_r1_replay_remove_declaration(store):
    replay_refusal(store, G, None)


def test_r1_replay_change_value(store):
    replay_refusal(store, G, "other")


def test_r1_new_invalid_declaration(store):
    for async_ in (False, True):
        for value in ("other", "", 1, " " + G):
            with pytest.raises(ToolRecordFieldError):
                reserve(store, async_=async_, request_graph_input=value)
    assert audit_rows(store) == []


def test_r1_predecessor_binding(store):
    predecessor = published(store)
    row = reserve(store, base_catalog_change_set_id=predecessor.change_set_id)
    assert load(store, row).base_catalog_change_set_id == predecessor.change_set_id
    rolled = store.transition(tenant_id="tenant-a", change_set_id=predecessor.change_set_id,
                              next_state=ChangeState.ROLLED_BACK, expected_version=predecessor.version,
                              idempotency_key="rollback")
    assert reserve(store, key="rolled", base_catalog_change_set_id=rolled.change_set_id).base_catalog_change_set_id == rolled.change_set_id


def test_r1_predecessor_refusal(store):
    wrong_commit = published(store, key="wrong-commit", commit=STAGED)
    foreign = published(store, key="foreign", tenant="tenant-b")
    wrong_state = reserve(store, key="wrong-state")
    self_id = str(uuid4())
    cases = [("malformed", ValueError, {}), (str(uuid4()), ChangeSetNotFoundError, {}),
             (foreign.change_set_id, ChangeSetNotFoundError, {}),
             (wrong_commit.change_set_id, ChangeSetConflictError, {}),
             (wrong_state.change_set_id, ChangeSetConflictError, {}),
             (self_id, ChangeSetConflictError, {"change_set_id": self_id})]
    before = audit_rows(store)
    with store._connection() as conn:
        count = conn.execute("SELECT count(*) AS n FROM customization_change_sets").fetchone()["n"]
    for predecessor, error, extra in cases:
        with pytest.raises(error):
            reserve(store, key="refused", base_catalog_change_set_id=predecessor, **extra)
    assert audit_rows(store) == before
    with store._connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM customization_change_sets").fetchone()["n"] == count


def test_r1_predecessor_replay(store):
    first = published(store, key="first")
    row = reserve(store, base_catalog_change_set_id=first.change_set_id)
    second = published(store, key="second")
    assert reserve(store, base_catalog_change_set_id=first.change_set_id) == row
    for value in (None, second.change_set_id, "invalid"):
        with pytest.raises(IdempotencyReplayError):
            reserve(store, base_catalog_change_set_id=value)


def test_r1_staging_requires_snapshot(store):
    predecessor = published(store)
    for index, metadata in enumerate(({"request_graph_input": G},
                                     {"base_catalog_change_set_id": predecessor.change_set_id})):
        row = staging(store, reserve(store, key=str(index), **metadata), str(index) + "-stage")
        before, events = persisted(store, row), audit_rows(store)
        with pytest.raises(ChangeSetConflictError):
            complete(store, row)
        with pytest.raises(InvalidTransitionError):
            store.transition(tenant_id=row.tenant_id, change_set_id=row.change_set_id,
                             expected_version=row.version, next_state=ChangeState.STAGED,
                             idempotency_key="bypass")
        assert persisted(store, row) == before and audit_rows(store) == events
        empty = canonicalize_catalog_record_fields({"schema": SNAPSHOT["schema"], "tools": {}})
        assert complete(store, row, idempotency_key=str(index) + "-complete",
                        catalog_record_fields_json=empty).catalog_record_fields_json == empty


def test_r1_staging_atomicity(store, monkeypatch):
    row, _ = reserve(store, async_=True, request_graph_input=G)
    staging(store, row)
    row = store.claim_stage(owner="worker", lease_seconds=60)
    assert row is not None
    before, events = persisted(store, row), audit_rows(store)
    original = store._append_audit

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(store, "_append_audit", fail)
    with pytest.raises(RuntimeError, match="injected"):
        complete(store, row, catalog_record_fields_json=S,
                 stage_lease_owner="worker", stage_attempt=row.stage_attempt)
    assert persisted(store, row) == before and audit_rows(store) == events


def test_r1_staging_immutable_replay(store):
    row = staging(store, reserve(store, request_graph_input=G))
    first = complete(store, row, catalog_record_fields_json=S)
    before, events = persisted(store, row), audit_rows(store)
    assert complete(store, row, catalog_record_fields_json=json.dumps(SNAPSHOT, indent=2)) == first
    changed = copy.deepcopy(SNAPSHOT)
    changed["tools"]["drape-onto-spheres"]["version"] = "2.0.0"
    for overrides in ({"catalog_record_fields_json": None},
                      {"catalog_record_fields_json": json.dumps(changed)},
                      {"staged_commit": "e" * 40}, {"catalog_digest": "f" * 64},
                      {"platform_release": "different"}, {"workspace_contract_digest": "e" * 64}):
        kwargs = {"catalog_record_fields_json": S, **overrides}
        with pytest.raises(IdempotencyReplayError):
            complete(store, row, **kwargs)
    with pytest.raises(ChangeSetConflictError):
        complete(store, first, idempotency_key="later", catalog_record_fields_json=S)
    assert persisted(store, row) == before and audit_rows(store) == events


def test_r1_staging_replay_malformed(store):
    row = staging(store, reserve(store, request_graph_input=G))
    empty = canonicalize_catalog_record_fields({"schema": SNAPSHOT["schema"], "tools": {}})
    first = complete(store, row, idempotency_key="done", catalog_record_fields_json=empty)
    before, events = persisted(store, row), audit_rows(store)
    unsupported = copy.deepcopy(SNAPSHOT)
    unsupported["tools"]["drape-onto-spheres"]["graph_input"] = "other"
    for overrides in ({"catalog_record_fields_json": "{"},
                      {"catalog_record_fields_json": json.dumps(unsupported)},
                      {"catalog_digest": "X"}):
        with pytest.raises(IdempotencyReplayError):
            complete(store, row, **{"idempotency_key": "done",
                                    "catalog_record_fields_json": empty, **overrides})
        assert persisted(store, row) == before and audit_rows(store) == events
    with pytest.raises(ChangeSetConflictError):
        complete(store, first, idempotency_key="later", catalog_record_fields_json="{")
    assert persisted(store, row) == before and audit_rows(store) == events


def test_r1_corrupt_readback_huge_integer(store):
    row = reserve(store, request_graph_input=G)
    text = json.dumps(SNAPSHOT).replace('"version": "1.0.0"', '"version": ' + "9" * 5000)
    with store._transaction() as conn:
        conn.execute("UPDATE customization_change_sets SET catalog_record_fields_json = ? WHERE change_set_id = ?",
                     (text, row.change_set_id))
    with pytest.raises(ToolRecordFieldError) as caught:
        load(store, row)
    assert caught.value.field == "catalog_record_fields_json"


def test_r1_transition_invalid_state_legacy(store):
    with pytest.raises(ChangeSetNotFoundError, match="change set was not found"):
        store.transition(tenant_id="tenant-a", change_set_id=str(uuid4()),
                         next_state="bogus", expected_version=0, idempotency_key="invalid")


def test_r1_stale_worker_and_corrupt_readback(store, monkeypatch):
    row, _ = reserve(store, async_=True, request_graph_input=G)
    staging(store, row)
    clock = [1000.0]
    monkeypatch.setattr(customization_store.time, "time", lambda: clock[0])
    first = store.claim_stage(owner="old", lease_seconds=10)
    clock[0] += 11
    second = store.claim_stage(owner="new", lease_seconds=10)
    assert first and second and second.stage_attempt > first.stage_attempt
    with pytest.raises(ChangeSetConflictError):
        complete(store, first, catalog_record_fields_json=S,
                 stage_lease_owner="old", stage_attempt=first.stage_attempt)
    complete(store, second, catalog_record_fields_json=S,
             stage_lease_owner="new", stage_attempt=second.stage_attempt)
    with store._transaction() as conn:
        conn.execute("UPDATE customization_change_sets SET catalog_record_fields_json = ? WHERE change_set_id = ?",
                     ('{"schema":null}', first.change_set_id))
    with pytest.raises(ToolRecordFieldError):
        load(store, first)


def test_r1_sqlite_upgrade(tmp_path):
    for partial in (False, True):
        path = tmp_path / (str(partial) + ".db")
        legacy_schema = customization_store._SCHEMA
        for name in ("request_graph_input", "base_catalog_change_set_id", "catalog_record_fields_json"):
            legacy_schema = legacy_schema.replace("  " + name + " TEXT,\n", "")
        row_id = str(uuid4())
        with sqlite3.connect(path) as conn:
            conn.executescript(legacy_schema)
            conn.execute("INSERT INTO customization_change_sets "
                         "(change_set_id, tenant_id, idempotency_key, state, version, base_commit, "
                         "desired_platform_release, workspace_contract_digest, author_subject, request_description) "
                         "VALUES (?, 'tenant-a', 'old', 'created', 0, ?, 'platform@sha256:abc', ?, 'author', ?)",
                         (row_id, BASE, WORKSPACE, " untouched bytes "))
            if partial:
                conn.execute("ALTER TABLE customization_change_sets ADD COLUMN catalog_record_fields_json TEXT")
                conn.execute("UPDATE customization_change_sets SET catalog_record_fields_json = ?", (json.dumps(SNAPSHOT, indent=2),))
            before = dict(zip([item[0] for item in conn.execute("SELECT * FROM customization_change_sets").description],
                              conn.execute("SELECT * FROM customization_change_sets").fetchone()))
        for _ in range(2):
            upgraded = SQLiteCustomizationStore(path)
            upgraded.initialize()
            row = upgraded.get_change_set(tenant_id="tenant-a", change_set_id=row_id)
            after = persisted(upgraded, row)
            assert all(after[key] == value for key, value in before.items())
            assert row.request_graph_input is None and row.base_catalog_change_set_id is None
            assert row.catalog_record_fields_json == (S if partial else None)


def test_r1_reconcile_projection(store, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "record_fields_reconcile", Path(__file__).resolve().parents[2] / "scripts" / "reconcile_customization_authority.py")
    assert spec and spec.loader
    reconcile = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reconcile)
    assert reconcile.TABLE_COLUMNS["customization_change_sets"][-9:] == (
        "change_kind", "target_tool_name", "request_description", "request_fingerprint",
        "authority_session_id", "authority_turn_id", "request_graph_input",
        "base_catalog_change_set_id", "catalog_record_fields_json")
    predecessor = published(store)
    row = staging(store, reserve(store, async_=True, request_graph_input=G,
                                 base_catalog_change_set_id=predecessor.change_set_id)[0])
    complete(store, row, catalog_record_fields_json=S)
    source = reconcile._sqlite_snapshot(Path(store.database_path))

    class Connection:
        def __init__(self):
            self.snapshot = {table: [] for table in reconcile.TABLE_COLUMNS}
            self.inserts = 0

        def execute(self, statement, values=()):
            if statement.startswith("SELECT pg_advisory_xact_lock"):
                return
            assert statement.startswith("INSERT INTO ")
            table = statement.split()[2]
            self.snapshot[table].append(dict(zip(reconcile.TABLE_COLUMNS[table], values, strict=True)))
            self.inserts += 1

    class Database:
        def __init__(self):
            self.connection = Connection()

        def assert_schema_current(self):
            pass

        @contextmanager
        def transaction(self, *, isolation):
            assert isolation == "serializable"
            before = copy.deepcopy(self.connection.snapshot)
            try:
                yield self.connection
            except Exception:
                self.connection.snapshot = before
                raise

    database = Database()
    monkeypatch.setattr(reconcile, "_platform_db", lambda: database)
    monkeypatch.setattr(reconcile, "_postgres_snapshot", lambda conn: copy.deepcopy(conn.snapshot))
    assert reconcile.reconcile(sqlite_path=Path(store.database_path), mode="backfill")["parity"]
    assert database.connection.snapshot == source
    copied = next(item for item in database.connection.snapshot["customization_change_sets"]
                  if item["change_set_id"] == row.change_set_id)
    assert (copied["request_graph_input"], copied["base_catalog_change_set_id"], copied["catalog_record_fields_json"]) == (G, predecessor.change_set_id, S)
    inserts = database.connection.inserts
    reconcile.reconcile(sqlite_path=Path(store.database_path), mode="backfill")
    assert database.connection.inserts == inserts
    copied["request_description"] = "changed target bytes"
    before = copy.deepcopy(database.connection.snapshot)
    with pytest.raises(RuntimeError, match="customization authority has a conflicting row in"):
        reconcile.reconcile(sqlite_path=Path(store.database_path), mode="backfill")
    assert database.connection.snapshot == before
