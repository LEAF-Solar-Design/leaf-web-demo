"""Generation-fenced writes and crash recovery against a stateful harness twin."""
from datetime import timedelta
import hashlib
import json
import os
import uuid

import pytest

import lab_artifacts as ledger
import lab_artifacts_git as git
import lab_git_authority as store
from test_lab_artifacts_git import GRAPH, WIDGET

CAP = 'lab-generation-v1'
BASE = 'a' * 40


class Harness:
    def __init__(self):
        self.head = BASE
        self.remote = BASE
        self.generation = '1'
        self.documents = {}
        self.commits = []
        self.calls = []
        self.before_commit = None
        self.crash_after_commit = False
        self.unknown_after_commit = False
        self.fail = None
        self.capability = CAP
        self.history_error = None
        self.incomplete = False
        self.page_size = 1
        self.conflicts = 0

    def foreign(self, path='lab/graphs/other.json', content='{}', message='foreign'):
        commit = f'{len(self.commits) + 1:040x}'
        self.commits.insert(0, {'commit': commit, 'parent': self.head,
            'changed_paths': [path], 'trailers': dict(line.split(': ', 1)
                for line in message.splitlines() if line.startswith('Lab-')), 'message': message})
        self.head = commit
        self.documents[path] = content
        return commit

    def post(self, tenant, endpoint, body):
        self.calls.append((endpoint, dict(body)))
        if endpoint == self.fail:
            raise git.MirrorError('unknown', capability=self.capability)
        if endpoint == 'generation':
            result = {'generation': self.generation, 'head': self.head}
        elif endpoint == 'inspect':
            result = {'ref': git.REF, 'head': self.head, 'commits': self.commits[:200],
                'documents': [{'path': path, 'blob': git._blob(content.encode()),
                    'sha256': hashlib.sha256(content.encode()).hexdigest()}
                    for path, content in self.documents.items()]}
        elif endpoint == 'commit':
            hook, self.before_commit = self.before_commit, None
            if hook:
                hook()
            if self.conflicts:
                self.conflicts -= 1
                self.foreign()
            if body.get('generation') != self.generation and self.generation is not None:
                raise git.MirrorError('lab_generation_conflict', capability=self.capability)
            if body['expected_sha'] != self.head:
                raise git.MirrorError('lab_ref_conflict', self.head, self.capability)
            doc = body['documents'][0]
            commit = self.foreign(doc['path'], doc['content'], body['message'])
            if self.crash_after_commit:
                self.crash_after_commit = False
                raise SystemExit('crash after commit')
            if self.unknown_after_commit:
                self.unknown_after_commit = False
                raise git.MirrorError('unknown', capability=self.capability)
            result = {'ref': git.REF, 'commit': commit}
        elif endpoint == 'ops':
            assert body['head'] == self.head
            ops = dict.fromkeys(body['op_ids'])
            for item in self.commits:
                trailers = item['trailers']
                op = trailers.get('Lab-Op')
                if op in ops:
                    assert ops[op] is None
                    ops[op] = {'commit': item['commit'], 'doc_id': trailers['Lab-Doc'],
                        'revision': int(trailers['Lab-Revision']), 'sha256': trailers['Lab-Sha256']}
            result = {'ops': ops}
        elif endpoint == 'changes':
            if self.history_error:
                raise git.MirrorError(self.history_error, capability=self.capability)
            assert body['head'] == self.head
            history = []
            for entry in self.commits:
                if entry['commit'] == body['since']:
                    break
                history.append(entry)
            if self.incomplete and history:
                history.pop()
            if 'after' in body:
                index = next(i for i, entry in enumerate(history) if entry['commit'] == body['after'])
                history = history[index + 1:]
            entries = history[:self.page_size]
            result = {'commits': entries, 'next': entries[-1]['commit'] if len(history) > len(entries) else None}
        elif endpoint == 'publish':
            if self.generation is not None:
                if body.get('generation') != self.generation:
                    raise git.MirrorError('lab_generation_conflict', capability=self.capability)
                assert 'target_sha' in body
            if body['expected_remote_sha'] != self.remote:
                raise git.MirrorError('lab_publish_conflict', self.remote, self.capability)
            target = body.get('target_sha', self.head)
            order = [BASE] + [item['commit'] for item in reversed(self.commits)]
            if order.index(target) >= order.index(self.remote):
                self.remote = target
            result = {'ref': git.REF, 'published': self.remote}
        else:
            raise AssertionError(endpoint)
        return {**result, '_capability': self.capability}


@pytest.fixture(params=['sqlite', 'postgres'])
def world(request, monkeypatch, tmp_path):
    monkeypatch.setenv('LEAF_RUNTIME_ENV', 'test')
    monkeypatch.setenv('LEAF_PLATFORM_POSTGRES_REQUIRED', '0')
    monkeypatch.setenv('LEAF_LAB_STORE', request.param)
    monkeypatch.setenv('LEAF_LAB_DB', str(tmp_path / 'write.db'))
    if request.param == 'postgres':
        if not os.environ.get('DATABASE_URL'):
            pytest.skip('platform DB unreachable: DATABASE_URL unset')
        import platform_link
        platform_link.platform_db().apply_migration()
    tenant = 'write-' + uuid.uuid4().hex
    git.mirror_status(tenant)
    ledger._run(tenant, lambda sql: git._update(sql, tenant, mode='mirror', frozen=True,
                                              local_sha=BASE, remote_sha=BASE), write=True)
    generation = store.next_generation(tenant)
    store.authority_insert(tenant, generation, BASE, BASE)
    harness = Harness()
    monkeypatch.setattr(git, '_post', harness.post)
    return tenant, harness


def save(tenant, doc='x', expected=None, kind='graph'):
    return ledger.save(tenant, doc, WIDGET if kind == 'widget' else GRAPH, expected, 'writer', kind)


def pending(tenant):
    return store._run(tenant, lambda sql: [store.get_sql(sql, tenant, row['op_id']) for row in
        sql.execute("SELECT op_id FROM lab_git_pending WHERE tenant_id=? AND state<>'abandoned'", (tenant,)).fetchall()])


def outbox(tenant):
    return store._run(tenant, lambda sql: [store._get(sql, 'lab_git_outbox', tenant, 'outbox_id', row['outbox_id'])
        for row in sql.execute('SELECT outbox_id FROM lab_git_outbox WHERE tenant_id=? ORDER BY sequence', (tenant,)).fetchall()])


def expire(tenant):
    store._run(tenant, lambda sql: sql.execute('UPDATE lab_git_pending SET lease_expires_at=? WHERE tenant_id=?',
        ((store._now() - timedelta(seconds=1)).isoformat(), tenant)), write=True)


def crash_staged(tenant, harness):
    def crash():
        raise SystemExit('crash after staging')
    harness.before_commit = crash
    with pytest.raises(SystemExit):
        save(tenant)
    return pending(tenant)[0]


def test_save_stages_before_commit_and_completes_atomically(world):
    tenant, harness = world
    def observe():
        row = pending(tenant)[0]
        assert row['revision'] == 1 and row['fence'] == 1
        assert row['expected_parents'] == [BASE]
        assert row['base_blob'] is None and row['new_document']
        assert store._date(row['lease_expires_at']) > store._now() + timedelta(seconds=50)
        with pytest.raises(ledger.NotFound):
            ledger.get(tenant, 'x')
        assert outbox(tenant) == []
    harness.before_commit = observe
    row = save(tenant)
    request = next(body for endpoint, body in harness.calls if endpoint == 'commit')
    assert request['generation'] == '1'
    op = harness.commits[0]['trailers']['Lab-Op']
    assert len(op) == 32 and int(op, 16) >= 0
    assert row['author'] == 'writer' and row['revision'] == 1
    assert pending(tenant) == []
    assert store.commit_get(tenant, harness.head)['op_id'] == op
    assert git.mirror_status(tenant)['local_sha'] == harness.head
    assert store.authority_get(tenant)['verified_head'] == harness.head
    assert outbox(tenant)[0]['payload'] == {'generation': '1', 'target_sha': harness.head}
    assert harness.remote == BASE


def test_interleaved_documents_rebase_without_losing_either(world):
    tenant, harness = world
    harness.before_commit = lambda: save(tenant, 'other')
    save(tenant)
    assert set(harness.documents) == {'lab/graphs/x.json', 'lab/graphs/other.json'}
    assert ledger.get(tenant, 'x')['revision'] == ledger.get(tenant, 'other')['revision'] == 1
    assert len([1 for endpoint, _ in harness.calls if endpoint == 'commit']) == 3
    assert pending(tenant) == []


def test_same_document_conflict_abandons_definite_attempt(world):
    tenant, harness = world
    harness.before_commit = lambda: harness.foreign('lab/graphs/x.json')
    with pytest.raises(ledger.GitConflict, match='lab_artifact_conflict'):
        save(tenant)
    assert pending(tenant) == []
    with pytest.raises(ledger.NotFound):
        ledger.get(tenant, 'x')


def test_change_then_restore_rebases_using_head_blob_and_paginates(world):
    tenant, harness = world
    first = save(tenant)
    def restored():
        harness.foreign('lab/graphs/x.json', '{}')
        harness.foreign('lab/graphs/x.json', first['canonical_text'])
    harness.before_commit = restored
    assert save(tenant, expected=1)['revision'] == 2
    pages = [body for endpoint, body in harness.calls if endpoint == 'changes']
    assert len(pages) == 2 and 'after' in pages[-1]


def test_four_unrelated_conflicts_exhaust_durable_budget(world):
    tenant, harness = world
    harness.conflicts = 4
    with pytest.raises(ledger.GitConflict):
        save(tenant)
    assert len([1 for endpoint, _ in harness.calls if endpoint == 'commit']) == 4
    assert pending(tenant) == []
    row = store._run(tenant, lambda sql: dict(sql.execute('SELECT * FROM lab_git_pending WHERE tenant_id=?', (tenant,)).fetchone()))
    assert row['rebase_count'] == 3 and row['state'] == 'abandoned'


@pytest.mark.parametrize('history_failure', ['lab_history_nonlinear', 'lab_history_too_long', 'incomplete'])
def test_unprovable_history_refuses_rebase(world, history_failure):
    tenant, harness = world
    harness.history_error = None if history_failure == 'incomplete' else history_failure
    harness.incomplete = history_failure == 'incomplete'
    harness.conflicts = 1
    with pytest.raises(ledger.GitConflict):
        save(tenant)
    assert pending(tenant) == []


def test_crash_after_staging_recovers_by_committing(world):
    tenant, harness = world
    row = crash_staged(tenant, harness)
    expire(tenant)
    git.recover_pending(tenant)
    assert ledger.get(tenant, 'x')['revision'] == row['revision']
    assert len(harness.commits) == 1 and pending(tenant) == []


def test_crash_after_commit_recovers_same_revision_without_recommit(world):
    tenant, harness = world
    harness.crash_after_commit = True
    with pytest.raises(SystemExit):
        save(tenant)
    row = pending(tenant)[0]
    expire(tenant)
    git.recover_pending(tenant)
    assert ledger.get(tenant, 'x')['revision'] == row['revision']
    assert ledger.get(tenant, 'x')['author'] == 'writer'
    assert len(harness.commits) == 1 and pending(tenant) == []


def test_unknown_commit_stays_ambiguous_until_proven(world):
    tenant, harness = world
    harness.unknown_after_commit = True
    with pytest.raises(ledger.GitUnavailable, match='lab_store_unavailable'):
        save(tenant)
    row = pending(tenant)[0]
    assert row['state'] == 'ambiguous'
    harness.history_error = 'lab_history_nonlinear'
    expire(tenant)
    git.recover_pending(tenant)
    assert pending(tenant)[0]['state'] == 'ambiguous'
    assert pending(tenant)[0]['op_id'] == row['op_id']
    harness.history_error = None
    expire(tenant)
    git.recover_pending(tenant)
    assert len(harness.commits) == 1 and pending(tenant) == []


def test_stale_fence_and_generation_complete_nothing(world):
    tenant, harness = world
    row = crash_staged(tenant, harness)
    expire(tenant)
    taken = store.take_over_expired(tenant, row['op_id'], row['lease_owner'], 60)
    assert git._complete_pending(tenant, row, '1', 'b' * 40, BASE) is None
    assert git._complete_pending(tenant, taken, '2', 'b' * 40, BASE) is None
    with pytest.raises(ledger.NotFound):
        ledger.get(tenant, 'x')
    assert outbox(tenant) == [] and store.commit_get(tenant, 'b' * 40) is None


def test_recovery_contradiction_quarantines_and_notifies_once(world):
    tenant, harness = world
    row = crash_staged(tenant, harness)
    harness.foreign('lab/graphs/x.json')
    expire(tenant)
    git.recover_pending(tenant)
    git.recover_pending(tenant)
    assert pending(tenant) == []
    quarantined = store._run(tenant, lambda sql: store._get(sql, 'lab_git_quarantine', tenant, 'op_id', row['op_id']))
    assert quarantined['canonical_text'] == row['canonical_text']
    notifications = outbox(tenant)
    assert len(notifications) == 1 and notifications[0]['payload']['event'] == 'lab_replay_divergence'


@pytest.mark.parametrize('cap', ['MAX_TENANT_DOCS', 'MAX_TENANT_REVISIONS', 'MAX_TENANT_BYTES'])
def test_reservations_prevent_overcommitting_last_slot(world, monkeypatch, cap):
    tenant, harness = world
    row = crash_staged(tenant, harness)
    monkeypatch.setattr(ledger, cap, len(row['canonical_text'].encode()) if cap.endswith('BYTES') else 1)
    with pytest.raises(ledger.TooLarge):
        save(tenant, 'other')
    assert len(pending(tenant)) == 1


def test_outbox_publishes_in_order_and_defers_with_one_notification(world, monkeypatch):
    tenant, harness = world
    save(tenant)
    save(tenant, 'other')
    harness.fail = 'publish'
    assert git.process_outbox(tenant) == 0
    rows = outbox(tenant)
    assert len([row for row in rows if row['kind'] == 'notify']) == 2
    assert all(row['attempts'] == 1 for row in rows if row['kind'] == 'publish')
    # Reclaim immediately to prove retries retain one alert identity per commit.
    store._run(tenant, lambda sql: sql.execute('UPDATE lab_git_outbox SET next_attempt_at=? WHERE tenant_id=?',
        ((store._now() - timedelta(seconds=1)).isoformat(), tenant)), write=True)
    events = []
    monkeypatch.setattr(git, '_notify', lambda t, event, identity: events.append((event, identity)))
    git.process_outbox(tenant)
    assert len([row for row in outbox(tenant) if row['kind'] == 'notify']) == 2
    store._run(tenant, lambda sql: sql.execute('UPDATE lab_git_outbox SET next_attempt_at=? WHERE tenant_id=?',
        ((store._now() - timedelta(seconds=1)).isoformat(), tenant)), write=True)
    harness.fail = None
    git.process_outbox(tenant)
    published = [body['target_sha'] for endpoint, body in harness.calls if endpoint == 'publish'][-2:]
    assert published == [row['identity'] for row in rows if row['kind'] == 'publish']
    assert harness.remote == harness.head and len(events) == 2
    assert all(row['done_at'] is not None for row in outbox(tenant))


def test_missing_capability_refuses_and_preserves_pending(world):
    tenant, harness = world
    harness.capability = None
    with pytest.raises(ledger.GitUnavailable, match='lab_harness_capability_missing'):
        save(tenant)
    assert len(pending(tenant)) == 1
    with pytest.raises(ledger.NotFound):
        ledger.get(tenant, 'x')


def test_generation_conflict_retains_ambiguous_pending(world):
    tenant, harness = world
    harness.generation = '2'
    with pytest.raises(ledger.GitUnavailable, match='lab_store_migrating'):
        save(tenant)
    assert pending(tenant)[0]['state'] == 'ambiguous'


def test_live_document_lease_refuses_second_writer(world):
    tenant, harness = world
    row = crash_staged(tenant, harness)
    with pytest.raises(ledger.GitConflict):
        save(tenant)
    assert pending(tenant)[0]['op_id'] == row['op_id']


def test_restore_preserves_widget_kind_and_source_parent(world):
    tenant, harness = world
    save(tenant, kind='widget')
    save(tenant, expected=1, kind='widget')
    restored = ledger.restore(tenant, 'x', 1, 2, 'restorer')
    assert restored['kind'] == 'widget' and restored['parent_revision'] == 1
    assert harness.commits[0]['changed_paths'] == ['lab/widgets/x.json']
    assert restored['author'] == 'restorer'


def test_publish_moves_pointer_and_reuses_commit_outbox_identity(world):
    tenant, harness = world
    save(tenant)
    receipt = ledger.publish(tenant, 'x', 1, 'publisher')
    assert receipt['new_head'] == 1 and ledger.get(tenant, 'x')['published_revision'] == 1
    assert len(harness.commits) == 1 and len(outbox(tenant)) == 1


@pytest.mark.parametrize('generation', [None, '7'])
def test_mirror_mutations_discover_generation_and_keep_legacy_shape(world, generation):
    tenant, harness = world
    harness.generation = generation
    git._generation_post(tenant, 'commit', {'expected_sha': BASE,
        'documents': [{'path': 'lab/graphs/x.json', 'content': '{}'}], 'message': 'mirror'})
    git._generation_post(tenant, 'publish', {'expected_remote_sha': BASE}, harness.head)
    mutations = [body for endpoint, body in harness.calls if endpoint in ('commit', 'publish')]
    assert len([1 for endpoint, _ in harness.calls if endpoint == 'generation']) == 2
    assert all(body.get('generation') == generation for body in mutations)
    assert ('target_sha' in mutations[-1]) == (generation is not None)


def test_mirror_generation_conflict_rereads_once(world, monkeypatch):
    tenant, harness = world
    original = harness.post
    mutations = 0
    def fenced(t, endpoint, body):
        nonlocal mutations
        if endpoint == 'commit':
            mutations += 1
            if mutations == 1:
                harness.generation = '2'
        return original(t, endpoint, body)
    monkeypatch.setattr(git, '_post', fenced)
    git._generation_post(tenant, 'commit', {'expected_sha': BASE,
        'documents': [{'path': 'lab/graphs/x.json', 'content': '{}'}], 'message': 'mirror'})
    assert mutations == 2 and len(harness.commits) == 1
    assert [body['generation'] for endpoint, body in harness.calls if endpoint == 'commit'] == ['1', '2']


def test_router_uses_git_error_status_and_safe_detail(world):
    from fastapi import HTTPException
    from routers.lab_artifacts import _call

    def fail(error):
        raise error
    for error, status, detail in [
            (ledger.GitConflict('private'), 409, 'lab_artifact_conflict'),
            (ledger.GitUnavailable('lab_store_migrating'), 503, 'lab_store_migrating'),
            (ledger.GitUnavailable('lab_harness_capability_missing'), 503, 'lab_harness_capability_missing'),
            (ledger.GitUnavailable('private'), 503, 'lab_store_unavailable')]:
        with pytest.raises(HTTPException) as caught:
            _call(fail, error)
        assert caught.value.status_code == status and caught.value.detail == detail


def test_completion_failure_rolls_back_ledger_commit_and_outbox(world, monkeypatch):
    tenant, harness = world
    original = store.enqueue_sql
    def fail(sql, t, kind, identity, payload):
        if kind == 'publish':
            raise RuntimeError('crash completing')
        return original(sql, t, kind, identity, payload)
    monkeypatch.setattr(store, 'enqueue_sql', fail)
    with pytest.raises(RuntimeError, match='crash completing'):
        save(tenant)
    assert outbox(tenant) == [] and len(pending(tenant)) == 1
    assert store.commit_get(tenant, harness.head) is None
    with pytest.raises(ledger.NotFound):
        ledger.get(tenant, 'x')
    monkeypatch.setattr(store, 'enqueue_sql', original)
    expire(tenant)
    git.recover_pending(tenant)
    assert ledger.get(tenant, 'x')['revision'] == 1 and len(harness.commits) == 1


def test_store_sql_helpers_share_the_callers_transaction(world):
    tenant, _ = world
    op = uuid.uuid4().hex
    def transaction(sql):
        assert store.authority_get_sql(sql, tenant)['generation'] == 1
        staged = store.stage_sql(sql, tenant, op, 'sql-doc', 1, None, 'graph', '{}', 'a' * 64,
                                 expected_parents=[BASE], new_document=True)
        assert store.get_sql(sql, tenant, op) == staged
        assert store.reservations_sql(sql, tenant) == {'bytes': 2, 'revisions': 1, 'new_documents': 1}
        assert not store.complete_conditional_sql(sql, tenant, op, 2)
        assert store.complete_conditional_sql(sql, tenant, op, 1)
        store.record_sql(sql, tenant, 'b' * 40, op, 'sql-doc', 1, 'graph', BASE)
        store.enqueue_sql(sql, tenant, 'publish', 'b' * 40, {'target_sha': 'b' * 40, 'generation': '1'})
        raise RuntimeError('rollback all helpers')
    with pytest.raises(RuntimeError, match='rollback all helpers'):
        ledger._run(tenant, transaction, write=True)
    assert pending(tenant) == [] and outbox(tenant) == []
    assert store.commit_get(tenant, 'b' * 40) is None


def test_git_pending_recovery_and_outbox_are_tenant_bound(world):
    tenant, harness = world
    row = crash_staged(tenant, harness)
    other = tenant + '-other'
    assert store.get(other, row['op_id']) is None
    assert git.recover_pending(other) == []
    assert git.process_outbox(other) == 0
    assert not store.complete_conditional(other, row['op_id'], row['fence'])
    assert store.reservations(other) == {'bytes': 0, 'revisions': 0, 'new_documents': 0}
    expire(tenant)
    git.recover_pending(tenant)
    assert store.commit_get(other, harness.head) is None and outbox(other) == []
    with pytest.raises(ledger.NotFound):
        ledger.get(other, 'x')


def test_recovery_of_uncommitted_write_rebases_after_unrelated_commit(world):
    tenant, harness = world
    crash_staged(tenant, harness)
    harness.foreign()
    expire(tenant)
    git.recover_pending(tenant)
    assert ledger.get(tenant, 'x')['revision'] == 1
    assert len(harness.commits) == 2 and pending(tenant) == []


def test_save_recovers_expired_document_before_comparing_expected_head(world):
    tenant, harness = world
    crash_staged(tenant, harness)
    expire(tenant)
    assert save(tenant, expected=1)['revision'] == 2
    assert len(harness.commits) == 2 and pending(tenant) == []


def test_recovery_retains_original_generation_across_takeover(world):
    tenant, harness = world
    row = crash_staged(tenant, harness)
    expire(tenant)
    store._run(tenant, lambda sql: sql.execute('UPDATE lab_git_authority SET generation=2 WHERE tenant_id=?',
                                              (tenant,)), write=True)
    harness.generation = '2'
    git.recover_pending(tenant)
    assert pending(tenant)[0]['op_id'] == row['op_id']
    assert pending(tenant)[0]['state'] == 'ambiguous'
    assert harness.commits == []


def test_outbox_retry_after_lost_publish_response_cannot_regress_remote(world):
    tenant, harness = world
    first = save(tenant)
    old_head = harness.head
    save(tenant, 'other')
    # A different worker published the newest commit before persisting its receipt.
    harness.remote = harness.head
    assert git.process_outbox(tenant) == 2
    assert harness.remote == harness.head and harness.remote != old_head
    assert ledger.get(tenant, 'x')['sha256'] == first['sha256']
    assert all(row['done_at'] is not None for row in outbox(tenant))


def test_http_transport_preserves_generation_errors_and_capability(world, monkeypatch):
    import io
    import urllib.error

    tenant, _ = world
    monkeypatch.setenv('LEAF_AUTHOR_HARNESS_URL', 'https://harness.test')
    monkeypatch.setenv('LEAF_HARNESS_SECRET', 'private-secret')
    # Bypass the model so the real client, including HTTP error headers, is exercised.
    monkeypatch.setattr(git, '_post', REAL_POST)
    def conflict(*args, **kwargs):
        raise urllib.error.HTTPError('private-url', 409, 'private', {'x-leaf-lab-capability': CAP},
            io.BytesIO(json.dumps({'error': {'code': 'lab_generation_conflict'}}).encode()))
    monkeypatch.setattr(git.urllib.request, 'urlopen', conflict)
    with pytest.raises(git.MirrorError, match='^lab_generation_conflict$'):
        git._git_post(tenant, 'commit', {})
    def missing(*args, **kwargs):
        raise urllib.error.HTTPError('private-url', 409, 'private', {},
            io.BytesIO(json.dumps({'error': {'code': 'lab_ref_conflict'}}).encode()))
    monkeypatch.setattr(git.urllib.request, 'urlopen', missing)
    with pytest.raises(git.MirrorError, match='^lab_harness_capability_missing$'):
        git._git_post(tenant, 'commit', {})


def test_revision_divergence_quarantines_without_recording_completion(world):
    tenant, harness = world
    row = crash_staged(tenant, harness)
    ledger._run(tenant, lambda sql: ledger._append(sql, tenant, 'x', row['canonical_text'].encode(),
                                                 'unexpected-writer', None, None), write=True)
    assert git._complete_pending(tenant, row, '1', 'b' * 40, BASE) is None
    assert store.commit_get(tenant, 'b' * 40) is None
    assert ledger.get(tenant, 'x')['author'] == 'unexpected-writer'
    assert pending(tenant) == []
    quarantined = store._run(tenant, lambda sql: store._get(sql, 'lab_git_quarantine', tenant, 'op_id', row['op_id']))
    assert quarantined['reason'] == 'revision_divergence'
    assert [item['kind'] for item in outbox(tenant)] == ['notify']


@pytest.mark.parametrize('generation', [None, '1'])
def test_mirror_mode_without_authority_saves_and_publishes_with_discovered_generation(world, monkeypatch, generation):
    tenant, harness = world
    transition = store.begin(tenant, uuid.uuid4().hex, 'rollback', 1, 2, 'operator')
    store.authority_delete(tenant, transition['transition_id'])
    ledger._run(tenant, lambda sql: git._update(sql, tenant, frozen=False), write=True)
    harness.generation = generation
    monkeypatch.setenv('LEAF_LAB_GIT_TENANTS', tenant)
    assert save(tenant)['revision'] == 1
    assert harness.remote == harness.head and len(harness.commits) == 1
    requests = [(endpoint, body) for endpoint, body in harness.calls if endpoint in ('commit', 'publish')]
    assert [endpoint for endpoint, _ in requests] == ['commit', 'publish']
    assert all(body.get('generation') == generation for _, body in requests)
    assert pending(tenant) == [] and outbox(tenant) == []


REAL_POST = git._post
