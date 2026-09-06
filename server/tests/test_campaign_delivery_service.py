import copy
import uuid

import pytest

import campaign_delivery_service as delivery
import campaign_release_service as service

DXF = b'0\nSECTION\n2\nENTITIES\n0\nLINE\n10\n0\n20\n0\n11\n10\n21\n10\n0\nENDSEC\n0\nEOF\n'


@pytest.mark.parametrize('path,raw', [('drawing.dxf', DXF), ('result.txt', b'Useful result'),
                                      ('result.json', b'{"value":1}'), ('result.csv', b'name,value\na,1\n')])
def test_valid_formats_bind_actual_bytes(path, raw):
    result = delivery.validate_bytes(path, raw)
    assert result['size_bytes'] == len(raw)
    assert result['content_valid'] is True


@pytest.mark.parametrize('path,raw', [('drawing.dxf', DXF.replace(b'0\nEOF\n', b'')),
    ('drawing.dxf', DXF.replace(b'11\n10', b'11\nnan')), ('empty.json', b'[]'),
    ('bad.csv', b'a,b\n1\n'), ('empty.txt', b'  '), ('bad.txt', b'\xff'),
    ('huge.txt', b'x' * (delivery.MAX_BYTES + 1))])
def test_invalid_artifact_never_validates(path, raw):
    with pytest.raises((ValueError, UnicodeError)):
        delivery.validate_bytes(path, raw)


@pytest.mark.parametrize('path', ['../x.txt', '/x.txt', 'https://x/a.txt', 'a\\b.txt',
                                  'a//b.txt', 'a/./b.txt', 'a%2fb.txt'])
def test_unsafe_refs(path):
    with pytest.raises(ValueError):
        delivery.safe_path(path)


class Lifecycle:
    def __init__(self, path, raw):
        self.files = [{'path': path, 'content': raw.decode()}]
        self.receipts = []
        self.writes = 0
        self.interrupt = False

    def project_snapshot(self, *args):
        return copy.deepcopy({'files': self.files, 'receipts': self.receipts})

    def put_project_file(self, *args, **kwargs):
        self.writes += 1
        self.files.append({'path': kwargs['path'], 'content': kwargs['content']})
        meta = delivery.validate_bytes(kwargs['path'], kwargs['content'].encode())
        receipt = {'receipt_id': str(uuid.uuid4()), 'action': 'file_put',
                   'input_digest': delivery.input_digest(kwargs['path'], kwargs['media_type'], meta['sha256'])}
        self.receipts.append(receipt)
        if self.interrupt:
            self.interrupt = False
            raise RuntimeError('lost response after commit')
        return {'receipt': receipt}


class Store:
    def __init__(self, path, raw):
        self.release = {'release_id': str(uuid.uuid4()), 'contract_version': 1, 'status': 'active',
            'contract': {'original_goal': 'Finish the larger design', 'workflow': 'Open project and retrieve file',
                'release_boundary': 'Deliver existing artifact', 'deferred_items': ['Larger design deferred'],
                'selected_artifact': delivery.validate_bytes(path, raw),
                'required_checks': [{'check_id': s + '.verified', 'stage': s} for s in service.STAGES]}}
        self.stages = []
        self.decisions = []

    def get_release(self, *args):
        return copy.deepcopy({'release': self.release, 'stages': self.stages, 'decisions': self.decisions})

    def record_decision(self, *args, **kwargs):
        self.decisions.append(kwargs)

    def record_stage(self, *args, **kwargs):
        self.stages.append(dict(kwargs, contract_version=kwargs['evidence']['contract_version']))

    def finish_release(self, *args):
        assert {s['stage'] for s in self.stages if s['status'] == 'passed'} == set(service.STAGES)
        assert all(s['evidence']['checks'] for s in self.stages)
        self.release['status'] = 'finished'


@pytest.fixture
def runtime(monkeypatch):
    def make(path='result.txt', raw=b'Useful result'):
        store, lifecycle = Store(path, raw), Lifecycle(path, raw)
        monkeypatch.setattr(service, '_STORE', store)
        monkeypatch.setattr(service, '_LIFECYCLE', lifecycle)
        scope = tuple(uuid.uuid4() for _ in range(3))
        monkeypatch.setattr(service, 'authority', lambda *args: scope)
        return store, lifecycle
    return make


@pytest.mark.parametrize('path,raw', [('result.txt', b'Useful result'), ('drawing.dxf', DXF)])
def test_full_delivery_and_recipient_download(runtime, path, raw):
    store, lifecycle = runtime(path, raw)
    rid = store.release['release_id']
    completion = service.advance('tenant', 'project', 'campaign', rid)
    assert completion['release']['status'] == 'finished'
    actual, metadata = service.read_artifact('tenant', 'project', 'campaign', rid, path)
    assert actual == raw
    assert metadata['retrieved'] and metadata['content_valid']
    assert store.stages[-1]['evidence']['replay_recipe']
    assert lifecycle.writes == 1


def test_interrupted_publication_recovers_without_second_write(runtime):
    store, lifecycle = runtime()
    lifecycle.interrupt = True
    rid = store.release['release_id']
    with pytest.raises(RuntimeError):
        service.advance('tenant', 'project', 'campaign', rid)
    assert store.decisions and lifecycle.writes == 1
    assert service.advance('tenant', 'project', 'campaign', rid)['release']['status'] == 'finished'
    assert lifecycle.writes == 1


def test_saved_bytes_drift_cannot_be_downloaded(runtime):
    store, lifecycle = runtime()
    rid = store.release['release_id']
    service.advance('tenant', 'project', 'campaign', rid)
    lifecycle.files[-1]['content'] = 'changed'
    with pytest.raises(delivery.DeliveryConflict):
        service.read_artifact('tenant', 'project', 'campaign', rid, 'result.txt')


def test_source_drift_and_missing_provider_stay_incomplete(runtime):
    store, lifecycle = runtime()
    lifecycle.files[0]['content'] = 'changed'
    service.advance('tenant', 'project', 'campaign', store.release['release_id'])
    assert store.release['status'] != 'finished' and lifecycle.writes == 0
    store.release['contract']['selected_artifact'] = None
    service.advance('tenant', 'project', 'campaign', store.release['release_id'])
    assert store.release['status'] != 'finished'


def test_revoked_actor_prevents_publication(runtime, monkeypatch):
    store, lifecycle = runtime()
    def revoked(*args):
        raise PermissionError('revoked')
    monkeypatch.setattr(service, 'authority', revoked)
    with pytest.raises(PermissionError):
        service.advance('tenant', 'project', 'campaign', store.release['release_id'])
    assert lifecycle.writes == 0


def test_empty_checks_and_paused_release(runtime):
    store, lifecycle = runtime()
    store.release['status'] = 'paused'
    service.advance('tenant', 'project', 'campaign', store.release['release_id'])
    assert lifecycle.writes == 0
    store.release['status'] = 'active'
    store.release['contract']['required_checks'] = []
    with pytest.raises(delivery.DeliveryConflict):
        service.advance('tenant', 'project', 'campaign', store.release['release_id'])
