"""Customer bundle boundary: exact durable witnesses and fresh authorization."""
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import browser_project_source_export as export
import browser_project_flow as flow
import deps

ORG = '11111111-1111-4111-8111-111111111111'
PROJECT = '22222222-2222-4222-8222-222222222222'
EDIT = '44444444-4444-4444-8444-444444444444'
ACTOR = '33333333-3333-4333-8333-333333333333'
BUNDLE = b'# v2 git bundle\n' + b'c' * 40 + b' refs/heads/main\n\nPACK\x00\x00\x00\x02'


@pytest.fixture
def boundary(monkeypatch):
    tenant = deps.TenantContext(ORG, subject='person', authority_resolved=True)
    row = dict(project_id=PROJECT, source_request_id=EDIT, state='published', version=2,
               source_commit='c' * 40, source_tree='d' * 40, receipt_digest='a' * 64)
    authority = dict(tenant_id=ORG, organization_id=ORG, project_id=PROJECT, repo_key=ACTOR)
    result = dict(bundle=BUNDLE, size_bytes=len(BUNDLE), bundle_sha256=hashlib.sha256(BUNDLE).hexdigest(),
                  source_commit=row['source_commit'], source_tree=row['source_tree'],
                  lease_id=ACTOR, lease_generation='1')
    calls = []
    monkeypatch.setattr(flow, 'scope', lambda *a: (ORG, PROJECT, ACTOR))
    monkeypatch.setattr(flow, 'repository', lambda *a: authority)
    monkeypatch.setattr(flow, 'read', lambda *a: row)
    def producer(*args, **kwargs):
        calls.append((args, kwargs))
        return result
    monkeypatch.setattr(export.source, 'export_project_source_bundle', producer)
    query = [(key, str(row[key])) for key in export._WITNESSES]
    return tenant, row, authority, result, calls, query


def download(boundary):
    return export.export_bundle(boundary[0], PROJECT, EDIT, boundary[-1])


def test_exact_bytes_headers_and_real_producer_entrypoint(boundary):
    bundle, headers = download(boundary)
    assert bundle == BUNDLE
    assert headers['Content-Length'] == str(len(BUNDLE))
    assert headers['X-Leaf-Bundle-Sha256'] == hashlib.sha256(bundle).hexdigest()
    assert headers['Content-Disposition'] == 'attachment; filename="project-source-' + 'c' * 40 + '.bundle"'
    assert headers['X-Leaf-Source-Receipt-Digest'] == 'a' * 64
    assert headers['X-Leaf-Source-Version'] == '2'
    assert not any('lease' in key.lower() or 'repo' in key.lower() for key in headers)
    assert boundary[4] == [((ORG, ORG, PROJECT, 'c' * 40, 'd' * 40), {'max_bytes': 67108864})]


@pytest.mark.parametrize('change', [
    [('version', '02')], [('version', '0')], [('version', '2147483648')],
    [('source_commit', 'C' * 40)], [('source_tree', 'e' * 39)],
    [('receipt_digest', 'a' * 65)], [('repo_key', ACTOR)],
])
def test_closed_query(boundary, change):
    boundary[-1][:] = [pair for pair in boundary[-1] if pair[0] != change[0][0]] + change
    with pytest.raises(flow.FlowError) as failure:
        download(boundary)
    assert failure.value.status == 422
    assert not boundary[4]


def test_duplicate_query_and_authorization_before_validation(boundary, monkeypatch):
    boundary[-1].append(('version', '2'))
    with pytest.raises(flow.FlowError) as failure:
        download(boundary)
    assert failure.value.status == 422
    def denied(*args):
        raise flow.FlowError(403)
    monkeypatch.setattr(flow, 'scope', denied)
    with pytest.raises(flow.FlowError) as failure:
        download(boundary)
    assert failure.value.status == 403
    assert not boundary[4]


@pytest.mark.parametrize('field,value', [('state', 'staged'), ('version', 3),
    ('receipt_digest', 'b' * 64), ('source_commit', 'e' * 40), ('source_tree', 'f' * 40),
    ('project_id', ORG), ('source_request_id', ACTOR)])
def test_only_exact_published_candidate(boundary, field, value):
    boundary[1][field] = value
    with pytest.raises(flow.FlowError) as failure:
        download(boundary)
    assert failure.value.status == 409
    assert not boundary[4]


@pytest.mark.parametrize('field,value', [('bundle', b'fake zip'), ('size_bytes', True),
    ('size_bytes', len(BUNDLE) + 1), ('bundle_sha256', '0' * 64),
    ('source_commit', 'e' * 40), ('source_tree', 'f' * 40), ('bundle', b'')])
def test_producer_result_is_independently_verified(boundary, field, value):
    boundary[3][field] = value
    with pytest.raises(flow.FlowError) as failure:
        download(boundary)
    assert failure.value.status == 503


def test_customer_boundary_caps_bundle_size(boundary, monkeypatch):
    monkeypatch.setattr(export, 'MAX_BUNDLE_BYTES', len(BUNDLE) - 1)
    with pytest.raises(flow.FlowError) as failure:
        download(boundary)
    assert failure.value.status == 503


@pytest.mark.parametrize('change,status', [('mapping', 403), ('candidate', 409), ('revoked', 403), ('principal', 403), ('missing', 409)])
def test_producer_cannot_race_authority_or_candidate(boundary, monkeypatch, change, status):
    def producer(*args, **kwargs):
        if change == 'mapping':
            boundary[2]['repo_key'] = EDIT
        elif change == 'candidate':
            boundary[1]['version'] += 1
        elif change == 'principal':
            boundary[0].subject = 'other person'
        elif change == 'missing':
            def missing(*args):
                raise flow.FlowError(404)
            monkeypatch.setattr(flow, 'read', missing)
        else:
            def denied(*args):
                raise flow.FlowError(403)
            monkeypatch.setattr(flow, 'scope', denied)
        return boundary[3]
    monkeypatch.setattr(export.source, 'export_project_source_bundle', producer)
    with pytest.raises(flow.FlowError) as failure:
        download(boundary)
    assert failure.value.status == status


@pytest.mark.parametrize('error,status', [(export.source.SourceConflict, 409), (export.source.SourceUnavailable, 503)])
def test_producer_errors_are_fixed(boundary, monkeypatch, error, status):
    def producer(*args, **kwargs):
        raise error('private provider diagnostic')
    monkeypatch.setattr(export.source, 'export_project_source_bundle', producer)
    with pytest.raises(flow.FlowError) as failure:
        download(boundary)
    assert failure.value.status == status
    assert 'private' not in str(failure.value)
