from __future__ import annotations

import hashlib
import json
import sys
import uuid
from pathlib import Path

import pytest
import requests

SERVER = Path(__file__).resolve().parents[1]
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import annotation_source_authority as authority  # noqa: E402

TENANT = str(uuid.uuid4())
PROJECT = str(uuid.uuid4())
DRAWING = str(uuid.uuid4())
REPO = str(uuid.uuid4())
BASE = "1" * 40
BASE_TREE = "2" * 40
HEAD = "3" * 40
HEAD_TREE = "4" * 40


class MappingStore:
    def resolve_project_repository_authority(self, tenant, organization, project):
        assert (tenant, organization, project) == (TENANT, TENANT, PROJECT)
        return {"repo_key": REPO}


class Response:
    def __init__(self, status_code: int, body: object):
        self.status_code = status_code
        self.content = (
            body if isinstance(body, bytes)
            else json.dumps(body, separators=(",", ":")).encode("utf-8")
        )


def canonical_digest(body: dict[str, str]) -> str:
    encoded = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(authority.platform_link, "platform_store", lambda: MappingStore())
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.internal:8150/")
    monkeypatch.setenv("LEAF_HARNESS_SECRET", "private-harness-secret")
    calls = []

    def post(url, *, json, headers, timeout):
        calls.append((url, json, headers, timeout))
        return Response(200, {
            "contract": "leaf.project-repository-source-witness.v1",
            "request_digest": canonical_digest(json),
        })

    monkeypatch.setattr(authority.requests, "post", post)
    return calls


def request(**changes):
    values = {
        "tenant_id": TENANT, "org_id": TENANT, "project_id": PROJECT,
        "drawing_id": DRAWING, "repository_id": REPO, "relation": "preview",
        "commit_sha": HEAD, "tree_sha": HEAD_TREE, "base_commit": BASE,
        "base_tree": BASE_TREE, "reverses_commit": None, "reverses_tree": None,
    }
    values.update(changes)
    return authority.source_request(**values)


def test_preview_receipt_calls_private_harness_and_revalidates(configured):
    value = request()
    receipt = authority.SOURCE_AUTHORITY.verify(value)
    assert authority.SOURCE_AUTHORITY.validate(receipt, value) is True
    assert len(configured) == 2
    url, body, headers, timeout = configured[0]
    assert url == "http://harness.internal:8150/internal/project-repository-source/verify"
    assert headers == {
        "X-Harness-Secret": "private-harness-secret", "X-Tenant-Id": TENANT,
    }
    assert timeout == 10
    assert body == {
        "tenant_id": TENANT, "organization_id": TENANT,
        "project_id": PROJECT, "repo_key": REPO, "relation": "preview",
        "base_commit": BASE, "base_tree": BASE_TREE,
        "candidate_commit": HEAD, "candidate_tree": HEAD_TREE,
    }
    assert receipt.receipt_digest == canonical_digest(body)
    assert not any(key in body for key in ("root", "path", "ref", "url", "drawing_id"))


def test_inverse_maps_original_target_and_inverse_exactly(configured):
    original = "5" * 40
    original_tree = "6" * 40
    inverse = request(
        relation="inverse", reverses_commit=original,
        reverses_tree=original_tree,
    )
    receipt = authority.SOURCE_AUTHORITY.verify(inverse)
    body = configured[0][1]
    assert body == {
        "tenant_id": TENANT, "organization_id": TENANT,
        "project_id": PROJECT, "repo_key": REPO, "relation": "inverse",
        "original_commit": original, "original_tree": original_tree,
        "target_commit": BASE, "target_tree": BASE_TREE,
        "inverse_commit": HEAD, "inverse_tree": HEAD_TREE,
    }
    assert receipt.receipt_digest == canonical_digest(body)


@pytest.mark.parametrize("missing", ["url", "secret"])
def test_missing_private_harness_configuration_fails_closed(monkeypatch, missing):
    monkeypatch.delenv(
        "LEAF_AUTHOR_HARNESS_URL" if missing == "url" else "LEAF_HARNESS_SECRET",
        raising=False,
    )
    monkeypatch.setattr(
        authority.requests, "post", lambda *_a, **_k: pytest.fail("must not call harness"),
    )
    with pytest.raises(authority.SourceAuthorityUnavailable):
        authority.SOURCE_AUTHORITY.verify(request())


@pytest.mark.parametrize(
    "response",
    [
        Response(409, {"error": {"code": "source_verification_failed"}}),
        Response(200, b"not-json"),
        Response(200, {"contract": "wrong", "request_digest": "0" * 64}),
        Response(200, {"contract": "leaf.project-repository-source-witness.v1",
                       "request_digest": "0" * 64}),
        Response(200, {"contract": "leaf.project-repository-source-witness.v1",
                       "request_digest": "0" * 64, "extra": "field"}),
        Response(200, b'{"contract":"leaf.project-repository-source-witness.v1",'
                        b'"contract":"leaf.project-repository-source-witness.v1",'
                        b'"request_digest":"0000000000000000000000000000000000000000000000000000000000000000"}'),
    ],
)
def test_malformed_error_or_mismatched_harness_response_fails_closed(
    monkeypatch, response,
):
    monkeypatch.setattr(authority.requests, "post", lambda *_a, **_k: response)
    with pytest.raises(authority.SourceAuthorityUnavailable):
        authority.SOURCE_AUTHORITY.verify(request())


def test_transport_failure_fails_closed(monkeypatch):
    monkeypatch.setattr(
        authority.requests, "post",
        lambda *_a, **_k: (_ for _ in ()).throw(requests.ConnectionError("offline")),
    )
    with pytest.raises(authority.SourceAuthorityUnavailable):
        authority.SOURCE_AUTHORITY.verify(request())


def test_foreign_repository_mapping_never_reaches_harness(monkeypatch):
    monkeypatch.setattr(
        authority.platform_link, "platform_store",
        lambda: type("S", (), {"resolve_project_repository_authority":
            lambda *_: {"repo_key": str(uuid.uuid4())}})(),
    )
    monkeypatch.setattr(
        authority.requests, "post", lambda *_a, **_k: pytest.fail("must not call harness"),
    )
    with pytest.raises(authority.SourceAuthorityUnavailable):
        authority.SOURCE_AUTHORITY.verify(request())


@pytest.mark.parametrize("digest", ["0" * 64, "short", None])
def test_tampered_or_malformed_receipt_digest_is_rejected(digest):
    receipt = authority.SOURCE_AUTHORITY.verify(request())
    changed = type(receipt)(receipt.request, digest)
    assert authority.SOURCE_AUTHORITY.validate(changed, request()) is False


@pytest.mark.parametrize(
    "changes",
    [
        {"project_id": "not-a-uuid"},
        {"commit_sha": "A" * 40},
        {"tree_sha": "1" * 39},
        {"org_id": str(uuid.uuid4())},
        {"relation": "target_source", "commit_sha": BASE, "tree_sha": BASE_TREE},
        {"reverses_commit": "5" * 40, "reverses_tree": "6" * 40},
    ],
)
def test_malformed_or_unsupported_tuple_never_reaches_harness(monkeypatch, changes):
    monkeypatch.setattr(
        authority.requests, "post", lambda *_a, **_k: pytest.fail("must not call harness"),
    )
    with pytest.raises(authority.SourceAuthorityUnavailable):
        authority.SOURCE_AUTHORITY.verify(request(**changes))
