"""O2/O3 BEHAVIORAL: a generic operator handler cannot CALL a production deploy
route (contract/OPERATOR.md section 7.2-7.3).

These are mutation tests against the reproduced counterexamples: the exact
neutral-helper (ship() -> vercel promote) and existing-handler (ECS / HTTP)
paths must be DENIED at runtime, not merely absent from a source scan. The
boundary is operator_egress_guard, armed while an operator handler runs.
"""
from __future__ import annotations

import socket
import subprocess
import urllib.request

import pytest

import operator_egress_guard as guard
from operator_egress_guard import OperatorEgressDenied, operator_execution


# --- the reproduced counterexamples: each must be DENIED in operator context ---

def test_neutral_ship_helper_to_vercel_is_denied():
    # A neutral helper in "any other file" that promotes to production. It names
    # nothing a source scan would flag; the egress boundary denies it anyway.
    def ship():  # the exact ship()->vercel promote shape
        return urllib.request.urlopen("https://api.vercel.com/v13/deployments", timeout=5)

    with operator_execution():
        with pytest.raises(OperatorEgressDenied) as e:
            ship()
    assert "api.vercel.com" in str(e.value)


def test_existing_handler_ecs_network_path_is_denied():
    # An existing handler reaching AWS ECS (boto3 or raw) to update the prod
    # service. The underlying socket resolution/connect is denied.
    with operator_execution():
        with pytest.raises(OperatorEgressDenied):
            socket.getaddrinfo("ecs.us-east-1.amazonaws.com", 443)
        with pytest.raises(OperatorEgressDenied):
            socket.create_connection(("ecs.us-east-1.amazonaws.com", 443), timeout=5)


def test_existing_handler_http_to_production_surface_is_denied():
    with operator_execution():
        with pytest.raises(OperatorEgressDenied) as e:
            socket.getaddrinfo("api.leafdesign.ai", 443)
    assert "api.leafdesign.ai" in str(e.value)


def test_cloud_metadata_is_denied():
    with operator_execution():
        with pytest.raises(OperatorEgressDenied):
            socket.create_connection(("169.254.169.254", 80), timeout=5)


@pytest.mark.parametrize("argv", [
    ["vercel", "promote", "https://x.vercel.app"],
    ["aws", "ecs", "update-service", "--cluster", "production"],
    ["sh", "-c", "curl https://api.vercel.com"],
])
def test_subprocess_deploy_cli_is_denied(argv):
    # The `vercel` / `aws` CLI paths: an operator handler spawns no process.
    with operator_execution():
        with pytest.raises(OperatorEgressDenied):
            subprocess.Popen(argv)


# --- non-vacuous: legitimate operator egress + tenant path are NOT broken ------

def test_loopback_egress_is_allowed_in_operator_context():
    # A local connection (the shape of the platform DB over loopback) is NOT
    # denied. Bind a real listener and connect to it while armed.
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        with operator_execution():
            # getaddrinfo for loopback is allowed...
            socket.getaddrinfo("127.0.0.1", port)
            # ...and a real connect succeeds (no OperatorEgressDenied).
            c = socket.create_connection(("127.0.0.1", port), timeout=5)
            c.close()
    finally:
        srv.close()


def test_deployment_declared_db_host_is_allowed(monkeypatch):
    # Deployment declares its DB host; it is then permitted while other hosts
    # stay denied (deny-by-default, not a denylist).
    monkeypatch.setenv("LEAF_OPERATOR_EGRESS_ALLOW", "db.internal.example")
    with operator_execution():
        # Allowed: the guard does NOT fire. Real DNS then fails (the host is
        # fake), which itself proves the guard let the resolution through.
        try:
            socket.getaddrinfo("db.internal.example", 5432)
        except OperatorEgressDenied:
            pytest.fail("declared DB host must be allowed")
        except OSError:
            pass
        # Still denied: a non-allowlisted host.
        with pytest.raises(OperatorEgressDenied):
            socket.getaddrinfo("api.vercel.com", 443)


def test_guard_is_a_noop_outside_operator_context():
    # OUTSIDE operator execution the hook does nothing, so the tenant surface is
    # unaffected. Resolving vercel here must NOT raise OperatorEgressDenied (it
    # may fail with a normal network error, which is fine).
    assert not guard.is_armed()
    try:
        socket.getaddrinfo("api.vercel.com", 443)
    except OperatorEgressDenied:
        pytest.fail("egress guard fired outside operator context")
    except OSError:
        pass  # normal DNS/network failure is acceptable; our guard did not fire


# --- the boundary is WIRED into the real request path, not an unused helper ----

def test_require_operator_arms_the_egress_boundary(monkeypatch):
    # require_operator is a yield-dependency: while the handler runs (between
    # yield and cleanup) the boundary is armed. Drive the generator directly.
    import operator_deps
    import operator_principals

    class _Tenant:
        subject = "auth0|op-egress-test"

    principal = operator_principals.OperatorPrincipal(
        subject="auth0|op-egress-test", role="operator", role_revision=1,
        status="active", profiles=("default",), environment="staging")
    monkeypatch.setattr(operator_principals, "resolve_principal",
                        lambda subject: principal)

    gen = operator_deps.require_operator(
        tenant=_Tenant(), x_operator_subject=None, x_operator_profile=None)
    ctx = next(gen)                      # enters `with operator_execution()`
    try:
        assert guard.is_armed(), "handler must run with egress armed"
        assert ctx.subject == "auth0|op-egress-test"
        with pytest.raises(OperatorEgressDenied):
            socket.getaddrinfo("api.vercel.com", 443)
    finally:
        with pytest.raises(StopIteration):
            next(gen)                    # runs cleanup, disarms
    assert not guard.is_armed()
