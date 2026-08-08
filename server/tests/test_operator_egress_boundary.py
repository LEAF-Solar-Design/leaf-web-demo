"""O2/O3 BEHAVIORAL: a generic operator handler cannot CALL a production deploy
route (contract/OPERATOR.md section 7.2-7.3).

Two layers (operator_egress_guard):
  Layer 1 - the known production deploy control plane + deploy CLIs are denied
            for the whole process, UNCONDITIONALLY, so a context/thread/executor
            escape reaching a real deploy route is denied anyway.
  Layer 2 - while an operator handler runs, deny-by-default (only loopback + DB +
            a declared extra), closing an aliased/env-provided target on the
            innocent same-context path.
"""
from __future__ import annotations

import concurrent.futures
import contextvars
import socket
import subprocess
import threading
import urllib.request

import pytest

import operator_egress_guard as guard
from operator_egress_guard import OperatorEgressDenied, operator_execution


def _resolve(host, port=443):
    socket.getaddrinfo(host, port)


# === Layer 1: deploy control plane denied UNCONDITIONALLY (no arming) =========

@pytest.mark.parametrize("host", [
    "api.vercel.com", "myapp.vercel.app", "api.leafdesign.ai",
    "ecs.us-east-1.amazonaws.com", "169.254.169.254",
    # FIPS, dual-stack, and partition ECS endpoints an executor AWS SDK call
    # could otherwise reach (round-4 review completeness gap).
    "ecs-fips.us-east-1.amazonaws.com", "ecs.us-east-1.api.aws",
    "ecs.cn-north-1.amazonaws.com.cn",
])
def test_deploy_control_plane_denied_without_arming(host):
    assert not guard.is_armed()
    with pytest.raises(OperatorEgressDenied):
        _resolve(host)


def test_neutral_ship_helper_to_vercel_is_denied():
    def ship():  # the exact ship()->vercel promote shape
        return urllib.request.urlopen("https://api.vercel.com/v13/deployments", timeout=5)

    with operator_execution():
        with pytest.raises(OperatorEgressDenied) as e:
            ship()
    assert "api.vercel.com" in str(e.value)


# === The REFUTED bypass, now CLOSED: context/thread/executor escapes reaching a
#     real deploy route are denied by Layer 1 (which has no context to escape). ==

def _probe_deploy(results, label):
    try:
        _resolve("api.vercel.com")
        results[label] = "NOT_DENIED"
    except OperatorEgressDenied:
        results[label] = "DENIED"
    except OSError:
        results[label] = "DENIED_OR_NETERR"


def test_deploy_route_denied_across_context_escapes():
    results: dict[str, str] = {}
    with operator_execution():
        _probe_deploy(results, "same-context")
        contextvars.Context().run(_probe_deploy, results, "fresh-context")
        t = threading.Thread(target=_probe_deploy, args=(results, "raw-thread"))
        t.start(); t.join()
        with concurrent.futures.ThreadPoolExecutor() as ex:
            ex.submit(_probe_deploy, results, "run-in-executor").result()
    # Every escape that reaches a real deploy route is DENIED (Layer 1).
    for label, outcome in results.items():
        assert outcome == "DENIED", f"{label} was not denied: {outcome}"


@pytest.mark.parametrize("argv", [
    ["vercel", "promote", "https://x.vercel.app"],
    ["aws", "ecs", "update-service", "--cluster", "production"],
    ["cdk", "deploy"], ["terraform", "apply"], ["pulumi", "up"],
    ["npx", "cdk", "deploy"],                       # wrapper form
])
def test_deploy_cli_spawn_denied_unconditionally(argv):
    # Denied WITHOUT arming (Layer 1) and inside a fresh context (no escape).
    assert not guard.is_armed()
    with pytest.raises(OperatorEgressDenied):
        subprocess.Popen(argv)
    with pytest.raises(OperatorEgressDenied):
        contextvars.Context().run(lambda: subprocess.Popen(argv))


def test_benign_subprocess_not_denied_by_layer1_outside_context():
    # A non-deploy spawn is NOT denied process-wide (only in operator context).
    # `echo aws` must not trip on the argument "aws".
    assert not guard.is_armed()
    import operator_egress_guard as g
    assert not g._is_deploy_cli_spawn(["echo", "aws"])
    assert not g._is_deploy_cli_spawn(["sh", "-c", "true"])


# === Layer 2: operator context denies an aliased / non-deploy target ==========

def test_operator_context_denies_non_deploy_non_allowlisted_host():
    # example.com is NOT a known deploy route (Layer 1 lets it by), but an
    # operator handler still may not reach it: deny-by-default closes an aliased
    # or environment-provided production target on the innocent same-context path.
    with operator_execution():
        with pytest.raises(OperatorEgressDenied):
            _resolve("example.com")


def test_non_deploy_host_allowed_outside_operator_context():
    # OUTSIDE operator execution a non-deploy host is NOT denied (tenant path is
    # unaffected); it may fail with a normal network error, which is fine.
    assert not guard.is_armed()
    try:
        _resolve("example.com")
    except OperatorEgressDenied:
        pytest.fail("guard denied a non-deploy host outside operator context")
    except OSError:
        pass


def test_subprocess_denied_in_operator_context():
    with operator_execution():
        with pytest.raises(OperatorEgressDenied):
            subprocess.Popen(["sh", "-c", "true"])


# === Non-vacuous: legitimate operator egress is NOT broken ====================

def test_loopback_egress_is_allowed_in_operator_context():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        with operator_execution():
            socket.getaddrinfo("127.0.0.1", port)
            c = socket.create_connection(("127.0.0.1", port), timeout=5)
            c.close()
    finally:
        srv.close()


def test_deployment_declared_db_host_is_allowed(monkeypatch):
    monkeypatch.setenv("LEAF_OPERATOR_EGRESS_ALLOW", "db.internal.example")
    with operator_execution():
        try:
            socket.getaddrinfo("db.internal.example", 5432)
        except OperatorEgressDenied:
            pytest.fail("declared DB host must be allowed")
        except OSError:
            pass  # real DNS failure (fake host) proves the guard let it through
        with pytest.raises(OperatorEgressDenied):
            socket.getaddrinfo("api.vercel.com", 443)   # still denied (Layer 1)


def test_tenant_s3_host_not_denied_by_layer1():
    # S3 (s3.amazonaws.com) is legitimate tenant egress and must NOT match the
    # ECS deploy pattern, so Layer 1 does not deny it outside operator context.
    assert not guard.is_deploy_control_plane("s3.amazonaws.com")
    assert not guard.is_deploy_control_plane("developer.api.autodesk.com")
    assert guard.is_deploy_control_plane("ecs.us-east-1.amazonaws.com")


def test_layer1_denies_production_but_not_staging():
    # Production unreachability denies PRODUCTION, never STAGING (the operator
    # plane legitimately stages releases). api.leafdesign.ai is production;
    # staging-api.leafdesign.ai is staging and must pass Layer 1.
    assert guard.is_deploy_control_plane("api.leafdesign.ai")
    assert not guard.is_deploy_control_plane("staging-api.leafdesign.ai")


# === The boundary is WIRED into the real request path =========================

def test_require_operator_arms_the_egress_boundary(monkeypatch):
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
    ctx = next(gen)
    try:
        assert guard.is_armed(), "handler must run with egress armed"
        assert ctx.subject == "auth0|op-egress-test"
        with pytest.raises(OperatorEgressDenied):
            socket.getaddrinfo("api.vercel.com", 443)
    finally:
        with pytest.raises(StopIteration):
            next(gen)
    assert not guard.is_armed()
