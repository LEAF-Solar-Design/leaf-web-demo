"""PostgreSQL contract tests for the atomic operator identity pair rail."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import uuid

import pytest

from leaf_platform import db, store


def _operator(environment: str = "staging") -> str:
    subject = f"auth0|pair-operator-{uuid.uuid4().hex}"
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO operator_principals "
            "(subject, role, role_revision, status, profiles, environment,"
            " granted_by) "
            "VALUES (%s, 'operator', 1, 'active', ARRAY['default'], %s,"
            " 'pair-test-harness')",
            (subject, environment),
        )
    return subject


def _request(org_a, subject_a: str, org_b, subject_b: str):
    return [
        {"tenant_id": org_a.org_id, "subject": subject_a,
         "role": "read_only"},
        {"tenant_id": org_b.org_id, "subject": subject_b,
         "role": "read_only"},
    ]


def _bind(pair, operator: str, key: str = "pair-test-key"):
    return store.bind_identity_pair(
        pair, operator_subject=operator, operator_role_revision=1,
        environment="staging", idempotency_key=key)


def test_pair_create_replay_preserves_rows_and_hashes_targets(make_org):
    org_a, org_b = make_org("Pair A"), make_org("Pair B")
    subject_a = f"auth0|synthetic-a-{uuid.uuid4().hex}"
    subject_b = f"auth0|synthetic-b-{uuid.uuid4().hex}"
    operator = _operator()
    pair = _request(org_a, subject_a, org_b, subject_b)

    created = _bind(pair, operator)
    replayed = _bind(pair, operator, "pair-replay-key")

    assert {row["state"] for row in created["bindings"]} == {"created"}
    assert {row["state"] for row in replayed["bindings"]} == {"preserved"}
    assert {row["binding_id"] for row in created["bindings"]} == {
        row["binding_id"] for row in replayed["bindings"]}
    with db.cursor() as cur:
        cur.execute(
            "SELECT external_subject, role FROM identity_bindings "
            "WHERE external_subject = ANY(%s) AND status = 'active'",
            ([subject_a, subject_b],),
        )
        assert {row["role"] for row in cur.fetchall()} == {"read_only"}
        cur.execute(
            "SELECT args_hash, environment, extra::text AS extra "
            "FROM operator_security_audit "
            "WHERE subject = %s AND action = 'operator.identity_bind_pair' "
            "ORDER BY audit_id",
            (operator,),
        )
        audits = cur.fetchall()
    assert len(audits) == 2
    expected_subject_hashes = sorted(
        hashlib.sha256(value.encode("utf-8")).hexdigest()
        for value in (subject_a, subject_b)
    )
    expected_tenant_hashes = sorted(
        hashlib.sha256(str(value).encode("utf-8")).hexdigest()
        for value in (org_a.org_id, org_b.org_id)
    )
    for row, key in zip(audits, ("pair-test-key", "pair-replay-key"), strict=True):
        assert row["environment"] == "staging"
        payload = json.loads(row["extra"])
        assert payload == {
            "idempotency_key_sha256": hashlib.sha256(key.encode("utf-8")).hexdigest(),
            "subject_sha256": expected_subject_hashes,
            "tenant_sha256": expected_tenant_hashes,
        }
        expected_args_hash = hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        assert row["args_hash"] == expected_args_hash
        for raw in (
            subject_a, subject_b, str(org_a.org_id), str(org_b.org_id), key,
        ):
            assert raw not in row["extra"]
        assert "staging" not in row["extra"]
        assert "read_only" not in row["extra"]


def test_pair_conflict_preserves_exact_preexisting_row(make_org):
    org_a, org_b, org_c = make_org("Pair A"), make_org("Pair B"), make_org("Pair C")
    subject_a = f"auth0|synthetic-a-{uuid.uuid4().hex}"
    subject_b = f"auth0|synthetic-b-{uuid.uuid4().hex}"
    operator = _operator()
    first = _bind(_request(org_a, subject_a, org_b, subject_b), operator)
    binding_a = next(row for row in first["bindings"]
                     if row["tenant_id"] == org_a.org_id)["binding_id"]

    with pytest.raises(store.IdentityBindingPairConflict):
        _bind(_request(org_c, subject_a, org_b, subject_b), operator,
              "pair-conflict-key")

    current = store.resolve_active_identity_binding("auth0", subject_a)
    assert current.binding_id == binding_a
    assert current.platform_tenant_id == org_a.org_id


def test_second_uuid_failure_rolls_back_first_insert_and_audit(make_org, monkeypatch):
    org_a, org_b = make_org("Rollback A"), make_org("Rollback B")
    subject_a = f"auth0|rollback-a-{uuid.uuid4().hex}"
    subject_b = f"auth0|rollback-b-{uuid.uuid4().hex}"
    operator = _operator()
    calls = 0

    def fail_second_uuid():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected UUID allocation failure")
        return uuid.uuid4()

    monkeypatch.setattr(store, "new_uuid", fail_second_uuid)
    with pytest.raises(RuntimeError, match="injected UUID allocation failure"):
        _bind(_request(org_a, subject_a, org_b, subject_b), operator,
              "pair-rollback-key")

    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM identity_bindings "
            "WHERE external_subject = ANY(%s) AND status = 'active'",
            ([subject_a, subject_b],),
        )
        assert cur.fetchone()["n"] == 0
        cur.execute(
            "SELECT count(*) AS n FROM operator_security_audit "
            "WHERE subject = %s AND action = 'operator.identity_bind_pair'",
            (operator,),
        )
        assert cur.fetchone()["n"] == 0


def test_concurrent_conflicting_pair_has_one_complete_winner(make_org):
    org_a, org_b, org_c = make_org("Race A"), make_org("Race B"), make_org("Race C")
    shared = f"auth0|race-shared-{uuid.uuid4().hex}"
    subject_b = f"auth0|race-b-{uuid.uuid4().hex}"
    subject_c = f"auth0|race-c-{uuid.uuid4().hex}"
    operator = _operator()
    pairs = [
        _request(org_a, shared, org_b, subject_b),
        _request(org_c, shared, org_b, subject_c),
    ]

    def attempt(index):
        try:
            _bind(pairs[index], operator, f"pair-race-key-{index}")
            return "ok"
        except store.IdentityBindingPairConflict:
            return "conflict"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, (0, 1)))
    assert sorted(outcomes) == ["conflict", "ok"]
    with db.cursor() as cur:
        cur.execute(
            "SELECT external_subject FROM identity_bindings "
            "WHERE external_subject = ANY(%s) AND status = 'active'",
            ([shared, subject_b, subject_c],),
        )
        subjects = {row["external_subject"] for row in cur.fetchall()}
    assert shared in subjects and len(subjects) == 2
