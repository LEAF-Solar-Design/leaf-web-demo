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
import re
from types import SimpleNamespace

import pytest

import customization_service
from customization_service import CustomizationServiceError, _bare_repo, _ensure_bare_repo
from routers import author as author_router


def _status_token(detail: str) -> str | None:
    """Return the exact value recorded for the `status` key.

    Anchored on a word boundary at both ends: an unanchored search accepted
    `status=5030` for 503, and a leading-boundary-only one accepted `xstatus=503`,
    so renaming the field while keeping the suffix would have survived.
    """
    found = re.search(r"(?:^|\s)status=(\S+)", detail)
    return found.group(1) if found else None


@pytest.fixture(autouse=True)
def _clear_repo_env(monkeypatch):
    for name in (
        "LEAF_TENANT_GIT_DIR", "LEAF_TENANT_BARE_BASE",
        "LEAF_AUTHOR_HARNESS_URL", "LEAF_HARNESS_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)


def test_missing_git_dir_uses_stable_code():
    with pytest.raises(CustomizationServiceError) as caught:
        _bare_repo("11111111-1111-4111-8111-111111111111")
    assert caught.value.code == "tenant_repository_unavailable"
    assert "git_dir_unset" in caught.value.detail
    assert caught.value.detail == "git_dir_unset"
    assert caught.value.exception_type is None


def test_absent_repo_uses_code_and_exception_type(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_TENANT_GIT_DIR", str(tmp_path))
    with pytest.raises(CustomizationServiceError) as caught:
        _bare_repo("22222222-2222-4222-8222-222222222222")
    assert "path_unresolvable" in caught.value.detail
    assert caught.value.detail == "path_unresolvable"
    assert caught.value.exception_type == "FileNotFoundError"


def test_repo_without_head_is_distinguished_from_an_absent_one(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_TENANT_GIT_DIR", str(tmp_path))
    tenant = "33333333-3333-4333-8333-333333333333"
    (tmp_path / f"{tenant}.git").mkdir()
    with pytest.raises(CustomizationServiceError) as caught:
        _bare_repo(tenant)
    assert "head_missing" in caught.value.detail


def test_unconfigured_harness_uses_stable_code(tmp_path, monkeypatch):
    """The staging failure mode: provisioning cannot even be attempted."""
    monkeypatch.setenv("LEAF_TENANT_GIT_DIR", str(tmp_path))
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.internal:8150")
    with pytest.raises(CustomizationServiceError) as caught:
        _ensure_bare_repo("44444444-4444-4444-8444-444444444444")
    assert "harness_unconfigured" in caught.value.detail
    assert caught.value.detail == "harness_unconfigured"
    assert caught.value.exception_type is None


def test_secret_value_is_never_placed_in_the_detail(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_TENANT_GIT_DIR", str(tmp_path))
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "s3cret-harness-token")
    with pytest.raises(CustomizationServiceError) as caught:
        _ensure_bare_repo("55555555-5555-4555-8555-555555555555")
    detail = caught.value.detail
    # Assert the detail SAYS something first, so this cannot pass vacuously on an
    # empty string the way it would if the branch simply stopped setting detail.
    assert "harness_unconfigured" in detail
    assert detail == "harness_unconfigured"
    assert "s3cret-harness-token" not in detail


def test_failed_provision_call_records_code_and_exception_type(tmp_path, monkeypatch):
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
    assert caught.value.exception_type == "HTTPError"
    assert detail == "harness_provision_failed"
    # HTTP status metadata is excluded from operator diagnostics.
    assert _status_token(detail) is None


def test_a_transport_failure_records_only_code_and_type(monkeypatch, tmp_path):
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
    assert caught.value.detail == "harness_provision_failed"
    assert caught.value.exception_type == "_Boom"


def test_git_failure_omits_stderr(tmp_path):
    """Git diagnostics identify the operation and type without stderr."""
    with pytest.raises(CustomizationServiceError) as caught:
        customization_service._git(tmp_path / "not-a-repo.git", "rev-parse", "HEAD")
    assert caught.value.code == "tenant_repository_unavailable"
    assert "git_failed" in caught.value.detail
    assert caught.value.detail == "git_failed"
    assert caught.value.exception_type == "CalledProcessError"
    # Captured Git output is excluded from operator diagnostics.
    assert "not a git repository" not in caught.value.detail


def test_refusal_is_logged_with_its_cause(caplog):
    caplog.set_level(logging.WARNING, logger=author_router._LOG.name)
    author_router._customization_error(
        CustomizationServiceError(
            "tenant_repository_unavailable", 503, diagnostic_code="head_missing"
        )
    )
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "tenant_repository_unavailable" in logged
    assert "head_missing" in logged


def test_detail_never_reaches_the_response_body():
    """Diagnostic fields never enter the client response."""
    secret_ish = "/data/tenant-git/9999.git/HEAD"
    response = author_router._customization_error(
        CustomizationServiceError("tenant_repository_unavailable", 503, f"head_missing: {secret_ish}")
    )
    body = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 503
    assert body["reason_code"] == "tenant_repository_unavailable"
    # Prove the KEY is absent, not just the value: a body carrying "detail": "-"
    # would still pass the substring checks below.
    assert "detail" not in body
    assert secret_ish not in response.body.decode("utf-8")
    assert "head_missing" not in response.body.decode("utf-8")


SENTINEL = "dbname=leaf password=hunter2-should-never-be-logged"


TENANT = "77777777-7777-4777-8777-777777777777"
AUTHOR_SUBJECT = "auth0|refusal-observability-author"
AUTHORITY_TURN_ID = "turn-refusal-observability"


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


def _tenant_route_client(monkeypatch, tmp_path, raiser):
    """Mount the router with a real same-subject active turn."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import session_store

    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "all")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "all")
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "sessions.db")
    monkeypatch.setattr(session_store, "_conn", None)
    session_store.ensure_started()
    session = session_store.get_or_create_session(TENANT, "drawing-refusal-observability")
    assert session_store.try_begin_turn(
        session["session_id"],
        AUTHORITY_TURN_ID,
        60,
        tier="hosted_pro",
        subject=AUTHOR_SUBJECT,
    )
    monkeypatch.setattr(
        author_router.CustomizationService, "configured",
        classmethod(lambda cls: raiser()), raising=True,
    )
    route_app = FastAPI()
    route_app.include_router(author_router.router)
    route_app.dependency_overrides[author_router.deps.require_tenant] = lambda: (
        author_router.deps.TenantContext(
            TENANT, tier="hosted_pro", subject=AUTHOR_SUBJECT
        )
    )
    client = TestClient(route_app, raise_server_exceptions=False)
    client.headers.update({
        "X-Authority-Session-Id": session["session_id"],
        "X-Authority-Turn-Id": AUTHORITY_TURN_ID,
    })
    return client


@pytest.mark.parametrize("path,body,headers,code", ALL_HANDOFFS)
def test_every_tenant_route_reports_its_unexpected_failure(
    caplog, monkeypatch, tmp_path, path, body, headers, code
):
    """Blocker: one covered route left the other five free to regress silently."""
    def _raise():
        raise RuntimeError(SENTINEL)

    caplog.set_level(logging.DEBUG, logger=author_router._LOG.name)
    client = _tenant_route_client(monkeypatch, tmp_path, _raise)
    response = client.post(path, json=body, headers=headers)
    assert response.status_code == 503, response.text
    assert response.json()["reason_code"] == code
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, f"{path} must report its unexpected failure"
    assert "RuntimeError" in errors[0].getMessage()


def test_a_real_route_logs_only_the_type_of_an_unexpected_failure(caplog, monkeypatch):
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
    assert "author.py" not in message
    assert "detail=customization_unexpected_failure exception_type=RuntimeError" in message


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
            "tenant_repository_unavailable", 503, diagnostic_code="head_missing"
        )
    )
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "a 503 carrying a detail must warn"
    assert "head_missing" in warnings[0].getMessage()


def _raise_with_secret_in_source():
    raise RuntimeError("password=literal-secret-in-source-9f3a")


def test_the_source_line_at_the_raise_site_is_never_echoed(
    caplog, monkeypatch, tmp_path
):
    """traceback.format_tb renders each frame's SOURCE, not just its location.

    So a literal at the raise site is reproduced verbatim in the log. Only a
    type-only rendering is safe. This
    fails if format_tb, str(cause) or exc_info comes back.
    """
    caplog.set_level(logging.DEBUG, logger=author_router._LOG.name)
    client = _tenant_route_client(
        monkeypatch, tmp_path, _raise_with_secret_in_source
    )
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
    # The exception type identifies the failure without frame locations.
    assert "_raise_with_secret_in_source" not in rendered
    assert "RuntimeError" in rendered


def test_an_unknown_change_set_is_a_caller_error_not_an_operator_event(
    caplog, monkeypatch, tmp_path
):
    """The change-set id is caller-supplied, and authority is checked after load.

    Any tenant member can name arbitrary ids, so this must not cost one ERROR per
    request. The response is unchanged.
    """
    from customization_models import ChangeSetNotFoundError

    def _raise():
        raise ChangeSetNotFoundError("change set was not found")

    caplog.set_level(logging.DEBUG, logger=author_router._LOG.name)
    client = _tenant_route_client(monkeypatch, tmp_path, _raise)
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




def _levels_for(exc, **kwargs):
    """Return the levels logged for one refusal, isolated from caplog's root setup."""
    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture()
    author_router._LOG.addHandler(handler)
    previous = author_router._LOG.level
    author_router._LOG.setLevel(logging.DEBUG)
    try:
        author_router._customization_error(exc, **kwargs)
    finally:
        author_router._LOG.removeHandler(handler)
        author_router._LOG.setLevel(previous)
    assert records, "every refusal must be recorded at some level"
    return [r.levelno for r in records]


@pytest.mark.parametrize("code", [
    "invalid_stage_request", "independent_approval_pending", "tenant_role_denied",
    "publication_request_not_available", "customization_rollback_disabled",
    "stage_not_available", "builder_entitlement_missing",
])
def test_ordinary_request_outcomes_stay_quiet(code):
    """A caller polling, or sending an unsupported mode, is not an operator event."""
    levels = _levels_for(CustomizationServiceError(code, 422))
    assert max(levels) < logging.WARNING, f"{code} should be quiet"


def test_an_integrity_failure_answering_4xx_still_warns():
    """Guards the classification against collapsing into `all 4xx are quiet`.

    A denied symlink or a changed frozen path is a trusted-input integrity failure.
    It answers 4xx but an operator must see it.
    """
    for code in ("staged_symlink_denied", "frozen_path_changed",
                 "catalog_digest_mismatch", "publish_not_authorized"):
        levels = _levels_for(CustomizationServiceError(code, 409))
        assert max(levels) >= logging.WARNING, f"{code} must stay visible"


def test_the_dual_use_codes_split_on_status():
    """One code, two meanings: the caller's 4xx is quiet, the deployment's 5xx warns."""
    assert max(_levels_for(
        CustomizationServiceError("tenant_identity_binding_unavailable", 403)
    )) < logging.WARNING
    assert max(_levels_for(
        CustomizationServiceError("tenant_identity_binding_unavailable", 503,
                                  diagnostic_code="binding_resolution_failed")
    )) >= logging.WARNING
    assert max(_levels_for(
        CustomizationServiceError("invalid_staged_receipt", 422)
    )) < logging.WARNING
    assert max(_levels_for(
        CustomizationServiceError("invalid_staged_receipt", 502)
    )) >= logging.WARNING


def test_an_unmarked_authority_code_would_warn():
    """The classification's baseline: an unknown code is NOT quiet by default."""
    levels = _levels_for(CustomizationServiceError("stage_role_denied", 403))
    assert max(levels) >= logging.WARNING


def test_every_authority_conversion_site_is_marked():
    """The invariant a single-route test cannot enforce.

    There were TEN of these branches, not the eight a string search found: two were
    formatted differently and went unmarked, so a `confirmation_tampered` denial on
    /api/author/publication-requests warned once per request. This walks the module's
    AST so a new conversion site cannot be added unmarked.

    A call is a conversion site if `exc.reason_code` appears anywhere in its
    argument list, positional or keyword, not just as a literal first positional
    arg: `CustomizationServiceError(code=exc.reason_code, status_code=403)` and
    `err = CustomizationServiceError(exc.reason_code, 403); _customization_error(err)`
    must both be caught, so the object is resolved through one level of local
    variable assignment before it's checked.

    The expected count is derived from the number of `isinstance(exc, AuthorityError)`
    handlers in the module rather than a hardcoded floor: a floor stops detecting
    deletion once more sites exist than the floor, while a derived count tracks the
    real handler total and still catches a conversion dropped out from under an
    unchanged handler.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(author_router))

    def _has_reason_code(node):
        return any(
            isinstance(sub, ast.Attribute) and sub.attr == "reason_code"
            for sub in ast.walk(node)
        )

    conversions = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        # One level of local assignment resolution, scoped to this function: a
        # conversion built via a temporary (`err = CustomizationServiceError(...)`)
        # must still be recognised at its later `_customization_error(err)` call.
        assigned = {
            node.targets[0].id: node.value
            for node in ast.walk(func)
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        }

        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Name) and node.func.id == "_customization_error"):
                continue
            if not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Name) and first.id in assigned:
                first = assigned[first.id]
            # Only the AuthorityError conversions, identified by their reason_code
            # arg appearing anywhere in the built error's arguments.
            if not (isinstance(first, ast.Call) and _has_reason_code(first)):
                continue
            marked = any(
                kw.arg == "from_authority"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in node.keywords
            )
            conversions.append((node.lineno, marked))

    expected = sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "isinstance"
        and len(node.args) == 2
        and isinstance(node.args[1], ast.Name)
        and node.args[1].id == "AuthorityError"
    )
    assert expected >= 10, (
        f"expected at least ten AuthorityError handlers, found {expected}"
    )
    assert len(conversions) == expected, (
        f"found {len(conversions)} authority conversions but {expected} "
        f"AuthorityError handlers; every handler must build and mark exactly one"
    )
    unmarked = [line for line, marked in conversions if not marked]
    assert not unmarked, (
        f"authority conversions missing from_authority=True at lines {unmarked}"
    )


@pytest.mark.parametrize("path,body", [
    ("/api/author/stage",
     {"description": "count panels", "mode": "build", "idempotency_key": "k"}),
    ("/api/author/publication-requests", {"change_set_id": CHANGE_SET}),
    ("/api/author/rollback", {"change_set_id": CHANGE_SET, "idempotency_key": "k"}),
])
def test_a_real_route_marks_its_authority_denial_quiet(
    caplog, monkeypatch, tmp_path, path, body
):
    """The behavioural half: the marking must actually reach the level decision.

    More than one route, because a single-route test let two unmarked branches
    through. The AST test above covers the sites this cannot reach.
    """
    from customization_authority import AuthorityError

    def _raise():
        raise AuthorityError("stage_role_denied")

    caplog.set_level(logging.DEBUG, logger=author_router._LOG.name)
    client = _tenant_route_client(monkeypatch, tmp_path, _raise)
    response = client.post(path, json=body)
    assert response.status_code == 403, response.text
    assert response.json()["reason_code"] == "stage_role_denied"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
        "an authority denial is a per-caller outcome, not an operator event"
    )
    assert [r for r in caplog.records if r.levelno == logging.DEBUG]


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
    assert caught.value.exception_type == "RuntimeError"


def _change_set():
    return SimpleNamespace(
        change_set_id=CHANGE_SET, tenant_id=TENANT, base_commit="a" * 40,
        desired_platform_release="r1", workspace_contract_digest="d" * 64,
        idempotency_key="k", staged_commit="b" * 40, catalog_digest="c" * 64,
        authority_session_id="session-refusal-observability",
        authority_turn_id=AUTHORITY_TURN_ID,
        author_subject=AUTHOR_SUBJECT,
    )


def test_harness_stage_failure_records_code_and_type(monkeypatch):
    """Previously asserted by reading the source, which `from exc` removal survived.

    This calls the production method. `_harness_stage` never touches `self`, so it
    runs unbound with no service instance to build.
    """
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.internal:8150")
    import requests

    class _Response:
        status_code = 502

        def raise_for_status(self):
            raise requests.HTTPError("bad gateway", response=self)

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Response())
    monkeypatch.setattr(
        customization_service.deps,
        "active_stage_author_subject",
        lambda tenant_id, session_id, turn_id: (
            AUTHOR_SUBJECT
            if (
                tenant_id,
                session_id,
                turn_id,
            ) == (
                TENANT,
                "session-refusal-observability",
                AUTHORITY_TURN_ID,
            )
            else None
        ),
    )
    with pytest.raises(CustomizationServiceError) as caught:
        customization_service.CustomizationService._harness_stage(
            None, TENANT, "count panels", _change_set()
        )
    assert caught.value.code == "customization_harness_unavailable"
    assert "harness_stage_failed" in caught.value.detail
    assert caught.value.detail == "harness_stage_failed"
    assert caught.value.exception_type == "HTTPError"
    # `from exc` must survive: the chain is what a traceback-free log relies on.
    assert isinstance(caught.value.__cause__, requests.HTTPError)


def test_harness_publish_failure_records_code_and_type(monkeypatch):
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.internal:8150")
    import requests

    class _Response:
        status_code = 504

        def raise_for_status(self):
            raise requests.HTTPError("gateway timeout", response=self)

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Response())
    service = SimpleNamespace(_raw_receipt=lambda change: {"change_set_id": CHANGE_SET})
    with pytest.raises(CustomizationServiceError) as caught:
        customization_service.CustomizationService._harness_publish(service, _change_set())
    assert caught.value.code == "customization_publish_incomplete"
    assert "harness_publish_failed" in caught.value.detail
    assert caught.value.detail == "harness_publish_failed"
    assert caught.value.exception_type == "HTTPError"
    assert isinstance(caught.value.__cause__, requests.HTTPError)


def test_an_operator_misconfiguration_still_warns(caplog):
    """The reverse risk: keying the level on `detail` hid real deployment faults.

    A missing harness URL raises a detail-free 503. Under the old rule that went to
    DEBUG and vanished at INFO in production.
    """
    caplog.set_level(logging.DEBUG, logger=author_router._LOG.name)
    author_router._customization_error(
        CustomizationServiceError("customization_harness_unavailable", 503)
    )
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "a deployment fault must be visible at WARNING"
    assert "customization_harness_unavailable" in warnings[0].getMessage()


def test_the_legacy_author_path_does_not_emit_exception_text(caplog, capsys, monkeypatch):
    """`/api/author` falls back to the templater and used to `print(str(exc))`.

    stdout is captured as well as the logger: the original defect was a bare
    `print`, which caplog cannot see, so a caplog-only assertion would pass
    vacuously the moment the print came back.
    """
    import requests

    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.internal:8150")
    monkeypatch.setattr(
        requests, "post",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError(SENTINEL)),
    )
    caplog.set_level(logging.DEBUG, logger=author_router._LOG.name)
    try:
        author_router._legacy_author(
            author_router.AuthorRequest(description="count panels", mode="build"), TENANT
        )
    except Exception:
        pass  # the templater may be unconfigured here; what it emitted is the point
    captured = capsys.readouterr()
    emitted = "\n".join(
        [r.getMessage() for r in caplog.records] + [captured.out, captured.err]
    )
    # Non-vacuous: the fallback must have said something, or this proves nothing.
    assert "author_harness_unreachable" in emitted
    assert "hunter2-should-never-be-logged" not in emitted
    assert SENTINEL not in emitted


@pytest.mark.parametrize("operation,diagnostic,public_code", [
    ("provision", "harness_provision_failed", "tenant_repository_unavailable"),
    ("stage", "harness_stage_failed", "customization_harness_unavailable"),
    ("publish", "harness_publish_failed", "customization_publish_incomplete"),
])
def test_harness_diagnostics_discard_endpoint_status_and_message(
    operation, diagnostic, public_code, monkeypatch, caplog, capsys
):
    import requests

    endpoint_marker = "private-endpoint-marker-9f3a"
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", f"http://{endpoint_marker}:8150")
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "unused-secret")
    failure = requests.HTTPError(SENTINEL)
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(requests, "post", fail)
    monkeypatch.setattr(customization_service.deps, "active_stage_author_subject",
                        lambda *args: AUTHOR_SUBJECT)
    with pytest.raises(CustomizationServiceError) as caught:
        if operation == "provision":
            customization_service._ensure_bare_repo(TENANT)
        elif operation == "stage":
            customization_service.CustomizationService._harness_stage(
                None, TENANT, "count panels", _change_set(),
            )
        else:
            service = SimpleNamespace(_raw_receipt=lambda change: {})
            customization_service.CustomizationService._harness_publish(service, _change_set())
    _assert_safe_failure(caught.value, diagnostic, "HTTPError", caplog, capsys,
                         public_code=public_code)
    assert caught.value.__cause__ is failure
    assert endpoint_marker not in repr(vars(caught.value)) + caplog.text


def test_unavailable_validates_before_logging(caplog, capsys):
    failure = OSError(SENTINEL)
    error = customization_service._unavailable(SENTINEL, cause=failure)
    _assert_safe_failure(error, "customization_refused", "OSError", caplog, capsys)


def test_unexpected_cause_never_renders_chained_messages(caplog, capsys):
    caplog.set_level(logging.DEBUG, logger=author_router._LOG.name)
    try:
        try:
            raise ValueError(SENTINEL)
        except ValueError as inner:
            raise RuntimeError("private-chained-message-marker-9f3a") from inner
    except RuntimeError as failure:
        author_router._customization_error(
            CustomizationServiceError("customization_stage_failed", 503),
            cause=failure,
        )
    assert caplog.records[-1].levelno == logging.ERROR
    assert caplog.records[-1].getMessage() == (
        "customization_refused: code=customization_stage_failed "
        "detail=customization_unexpected_failure exception_type=RuntimeError"
    )
    emitted = caplog.text + capsys.readouterr().err
    assert SENTINEL not in emitted
    assert "private-chained-message-marker-9f3a" not in emitted
    assert "test_unexpected_cause_never_renders_chained_messages" not in emitted
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("kwargs,expected", [
    ({"detail": SENTINEL}, "customization_refused"),
    ({"detail": "git_failed: " + SENTINEL}, "customization_refused"),
    ({"detail": "git_failed"}, "git_failed"),
    ({"diagnostic_code": SENTINEL}, "customization_refused"),
    ({"diagnostic_code": []}, "customization_refused"),
    ({"detail": "git_failed", "diagnostic_code": "unknown"}, "customization_refused"),
])
def test_diagnostic_constructor_validates_exact_codes(kwargs, expected, caplog, capsys):
    error = CustomizationServiceError("tenant_repository_unavailable", 503, **kwargs)
    assert str(error) == "tenant_repository_unavailable"
    assert error.code == "tenant_repository_unavailable"
    assert error.status_code == 503
    assert error.diagnostic_code == error.detail == expected
    assert error.exception_type is None
    author_router._customization_error(error)
    assert SENTINEL not in repr(vars(error)) + caplog.text + capsys.readouterr().err
    assert caplog.records[-1].getMessage() == (
        f"customization_refused: code=tenant_repository_unavailable "
        f"detail={expected} exception_type=None"
    )


def test_legacy_positional_detail_and_actual_exception_are_safe(caplog, capsys):
    failure = RuntimeError(SENTINEL)
    error = CustomizationServiceError(
        "tenant_repository_unavailable", 503, SENTINEL, cause=failure,
    )
    assert error.diagnostic_code == error.detail == "customization_refused"
    assert error.exception_type == "RuntimeError"
    author_router._customization_error(error)
    assert caplog.records[-1].getMessage() == (
        "customization_refused: code=tenant_repository_unavailable "
        "detail=customization_refused exception_type=RuntimeError"
    )
    assert SENTINEL not in repr(vars(error)) + caplog.text + capsys.readouterr().err


def _assert_safe_failure(error, diagnostic, exception_type, caplog, capsys,
                         public_code="effective_catalog_unavailable"):
    assert error.code == public_code
    assert error.status_code == 503
    assert error.diagnostic_code == error.detail == diagnostic
    assert error.exception_type == exception_type
    author_router._customization_error(error)
    emitted = caplog.text + capsys.readouterr().err
    assert SENTINEL not in emitted + repr(vars(error))
    assert all(record.exc_info is None for record in caplog.records)
    assert f"detail={diagnostic} exception_type={exception_type}" in emitted


@pytest.mark.parametrize("operation,diagnostic", [
    ("_git", "git_failed"), ("_git_blob", "git_show_failed"),
])
def test_git_helpers_discard_marked_output(
    operation, diagnostic, tmp_path, monkeypatch, caplog, capsys
):
    import subprocess

    failure = subprocess.CalledProcessError(
        128, ["git", SENTINEL], output=SENTINEL, stderr=SENTINEL,
    )
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(customization_service.subprocess, "run", fail)
    with pytest.raises(CustomizationServiceError) as caught:
        getattr(customization_service, operation)(tmp_path / SENTINEL, SENTINEL)
    _assert_safe_failure(caught.value, diagnostic, "CalledProcessError", caplog, capsys,
                         public_code="tenant_repository_unavailable")
    assert caught.value.__cause__ is failure


@pytest.mark.parametrize("operation,exception_class,diagnostic", [
    ("effective_catalog_dir", OSError, "catalog_directory_failed"),
    ("effective_catalog_pin", RuntimeError, "catalog_pin_lookup_failed"),
])
def test_catalog_store_failures_are_safe(
    operation, exception_class, diagnostic, tmp_path, monkeypatch, caplog, capsys
):
    database = tmp_path / "customization.db"
    database.touch()
    monkeypatch.setenv("LEAF_CUSTOMIZATION_STORE", "sqlite")
    monkeypatch.setattr(customization_service, "database_path", lambda: database)
    failure = exception_class(SENTINEL)
    def fail(*args, **kwargs):
        raise failure
    if operation == "effective_catalog_dir":
        monkeypatch.setattr(
            customization_service.CustomizationService, "configured",
            lambda: SimpleNamespace(store=SimpleNamespace(
                get_effective_catalog=lambda **kwargs: SimpleNamespace(
                    catalog_commit="a" * 40, catalog_digest="b" * 64,
                ),
            )),
        )
        monkeypatch.setattr(customization_service, "_bare_repo", fail)
    else:
        monkeypatch.setattr(
            customization_service.CustomizationService, "configured",
            lambda: SimpleNamespace(store=SimpleNamespace(get_effective_catalog=fail)),
        )
    with pytest.raises(CustomizationServiceError) as caught:
        getattr(customization_service, operation)(TENANT)
    _assert_safe_failure(caught.value, diagnostic, exception_class.__name__, caplog, capsys)
    assert caught.value.__cause__ is failure


@pytest.mark.parametrize("branch,diagnostic,exception_type,public_code", [
    ("exception", "catalog_worktree_verification_failed", "TimeoutExpired",
     "effective_catalog_unavailable"),
    ("probe", "catalog_head_probe_failed", None, "effective_catalog_unavailable"),
    ("pending", "catalog_head_pending", None, "effective_catalog_unavailable"),
    ("commit", "catalog_commit_mismatch", None, "effective_catalog_commit_mismatch"),
    ("digest", "catalog_digest_mismatch", None, "effective_catalog_digest_mismatch"),
])
def test_worktree_verification_diagnostics(
    branch, diagnostic, exception_type, public_code,
    tmp_path, monkeypatch, caplog, capsys
):
    import subprocess

    commit = "a" * 40
    target = tmp_path / "worktree"
    target.mkdir()
    (target / "registry.json").write_bytes(b"{}")
    monkeypatch.setattr(customization_service, "_MATERIALIZE_ATTEMPTS", 1)
    def probe(*args, **kwargs):
        if branch == "exception":
            raise subprocess.TimeoutExpired(SENTINEL, 10, output=SENTINEL, stderr=SENTINEL)
        observed = {
            "probe": "", "pending": "0" * 40, "commit": "b" * 40, "digest": commit,
        }[branch]
        return SimpleNamespace(returncode=1 if branch == "probe" else 0,
                               stdout=observed, stderr=SENTINEL)
    monkeypatch.setattr(customization_service.subprocess, "run", probe)
    with pytest.raises(CustomizationServiceError) as caught:
        customization_service._verified_worktree(tmp_path, target, commit, "c" * 64)
    _assert_safe_failure(caught.value, diagnostic, exception_type, caplog, capsys,
                         public_code=public_code)


def test_materialization_results_do_not_become_diagnostic_text(
    tmp_path, monkeypatch, caplog, capsys
):
    calls = []
    def result(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=128, stdout=SENTINEL, stderr=SENTINEL)
    monkeypatch.setattr(customization_service.subprocess, "run", result)
    with pytest.raises(CustomizationServiceError) as caught:
        customization_service._materialize_worktree(
            tmp_path / "bare", tmp_path / "catalog" / "target", "a" * 40,
        )
    assert len(calls) == 3  # add, prune, retry
    _assert_safe_failure(caught.value, "catalog_worktree_add_failed", None, caplog, capsys)


@pytest.mark.parametrize("branch,diagnostic", [
    ("import", "dependency_import_failed"),
    ("engine", "engine_registry_load_failed"),
    ("write", "write_tools_load_failed"),
    ("catalog", "catalog_tools_load_failed"),
    ("tenant", "tenant_registry_load_failed"),
    ("surface", "tenant_surface_config_load_failed"),
])
def test_dependency_diagnostics_omit_messages_and_paths(
    branch, diagnostic, tmp_path, monkeypatch, caplog, capsys
):
    import deps
    from _vendor.mushy_fold import registry

    marked = tmp_path / "private-path-marker-9f3a"
    marked.mkdir()
    failure = RuntimeError(SENTINEL)
    def fail(*args, **kwargs):
        raise failure
    def callback_loader(root, *, on_error):
        on_error(marked, failure)
        return [] if branch == "tenant" else {}

    if branch == "import":
        source = marked / "module.py"
        source.touch()
        monkeypatch.setattr(deps.importlib.util, "spec_from_file_location", fail)
        assert deps._load_module_from(source, "test_diagnostic_import") is None
    elif branch in {"engine", "write", "catalog"}:
        source = marked / "registry.json"
        source.touch()
        attribute, loader = {
            "engine": ("ENGINE_REGISTRY", deps.load_engine_registry_tools),
            "write": ("WRITE_TOOLS_STORE", deps.load_seed_write_tools),
            "catalog": ("CATALOG_TOOLS_STORE", deps.load_seed_catalog_tools),
        }[branch]
        monkeypatch.setattr(deps, attribute, source)
        monkeypatch.setattr(type(source), "read_text", fail)
        monkeypatch.setattr(deps.solar_tools, "registry_records", lambda: [])
        assert loader() == (list(deps.fb.DEFAULT_TOOLS) if branch == "engine" else [])
    elif branch == "tenant":
        monkeypatch.setattr(customization_service, "load_authoritative_tenant_tools",
                            lambda tenant: None)
        monkeypatch.setattr(deps, "_verified_tenant_fallback", lambda tenant: (marked, None))
        monkeypatch.setattr(registry, "load_repo_registry_tools", callback_loader)
        assert deps.load_tenant_repo_tools(TENANT) == []
    else:
        monkeypatch.setattr(deps, "_surface_config_cache", {})
        monkeypatch.setattr(deps, "_surface_config_warned_tenants", set())
        monkeypatch.setattr(deps, "_contained_tenant_root", lambda tenant: str(marked))
        monkeypatch.setattr(deps, "_contained_surface_config_path",
                            lambda root: str(marked / "surface-config.json"))
        monkeypatch.setattr(deps, "load_repo_surface_config", callback_loader)
        assert deps.effective_surface_config(TENANT) == {}
        # Expire the cache but retain the existing once-per-tenant suppression.
        deps._surface_config_cache.clear()
        assert deps.effective_surface_config(TENANT) == {}
    captured = capsys.readouterr()
    assert captured.err.count(f"diagnostic_code={diagnostic} exception_type=RuntimeError") == 1
    assert SENTINEL not in caplog.text + captured.err
    assert str(marked) not in caplog.text + captured.err


@pytest.mark.parametrize("branch", ["provenance", "collision", "picker"])
def test_capabilities_stderr_uses_codes_and_types(
    branch, monkeypatch, caplog, capsys
):
    from routers import capabilities as capabilities_router
    deps = capabilities_router.deps
    exception_class = {
        "provenance": deps.ToolCatalogProvenanceError,
        "collision": deps.ToolCatalogCollisionError,
        "picker": RuntimeError,
    }[branch]
    def fail(*args, **kwargs):
        raise exception_class(SENTINEL)
    if branch == "picker":
        monkeypatch.setattr(deps, "all_tools", fail)
        captured_tools = []
        monkeypatch.setattr(
            capabilities_router.converse_registry, "build_registry",
            lambda *, tools, skills: captured_tools.append(tools) or {"commands": [], "tools": []},
        )
        assert capabilities_router.converse_registry_route(tenant=TENANT) == {
            "commands": [], "tools": [], "error": None, "degraded_mode": False,
        }
        assert captured_tools == [[]]
        diagnostic = "tool_catalog_unavailable"
    else:
        tool = {"name": "diagnostic-reader", "version": "1.0.0", "aps_live": True,
                "capabilities": ["drawing.read"], "params": {"type": "object"},
                "entry": "tools/diagnostic-reader/tool.py"}
        monkeypatch.setattr(deps, "effective_tools_with_provenance", fail)
        monkeypatch.setattr(deps, "TOOL_SOURCE_OPERATOR_OWNED_ENGINE", "operator_owned_engine")
        monkeypatch.setattr(deps, "all_tools", lambda tenant: [tool])
        monkeypatch.setattr(deps, "load_engine_registry_tools", lambda: [tool])
        monkeypatch.setattr(deps, "APS_LIVE", True)
        monkeypatch.setattr(customization_service, "effective_catalog_pin", lambda tenant: None)
        monkeypatch.setattr(capabilities_router.mcp_tool_projection, "projected_tools",
                            lambda tenant: [])
        monkeypatch.setattr(
            capabilities_router.catalog, "build_catalog",
            lambda tools, **kwargs: [{"capabilities": [
                capabilities_router.catalog._capability_entry(row) for row in tools]}],
        )
        monkeypatch.setattr(capabilities_router.availability, "annotate_w1_availability",
                            lambda families, *args, **kwargs: families)
        response = capabilities_router.capabilities(
            tenant=TENANT, x_internal_role=None, x_ops_secret=None,
            project_id=None, x_project_id=None,
        )
        assert response["error"] is None
        rows = response["families"][0]["capabilities"]
        assert len(rows) == 1
        assert rows[0]["name"] == tool["name"]
        assert rows[0]["aps_live"] is False
        diagnostic = "catalog_provenance_unavailable"
    captured = capsys.readouterr()
    assert f"diagnostic_code={diagnostic} exception_type={exception_class.__name__}" in captured.err
    assert SENTINEL not in caplog.text + captured.err



def test_the_legacy_response_catch_also_hides_exception_text(caplog, capsys, monkeypatch):
    """The SECOND fallback, which the transport stub never reached.

    `requests.post` must SUCCEED and the failure happen while processing the
    response, or `author.py`'s response-processing catch stays uncovered and
    restoring its raw print goes unnoticed.
    """
    import requests

    class _Response:
        status_code = 200

        def json(self):
            # Must be ValueError: production guards only that, so anything else
            # escapes the function before the response catch is reached.
            raise ValueError("not json")

        def raise_for_status(self):
            raise RuntimeError(SENTINEL)

    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.internal:8150")
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Response())
    caplog.set_level(logging.DEBUG, logger=author_router._LOG.name)
    try:
        author_router._legacy_author(
            author_router.AuthorRequest(description="count panels", mode="build"), TENANT
        )
    except Exception:
        pass
    captured = capsys.readouterr()
    emitted = "\n".join(
        [r.getMessage() for r in caplog.records] + [captured.out, captured.err]
    )
    assert "author_harness_error" in emitted, "the response catch must be reached"
    assert "hunter2-should-never-be-logged" not in emitted
    assert SENTINEL not in emitted
