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
    Binding, IntegrationTurnGrantStore, GrantStoreError, TenantProject,
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
