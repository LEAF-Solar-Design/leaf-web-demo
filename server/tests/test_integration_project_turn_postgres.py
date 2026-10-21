"""Opt-in project producer custody proof; starts AFTER JWT verification.

Only KMS is synthetic. Sessions, identity, membership, capture, authority and
encrypted persistence all use their production PostgreSQL paths.
"""
from dataclasses import replace
from functools import partial
from pathlib import Path
import sys
import time

import pytest
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import platform_link

platform_link._ensure_platform_package()
import deps
import integration_tool_service as service
import integration_turn_grant_crypto as crypto
import integration_turn_grants as grants
import session_store
from leaf_platform.integration_turn_grant_store import Authority, CURRENT_OPERATIONS, IntegrationTurnGrantStore
from server.tests.integration_project_postgres_fixture import (
    connection_factory, project_database, scoped,
)
from server.tests.test_integration_turn_grant_crypto import ARN, FakeKeys


ORIGIN = "https://integration.example"
TURN = "colliding-turn-label"
DRAWING = "colliding-drawing-id"


class ContextKeys:
    """One existing crypto-test fake per grant, preserving its context checks."""
    def __init__(self):
        self.keys = {}

    def canonical_key_arn(self):
        return ARN

    def generate_data_key(self, key_arn, context):
        grant = context["grant_id"]
        assert grant not in self.keys
        self.keys[grant] = FakeKeys()
        return self.keys[grant].generate_data_key(key_arn, context)

    def decrypt_data_key(self, key_arn, wrapped, context):
        return self.keys[context["grant_id"]].decrypt_data_key(key_arn, wrapped, context)


def _callback(scope, token):
    tenant = deps.TenantContext(scope.org, org_id=scope.org, subject=scope.subject,
                                tier="hosted_starter", authority_resolved=True)
    request = Request({"type": "http", "method": "POST", "path": "/synthetic",
                       "headers": [(b"authorization", ("Bearer " + token).encode())]})
    request.state._integration_verified_expiry = deps._VerifiedPlatformExpiry(
        scope.subject, time.time() + 240)
    callback = grants.for_request(request, tenant)
    assert callback is not None
    return callback


def test_project_producer_custody_and_current_authority(
        project_database, monkeypatch, caplog, capsys):
    h = project_database
    for name, value in {
        "LEAF_SESSIONS_STORE": "postgres", "LEAF_AUTH_LIVE": "1",
        "LEAF_INTEGRATION_TURN_GRANTS_ENABLED": "1",
        "LEAF_INTEGRATION_SERVICE_ORIGIN": ORIGIN,
        "LEAF_INTEGRATION_TURN_GRANTS_KMS_KEY_ID": "alias/synthetic-project-test",
        "AWS_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(grants, "_references", {})
    keys = ContextKeys()
    monkeypatch.setattr(crypto, "KmsKeyService", lambda: keys)
    # Keep S11 _adapters, seal, the grant store and its default pool connection.
    reader = IntegrationTurnGrantStore(partial(connection_factory, h.connections[1]))
    sessions, authorities, tokens = [], [], []
    for index, scope in enumerate(h.scopes):
        session = session_store.get_or_create_session(
            scope.org, DRAWING, org_id=scope.org, project_id=scope.project)
        sid = session["session_id"]
        sessions.append(sid)
        assert session["tenant_id"] == scope.org
        assert session_store.try_begin_turn(sid, TURN, 300, tier="hosted_starter",
                                             subject=scope.subject)
        with h.db.cursor() as cur:
            cur.execute("SELECT tenant_id FROM public.app_sessions WHERE session_id=%s", (sid,))
            marker = cur.fetchone()["tenant_id"]
        assert marker == f"project:{scope.org}:{scope.project}"
        assert session_store.get_session(sid)["tenant_id"] == scope.org
        for tenant in (scope.org, marker):
            assert session_store.active_turn_subject(sid, TURN, tenant, 300) == scope.subject
        other = h.scopes[1 - index]
        assert session_store.active_turn_subject(sid, TURN, other.org, 300) is None
        assert session_store.active_turn_subject(sid, "wrong-turn", scope.org, 300) is None
        authority = Authority(scope.org, scope.project, scope.user, scope.binding,
                              "auth0", scope.subject, sid, TURN, ORIGIN, operations=CURRENT_OPERATIONS)
        authorities.append(authority)
        token = "S13P-SYNTHETIC-PRIVATE-BEARER-" + str(index)
        tokens.append(token)
        callback = _callback(scope, token)
        result = callback(sid, TURN, 300)
        assert result.code == "captured"
        assert callback(sid, TURN, 300).code == "unavailable"
        assert service.resolve_current(scope.org, sid, TURN, write=True).authority == authority
        loaded = reader.load_active(authority, time.time())
        assert loaded is not None and loaded.binding.authority == authority
        assert loaded.binding.grant_id == result.grant_id
        exposed = []
        def consume(value):
            exposed.append(value)
            return value == token.encode()
        assert crypto.with_decrypted(loaded.envelope, loaded.binding, consume, keys)
        assert not any(exposed[0])
        assert token.encode() not in loaded.envelope

    assert sessions[0] != sessions[1]
    assert h.scopes[0].subject != h.scopes[1].subject
    for index, authority in enumerate(authorities):
        conn = h.connections[index]
        role = conn.execute("SELECT rolsuper,rolbypassrls FROM pg_roles "
                            "WHERE rolname=current_user").fetchone()
        assert role == (False, False)
        assert conn.execute("SELECT count(*) FROM pg_class WHERE relnamespace="
                            "'public'::regnamespace AND relowner=(SELECT oid FROM pg_roles "
                            "WHERE rolname=current_user)").fetchone()[0] == 0
        assert conn.execute("SELECT relrowsecurity,relforcerowsecurity FROM pg_class "
                            "WHERE oid='public.integration_turn_grants'::regclass").fetchone() == (True, True)
        # A raw SELECT without a WHERE must be scoped by FORCE RLS.
        assert conn.execute("SELECT count(*) FROM public.integration_turn_grants").fetchone()[0] == 0
        with scoped(conn, authority):
            rows = conn.execute("SELECT tenant_id::text,project_id::text FROM "
                                "public.integration_turn_grants").fetchall()
            assert rows == [(authority.tenant_id, authority.project_id)]
        other = authorities[1 - index]
        with pytest.raises(service.ReadFailure, match="denied"):
            service.resolve_current(authority.tenant_id, other.session_id, TURN, write=True)
        assert _callback(h.scopes[index], tokens[index])(other.session_id, TURN, 300).code == "unavailable"
        assert reader.load_active(replace(authority, session_id=other.session_id), time.time()) is None
        assert reader.load_active(replace(authority, subject=other.subject), time.time()) is None

    first, second = authorities
    # Revocation must be observed by fresh authority reads while the encrypted
    # row and active session still exist. The sibling organization stays valid.
    h.owner.execute("UPDATE public.project_member_bindings SET status='revoked',revoked_at=NOW() "
                    "WHERE membership_id=%s", (h.scopes[0].membership,))
    assert reader.load_active(first, time.time()) is not None
    with pytest.raises(service.ReadFailure, match="denied"):
        service.resolve_current(first.tenant_id, first.session_id, TURN, write=True)
    assert _callback(h.scopes[0], tokens[0])(first.session_id, TURN, 300).code == "unavailable"
    assert service.resolve_current(second.tenant_id, second.session_id, TURN, write=True).authority == second
    sibling = reader.load_active(second, time.time())
    assert sibling is not None
    assert crypto.with_decrypted(sibling.envelope, sibling.binding,
                                 lambda value: value == tokens[1].encode(), keys)

    # Replace the remaining authorized turn through the real producer.
    session_store.end_turn(second.session_id, TURN)
    assert session_store.try_begin_turn(second.session_id, "replacement-turn", 300,
                                       tier="hosted_starter", subject=second.subject)
    with pytest.raises(service.ReadFailure, match="conflict"):
        service.resolve_current(second.tenant_id, second.session_id, TURN, write=True)
    assert _callback(h.scopes[1], tokens[1])(second.session_id, TURN, 300).code == "unavailable"
    current_second = replace(second, turn_id="replacement-turn")
    assert service.resolve_current(second.tenant_id, second.session_id, "replacement-turn",
                                   write=True).authority == current_second

    # Real release invokes S11 cleanup and scrubs the encrypted material.
    for authority in (first, current_second):
        original = authorities[0] if authority == first else second
        session_store.end_turn(authority.session_id, authority.turn_id)
        assert reader.load_active(original, time.time()) is None
        with scoped(h.connections[1], original):
            row = h.connections[1].execute("SELECT envelope,revoked_at FROM "
                                           "public.integration_turn_grants").fetchone()
            assert row[0] is None and row[1] is not None
        assert session_store.get_session(authority.session_id)["active_turn_id"] is None
    assert all(not any(buf) for fake in keys.keys.values() for buf in fake.buffers)
    output = caplog.text + str(capsys.readouterr())
    assert all(token not in output for token in tokens)
    assert "postgresql://" not in output and "postgres://" not in output
