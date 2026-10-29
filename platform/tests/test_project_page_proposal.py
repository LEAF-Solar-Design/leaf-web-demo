"""Real PostgreSQL source/authority proof; only the harness HTTP edge is faked."""
import copy
import hashlib
import json
import logging
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import uuid

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'server'))
import broker_client
from leaf_platform import project_lifecycle, project_materials, project_web_page, store
from leaf_platform import project_page_proposal as proposals
from leaf_platform.db import run_transaction
from leaf_platform.web_page_template import document_digest, validate_document


def headers(org, actor):
    return {'X-Org-Id': str(org), 'X-Actor-Binding-Id': str(actor)}


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=proposals.__name__)
    monkeypatch.setenv('LEAF_AUTH_LIVE', '0')
    monkeypatch.setenv('LEAF_AUTHOR_HARNESS_URL', 'http://harness.invalid')
    monkeypatch.setenv('LEAF_HARNESS_SECRET', 'test-only-harness-secret')

    def deny(*args, **kwargs):
        pytest.fail('Unexpected external network request')

    monkeypatch.setattr(requests, 'post', deny)


@pytest.fixture
def saved(client, make_org):
    org = make_org().org_id
    actor = store.create_identity_binding(org, 'auth0', str(uuid.uuid4()), role='owner').binding_id
    project = uuid.UUID(project_lifecycle.create_blank_project(
        org, actor, name='Portfolio', idempotency_key=str(uuid.uuid4()))['project']['project_id'])
    material = project_materials.commit(org, project, actor, {
        'name': 'cake.jpg', 'media_type': 'image/jpeg', 'byte_size': 10,
        'sha256': hashlib.sha256(b'original photo').hexdigest(), 'width': 100, 'height': 100,
        'preview_media_type': 'image/jpeg', 'preview_sha256': hashlib.sha256(b'preview photo').hexdigest(),
    })
    document = {'schema': 'leaf.portfolio.v1', 'name': 'Jessica Sargent', 'headline': 'Selected work',
        'introduction': 'Handmade cakes', 'about': 'Baking with care',
        'contact': {'email': 'jessica@example.com', 'location': 'Portland'},
        'resume': [{'heading': 'Experience', 'entries': [
            {'title': 'Baker', 'subtitle': 'Studio', 'dates': '2020', 'description': 'Custom cakes'}]}],
        'theme': 'light', 'categories': [{'slug': 'cakes', 'title': 'Cakes',
            'description': 'Celebrations', 'hero': 0, 'images': [
                {key: material[key] for key in ('material_id', 'sha256', 'preview_sha256')}
                | {'title': 'Cake', 'alt': 'Chocolate cake'}]}]}
    url = f'/api/projects/{project}/page'
    auth = headers(org, actor)
    response = client.put(url, headers=auth, json={'expected_revision': 0, 'document': document})
    assert response.status_code == 200, response.text
    return {'org': org, 'project': project, 'actor': actor, 'url': url, 'auth': auth,
            'page': response.json()['page'], 'document': document}


def request_body(saved):
    return {'expected_revision': saved['page']['revision'], 'expected_digest': saved['page']['digest'],
            'instruction': 'Make the headline warmer'}


def result(payload, edits=None):
    return {'source': copy.deepcopy(payload['source']), 'model': 'claude-test-1',
            'edits': [] if edits is None else edits, 'usage': {'input_tokens': 12, 'output_tokens': 4}}


class Response:
    def __init__(self, raw, status=200, failure=None):
        self.raw, self.status_code, self.failure = raw, status, failure
        self.headers = {}
        self.closed = False

    def iter_content(self, chunk_size):
        assert chunk_size == 1  # deadline checked even for a trickling peer
        if self.failure:
            raise self.failure
        for index in range(0, len(self.raw), chunk_size):
            yield self.raw[index:index + chunk_size]

    def close(self):
        self.closed = True


def install_transport(monkeypatch, produce):
    calls, responses = [], []

    def post(url, **kwargs):
        payload = json.loads(kwargs['data'])
        calls.append((url, kwargs, payload, threading.get_ident()))
        response = produce(payload)
        responses.append(response)
        return response

    monkeypatch.setattr(requests, 'post', post)
    return calls, responses


def encoded(value):
    return json.dumps(value).encode()


def failure_records(caplog):
    return [record for record in caplog.records if record.name == proposals.__name__]


def assert_failure(caplog, stage, code, upstream_status=0):
    records = failure_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert record.msg == ('portfolio_proposal_failed correlation_id=%s stage=%s '
                          'code=%s upstream_status=%s')
    correlation_id, actual_stage, actual_code, actual_status = record.args
    parsed = uuid.UUID(correlation_id)
    assert parsed.version == 4 and str(parsed) == correlation_id
    assert (actual_stage, actual_code, actual_status) == (stage, code, upstream_status)
    assert record.exc_info is None and record.stack_info is None
    rendered = record.getMessage()
    assert rendered == (f'portfolio_proposal_failed correlation_id={correlation_id} '
                        f'stage={stage} code={code} upstream_status={upstream_status}')
    assert rendered in caplog.text
    for sentinel in ('Make the headline warmer', 'Jessica Sargent', 'Selected work',
                     'Handmade cakes', 'jessica@example.com', 'test-only-harness-secret',
                     'private provider error', 'private upstream body', 'private connection detail',
                     'private read error', 'private request error', 'compressed private body',
                     'private text', 'metadata-secret', 'client-correlation-secret', 'claude-test-1'):
        assert sentinel not in rendered
        assert sentinel not in caplog.text
        assert sentinel not in repr(record.args)


def revision_count(saved):
    def operation(conn):
        with conn.cursor() as cur:
            cur.execute('SELECT count(*) AS n FROM project_web_page_revisions WHERE org_id=%s AND project_id=%s',
                        (saved['org'], saved['project']))
            return cur.fetchone()['n']
    return run_transaction(operation)


def test_proposal_uses_saved_catalog_and_never_saves_then_explicit_put(client, saved, monkeypatch, caplog):
    def produce(payload):
        return Response(encoded(result(payload, [{'id': 'f0002', 'value': 'Made with love'}])))
    calls, responses = install_transport(monkeypatch, produce)
    response = client.post(saved['url'] + '/proposals', headers=saved['auth'], json=request_body(saved))
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'private, no-store'
    proposal = response.json()
    assert set(proposal) == {'source', 'proposed_document', 'proposed_digest', 'edits', 'model', 'usage'}
    assert proposal['source'] == {'project_id': str(saved['project']), 'revision': 1, 'digest': saved['page']['digest']}
    expected = copy.deepcopy(saved['document'])
    expected['headline'] = 'Made with love'
    assert proposal['proposed_document'] == expected
    assert proposal['proposed_digest'] == document_digest(expected)
    assert proposal['edits'] == [{'id': 'f0002', 'value': 'Made with love'}]
    assert proposal['usage'] == {'input_tokens': 12, 'output_tokens': 4}
    assert client.get(saved['url'], headers=saved['auth']).json()['page'] == saved['page']
    assert revision_count(saved) == 1
    assert len(calls) == 1 and responses[0].closed
    url, options, payload, thread_id = calls[0]
    assert url == 'http://harness.invalid/internal/portfolio/proposals'
    assert options['headers'] == {**broker_client.harness_headers(), 'X-Tenant-Id': str(saved['org']),
                                  'Content-Type': 'application/json', 'Accept-Encoding': 'identity'}
    assert options['stream'] is True and options['allow_redirects'] is False
    assert options['timeout'] == (proposals.CONNECT_SECONDS, proposals.READ_SECONDS)
    assert thread_id != threading.get_ident()
    assert set(payload) == {'tenant_id', 'source', 'instruction', 'fields'}
    assert payload['tenant_id'] == str(saved['org'])
    assert payload['source'] == {'project_id': str(saved['project']), 'revision': 1,
                                 'digest64hex': saved['page']['digest']}
    assert payload['instruction'] == request_body(saved)['instruction']
    assert [(f['value'], f['max_length']) for f in payload['fields']] == [
        ('Jessica Sargent', 120), ('Selected work', 240), ('Handmade cakes', 2400), ('Baking with care', 8000),
        ('jessica@example.com', 254), ('Portland', 160), ('Cakes', 120), ('Celebrations', 1200),
        ('Cake', 200), ('Chocolate cake', 400), ('Experience', 120), ('Baker', 200),
        ('Studio', 240), ('2020', 120), ('Custom cakes', 2400)]
    assert [f['id'] for f in payload['fields']] == [f'f{i:04d}' for i in range(1, 16)]
    assert all(set(f) == {'id', 'value', 'max_length'} for f in payload['fields'])
    assert saved['document']['categories'][0]['images'][0]['material_id'] not in json.dumps(payload)
    applied = client.put(saved['url'], headers=saved['auth'], json={
        'expected_revision': proposal['source']['revision'], 'document': proposal['proposed_document']})
    assert applied.status_code == 200 and applied.json()['page']['revision'] == 2
    assert revision_count(saved) == 2
    assert failure_records(caplog) == []
    # A distinct stale draft cannot apply; PUT retains its existing retry semantics.
    stale = {**proposal['proposed_document'], 'headline': 'Another draft'}
    assert client.put(saved['url'], headers=saved['auth'], json={
        'expected_revision': 1, 'document': stale}).status_code == 409
    assert revision_count(saved) == 2


def test_every_catalog_target_changes_only_its_text(client, saved, monkeypatch):
    values = ['New name', 'New headline', 'New introduction', 'New about', 'new@example.com',
              'New location', 'New gallery', 'New description', 'New image title', 'New alt',
              'New section', 'New entry', 'New subtitle', '2021', 'New experience']

    def produce(payload):
        return Response(encoded(result(payload, [
            {'id': field['id'], 'value': value} for field, value in zip(payload['fields'], values)])))

    install_transport(monkeypatch, produce)
    response = client.post(saved['url'] + '/proposals', headers=saved['auth'], json=request_body(saved))
    assert response.status_code == 200, response.text
    expected = copy.deepcopy(saved['document'])
    expected.update(zip(['name', 'headline', 'introduction', 'about'], values[:4]))
    expected['contact'] = dict(zip(['email', 'location'], values[4:6]))
    expected['categories'][0].update(zip(['title', 'description'], values[6:8]))
    expected['categories'][0]['images'][0].update(zip(['title', 'alt'], values[8:10]))
    expected['resume'][0]['heading'] = values[10]
    expected['resume'][0]['entries'][0] = dict(zip(['title', 'subtitle', 'dates', 'description'], values[11:]))
    assert response.json()['proposed_document'] == expected
    assert response.json()['proposed_digest'] == document_digest(expected)
    assert revision_count(saved) == 1


def test_live_identity_uses_server_actor_despite_forged_dev_headers(client, saved, monkeypatch):
    from leaf_platform import deps
    monkeypatch.setenv('LEAF_AUTH_LIVE', '1')
    seen = []

    def identity(authorization):
        seen.append(authorization)
        return SimpleNamespace(platform_tenant_id=saved['org'], binding_id=saved['actor'])

    monkeypatch.setattr(deps, '_verified_identity', identity)
    calls, _ = install_transport(monkeypatch, lambda payload: Response(encoded(result(payload))))
    response = client.post(saved['url'] + '/proposals', json=request_body(saved), headers={
        **headers(uuid.uuid4(), uuid.uuid4()), 'Authorization': 'Bearer fixture'})
    assert response.status_code == 200, response.text
    assert seen == ['Bearer fixture']
    assert calls[0][2]['tenant_id'] == str(saved['org'])


@pytest.mark.parametrize('changes', [
    {'expected_revision': True}, {'expected_revision': 0}, {'expected_revision': 2147483648},
    {'expected_revision': 1.0}, {'expected_revision': '1'}, {'expected_digest': 'A' * 64},
    {'expected_digest': '0' * 63}, {'instruction': ''}, {'instruction': ' \n '},
    {'instruction': 'x' * 4001}, {'instruction': 5}, {'tenant_id': 'foreign'},
    {'actor': 'foreign'}, {'document': {}}, {'fields': []}, {'limit': 1}, {'grant': {}},
])
def test_invalid_request_never_calls_transport(client, saved, changes, caplog):
    response = client.post(saved['url'] + '/proposals', headers=saved['auth'], json=request_body(saved) | changes)
    assert response.status_code == 422, response.text
    assert response.headers['cache-control'] == 'private, no-store'
    assert revision_count(saved) == 1
    assert failure_records(caplog) == []


@pytest.mark.parametrize('raw,status', [
    (b' ' * (32 * 1024 + 1), 413), (b'{', 422), (b'[]', 422),
    (b'{"expected_revision":1,"expected_revision":2}', 422),
    (b'{"instruction":"\\ud800"}', 422), (b'\xff', 422),
], ids=['oversized', 'malformed', 'array', 'duplicate-key', 'surrogate', 'invalid-utf8'])
def test_raw_request_bound_and_strict_json(client, saved, raw, status):
    response = client.post(saved['url'] + '/proposals', headers=saved['auth'], content=raw)
    assert response.status_code == status


def test_auth_scope_and_write_role_before_body_consumption(client, saved, make_org, caplog):
    org, project, owner = saved['org'], saved['project'], saved['actor']
    outsider = store.create_identity_binding(org, 'auth0', str(uuid.uuid4()), role='owner').binding_id
    other_org = make_org().org_id
    foreign = store.create_identity_binding(other_org, 'auth0', str(uuid.uuid4()), role='owner').binding_id
    url = saved['url'] + '/proposals'
    missing_actor = client.post(url, content=b'{')
    assert missing_actor.status_code == 400
    assert missing_actor.headers['cache-control'] == 'private, no-store'
    for auth, status in [(headers(org, outsider), 403), (headers(other_org, foreign), 404)]:
        # Oversized invalid input would return 413/422 if consumed before authorization.
        response = client.post(url, headers=auth, content=b'x' * (32 * 1024 + 1))
        assert response.status_code == status
    project_lifecycle.invite_project_member(org, project, owner, member_binding_id=outsider,
        role='read_only', idempotency_key=str(uuid.uuid4()))
    assert client.post(url, headers=headers(org, outsider), content=b'{').status_code == 403
    store.soft_delete_project(org, project)
    assert client.post(url, headers=saved['auth'], json=request_body(saved)).status_code == 404
    assert failure_records(caplog) == []


@pytest.mark.parametrize('changes', [{'expected_digest': '0' * 64}, {'expected_revision': 2}])
def test_stale_source_rejected_before_transport(client, saved, changes, caplog):
    response = client.post(saved['url'] + '/proposals', headers=saved['auth'], json=request_body(saved) | changes)
    assert response.status_code == 409
    assert failure_records(caplog) == []


def test_missing_or_nonportfolio_source_rejected(client, saved):
    other = uuid.UUID(project_lifecycle.create_blank_project(saved['org'], saved['actor'], name='Empty',
        idempotency_key=str(uuid.uuid4()))['project']['project_id'])
    url = f'/api/projects/{other}/page'
    assert client.post(url + '/proposals', headers=saved['auth'], json=request_body(saved)).status_code == 422
    simple = {'label': 'Soon', 'headline': 'Coming soon', 'description': '', 'status': '',
              'theme': 'light', 'alignment': 'left'}
    page = project_web_page.save(saved['org'], other, saved['actor'], expected_revision=0, document=simple)
    body = request_body(saved) | {'expected_digest': page['digest']}
    assert client.post(url + '/proposals', headers=saved['auth'], json=body).status_code == 422


@pytest.mark.parametrize('kind', [
    'malformed', 'duplicate-key', 'unknown-top', 'unknown-source', 'changed-source', 'bool-revision',
    'unknown-id', 'path-id', 'duplicate-id', 'too-many', 'too-long', 'nonstring', 'unknown-edit',
    'bad-email', 'html', 'empty-required', 'bad-model', 'long-model', 'unknown-usage', 'bool-usage',
    'negative-usage', 'string-usage', 'float-usage', 'nan-usage', 'large-usage', 'oversize',
    'source-and-schema', 'source-and-edit', 'invalid-utf8',
])
def test_invalid_output_is_fixed_502_and_never_saves(client, saved, monkeypatch, kind, caplog):
    def produce(payload):
        value = result(payload)
        if kind == 'malformed':
            return Response(b'private provider error')
        if kind == 'duplicate-key':
            return Response(encoded(value)[:-1] + b',"model":"second"}')
        if kind == 'invalid-utf8': return Response(b'\xff')
        if kind in {'source-and-schema', 'source-and-edit'}:
            value['source']['digest64hex'] = '0' * 64
            if kind == 'source-and-schema': value['model'] = 'private text\n'
            else: value['edits'] = [{'id': 'f9999', 'value': 'Text'}]
        if kind == 'unknown-top': value['private'] = 'provider text'
        if kind == 'unknown-source': value['source']['tenant_id'] = 'foreign'
        if kind == 'changed-source': value['source']['digest64hex'] = '0' * 64
        if kind == 'bool-revision': value['source']['revision'] = True
        if kind == 'unknown-id': value['edits'] = [{'id': 'f9999', 'value': 'Text'}]
        if kind == 'path-id': value['edits'] = [{'id': 'categories.0.hero', 'value': '1'}]
        if kind == 'duplicate-id': value['edits'] = [{'id': 'f0002', 'value': 'A'}] * 2
        if kind == 'too-many': value['edits'] = [{'id': 'f0002', 'value': 'A'}] * 33
        if kind == 'too-long': value['edits'] = [{'id': 'f0002', 'value': 'A' * 241}]
        if kind == 'nonstring': value['edits'] = [{'id': 'f0002', 'value': 3}]
        if kind == 'unknown-edit': value['edits'] = [{'id': 'f0002', 'value': 'A', 'path': 'name'}]
        if kind == 'bad-email': value['edits'] = [{'id': 'f0005', 'value': 'invalid email'}]
        if kind == 'html': value['edits'] = [{'id': 'f0002', 'value': '<script>bad</script>'}]
        if kind == 'empty-required': value['edits'] = [{'id': 'f0001', 'value': ''}]
        if kind == 'bad-model': value['model'] = 'private text\n'
        if kind == 'long-model': value['model'] = 'x' * 129
        if kind == 'unknown-usage': value['usage'] = {'access_token': 'private'}
        for tag, number in [('bool-usage', True), ('negative-usage', -1), ('string-usage', '12'),
                            ('float-usage', 1.5), ('nan-usage', float('nan')), ('large-usage', 2**53)]:
            if kind == tag: value['usage'] = {'input_tokens': number}
        if kind == 'oversize': return Response(b' ' * (proposals.MAX_RESPONSE_BYTES + 1))
        return Response(encoded(value))
    calls, responses = install_transport(monkeypatch, produce)
    response = client.post(saved['url'] + '/proposals', headers={
        **saved['auth'], 'X-Correlation-Id': 'client-correlation-secret'}, json=request_body(saved))
    assert response.status_code == 502, response.text
    assert response.json() == {'detail': 'Invalid portfolio proposal response.'}
    assert response.headers['cache-control'] == 'private, no-store'
    assert len(calls) == 1 and responses[0].closed
    assert revision_count(saved) == 1
    assert project_web_page.read(saved['org'], saved['project'], saved['actor']) == saved['page']
    stage, code, upstream_status = 'response', 'invalid_schema', 0
    if kind in {'malformed', 'duplicate-key', 'nan-usage', 'invalid-utf8'}:
        code = 'invalid_json'
    elif kind in {'changed-source', 'source-and-edit'}:
        code = 'source_mismatch'
    elif kind in {'unknown-id', 'path-id', 'duplicate-id', 'too-long', 'nonstring', 'unknown-edit'}:
        code = 'invalid_edits'
    elif kind in {'bad-email', 'html', 'empty-required'}:
        stage, code = 'document', 'invalid_document'
    elif kind == 'oversize':
        stage, code, upstream_status = 'transport', 'response_size', 200
    assert_failure(caplog, stage, code, upstream_status)


@pytest.mark.parametrize('kind', ['http-failure', 'redirect', 'connect', 'connect-timeout', 'request',
                                 'read', 'deadline', 'header-deadline', 'end-deadline', 'compressed'])
def test_transport_failures_are_bounded_closed_and_not_retried(client, saved, monkeypatch, kind, caplog):
    def produce(payload):
        if kind == 'connect': raise requests.ConnectionError('private connection detail')
        if kind == 'connect-timeout': raise requests.ConnectTimeout('private connection detail')
        if kind == 'request': raise requests.RequestException('private request error')
        if kind == 'http-failure': return Response(b'private upstream body', status=500)
        if kind == 'redirect': return Response(b'', status=307)
        if kind == 'read': return Response(b'', failure=requests.Timeout('private read error'))
        if kind == 'compressed':
            response = Response(b'compressed private body')
            response.headers['Content-Encoding'] = 'gzip'
            return response
        if kind == 'end-deadline': return Response(b'')
        return Response(encoded(result(payload)))
    calls, responses = install_transport(monkeypatch, produce)
    if kind == 'deadline':
        # Headers arrive in time; the stream then exceeds the total operation budget.
        times = iter([0, 1, proposals.OPERATION_SECONDS + 1])
        monkeypatch.setattr(proposals, 'monotonic', lambda: next(times))
    if kind in {'header-deadline', 'end-deadline'}:
        times = iter([0, proposals.OPERATION_SECONDS + 1] if kind == 'header-deadline'
                     else [0, 1, proposals.OPERATION_SECONDS + 1])
        monkeypatch.setattr(proposals, 'monotonic', lambda: next(times))
    response = client.post(saved['url'] + '/proposals', headers=saved['auth'], json=request_body(saved))
    assert response.status_code == 502, response.text
    assert response.json() == {'detail': 'Invalid portfolio proposal response.'}
    assert len(calls) == 1 and all(r.closed for r in responses)
    assert revision_count(saved) == 1
    assert project_web_page.read(saved['org'], saved['project'], saved['actor']) == saved['page']
    code, upstream_status = {
        'http-failure': ('upstream_status', 500), 'redirect': ('upstream_status', 307),
        'connect': ('connection_error', 0), 'connect-timeout': ('timeout', 0),
        'request': ('request_error', 0), 'read': ('timeout', 200), 'deadline': ('deadline', 200),
        'header-deadline': ('deadline', 200), 'end-deadline': ('deadline', 200),
        'compressed': ('content_encoding', 200),
    }[kind]
    assert_failure(caplog, 'transport', code, upstream_status)


def test_rate_limit_is_fixed_429_with_unread_closed_body_and_unchanged_page(client, saved, monkeypatch, caplog):
    class UnreadResponse(Response):
        def iter_content(self, chunk_size):
            pytest.fail('Rate-limit body must not be read')

        @property
        def content(self):
            pytest.fail('Rate-limit body must not be read')

        @property
        def text(self):
            pytest.fail('Rate-limit body must not be read')

        def json(self):
            pytest.fail('Rate-limit body must not be read')

    def produce(payload):
        response = UnreadResponse(b'private upstream body', status=429)
        response.headers['Retry-After'] = 'private provider error'
        return response

    original = copy.deepcopy(saved)
    calls, responses = install_transport(monkeypatch, produce)
    response = client.post(saved['url'] + '/proposals', headers=saved['auth'], json=request_body(saved))
    assert response.status_code == 429, response.text
    assert response.json() == {'detail': 'Portfolio proposal service is temporarily rate limited.'}
    assert response.headers['cache-control'] == 'private, no-store'
    assert 'retry-after' not in response.headers
    assert len(calls) == 1 and responses[0].closed
    assert revision_count(saved) == 1
    assert project_web_page.read(saved['org'], saved['project'], saved['actor']) == saved['page']
    assert saved == original
    assert_failure(caplog, 'transport', 'upstream_status', 429)


@pytest.mark.parametrize('missing', ['LEAF_AUTHOR_HARNESS_URL', 'LEAF_HARNESS_SECRET'])
def test_unconfigured_transport_is_503_without_network(client, saved, monkeypatch, missing, caplog):
    monkeypatch.delenv(missing)
    response = client.post(saved['url'] + '/proposals', headers=saved['auth'], json=request_body(saved))
    assert response.status_code == 503
    assert response.json() == {'detail': 'Portfolio proposal service is unavailable.'}
    assert revision_count(saved) == 1
    assert project_web_page.read(saved['org'], saved['project'], saved['actor']) == saved['page']
    assert_failure(caplog, 'configuration', 'unavailable')


@pytest.mark.parametrize('stage,code,upstream_status,expected', [
    ('metadata-secret', 'metadata-secret', 'metadata-secret', ('response', 'invalid_schema', 0)),
    ([], {}, True, ('response', 'invalid_schema', 0)),
    ('transport', 'invalid_document', 99, ('response', 'invalid_schema', 0)),
    ('transport', 'timeout', 600, ('transport', 'timeout', 0)),
    ('transport', 'upstream_status', 100, ('transport', 'upstream_status', 100)),
    ('transport', 'upstream_status', 599, ('transport', 'upstream_status', 599)),
])
def test_diagnostic_metadata_is_closed_and_same_error_is_rethrown(
        saved, monkeypatch, caplog, stage, code, upstream_status, expected):
    error = proposals.ProposalError(502, stage=stage, code=code, upstream_status=upstream_status)
    assert (error.stage, error.code, error.upstream_status) == expected
    # Also normalize at the log boundary if an internal caller mutates metadata.
    error.stage, error.code, error.upstream_status = stage, code, upstream_status

    def fail(payload):
        raise error

    monkeypatch.setattr(proposals, '_transport', fail)
    with pytest.raises(proposals.ProposalError) as caught:
        proposals.propose(saved['org'], saved['project'], saved['actor'], **request_body(saved))
    assert caught.value is error
    assert str(caught.value) == 'Invalid portfolio proposal response.'
    assert_failure(caplog, *expected)
    assert revision_count(saved) == 1
    assert project_web_page.read(saved['org'], saved['project'], saved['actor']) == saved['page']


def test_logging_handler_failure_preserves_service_error(saved, monkeypatch):
    emitted = []

    class RaisingHandler(logging.Handler):
        def emit(self, record):
            emitted.append(record)
            raise RuntimeError('private logging exception')

    calls = []
    error = proposals.ProposalError(503, stage='configuration', code='unavailable')

    def fail(payload):
        calls.append(payload)
        raise error

    monkeypatch.setattr(proposals, '_transport', fail)
    handler = RaisingHandler()
    proposals.logger.addHandler(handler)
    try:
        with pytest.raises(proposals.ProposalError) as caught:
            proposals.propose(saved['org'], saved['project'], saved['actor'], **request_body(saved))
    finally:
        proposals.logger.removeHandler(handler)
        handler.close()
    assert caught.value is error
    assert caught.value.status == 503
    assert str(caught.value) == 'Portfolio proposal service is unavailable.'
    assert len(calls) == 1
    assert len(emitted) == 1
    assert revision_count(saved) == 1
    assert project_web_page.read(saved['org'], saved['project'], saved['actor']) == saved['page']


@pytest.mark.parametrize('raw', [None, bytearray(b'{}'), b' ' * (proposals.MAX_RESPONSE_BYTES + 1)])
def test_invalid_result_bytes_are_classified_at_operation_boundary(saved, monkeypatch, caplog, raw):
    monkeypatch.setattr(proposals, '_transport', lambda payload: raw)
    with pytest.raises(proposals.ProposalError) as caught:
        proposals.propose(saved['org'], saved['project'], saved['actor'], **request_body(saved))
    assert caught.value.status == 502
    assert str(caught.value) == 'Invalid portfolio proposal response.'
    assert_failure(caplog, 'response', 'invalid_bytes')
    assert revision_count(saved) == 1


def test_arbitrary_operation_error_is_not_logged_or_swallowed(saved, monkeypatch, caplog):
    error = RuntimeError('private connection detail')

    def fail(payload):
        raise error

    monkeypatch.setattr(proposals, '_transport', fail)
    with pytest.raises(RuntimeError) as caught:
        proposals.propose(saved['org'], saved['project'], saved['actor'], **request_body(saved))
    assert caught.value is error
    assert failure_records(caplog) == []
    assert revision_count(saved) == 1


@pytest.mark.parametrize('change,status', [('source', 409), ('membership', 403), ('role', 403), ('delete', 404)])
def test_rechecks_source_and_write_authority_after_model(client, saved, monkeypatch, change, status, caplog):
    def produce(payload):
        # Independent DB transactions here also prove the model call holds no project lock.
        if change == 'source':
            project_web_page.save(saved['org'], saved['project'], saved['actor'], expected_revision=1,
                                  document=saved['document'] | {'headline': 'Saved elsewhere'})
        elif change == 'delete':
            store.soft_delete_project(saved['org'], saved['project'])
        else:
            def operation(conn):
                with conn.cursor() as cur:
                    if change == 'membership':
                        cur.execute("UPDATE project_member_bindings SET status='revoked', revoked_at=now() "
                            'WHERE org_id=%s AND project_id=%s AND binding_id=%s',
                            (saved['org'], saved['project'], saved['actor']))
                    else:
                        cur.execute("UPDATE project_member_bindings SET role='read_only' "
                            'WHERE org_id=%s AND project_id=%s AND binding_id=%s',
                            (saved['org'], saved['project'], saved['actor']))
            run_transaction(operation)
        return Response(encoded(result(payload, [{'id': 'f0002', 'value': 'Proposed'}])))
    _, responses = install_transport(monkeypatch, produce)
    response = client.post(saved['url'] + '/proposals', headers=saved['auth'], json=request_body(saved))
    assert response.status_code == status, response.text
    assert responses[0].closed
    assert revision_count(saved) == (2 if change == 'source' else 1)
    assert failure_records(caplog) == []


def test_empty_edits_preserve_digest_and_absent_usage(client, saved, monkeypatch):
    def produce(payload):
        value = result(payload)
        value['usage'] = {}
        return Response(encoded(value))
    install_transport(monkeypatch, produce)
    response = client.post(saved['url'] + '/proposals', headers=saved['auth'], json=request_body(saved))
    assert response.status_code == 200, response.text
    assert response.json()['proposed_document'] == saved['document']
    assert response.json()['proposed_digest'] == saved['page']['digest']
    assert response.json()['edits'] == [] and response.json()['usage'] == {}
    assert revision_count(saved) == 1


def test_catalog_and_internal_payload_limits_reject_without_truncation(client, saved):
    document = copy.deepcopy(saved['document'])
    entry = {'title': '', 'subtitle': '', 'dates': '', 'description': 'x' * 2400}
    document['resume'] = [{'heading': 'Work', 'entries': [copy.deepcopy(entry) for _ in range(20)]}
                          for _ in range(8)]
    page = project_web_page.save(saved['org'], saved['project'], saved['actor'], expected_revision=1,
                                 document=document)
    response = client.post(saved['url'] + '/proposals', headers=saved['auth'], json={
        **request_body(saved), 'expected_revision': 2, 'expected_digest': page['digest']})
    assert response.status_code == 422
    assert revision_count(saved) == 2
    # A valid complete portfolio can exceed the field cap even within 512 KiB.
    image = document['categories'][0]['images'][0]
    document['categories'][0]['images'] = [dict(image, material_id=str(uuid.uuid4())) for _ in range(200)]
    for section in document['resume']:
        for item in section['entries']:
            item['description'] = ''
    document = validate_document(document)
    with pytest.raises(proposals.ProposalError) as error:
        proposals._catalog(document)
    assert error.value.status == 422
