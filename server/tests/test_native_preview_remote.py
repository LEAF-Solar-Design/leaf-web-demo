"""Remote protocol source proofs. All worker receipts here are synthetic test
evidence, never a simulator run. Durable cases use the existing PostgreSQL spine.
"""
import base64
import binascii
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import struct
from types import SimpleNamespace
import time
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import jobs
import native_preview_job as native
import native_preview_remote as remote
from leaf_platform import api, deps
from test_project_preview_source import custody  # noqa: F401
from test_jobs_callbacks_postgres import postgres_authority  # noqa: F401


@pytest.fixture
def protocol(custody, monkeypatch):
    c = custody
    c.worker = str(uuid.uuid4())
    c.reader = str(uuid.uuid4())
    c.revoked = set()
    c.binding = {'org_id': c.org, 'project_id': c.project,
                 'actor_id': c.worker, 'worker_id': 'fixed-test-worker'}
    monkeypatch.setenv('LEAF_NATIVE_PREVIEW_WORKER_BINDINGS', json.dumps([c.binding]))
    monkeypatch.setattr(native, '_executor', None)

    def authorize(org, project, actor, *, write=False):
        if org != c.org or project != c.project:
            raise remote.lifecycle.LifecycleUnavailable()
        if (actor not in (c.actor, c.worker, c.reader) or actor in c.revoked
                or (write and actor == c.reader) or not c.active):
            raise remote.lifecycle.LifecycleForbidden()
        return 'editor' if write else 'read_only'

    monkeypatch.setattr(remote.lifecycle, 'require_project_role', authorize)
    c.client_app = FastAPI()
    c.client_app.include_router(api.router)
    monkeypatch.setattr(deps, 'auth_live', lambda: True)
    identities = {a: SimpleNamespace(platform_tenant_id=uuid.UUID(c.org), binding_id=uuid.UUID(a))
                  for a in (c.actor, c.worker, c.reader)}

    def verified(authorization):
        # Synthetic authenticated identities only; production dependency verifies Auth0.
        if authorization not in identities:
            from fastapi import HTTPException
            raise HTTPException(401, 'Unauthorized')
        return identities[authorization]

    monkeypatch.setattr(deps, '_verified_identity', verified)
    c.client = TestClient(c.client_app)
    c.url = f'/api/projects/{c.project}/native-previews'
    c.headers = lambda actor=None: {'Authorization': actor or c.actor}
    return c


def submit(c, key='remote-one'):
    return remote.submit(c.org, c.project, c.actor, {'revision': 3}, key)['job_id']


def claim(c):
    job = remote.claim(c.org, c.project, c.worker, {})['job']
    assert job is not None
    return job, {k: job[k] for k in ('lease_owner', 'attempt')}


def png_bytes():
    # A complete one-pixel PNG used only as synthetic protocol evidence.
    import zlib
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', binascii.crc32(kind + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\x00\x00\x00\x00\xff')) + chunk(b'IEND', b''))


def completion(c, job, identity):
    image = png_bytes()
    receipt = {'schema': remote.RECEIPT_SCHEMA, 'job_id': job['job_id'], **identity,
               'archive_sha256': job['descriptor']['archive_sha256'],
               'worker_binding': c.binding.copy(), 'target': 'ios-simulator',
               'simulator_udid': str(uuid.uuid4()), 'simulator_runtime': 'iOS-18-0',
               'bundle_id': 'com.leaf.synthetic-test', 'allocator_ticket': 'synthetic-test-ticket',
               'build_exit_code': 0, 'install_exit_code': 0, 'launch_exit_code': 0,
               'screenshot_sha256': hashlib.sha256(image).hexdigest(),
               'cleanup_complete': True, 'allocator_release_confirmed': True}
    return {**identity, 'outcome': 'complete', 'receipt': receipt,
            'screenshot_base64': base64.b64encode(image).decode('ascii')}


def test_unavailable_before_custody_or_insertion(protocol, monkeypatch):
    c = protocol
    monkeypatch.delenv('LEAF_NATIVE_PREVIEW_WORKER_BINDINGS')
    monkeypatch.setattr(jobs, 'job_store_mode', lambda: 'postgres')
    monkeypatch.setattr(jobs, '_submit_job', lambda *a, **kw: pytest.fail('unavailable insert'))
    with pytest.raises(remote.ProtocolError) as exc:
        submit(c)
    assert exc.value.status == 503
    assert not c.exports


@pytest.mark.parametrize('configuration', ['[]', '{}', 'null', '[{}]', '[',
    '[{"org_id":"x","org_id":"y"}]'])
def test_closed_binding_configuration(protocol, monkeypatch, configuration):
    monkeypatch.setenv('LEAF_NATIVE_PREVIEW_WORKER_BINDINGS', configuration)
    with pytest.raises(remote.ProtocolError) as exc:
        remote.bindings()
    assert exc.value.status == 503


@pytest.mark.parametrize('change', ['duplicate', 'project', 'worker', 'uuid', 'extra', 'unsafe'])
def test_conflicting_or_malformed_bindings(protocol, monkeypatch, change):
    c = protocol
    values = [c.binding.copy()]
    if change in ('duplicate', 'project', 'worker'):
        other = c.binding.copy()
        if change == 'project': other['worker_id'] = 'other-worker'
        if change == 'worker': other['project_id'] = str(uuid.uuid4())
        values.append(other)
    elif change == 'uuid': values[0]['actor_id'] = uuid.UUID(c.worker).hex
    elif change == 'extra': values[0]['command'] = '/bin/false'
    else: values[0]['worker_id'] = '../unsafe'
    monkeypatch.setenv('LEAF_NATIVE_PREVIEW_WORKER_BINDINGS', json.dumps(values))
    with pytest.raises(remote.ProtocolError) as exc:
        remote.bindings()
    assert exc.value.status == 503


def test_raw_routes_bound_before_parse_and_auth_ignores_caller_scope(protocol, monkeypatch):
    c = protocol
    monkeypatch.setattr(jobs, 'job_store_mode', lambda: 'postgres')
    monkeypatch.setattr(jobs, '_submit_job', lambda *a, **kw: pytest.fail('unexpected insert'))
    assert c.client.post(c.url, headers={**c.headers(), 'Idempotency-Key': 'one'},
                         content=b'x' * 1025).status_code == 413
    assert c.client.post(c.url, headers=c.headers(), json={'revision': 3}).status_code == 422
    response = c.client.post(c.url + '/worker/claim', headers={**c.headers(),
        'X-Actor-Binding-Id': c.worker, 'X-Org-Id': str(uuid.uuid4())}, json={})
    assert response.status_code == 403  # verified submitter is not a worker
    assert c.client.post(c.url, json={'revision': 3}).status_code == 401


@pytest.mark.parametrize('body', [{'revision': True}, {'revision': 0},
    {'revision': 3, 'worker_id': 'x'}, {'revision': 3, 'source': {}}, {'revision': 3, 'command': 'x'}])
def test_submission_closed_and_write_authorized(protocol, monkeypatch, body):
    c = protocol
    monkeypatch.setattr(jobs, 'job_store_mode', lambda: 'postgres')
    with pytest.raises(ValueError):
        remote.submit(c.org, c.project, c.actor, body, 'one')
    with pytest.raises(remote.lifecycle.LifecycleForbidden):
        remote.submit(c.org, c.project, c.reader, {'revision': 3}, 'one')
    assert not c.exports


def test_png_and_duplicate_json_are_rejected():
    assert remote.png(png_bytes()) == png_bytes()
    for value in (b'', b'fake', b'\x89PNG\r\n\x1a\n' + b'\x00' * 25):
        with pytest.raises(remote.ProtocolError): remote.png(value)
    raw = bytearray(png_bytes())
    raw[16:20] = struct.pack('>I', 0)
    with pytest.raises(remote.ProtocolError): remote.png(bytes(raw))
    with pytest.raises(remote.ProtocolError): remote.parse(b'{"attempt":1,"attempt":2}')


def test_real_postgres_http_round_trip_and_readback(protocol, postgres_authority):
    c = protocol
    headers = {**c.headers(), 'Idempotency-Key': 'http-one'}
    response = c.client.post(c.url, headers=headers, json={'revision': 3})
    assert response.status_code == 202, response.text
    assert response.json()['status'] == 'queued'
    jid = response.json()['job_id']
    assert c.client.post(c.url, headers=headers, json={'revision': 3}).json()['job_id'] == jid
    _, executors = postgres_authority
    assert not any(e.calls for e in executors.values())
    response = c.client.post(c.url + '/worker/claim', headers=c.headers(c.worker), json={})
    assert response.status_code == 200, response.text
    job = response.json()['job']
    assert job['job_id'] == jid
    identity = {k: job[k] for k in ('lease_owner', 'attempt')}
    rec = jobs.get_job(jid)
    assert rec['lease']['owner'] == identity['lease_owner'] and rec['attempt'] == identity['attempt']
    source_url = c.url + f'/worker/{jid}/source'
    response = c.client.get(source_url, headers=c.headers(c.worker), params=identity)
    assert response.status_code == 200 and response.content == c.exported[1]
    assert c.client.get(source_url, headers=c.headers(c.actor), params=identity).status_code == 403
    assert c.client.get(source_url, headers=c.headers(c.worker)).status_code == 422
    response = c.client.post(c.url + f'/worker/{jid}/heartbeat', headers=c.headers(c.worker), json=identity)
    assert response.status_code == 200, response.text
    assert response.json()['deadline'] == job['deadline']
    body = completion(c, job, identity)
    response = c.client.post(c.url + f'/worker/{jid}/complete', headers=c.headers(c.worker), json=body)
    assert response.status_code == 200, response.text
    rec = jobs.get_job(jid)
    result = rec['result']['result']
    assert result['worker_id'] == identity['lease_owner'] and result['attempt'] == identity['attempt']
    assert result['receipt']['sha256'] == hashlib.sha256(remote.canonical(body['receipt'])).hexdigest()
    assert result['receipt']['receipt_id'] not in json.dumps(body)
    assert rec['provenance'] == {'attempt': 1, 'execution_path': 'native-worker'}
    response = c.client.get(c.url + f'/{jid}', headers=c.headers(c.reader))
    assert response.status_code == 200 and response.json()['status'] == 'complete'
    assert not {'native_preview', 'worker_binding', 'lease_owner', 'error', 'execution'} & set(response.json())
    response = c.client.get(c.url + f'/{jid}/screenshot', headers=c.headers(c.reader))
    assert response.status_code == 200 and response.content == png_bytes()
    assert response.headers['content-type'] == 'image/png'
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['cache-control'] == 'private, no-store'
    assert remote.claim(c.org, c.project, c.worker, {}) == {'job': None}


def test_real_postgres_competing_claims_and_no_local_dispatch(protocol, postgres_authority):
    c = protocol
    jid = submit(c)
    assert jobs._redispatch_record(jid)
    jobs._run_job(jid, c.org, {}, {}, '', False)
    jobs._run_native_preview(jid)
    assert jobs.get_job(jid)['attempt'] == 0
    assert not any(e.calls for e in postgres_authority[1].values())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: remote.claim(c.org, c.project, c.worker, {}), range(2)))
    winners = [r['job'] for r in results if r['job'] is not None]
    assert len(winners) == 1 and winners[0]['job_id'] == jid


@pytest.mark.parametrize('stop', ['binding', 'worker', 'submitter', 'attempt', 'owner', 'expired', 'cancel', 'deadline'])
def test_real_postgres_stopped_attempt_exposes_no_source_or_success(protocol, postgres_authority, monkeypatch, stop):
    c = protocol
    jid = submit(c)
    job, ident = claim(c)
    body = completion(c, job, ident)
    db, _ = postgres_authority
    if stop == 'binding': monkeypatch.delenv('LEAF_NATIVE_PREVIEW_WORKER_BINDINGS')
    elif stop == 'worker': c.revoked.add(c.worker)
    elif stop == 'submitter': c.revoked.add(c.actor)
    elif stop == 'attempt': ident['attempt'] += 1; body['attempt'] += 1
    elif stop == 'owner': ident['lease_owner'] = str(uuid.uuid4()); body['lease_owner'] = ident['lease_owner']
    elif stop == 'cancel': jobs.mark_job_closed(jid)
    else:
        with db.transaction() as conn:
            conn.execute('UPDATE async_jobs SET ' + ('lease_expires_at = 0' if stop == 'expired'
                else 'started_at = 1') + ' WHERE job_id = %s', (jid,))
    for operation in (
        lambda: remote.source_bytes(c.org, c.project, c.worker, jid, ident),
        lambda: remote.heartbeat(c.org, c.project, c.worker, jid, ident),
        lambda: remote.complete(c.org, c.project, c.worker, jid, body),
    ):
        with pytest.raises((remote.ProtocolError, remote.lifecycle.LifecycleForbidden)):
            operation()
    assert jobs.get_job(jid)['status'] != 'complete'
    with pytest.raises(remote.ProtocolError): remote.screenshot(c.org, c.project, c.reader, jid)


@pytest.mark.parametrize('change', ['archive', 'png', 'digest', 'exit', 'cleanup', 'release', 'extra', 'binding', 'attempt'])
def test_real_postgres_invalid_evidence_is_not_accepted(protocol, postgres_authority, change):
    c = protocol
    jid = submit(c)
    job, ident = claim(c)
    body = completion(c, job, ident)
    receipt = body['receipt']
    if change == 'archive': receipt['archive_sha256'] = '0' * 64
    elif change == 'png': body['screenshot_base64'] = base64.b64encode(b'not PNG').decode()
    elif change == 'digest': receipt['screenshot_sha256'] = '0' * 64
    elif change == 'exit': receipt['build_exit_code'] = False
    elif change == 'cleanup': receipt['cleanup_complete'] = False
    elif change == 'release': receipt['allocator_release_confirmed'] = False
    elif change == 'extra': receipt['path'] = '/secret'
    elif change == 'binding': receipt['worker_binding']['worker_id'] = 'other'
    else: receipt['attempt'] = True
    with pytest.raises(ValueError): remote.complete(c.org, c.project, c.worker, jid, body)
    assert jobs.get_job(jid)['status'] == 'running'


def test_real_postgres_generic_callback_and_cross_scope_rejected(protocol, postgres_authority):
    c = protocol
    jid = submit(c)
    job, ident = claim(c)
    result = {'schema': native.RESULT_SCHEMA, 'target': 'ios-simulator', 'job_id': jid,
              'worker_id': ident['lease_owner'], 'attempt': ident['attempt'],
              'archive_sha256': job['descriptor']['archive_sha256'],
              'receipt': {'kind': 'simulator', 'receipt_id': str(uuid.uuid4()), 'sha256': '0' * 64}}
    for path in ('local', 'native-worker'):
        with pytest.raises(ValueError):
            jobs.complete_callback(jid, 'complete', worker_id=ident['lease_owner'],
                result_env={'ok': True, 'result': result}, provenance={'attempt': 1, 'execution_path': path})
    for org, project in ((str(uuid.uuid4()), c.project), (c.org, str(uuid.uuid4()))):
        with pytest.raises(remote.ProtocolError) as exc:
            remote.record(org, project, jid)
        assert exc.value.status == 404
    with pytest.raises(remote.ProtocolError): remote.claim(c.org, c.project, c.actor, {})
    assert jobs.get_job(jid)['status'] == 'running'


def test_real_postgres_conflicting_idempotency_and_failure(protocol, postgres_authority, monkeypatch):
    c = protocol
    jid = submit(c)
    assert submit(c) == jid
    # A changed durable execution budget is different idempotent run input.
    monkeypatch.setenv('JOB_MAX_S', str(jobs.job_max_s() + 1))
    with pytest.raises(remote.ProtocolError) as exc:
        remote.submit(c.org, c.project, c.actor, {'revision': 3}, 'remote-one')
    assert exc.value.status == 409
    job, ident = claim(c)
    assert remote.complete(c.org, c.project, c.worker, jid, {**ident, 'outcome': 'failed'})['status'] == 'failed'
    rec = jobs.get_job(jid)
    assert rec['status'] == 'failed' and rec['result'] is None
    with pytest.raises(remote.ProtocolError): remote.screenshot(c.org, c.project, c.reader, jid)


def test_real_postgres_lease_loss_after_evidence_write_leaves_only_orphans(protocol, postgres_authority, monkeypatch):
    c = protocol
    jid = submit(c)
    job, ident = claim(c)
    body = completion(c, job, ident)
    real_put = c.backend.put_if_absent_or_verify
    def put(key, value):
        real_put(key, value)
        if '/receipts/' in key: jobs.mark_job_closed(jid)
    monkeypatch.setattr(c.backend, 'put_if_absent_or_verify', put)
    with pytest.raises(remote.ProtocolError): remote.complete(c.org, c.project, c.worker, jid, body)
    assert jobs.get_job(jid)['status'] != 'complete'
    with pytest.raises(remote.ProtocolError): remote.screenshot(c.org, c.project, c.reader, jid)


def test_real_postgres_retry_fences_old_owner_and_keeps_absolute_deadline(protocol, postgres_authority):
    c = protocol
    jid = submit(c)
    first, old = claim(c)
    assert jobs._pg_store.release_for_retry(jid, old['lease_owner'], old['attempt'], time.time(),
        {'retryable': True}, {'attempt': 1, 'execution_path': 'native-worker'})
    second, current = claim(c)
    assert current['attempt'] == 2 and current['lease_owner'] != old['lease_owner']
    assert second['deadline'] == first['deadline']
    with pytest.raises(remote.ProtocolError): remote.heartbeat(c.org, c.project, c.worker, jid, old)
    with pytest.raises(remote.ProtocolError):
        remote.complete(c.org, c.project, c.worker, jid, completion(c, first, old))
    remote.complete(c.org, c.project, c.worker, jid, completion(c, second, current))
    assert jobs.get_job(jid)['attempt'] == 2


def test_real_postgres_source_rechecks_lease_after_blocking_read(protocol, postgres_authority, monkeypatch):
    c = protocol
    jid = submit(c)
    _, ident = claim(c)
    real_read = native.read_source
    def read(context):
        raw = real_read(context)
        jobs.mark_job_closed(jid)
        return raw
    monkeypatch.setattr(native, 'read_source', read)
    with pytest.raises(remote.ProtocolError):
        remote.source_bytes(c.org, c.project, c.worker, jid, ident)


@pytest.mark.parametrize('which', ['receipt', 'screenshot'])
def test_real_postgres_evidence_integrity_checked_on_every_read(protocol, postgres_authority, which):
    c = protocol
    jid = submit(c)
    job, ident = claim(c)
    body = completion(c, job, ident)
    remote.complete(c.org, c.project, c.worker, jid, body)
    rec = jobs.get_job(jid)
    keys = remote._keys(rec['native_preview'], jid, ident['attempt'],
        rec['result']['result']['receipt']['sha256'], body['receipt']['screenshot_sha256'])
    c.backend.put(keys[0 if which == 'receipt' else 1], b'corrupt')
    with pytest.raises(remote.ProtocolError): remote.screenshot(c.org, c.project, c.reader, jid)
    with pytest.raises(remote.ProtocolError): remote.status(c.org, c.project, c.reader, jid)


def test_real_postgres_binding_drift_and_source_integrity_fail_closed(protocol, postgres_authority, monkeypatch):
    c = protocol
    jid = submit(c)
    job, ident = claim(c)
    changed = {**c.binding, 'worker_id': 'changed-test-worker'}
    monkeypatch.setenv('LEAF_NATIVE_PREVIEW_WORKER_BINDINGS', json.dumps([changed]))
    with pytest.raises(remote.ProtocolError): remote.source_bytes(c.org, c.project, c.worker, jid, ident)
    with pytest.raises(remote.ProtocolError):
        remote.complete(c.org, c.project, c.worker, jid, completion(c, job, ident))
    monkeypatch.setenv('LEAF_NATIVE_PREVIEW_WORKER_BINDINGS', json.dumps([c.binding]))
    c.backend.put(native.source._keys(job['descriptor'])[0], b'corrupt source')
    with pytest.raises(native.source.SourceStoreUnavailable):
        remote.source_bytes(c.org, c.project, c.worker, jid, ident)
    with pytest.raises(native.source.SourceStoreUnavailable):
        remote.complete(c.org, c.project, c.worker, jid, completion(c, job, ident))
    assert jobs.get_job(jid)['status'] != 'complete'
