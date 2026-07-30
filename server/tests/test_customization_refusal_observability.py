"""A tenant-facing refusal must stay opaque, and still be diagnosable.

Every repo failure answers the same `tenant_repository_unavailable` 503. Before
this suite an operator could not tell an unset env var from an unreachable
harness from a failed provision, because nothing logged the cause: staging
returned three of these with no line in CloudWatch beyond the access log.

Two properties are load-bearing and pull in opposite directions:
  1. the cause reaches the operator log
  2. the cause never reaches the HTTP response
Both are asserted here, because satisfying either alone is a bug.
"""
from __future__ import annotations

import json
import logging

import pytest

import customization_service
from customization_service import CustomizationServiceError, _bare_repo, _ensure_bare_repo
from routers import author as author_router


@pytest.fixture(autouse=True)
def _clear_repo_env(monkeypatch):
    for name in (
        "LEAF_TENANT_GIT_DIR", "LEAF_TENANT_BARE_BASE",
        "LEAF_AUTHOR_HARNESS_URL", "LEAF_HARNESS_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)


def test_missing_git_dir_names_the_unset_variable():
    with pytest.raises(CustomizationServiceError) as caught:
        _bare_repo("11111111-1111-4111-8111-111111111111")
    assert caught.value.code == "tenant_repository_unavailable"
    assert "git_dir_unset" in caught.value.detail
    assert "LEAF_TENANT_GIT_DIR" in caught.value.detail


def test_absent_repo_names_the_path_it_looked_for(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_TENANT_GIT_DIR", str(tmp_path))
    with pytest.raises(CustomizationServiceError) as caught:
        _bare_repo("22222222-2222-4222-8222-222222222222")
    assert "path_unresolvable" in caught.value.detail
    assert "22222222-2222-4222-8222-222222222222.git" in caught.value.detail


def test_repo_without_head_is_distinguished_from_an_absent_one(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_TENANT_GIT_DIR", str(tmp_path))
    tenant = "33333333-3333-4333-8333-333333333333"
    (tmp_path / f"{tenant}.git").mkdir()
    with pytest.raises(CustomizationServiceError) as caught:
        _bare_repo(tenant)
    assert "head_missing" in caught.value.detail


def test_unconfigured_harness_says_which_half_is_missing(tmp_path, monkeypatch):
    """The staging failure mode: provisioning cannot even be attempted."""
    monkeypatch.setenv("LEAF_TENANT_GIT_DIR", str(tmp_path))
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.internal:8150")
    with pytest.raises(CustomizationServiceError) as caught:
        _ensure_bare_repo("44444444-4444-4444-8444-444444444444")
    assert "harness_unconfigured" in caught.value.detail
    assert "url_set=True" in caught.value.detail
    assert "secret_set=False" in caught.value.detail


def test_secret_value_is_never_placed_in_the_detail(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_TENANT_GIT_DIR", str(tmp_path))
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "s3cret-harness-token")
    with pytest.raises(CustomizationServiceError) as caught:
        _ensure_bare_repo("55555555-5555-4555-8555-555555555555")
    detail = caught.value.detail
    # Assert the detail SAYS something first, so this cannot pass vacuously on an
    # empty string the way it would if the branch simply stopped setting detail.
    assert "harness_unconfigured" in detail
    assert "url_set=False" in detail
    assert "s3cret-harness-token" not in detail


def test_failed_provision_call_records_status_and_exception_type(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_TENANT_GIT_DIR", str(tmp_path))
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.internal:8150")
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "unused-by-the-stub")

    class _Boom(RuntimeError):
        pass

    import requests

    class _Response:
        status_code = 503

        def raise_for_status(self):
            raise requests.HTTPError("service unavailable", response=self)

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Response())
    with pytest.raises(CustomizationServiceError) as caught:
        _ensure_bare_repo("66666666-6666-4666-8666-666666666666")
    detail = caught.value.detail
    assert "harness_provision_failed" in detail
    assert "HTTPError" in detail
    assert "/author/repository" in detail
    # The REAL status the harness answered, not merely the presence of "status=".
    # An implementation that hardcoded status=None or a wrong value fails here.
    assert "status=503" in detail


def test_a_transport_failure_with_no_response_records_no_status(monkeypatch, tmp_path):
    """The other half: a refused connection has no HTTP status to report."""
    monkeypatch.setenv("LEAF_TENANT_GIT_DIR", str(tmp_path))
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.internal:8150")
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "unused-by-the-stub")

    class _Boom(RuntimeError):
        pass

    import requests

    def _explode(*_args, **_kwargs):
        raise _Boom("connection refused")

    monkeypatch.setattr(requests, "post", _explode)
    with pytest.raises(CustomizationServiceError) as caught:
        _ensure_bare_repo("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    assert "status=None" in caught.value.detail
    assert "_Boom" in caught.value.detail


def test_git_failure_carries_stderr(tmp_path):
    """`detail` is documented to carry git stderr. Prove it actually does."""
    with pytest.raises(CustomizationServiceError) as caught:
        customization_service._git(tmp_path / "not-a-repo.git", "rev-parse", "HEAD")
    assert caught.value.code == "tenant_repository_unavailable"
    assert "git_failed" in caught.value.detail
    assert "rev-parse HEAD" in caught.value.detail
    # git's own words, so discarding stderr again fails here rather than silently.
    assert "not a git repository" in caught.value.detail


def test_refusal_is_logged_with_its_cause(caplog):
    caplog.set_level(logging.WARNING, logger=author_router._LOG.name)
    author_router._customization_error(
        CustomizationServiceError(
            "tenant_repository_unavailable", 503, "head_missing: /data/tenant-git/x.git/HEAD"
        )
    )
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "tenant_repository_unavailable" in logged
    assert "head_missing" in logged


def test_detail_never_reaches_the_response_body():
    """The whole point of `detail`: operators see it, tenants never do."""
    secret_ish = "/data/tenant-git/9999.git/HEAD"
    response = author_router._customization_error(
        CustomizationServiceError("tenant_repository_unavailable", 503, f"head_missing: {secret_ish}")
    )
    body = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 503
    assert body["reason_code"] == "tenant_repository_unavailable"
    assert secret_ish not in response.body.decode("utf-8")
    assert "head_missing" not in response.body.decode("utf-8")


SENTINEL = "dbname=leaf password=hunter2-should-never-be-logged"


TENANT = "77777777-7777-4777-8777-777777777777"


def _authorize_publish(monkeypatch, raiser):
    """Drive the real /internal/customization/authorize-publish route.

    Chosen deliberately: of the six handlers that now pass `cause=`, this is the
    one that authenticates from headers rather than a JWT dependency, so a real
    request can reach its `except Exception` without minting a token.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "all")
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", "dispatch-secret")
    monkeypatch.setattr(
        author_router.CustomizationService, "configured",
        classmethod(lambda cls: raiser()), raising=True,
    )
    route_app = FastAPI()
    route_app.include_router(author_router.router)
    client = TestClient(route_app, raise_server_exceptions=False)
    return client.post(
        "/internal/customization/authorize-publish",
        json={"receipt": {"change_set_id": TENANT}, "expected_main_sha": "a" * 40},
        headers={"X-Tenant-Id": TENANT, "X-Dispatch-Secret": "dispatch-secret"},
    )


CHANGE_SET = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

# Every route that hands an unexpected exception to the funnel, with a body its
# model accepts. Reverting `cause=exc` on any one of these drops its ERROR record,
# so the parametrised test below fails for that route specifically.
ALL_HANDOFFS = [
    ("/api/author", {"description": "count panels", "mode": "build"},
     {"Idempotency-Key": "k-author"}, "customization_stage_failed"),
    ("/api/author/stage",
     {"description": "count panels", "mode": "build", "idempotency_key": "k-stage"},
     {}, "customization_stage_failed"),
    ("/api/author/publication-requests", {"change_set_id": CHANGE_SET},
     {}, "customization_publish_failed"),
    ("/api/author/confirmations",
     {"contract": "leaf.customization.v1", "tenant_id": TENANT,
      "change_set_id": CHANGE_SET, "state": "staged", "base_commit": "a" * 40,
      "staged_commit": "b" * 40, "catalog_digest": "c" * 64,
      "platform_release": "r1", "workspace_contract_digest": "d" * 64,
      "idempotency_key": "k-confirm"},
     {}, "customization_publish_failed"),
    ("/api/author/rollback",
     {"change_set_id": CHANGE_SET, "idempotency_key": "k-rollback"},
     {}, "customization_rollback_failed"),
]


def _tenant_route_client(monkeypatch, raiser):
    """Mount the router with the JWT dependency satisfied, so /api/* is reachable."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "all")
    monkeypatch.setattr(
        author_router.CustomizationService, "configured",
        classmethod(lambda cls: raiser()), raising=True,
    )
    route_app = FastAPI()
    route_app.include_router(author_router.router)
    route_app.dependency_overrides[author_router.deps.require_tenant] = lambda: TENANT
    return TestClient(route_app, raise_server_exceptions=False)


@pytest.mark.parametrize("path,body,headers,code", ALL_HANDOFFS)
def test_every_tenant_route_reports_its_unexpected_failure(
    caplog, monkeypatch, path, body, headers, code
):
    """Blocker: one covered route left the other five free to regress silently."""
    def _raise():
        raise RuntimeError(SENTINEL)

    caplog.set_level(logging.DEBUG, logger=author_router._LOG.name)
    client = _tenant_route_client(monkeypatch, _raise)
    response = client.post(path, json=body, headers=headers)
    assert response.status_code == 503, response.text
    assert response.json()["reason_code"] == code
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, f"{path} must report its unexpected failure"
    assert "RuntimeError" in errors[0].getMessage()


def test_a_real_route_logs_the_frames_of_an_unexpected_failure(caplog, monkeypatch):
    """The sixth handoff, on a route that authenticates from headers.

    Covers /internal/customization/authorize-publish; the five tenant routes are
    covered by the parametrised test above.
    """
    def _raise():
        raise RuntimeError(SENTINEL)

    caplog.set_level(logging.DEBUG, logger=author_router._LOG.name)
    response = _authorize_publish(monkeypatch, _raise)
    assert response.status_code == 503
    assert response.json()["reason_code"] == "customization_confirmation_failed"

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "an unexpected route failure must log at ERROR"
    message = errors[0].getMessage()
    assert "RuntimeError" in message, "the exception type identifies the fault"
    assert "author.py" in message, "the frames must say where it broke"


def test_the_exception_message_is_never_logged(caplog, monkeypatch):
    """An exception message can carry a DSN or a token. Log where, not what."""
    def _raise():
        raise RuntimeError(SENTINEL)

    caplog.set_level(logging.DEBUG, logger=author_router._LOG.name)
    _authorize_publish(monkeypatch, _raise)
    rendered = "\n".join(
        r.getMessage() + (str(r.exc_info) if r.exc_info else "") for r in caplog.records
    )
    assert "hunter2-should-never-be-logged" not in rendered
    assert SENTINEL not in rendered
    # exc_info would render the message via the traceback formatter, so refuse it.
    assert all(r.exc_info is None for r in caplog.records)


def test_an_unauthenticated_internal_refusal_does_not_warn(caplog, monkeypatch):
    """Blocker: these routes sit on a public ALB and authenticate in the handler.

    A caller with no secret must not be able to write a WARNING per request.
    """
    monkeypatch.setenv("LEAF_CUSTOMIZATION_APPROVAL_SECRET", "approval-secret")
    caplog.set_level(logging.DEBUG, logger=author_router._LOG.name)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    route_app = FastAPI()
    route_app.include_router(author_router.router)
    client = TestClient(route_app, raise_server_exceptions=False)
    response = client.post(
        "/internal/customization/deny",
        json={"change_set_id": "99999999-9999-4999-8999-999999999999"},
        headers={"X-Tenant-Id": "99999999-9999-4999-8999-999999999999",
                 "X-Approval-Secret": "wrong-secret"},
    )
    assert response.status_code == 403
    assert response.json()["reason_code"] == "approval_authority_denied"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
        "an unauthenticated refusal must not reach WARNING"
    )
    assert [r for r in caplog.records if r.levelno == logging.DEBUG], (
        "it should still be visible at DEBUG"
    )


def test_a_detail_carrying_refusal_still_warns(caplog):
    """The demotion above must not silence the failures worth seeing."""
    caplog.set_level(logging.DEBUG, logger=author_router._LOG.name)
    author_router._customization_error(
        CustomizationServiceError(
            "tenant_repository_unavailable", 503, "head_missing: /data/tenant-git/x.git/HEAD"
        )
    )
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "a 503 carrying a detail must warn"
    assert "head_missing" in warnings[0].getMessage()


def _raise_with_secret_in_source():
    raise RuntimeError("password=literal-secret-in-source-9f3a")


def test_the_source_line_at_the_raise_site_is_never_echoed(caplog, monkeypatch):
    """traceback.format_tb renders each frame's SOURCE, not just its location.

    So a literal at the raise site is reproduced verbatim in the log. Only a
    location-only rendering (extract_tb, filename:lineno:function) is safe. This
    fails if format_tb, str(cause) or exc_info comes back.
    """
    caplog.set_level(logging.DEBUG, logger=author_router._LOG.name)
    client = _tenant_route_client(monkeypatch, _raise_with_secret_in_source)
    response = client.post(
        "/api/author/stage",
        json={"description": "count panels", "mode": "build", "idempotency_key": "k"},
    )
    assert response.status_code == 503
    rendered = "\n".join(
        r.getMessage() + (str(r.exc_info) if r.exc_info else "") for r in caplog.records
    )
    assert "literal-secret-in-source-9f3a" not in rendered
    assert "password=" not in rendered
    # It must still say where the fault was, or the log is useless.
    assert "_raise_with_secret_in_source" in rendered
    assert "RuntimeError" in rendered


def test_an_unknown_change_set_is_a_caller_error_not_an_operator_event(caplog, monkeypatch):
    """The change-set id is caller-supplied, and authority is checked after load.

    Any tenant member can name arbitrary ids, so this must not cost one ERROR per
    request. The response is unchanged.
    """
    from customization_models import ChangeSetNotFoundError

    def _raise():
        raise ChangeSetNotFoundError("change set was not found")

    caplog.set_level(logging.DEBUG, logger=author_router._LOG.name)
    client = _tenant_route_client(monkeypatch, _raise)
    response = client.post(
        "/api/author/confirmations",
        json={"contract": "leaf.customization.v1", "tenant_id": TENANT,
              "change_set_id": CHANGE_SET, "state": "staged", "base_commit": "a" * 40,
              "staged_commit": "b" * 40, "catalog_digest": "c" * 64,
              "platform_release": "r1", "workspace_contract_digest": "d" * 64,
              "idempotency_key": "k-confirm"},
    )
    assert response.status_code == 503
    assert response.json()["reason_code"] == "customization_publish_failed"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
        "a caller-supplied unknown id must not reach WARNING or ERROR"
    )
    assert [r for r in caplog.records if r.levelno == logging.DEBUG]


def test_a_detail_free_503_does_not_warn():
    """Keying the level on status rather than content reopens the amplification.

    With auth off, five /api/* routes refuse at the gate with a detail-free 503,
    so `status >= 500` would let a caller drive one WARNING per request.
    """
    import logging as _logging
    records = []

    class _Capture(_logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture()
    author_router._LOG.addHandler(handler)
    author_router._LOG.setLevel(_logging.DEBUG)
    try:
        author_router._customization_error(
            CustomizationServiceError("customization_auth_required", 503)
        )
    finally:
        author_router._LOG.removeHandler(handler)
    assert records, "it must still be visible somewhere"
    assert all(r.levelno < _logging.WARNING for r in records)


def test_platform_store_failure_names_itself(monkeypatch):
    """`tenant_identity_binding_unavailable` had two detail-free 503 branches."""
    import platform_link

    monkeypatch.setenv("DATABASE_URL", "postgresql://unused/db")
    monkeypatch.setattr(author_router.deps, "auth_live", lambda: True)

    def _boom():
        raise RuntimeError("store is not reachable")

    monkeypatch.setattr(platform_link, "platform_store", _boom)

    class _Tenant(str):
        subject = "auth0|abc"

    with pytest.raises(CustomizationServiceError) as caught:
        customization_service._binding(_Tenant(TENANT))
    assert caught.value.code == "tenant_identity_binding_unavailable"
    assert "platform_store_unavailable" in caught.value.detail
    assert "RuntimeError" in caught.value.detail


def test_harness_stage_and_publish_failures_carry_their_cause():
    """The two sites that still answered with an empty detail."""
    import inspect
    stage_src = inspect.getsource(customization_service.CustomizationService._harness_stage)
    publish_src = inspect.getsource(customization_service.CustomizationService._harness_publish)
    assert "harness_stage_failed" in stage_src
    assert "harness_publish_failed" in publish_src
    for src in (stage_src, publish_src):
        # Every raise on these paths names a cause rather than defaulting to "".
        assert "status={status}" in src
