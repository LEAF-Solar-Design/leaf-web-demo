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


@pytest.fixture
def export_configured(monkeypatch):
    monkeypatch.setenv('LEAF_AUTHOR_HARNESS_URL', 'http://harness.invalid')
    monkeypatch.setenv('LEAF_HARNESS_SECRET', 'test-secret')
    authority = dict(tenant_id=ORG, organization_id=ORG, project_id=PROJECT, repo_key=REPO)
    calls = []
    def resolve(*args):
        assert args == (ORG, ORG, PROJECT)
        return authority
    monkeypatch.setattr(source.platform_link, 'platform_store', lambda: SimpleNamespace(
        resolve_project_repository_authority=resolve))
    bundle = b'# v2 git bundle\nexact private bytes\x00\xff'
    response = SimpleNamespace(status_code=200, headers={}, closed=False)
    def close():
        response.closed = True
    response.close = close
    response.iter_content = lambda chunk_size: iter([bundle[:8], bundle[8:]])
    def post(url, *, data, headers, timeout, stream, allow_redirects):
        assert url == 'http://harness.invalid/internal/project-repository-source/export'
        assert timeout == (10, 30) and stream is True and allow_redirects is False
        assert headers['Accept-Encoding'] == 'identity'
        assert headers['X-Tenant-Id'] == ORG and headers['X-Harness-Secret'] == 'test-secret'
        body = json.loads(data)
        assert body == dict(authority, source_commit='a' * 40, source_tree='b' * 40)
        calls.append(body)
        return response
    body = dict(authority, source_commit='a' * 40, source_tree='b' * 40, contract=source.BUNDLE_CONTRACT)
    response.headers.update({
        'Content-Type': 'application/octet-stream', 'Content-Length': str(len(bundle)),
        'X-Leaf-Source-Contract': source.BUNDLE_CONTRACT,
        'X-Leaf-Request-Digest': hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
        'X-Leaf-Source-Commit': 'a' * 40, 'X-Leaf-Source-Tree': 'b' * 40,
        'X-Leaf-Bundle-Sha256': hashlib.sha256(bundle).hexdigest(),
        'X-Leaf-Lease-Id': LEASE, 'X-Leaf-Lease-Generation': '8',
    })
    monkeypatch.setattr(source.requests, 'post', post)
    return response, bundle, calls


def export():
    return source.export_project_source_bundle(ORG, ORG, PROJECT, 'a' * 40, 'b' * 40)


def test_export_exact_bytes_and_read_witness(export_configured):
    response, bundle, calls = export_configured
    assert export() == dict(bundle=bundle, source_commit='a' * 40, source_tree='b' * 40,
                            bundle_sha256=hashlib.sha256(bundle).hexdigest(), size_bytes=len(bundle),
                            lease_id=LEASE, lease_generation='8')
    assert response.closed and len(calls) == 1


@pytest.mark.parametrize('field,value', [
    ('Content-Type', 'application/json'), ('Content-Encoding', 'gzip'),
    ('Content-Length', '0'), ('Content-Length', '67108865'), ('Content-Length', '01'),
    ('Content-Length', 'invalid'), ('Content-Length', '1'),
    ('X-Leaf-Source-Contract', 'wrong'), ('X-Leaf-Request-Digest', 'f' * 64),
    ('X-Leaf-Source-Commit', 'c' * 40), ('X-Leaf-Source-Tree', 'd' * 40),
    ('X-Leaf-Bundle-Sha256', 'f' * 64), ('X-Leaf-Lease-Id', 'invalid'),
    ('X-Leaf-Lease-Generation', '0'), ('X-Leaf-Lease-Generation', '01'),
])
def test_export_wrong_headers_or_hash_close_response(export_configured, field, value):
    response = export_configured[0]
    response.headers[field] = value
    with pytest.raises(source.SourceUnavailable, match='^source is unavailable$'):
        export()
    assert response.closed


def test_export_caps_stream_before_accumulation(export_configured):
    response = export_configured[0]
    response.headers['Content-Length'] = str(source.MAX_BUNDLE_BYTES)
    def chunks(chunk_size):
        assert chunk_size == 65536
        chunk = b'x' * chunk_size
        for _ in range(source.MAX_BUNDLE_BYTES // chunk_size + 1):
            yield chunk
        pytest.fail('read past the stream cap')
    response.iter_content = chunks
    with pytest.raises(source.SourceUnavailable):
        export()
    assert response.closed


def test_export_truncated_and_interrupted_stream_close(export_configured):
    response = export_configured[0]
    response.iter_content = lambda chunk_size: iter([b'short'])
    with pytest.raises(source.SourceUnavailable):
        export()
    assert response.closed
    response.closed = False
    def interrupted(chunk_size):
        yield b'partial'
        raise requests.Timeout('private path')
    response.iter_content = interrupted
    with pytest.raises(source.SourceUnavailable, match='^source is unavailable$'):
        export()
    assert response.closed


@pytest.mark.parametrize('status,exception', [(409, source.SourceConflict), (401, source.SourceUnavailable),
                                           (503, source.SourceUnavailable), (302, source.SourceUnavailable)])
def test_export_http_error_closes(export_configured, status, exception):
    response = export_configured[0]
    response.status_code = status
    with pytest.raises(exception):
        export()
    assert response.closed


@pytest.mark.parametrize('field', ['tenant_id', 'organization_id', 'project_id', 'repo_key'])
def test_export_denies_authority_mismatch_without_transport(export_configured, monkeypatch, field):
    authority = dict(tenant_id=ORG, organization_id=ORG, project_id=PROJECT, repo_key=REPO)
    authority[field] = 'invalid' if field == 'repo_key' else str(uuid.uuid4())
    monkeypatch.setattr(source.platform_link, 'platform_store', lambda: SimpleNamespace(
        resolve_project_repository_authority=lambda *args: authority))
    with pytest.raises(source.SourceConflict):
        export()
    assert not export_configured[2]


def test_export_does_not_create_missing_repository(export_configured, monkeypatch):
    monkeypatch.setattr(source.platform_link, 'platform_store', lambda: SimpleNamespace(
        resolve_project_repository_authority=lambda *args: None,
        ensure_project_repository_authority=lambda *args: pytest.fail('export must not create authority')))
    with pytest.raises(source.SourceConflict):
        export()
    assert not export_configured[2]
