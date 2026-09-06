"""Closed trusted-client proofs for campaign project source production."""
import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest
import requests

import project_repository_source as source

ORG, PROJECT, REPO, LEASE = [str(uuid.uuid4()) for _ in range(4)]
PROMPT = 'Recettes 🍲\nPréserver exactement.\n'


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv('LEAF_AUTHOR_HARNESS_URL', 'http://harness.invalid')
    monkeypatch.setenv('LEAF_HARNESS_SECRET', 'test-secret')
    calls = []

    def ensure(*args):
        assert args == (ORG, ORG, PROJECT)
        return dict(tenant_id=ORG, organization_id=ORG, project_id=PROJECT, repo_key=REPO)

    monkeypatch.setattr(source.platform_link, 'platform_store', lambda: SimpleNamespace(
        ensure_project_repository_authority=ensure))

    def post(url, *, data, headers, timeout):
        body = json.loads(data)
        calls.append(body)
        assert body['seed_document'] == PROMPT
        assert '🍲'.encode('utf-8') in data
        assert headers['X-Tenant-Id'] == ORG and headers['X-Harness-Secret'] == 'test-secret'
        assert timeout == 10
        canonical = {key: value for key, value in body.items() if key != 'seed_document'}
        canonical['contract'] = source.CONTRACT
        result = dict(contract=source.CONTRACT, request_digest=hashlib.sha256(json.dumps(
            canonical, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            source_commit='a' * 40, source_tree='b' * 40, seed_digest=body['seed_digest'],
            replayed=False, writer_lease_id=LEASE, writer_lease_generation='7')
        return SimpleNamespace(status_code=200, json=lambda **kwargs: result)

    monkeypatch.setattr(source.requests, 'post', post)
    return calls, post


def test_exact_unicode_and_closed_result(configured):
    result = source.initialize_project_source(ORG, ORG, PROJECT, PROMPT)
    assert result == dict(source_commit='a' * 40, source_tree='b' * 40,
                          seed_digest=hashlib.sha256(PROMPT.encode()).hexdigest(), replayed=False)
    assert len(configured[0]) == 1


@pytest.mark.parametrize('field,value', [
    ('contract', 'wrong'), ('request_digest', 'f' * 64), ('source_commit', 'a' * 39),
    ('source_tree', 'A' * 40), ('seed_digest', 'wrong'), ('replayed', 1),
    ('writer_lease_id', 'bad'), ('writer_lease_generation', 7),
    ('writer_lease_generation', '0'), ('path', 'private'), ('seed_digest', 'f' * 64),
])
def test_response_mismatch_fails_closed(configured, monkeypatch, field, value):
    def post(*args, **kwargs):
        response = configured[1](*args, **kwargs)
        result = response.json()
        result[field] = value
        return SimpleNamespace(status_code=200, json=lambda **kwargs: result)
    monkeypatch.setattr(source.requests, 'post', post)
    with pytest.raises(source.SourceUnavailable):
        source.initialize_project_source(ORG, ORG, PROJECT, PROMPT)


def test_replay_can_return_original_seed(configured, monkeypatch):
    def post(*args, **kwargs):
        result = configured[1](*args, **kwargs).json()
        result.update(replayed=True, seed_digest='f' * 64)
        return SimpleNamespace(status_code=200, json=lambda **kwargs: result)
    monkeypatch.setattr(source.requests, 'post', post)
    assert source.initialize_project_source(ORG, ORG, PROJECT, PROMPT)['seed_digest'] == 'f' * 64


@pytest.mark.parametrize('status,exception', [(409, source.SourceConflict), (401, source.SourceUnavailable),
                                           (503, source.SourceUnavailable)])
def test_http_failures_are_narrow(configured, monkeypatch, status, exception):
    monkeypatch.setattr(source.requests, 'post', lambda *a, **k: SimpleNamespace(status_code=status))
    with pytest.raises(exception):
        source.initialize_project_source(ORG, ORG, PROJECT, PROMPT)


def test_missing_secret_and_timeout(configured, monkeypatch):
    monkeypatch.delenv('LEAF_HARNESS_SECRET')
    with pytest.raises(source.SourceUnavailable):
        source.initialize_project_source(ORG, ORG, PROJECT, PROMPT)
    assert not configured[0]
    monkeypatch.setenv('LEAF_HARNESS_SECRET', 'test-secret')
    def timeout(*a, **k):
        raise requests.Timeout('private endpoint')
    monkeypatch.setattr(source.requests, 'post', timeout)
    with pytest.raises(source.SourceUnavailable, match='^source is unavailable$'):
        source.initialize_project_source(ORG, ORG, PROJECT, PROMPT)


def test_authority_mismatch_never_calls_provider(configured, monkeypatch):
    monkeypatch.setattr(source.platform_link, 'platform_store', lambda: SimpleNamespace(
        ensure_project_repository_authority=lambda *a: dict(
            tenant_id=ORG, organization_id=ORG, project_id=str(uuid.uuid4()), repo_key=REPO)))
    with pytest.raises(source.SourceConflict):
        source.initialize_project_source(ORG, ORG, PROJECT, PROMPT)
    assert not configured[0]
