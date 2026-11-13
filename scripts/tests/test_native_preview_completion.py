"""Actual worker/journal recovery with fake HTTP, SDK and allocator evidence."""
from contextlib import contextmanager
import copy
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import native_preview_service as service
import test_native_preview_worker as f

native_package = f.native_package
source = f.source
SCOPE = f.SCOPE
RUNTIME = '6ec56727dccf824a9c3fa67b031d2def52740c01'


@pytest.fixture(autouse=True)
def captured_git_source(monkeypatch):
    # Isolate Git availability while retaining the real worker source digest
    # and Journal fingerprint checks. Real Git binding has separate coverage.
    r = service.recovery
    captured = {name: (Path(r.__file__).parent / name.rsplit('/', 1)[1]).read_bytes()
                for name in ('scripts/native_preview_worker.py', 'scripts/native_preview_reconciliation.py')}
    def resolve(revision):
        assert revision in ('HEAD', RUNTIME)
        return RUNTIME
    def load(revision, name):
        assert revision == RUNTIME and name in captured
        return captured[name]
    monkeypatch.setattr(r, 'runtime_commit', resolve)
    monkeypatch.setattr(r, 'runtime_source', load)


class Stop:
    def __init__(self):
        self.stopped = False
    def is_set(self):
        return self.stopped
    def wait(self, seconds):
        self.stopped = True
        return True


def directory(root):
    return root / hashlib.sha256(service.worker.encode(SCOPE)).hexdigest()


def checkpoint(root):
    return directory(root) / 'consumer.json'


def cycle(root):
    return directory(root) / json.loads(checkpoint(root).read_bytes())['cycle']


class Scenario:
    def __init__(self, root, package, image=None):
        self.root = root
        self.raw, self.job = copy.deepcopy(package)
        self.events = []
        self.claims = self.executions = self.completions = 0
        self.receipt = None
        self.intent_seen = False
        self.status_hook = None
        self.close_error = False
        self.stop = Stop()
        self.pause_after_submit = True
        self.report = {}
        self.image = f.png() if image is None else image
        source = service.worker.native_identity(self.job['descriptor'])
        bundle = 'private.leaf.project.p' + f.PROJECT.replace('-', '')
        self.facts = dict(f.facts(), screenshot_sha256=hashlib.sha256(self.image).hexdigest())
        if self.job['descriptor'].get('schema') == service.worker.NATIVE_SCHEMA:
            self.facts.update(bundle_id=bundle, build_artifact=dict(
                source=source, bundle_id=bundle, sdk='iphonesimulator', app_tree_sha256='d' * 64,
                file_count=5, xcodegen_exit_code=0, xcode_version='Xcode fixture',
                xcodegen_version='fixture', sdk_version='fixture'))
        witness = dict(job_id=f.JOB, attempt=1, prefix='preview-' + f.JOB + '-1-',
                       ticket=self.facts['allocator_ticket'], event_times=dict(
                           grant=10, release=20, confirm_dead=30, free=40),
                       active_match_counts=[0, 0], history_complete=True)
        witness['witness_sha256'] = service.recovery.sha(service.worker.encode(witness))
        self.witness = witness

    def request(self, method, suffix, body=None, limit=16384):
        self.events.append(('request', method, suffix))
        if suffix == 'claim':
            self.claims += 1
            if self.claims > 1:
                assert service.recovery.receipt_path(directory(self.root), f.JOB).exists()
                return b'{"job":null}'
            return service.worker.encode({'job': self.job})
        if suffix.endswith('/heartbeat'):
            return service.worker.encode({k: self.job[k] for k in (
                'job_id', 'lease_owner', 'attempt', 'deadline')})
        if '/source?' in suffix:
            return self.raw
        assert suffix == f.JOB + '/complete' and body['outcome'] == 'complete'
        self.completions += 1
        self.receipt = copy.deepcopy(body['receipt'])
        intent = cycle(self.root) / (f.JOB + '-1-completion.json')
        self.intent_seen = intent.exists() and intent.read_bytes() == service.worker.encode(self.receipt)
        self.public = dict(job_id=f.JOB, status='complete',
                           **service.worker.native_identity(self.job['descriptor']),
                           receipt=dict(kind='simulator', receipt_id=f.UDID,
                                        sha256=service.recovery.sha(service.worker.encode(self.receipt))),
                           evidence_basis='authenticated-worker-report')
        if self.pause_after_submit:
            self.stop.stopped = True
        raise TimeoutError('synthetic dropped completion response')

    def __call__(self, raw, job, guard):
        self.executions += 1
        assert raw == self.raw and job == self.job
        guard.stage('mac-producer')
        guard.record(dict(event='allocator', ticket=self.facts['allocator_ticket'], marker_sha256='e' * 64))
        guard.record(dict(event='producer', producer_may_have_started=True))
        guard.record(dict(event='cleanup', cleanup_complete=True))
        guard.record(dict(event='release', allocator_release_confirmed=True))
        self.report = {k: self.facts[k] for k in (
            'cleanup_complete', 'allocator_release_confirmed', 'build_artifact') if k in self.facts}
        return copy.deepcopy(self.facts), self.image

    def status(self, jid):
        assert jid == f.JOB
        self.events.append(('status', jid))
        if self.status_hook:
            self.status_hook()
        return service.worker.encode(self.public)

    def read_reconciliation_allocator(self, jid, attempt):
        assert (jid, attempt) == (f.JOB, 1)
        self.events.append(('allocator', jid, attempt))
        return copy.deepcopy(self.witness)

    @contextmanager
    def session(self, scope):
        assert scope == SCOPE and scope is not SCOPE
        yield self, self
        if self.close_error:
            raise RuntimeError('synthetic close error')

    def start(self):
        assert service.serve(SCOPE, self.session, self.root, self.stop) == dict(
            state='stopped', completed=0)
        assert json.loads(checkpoint(self.root).read_bytes())['phase'] == 'reconciliation-required'
        assert self.executions == self.completions == self.claims == 1
        assert self.intent_seen
        return cycle(self.root)

    def resume(self):
        return service.serve(SCOPE, self.session, self.root, Stop())


def test_lost_completion_reconciles_exact_job_without_rebuild(native_package, tmp_path):
    scenario = Scenario(tmp_path, native_package)
    original_cycle = scenario.start()
    originals = {p: p.read_bytes() for p in original_cycle.iterdir()}
    assert scenario.resume() == dict(state='stopped', completed=0)
    receipt = service.recovery.receipt_path(directory(tmp_path), f.JOB)
    value = json.loads(receipt.read_bytes())
    assert value['outcome'] == 'completed-producer-reconciled'
    assert value['completion_receipt_sha256'] == scenario.public['receipt']['sha256']
    assert value['attempt_binding'] == 'authenticated-completion-receipt'
    assert (value['job_id'], value['attempt']) == (f.JOB, 1)
    assert value['source'] == service.worker.native_identity(scenario.job['descriptor'])
    assert scenario.executions == scenario.completions == 1 and scenario.claims == 2
    assert all(p.read_bytes() == raw for p, raw in originals.items())


def test_existing_portfolio_completion_uses_the_same_recovery(source, tmp_path):
    scenario = Scenario(tmp_path, source)
    scenario.start()
    assert scenario.resume() == dict(state='stopped', completed=0)
    value = json.loads(service.recovery.receipt_path(directory(tmp_path), f.JOB).read_bytes())
    assert value['source'] == service.worker.native_identity(scenario.job['descriptor'])
    assert value['outcome'] == 'completed-producer-reconciled'
    assert scenario.executions == scenario.completions == 1


def test_completion_can_reconcile_without_restarting_service(native_package, tmp_path):
    scenario = Scenario(tmp_path, native_package)
    scenario.pause_after_submit = False
    assert service.serve(SCOPE, scenario.session, tmp_path, Stop()) == dict(state='stopped', completed=0)
    assert scenario.intent_seen and scenario.executions == scenario.completions == 1
    assert scenario.claims == 2
    value = json.loads(service.recovery.receipt_path(directory(tmp_path), f.JOB).read_bytes())
    assert value['outcome'] == 'completed-producer-reconciled'


def test_screenshot_larger_than_local_json_bound_remains_recoverable(native_package, tmp_path):
    text = b'Note\0' + b'x' * 20000
    chunk = f.struct.pack('>I', len(text)) + b'tEXt' + text + f.struct.pack('>I', f.zlib.crc32(b'tEXt' + text))
    image = f.png()[:-12] + chunk + f.png()[-12:]
    assert len(image) > service.recovery.MAX_LOCAL
    scenario = Scenario(tmp_path, native_package, image)
    scenario.start()
    assert scenario.resume() == dict(state='stopped', completed=0)
    assert scenario.executions == scenario.completions == 1


@pytest.mark.parametrize('mutation', [
    'digest', 'job', 'source', 'scope', 'attempt', 'owner', 'image', 'ticket', 'artifact',
    'cleanup', 'release', 'extra', 'missing-intent', 'missing-image', 'extra-field',
])
def test_completion_evidence_drift_keeps_checkpoint(native_package, tmp_path, mutation):
    scenario = Scenario(tmp_path, native_package)
    output = scenario.start()
    intent = output / (f.JOB + '-1-completion.json')
    marker = checkpoint(tmp_path).read_bytes()
    if mutation == 'digest':
        scenario.public['receipt']['sha256'] = 'f' * 64
    elif mutation == 'job':
        scenario.public['job_id'] = f.ACTOR
    elif mutation == 'source':
        scenario.public['source_commit'] = 'f' * 40
    elif mutation in ('scope', 'attempt', 'owner', 'ticket', 'artifact', 'extra-field'):
        value = json.loads(intent.read_bytes())
        if mutation == 'scope': value['worker_binding']['actor_id'] = f.OWNER
        elif mutation == 'attempt': value['attempt'] = 2
        elif mutation == 'owner': value['lease_owner'] = f.ACTOR
        elif mutation == 'ticket': value['allocator_ticket'] = 'foreign-ticket'
        elif mutation == 'artifact': value['build_artifact']['source']['source_tree'] = 'f' * 40
        else: value['unexpected'] = 'not a receipt field'
        intent.write_bytes(service.worker.encode(value))
    elif mutation == 'image':
        (output / (f.JOB + '-1.png')).write_bytes(f.png() + b'changed')
    elif mutation in ('cleanup', 'release'):
        journal = output / 'journal.json'
        value = json.loads(journal.read_bytes())
        value['events'][-2 if mutation == 'cleanup' else -1][
            'cleanup_complete' if mutation == 'cleanup' else 'allocator_release_confirmed'] = False
        journal.write_bytes(service.worker.encode(value))
    elif mutation == 'missing-intent': intent.unlink()
    elif mutation == 'missing-image': (output / (f.JOB + '-1.png')).unlink()
    else: (output / 'extra.json').write_text('{}')
    assert scenario.resume()['state'] == 'reconciliation-required'
    assert checkpoint(tmp_path).read_bytes() == marker
    assert not service.recovery.receipt_path(directory(tmp_path), f.JOB).exists()
    assert scenario.executions == scenario.completions == scenario.claims == 1


@pytest.mark.parametrize('state', ['queued', 'running', 'failed', 'cancelled'])
def test_noncomplete_backend_cannot_clear_checkpoint(native_package, tmp_path, state):
    scenario = Scenario(tmp_path, native_package)
    scenario.start()
    scenario.public['status'] = state
    del scenario.public['receipt'], scenario.public['evidence_basis']
    marker = checkpoint(tmp_path).read_bytes()
    expected = 'stopped' if state in ('queued', 'running') else 'reconciliation-required'
    assert scenario.resume()['state'] == expected
    assert checkpoint(tmp_path).read_bytes() == marker
    assert not service.recovery.receipt_path(directory(tmp_path), f.JOB).exists()
    assert scenario.executions == scenario.completions == scenario.claims == 1
    assert not any(event[0] == 'allocator' for event in scenario.events)


@pytest.mark.parametrize('failure', ['context-close', 'changed-local-file', 'allocator-active', 'allocator-ticket'])
def test_collection_cannot_commit_unsettled_evidence(native_package, tmp_path, failure):
    scenario = Scenario(tmp_path, native_package)
    output = scenario.start()
    marker = checkpoint(tmp_path).read_bytes()
    if failure == 'context-close': scenario.close_error = True
    elif failure == 'changed-local-file':
        scenario.status_hook = lambda: (output / (f.JOB + '-1-completion.json')).write_bytes(b'changed')
    else:
        if failure == 'allocator-active': scenario.witness['active_match_counts'] = [1, 0]
        else: scenario.witness['ticket'] = 'foreign-ticket'
        scenario.witness['witness_sha256'] = service.recovery.sha(service.worker.encode(
            {k: v for k, v in scenario.witness.items() if k != 'witness_sha256'}))
    expected = 'stopped' if failure == 'context-close' else 'reconciliation-required'
    assert scenario.resume()['state'] == expected
    assert checkpoint(tmp_path).read_bytes() == marker
    assert not service.recovery.receipt_path(directory(tmp_path), f.JOB).exists()
    assert scenario.executions == scenario.completions == scenario.claims == 1


def test_capture_write_failure_prevents_completion_transport(native_package, tmp_path, monkeypatch):
    scenario = Scenario(tmp_path, native_package)
    immutable = service.recovery.immutable
    def fail_intent(path, raw):
        if path.name.endswith('-completion.json'):
            raise OSError('synthetic storage failure')
        return immutable(path, raw)
    monkeypatch.setattr(service.recovery, 'immutable', fail_intent)
    assert service.serve(SCOPE, scenario.session, tmp_path, Stop())['state'] == 'reconciliation-required'
    assert scenario.executions == scenario.claims == 1 and scenario.completions == 0
    assert scenario.resume()['state'] == 'reconciliation-required'
    assert not service.recovery.receipt_path(directory(tmp_path), f.JOB).exists()


def test_existing_settled_failure_recovery_assertions_still_hold(tmp_path):
    import test_native_preview_service as prior
    prior.test_settled_producer_recovers_once_preserves_evidence_and_resumes_polling(tmp_path)
