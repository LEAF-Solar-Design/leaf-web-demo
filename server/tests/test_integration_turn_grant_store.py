"""SQL/guard unit checks; the separate disposable-Postgres suite proves RLS."""
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from server.tests.test_integration_turn_grant_crypto import binding, FakeKeys
from server.integration_turn_grant_crypto import seal
from leaf_platform.integration_turn_grant_store import (
    Authority, Binding, IntegrationTurnGrantStore, GrantStoreError, TenantProject,
    LEGACY_OPERATIONS, CURRENT_OPERATIONS, OPERATIONS, authority_compatible,
)


class Cursor:
    def __init__(self, rows):
        self.rows = iter(rows)
        self.statements = []
        self.rowcount = 1
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, sql, params=None): self.statements.append((sql, params))
    def fetchone(self): return next(self.rows)


class Connection:
    def __init__(self, rows, status=0):
        self.cur = Cursor(rows)
        self.info = SimpleNamespace(transaction_status=status)
        self.transactions = 0
    def cursor(self, **kwargs): return self.cur
    @contextmanager
    def transaction(self):
        self.transactions += 1
        yield


def store(rows, status=0):
    conn = Connection(rows, status)
    @contextmanager
    def factory(): yield conn
    return IntegrationTurnGrantStore(factory), conn


def test_put_is_parameterized_scoped_bounded_and_no_crypto_in_transaction():
    b = binding()
    envelope = seal(b"bearer", b, FakeKeys())
    s, conn = store([{"safe": True}, {"grant_id": b.grant_id}])
    assert s.put_once(b, envelope) == "created"
    sqls = conn.cur.statements
    assert conn.transactions == 1
    assert "statement_timeout" in sqls[0][0] and "1000ms" in sqls[0][0]
    assert "lock_timeout" in sqls[1][0]
    assert "set_config" in sqls[4][0] and sqls[4][1] == (b.authority.tenant_id, b.authority.project_id)
    assert "ON CONFLICT DO NOTHING" in sqls[5][0]
    assert all(b.authority.tenant_id not in sql for sql, _ in sqls)
    assert envelope == sqls[5][1][-1]


@pytest.mark.parametrize("safe", [False, None])
def test_guard_rejects_unsafe_or_missing_table(safe):
    s, conn = store([None if safe is None else {"safe": safe}])
    with pytest.raises(GrantStoreError, match="GRANT_UNSAFE_DATABASE"):
        s.load_active(binding().authority, 1001)
    assert len(conn.cur.statements) == 4


def test_guard_refuses_outer_transaction_and_sanitizes_provider_errors():
    s, _ = store([], status=2)
    with pytest.raises(GrantStoreError, match="GRANT_UNSAFE_DATABASE"):
        s.load_active(binding().authority, 1001)
    def failure(): raise RuntimeError("postgres://private-password token")
    with pytest.raises(GrantStoreError) as exc:
        IntegrationTurnGrantStore(failure).load_active(binding().authority, 1001)
    assert str(exc.value) == "GRANT_UNAVAILABLE" and exc.value.__cause__ is None
    assert exc.value.__context__ is None

    s, conn = store([])
    def statement_failure(*args, **kwargs):
        raise RuntimeError("statement private-password token")
    conn.cur.execute = statement_failure
    with pytest.raises(GrantStoreError) as statement_exc:
        s.load_active(binding().authority, 1001)
    assert str(statement_exc.value) == "GRANT_UNAVAILABLE"
    assert statement_exc.value.__cause__ is None
    assert statement_exc.value.__context__ is None


def test_invalid_input_has_no_database_side_effect():
    def forbidden(): pytest.fail("database touched")
    s = IntegrationTurnGrantStore(forbidden)
    with pytest.raises(GrantStoreError): s.put_once(binding(), b"not-envelope")
    with pytest.raises(GrantStoreError): s.load_active(binding().authority, float("nan"))
    with pytest.raises(GrantStoreError): s.purge_expired(TenantProject(
        binding().authority.tenant_id, binding().authority.project_id), 1001, 101)


@pytest.mark.parametrize("changed", [False, True])
def test_duplicate_never_updates_or_extends(changed):
    b = binding()
    envelope = seal(b"bearer", b, FakeKeys())
    row = {"binding": b.to_dict(), "envelope": envelope, "revoked_at": None}
    s, conn = store([{"safe": True}, None, row])
    candidate = replace(b, expires_at=1101) if changed else b
    assert s.put_once(candidate, envelope) == ("conflict" if changed else "already_present")
    assert not any("UPDATE" in sql for sql, _ in conn.cur.statements)


def test_load_matches_all_authority_fields_and_validates_encrypted_result():
    b = binding()
    envelope = seal(b"bearer", b, FakeKeys())
    row = {"binding": b.to_dict(), "envelope": envelope}
    s, _ = store([{"safe": True}, row])
    result = s.load_active(b.authority, 1001)
    assert result.binding == b and result.envelope == envelope
    s, _ = store([{"safe": True}, row])
    assert s.load_active(replace(b.authority, subject="auth0|foreign"), 1001) is None


def test_revoke_and_purge_are_scoped_scrubs():
    b = binding()
    s, conn = store([{"safe": True}])
    assert s.revoke_turn(b.authority, 1002) == 1
    sql, args = conn.cur.statements[-1]
    assert "envelope=NULL" in sql and "binding @> %s::jsonb" in sql
    assert args[1:3] == (b.authority.tenant_id, b.authority.project_id)
    s, conn = store([{"safe": True}])
    assert s.purge_expired(TenantProject(b.authority.tenant_id, b.authority.project_id), 1101) == 1
    sql, args = conn.cur.statements[-1]
    assert "LIMIT %s FOR UPDATE SKIP LOCKED" in sql
    assert args == (b.authority.tenant_id, b.authority.project_id, 1101, 100)


def test_binding_json_is_closed_and_canonical():
    b = binding()
    assert Binding.from_dict(b.to_dict()) == b
    for field in b.to_dict():
        missing = b.to_dict()
        missing.pop(field)
        with pytest.raises(GrantStoreError):
            Binding.from_dict(missing)
    extra = b.to_dict()
    extra["request_controlled_config"] = "forbidden"
    with pytest.raises(GrantStoreError):
        Binding.from_dict(extra)


@pytest.mark.parametrize("operations", [LEGACY_OPERATIONS, CURRENT_OPERATIONS])
def test_exact_operation_vectors_round_trip_without_changing_binding_bytes(operations):
    b = binding()
    b = replace(b, authority=replace(b.authority, operations=operations))
    original = b.canonical_bytes()
    restored = Binding.from_dict(b.to_dict())
    assert restored == b and restored.canonical_bytes() == original
    assert restored.schema == 1 and restored.authority.operations == operations
    assert OPERATIONS is LEGACY_OPERATIONS
    assert Authority.__dataclass_fields__["operations"].default is LEGACY_OPERATIONS


@pytest.mark.parametrize("operations", [
    (), ("dataset_query",), ("action_discovery",),
    tuple(reversed(LEGACY_OPERATIONS)), tuple(reversed(CURRENT_OPERATIONS)),
    LEGACY_OPERATIONS + ("element_save",), CURRENT_OPERATIONS + ("action_discovery",),
    LEGACY_OPERATIONS + ("unknown",), list(LEGACY_OPERATIONS), list(CURRENT_OPERATIONS),
    (1, 2, 3), (None,), "dataset_query", None,
])
def test_other_operation_vectors_fail_before_database_or_crypto(monkeypatch, operations):
    from leaf_platform import integration_turn_grant_store as module
    def forbidden(*args): pytest.fail("database or envelope work")
    s = IntegrationTurnGrantStore(forbidden)
    monkeypatch.setattr(module, "envelope_bytes", forbidden)
    b = binding()
    with pytest.raises(GrantStoreError):
        replace(b.authority, operations=operations)
    # Revalidate even a frozen value corrupted by internal code.
    object.__setattr__(b.authority, "operations", operations)
    with pytest.raises(GrantStoreError): s.put_once(b, b"unused")
    with pytest.raises(GrantStoreError): s.load_active(b.authority, 1001)
    with pytest.raises(GrantStoreError): authority_compatible(b.authority, binding().authority)
    with pytest.raises(GrantStoreError): authority_compatible(binding().authority, b.authority)


def test_legacy_canonical_bytes_remain_schema_one():
    a = Authority("11111111-1111-4111-8111-111111111111",
                  "22222222-2222-4222-8222-222222222222",
                  "33333333-3333-4333-8333-333333333333",
                  "44444444-4444-4444-8444-444444444444",
                  "auth0", "auth0|trusted", "s", "t", "https://api.example.com")
    b = Binding(a, "55555555-5555-4555-8555-555555555555", 1000, 1120, 1110, 1100)
    expected = (
        b'{"binding_id":"44444444-4444-4444-8444-444444444444","expires_at":1100,'
        b'"external_authority":"auth0","grant_id":"55555555-5555-4555-8555-555555555555",'
        b'"issued_at":1000,"operations":["dataset_discovery","dataset_query","element_save"],'
        b'"origin":"https://api.example.com","platform_user_id":"33333333-3333-4333-8333-333333333333",'
        b'"project_id":"22222222-2222-4222-8222-222222222222","purpose":"studio-integration-turn-grant",'
        b'"schema":1,"session_id":"s","subject":"auth0|trusted",'
        b'"tenant_id":"11111111-1111-4111-8111-111111111111","turn_deadline":1110,'
        b'"turn_id":"t","verified_jwt_exp":1120}'
    )
    assert b.canonical_bytes() == expected
    assert Binding.from_dict(b.to_dict()).canonical_bytes() == expected


@pytest.mark.parametrize("stored_ops,current_ops,allowed", [
    (LEGACY_OPERATIONS, LEGACY_OPERATIONS, True),
    (LEGACY_OPERATIONS, CURRENT_OPERATIONS, True),
    (CURRENT_OPERATIONS, CURRENT_OPERATIONS, True),
    (CURRENT_OPERATIONS, LEGACY_OPERATIONS, False),
])
def test_load_compatibility_is_directional_and_preserves_encrypted_binding(stored_ops, current_ops, allowed):
    b = binding()
    b = replace(b, authority=replace(b.authority, operations=stored_ops))
    original = b.canonical_bytes()
    envelope = seal(b"bearer", b, FakeKeys())
    current = replace(b.authority, operations=current_ops)
    assert authority_compatible(b.authority, current) is allowed
    s, _ = store([{"safe": True}, {"binding": b.to_dict(), "envelope": envelope}])
    loaded = s.load_active(current, 1001)
    if allowed:
        assert loaded.binding == b and loaded.binding.canonical_bytes() == original
        assert loaded.envelope == envelope
    else:
        assert loaded is None


@pytest.mark.parametrize("field,value", [
    ("tenant_id", "11111111-1111-4111-8111-111111111111"),
    ("project_id", "11111111-1111-4111-8111-111111111111"),
    ("platform_user_id", "11111111-1111-4111-8111-111111111111"),
    ("binding_id", "11111111-1111-4111-8111-111111111111"),
    ("subject", "auth0|foreign"), ("session_id", "foreign-session"),
    ("turn_id", "foreign-turn"), ("origin", "https://foreign.example.com"),
])
def test_legacy_compatibility_never_relaxes_identity(field, value):
    b = binding()
    current = replace(b.authority, operations=CURRENT_OPERATIONS, **{field: value})
    assert not authority_compatible(b.authority, current)
    envelope = seal(b"bearer", b, FakeKeys())
    s, _ = store([{"safe": True}, {"binding": b.to_dict(), "envelope": envelope}])
    assert s.load_active(current, 1001) is None


def test_duplicate_current_grant_cannot_upgrade_legacy_turn():
    b = binding()
    envelope = seal(b"bearer", b, FakeKeys())
    candidate = replace(b, authority=replace(b.authority, operations=CURRENT_OPERATIONS))
    candidate_envelope = seal(b"bearer", candidate, FakeKeys())
    row = {"binding": b.to_dict(), "envelope": envelope, "revoked_at": None}
    s, conn = store([{"safe": True}, None, row])
    assert s.put_once(candidate, candidate_envelope) == "conflict"
    assert row["binding"] == b.to_dict() and row["envelope"] == envelope
    assert not any("UPDATE" in sql for sql, _ in conn.cur.statements)
