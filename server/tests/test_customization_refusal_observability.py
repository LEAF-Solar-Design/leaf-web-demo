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
    assert "s3cret-harness-token" not in caught.value.detail


def test_failed_provision_call_records_status_and_exception_type(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_TENANT_GIT_DIR", str(tmp_path))
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.internal:8150")
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "unused-by-the-stub")

    class _Boom(RuntimeError):
        pass

    def _explode(*_args, **_kwargs):
        raise _Boom("connection refused")

    import requests
    monkeypatch.setattr(requests, "post", _explode)
    with pytest.raises(CustomizationServiceError) as caught:
        _ensure_bare_repo("66666666-6666-4666-8666-666666666666")
    detail = caught.value.detail
    assert "harness_provision_failed" in detail
    assert "_Boom" in detail
    assert "/author/repository" in detail


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


def test_unexpected_exception_is_logged_with_a_traceback(caplog):
    """The six handlers that used to discard the exception entirely."""
    caplog.set_level(logging.ERROR, logger=author_router._LOG.name)
    author_router._customization_error(
        CustomizationServiceError("customization_stage_failed", 503),
        cause=ZeroDivisionError("division by zero"),
    )
    records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert records, "an unexpected cause must be logged at ERROR"
    assert records[0].exc_info is not None, "the traceback must be preserved"
    assert "ZeroDivisionError" in records[0].getMessage()
