"""Receipted cutover and rollback, with identical SQLite/PostgreSQL cases."""
from datetime import timedelta
import os
import uuid

import pytest

import lab_artifacts as ledger
import lab_artifacts_git as git
import lab_git_authority as store
from test_lab_artifacts_git import GRAPH
from test_lab_git_write import Harness as WriteHarness, BASE, pending, outbox, save


class Harness(WriteHarness):
    def __init__(self):
        super().__init__()
        self.remote = None
        self.generation = None
        self.commits = [{'commit': BASE, 'parent': None, 'changed_paths': [], 'trailers': {}, 'message': 'initial'}]
        self.receipts = {}
        self.after_fence = None

    def post(self, tenant, endpoint, body):
        if endpoint == 'publish' and self.remote is None:
            self.calls.append((endpoint, dict(body)))
            if self.fail == endpoint:
                raise git.MirrorError('unknown', capability=self.capability)
            if body['expected_remote_sha'] is not None:
                raise git.MirrorError('lab_publish_conflict', self.remote, self.capability)
            self.remote = body.get('target_sha', self.head)
            return {'ref': git.REF, 'published': self.remote, '_capability': self.capability}
        if endpoint != 'fence':
            return super().post(tenant, endpoint, body)
        self.calls.append((endpoint, dict(body)))
        if self.fail == endpoint:
            raise git.MirrorError('unknown', capability=self.capability)
        receipt = dict(transition_id=body['transition_id'], from_generation=body['expected_generation'],
                       to_generation=body['new_generation'], barrier_head=body['expected_head'])
        prior = self.receipts.get(body['transition_id'])
        if prior is not None:
            assert prior == receipt
        else:
            if (self.generation != body['expected_generation'] or self.head != body['expected_head']
                    or int(body['new_generation']) <= int(self.generation or 0)):
                raise git.MirrorError('lab_generation_conflict', capability=self.capability)
            self.receipts[body['transition_id']] = receipt
            self.generation = body['new_generation']
        hook, self.after_fence = self.after_fence, None
        if hook:
            hook()
        return {'receipt': receipt, 'head': self.head, '_capability': self.capability}


@pytest.fixture(params=['sqlite', 'postgres'])
def world(request, monkeypatch, tmp_path):
    monkeypatch.setenv('LEAF_RUNTIME_ENV', 'test')
    monkeypatch.setenv('LEAF_PLATFORM_POSTGRES_REQUIRED', '0')
    monkeypatch.setenv('LEAF_LAB_STORE', request.param)
    monkeypatch.setenv('LEAF_LAB_DB', str(tmp_path / 'cutover.db'))
    monkeypatch.setenv('LEAF_LAB_GIT_TENANTS', '')
    if request.param == 'postgres':
        if not os.environ.get('DATABASE_URL'):
            pytest.skip('platform DB unreachable: DATABASE_URL unset')
        import platform_link
        platform_link.platform_db().apply_migration()
    tenant = 'cutover-' + uuid.uuid4().hex
    ledger.save(tenant, 'seed', GRAPH, None, 'operator')
    harness = Harness()
    monkeypatch.setattr(git, '_post', harness.post)
    monkeypatch.setenv('LEAF_LAB_GIT_TENANTS', tenant)
    monkeypatch.setenv('LEAF_LAB_GIT_SINGLE_WRITER', 'attested')
    git.enable_mirror(tenant, 'operator')
    yield tenant, harness
    if request.param == 'postgres':
        def cleanup(sql):
            # Generations and commit evidence are retained; release live work
            # so a later scheduler test cannot adopt this fake harness's rows.
            sql.execute('DELETE FROM lab_git_authority WHERE tenant_id LIKE ?', (tenant + '%',))
            sql.execute('DELETE FROM lab_git_transitions WHERE tenant_id LIKE ?', (tenant + '%',))
            sql.execute('DELETE FROM lab_git_pending WHERE tenant_id LIKE ?', (tenant + '%',))
            sql.execute('DELETE FROM lab_git_outbox WHERE tenant_id LIKE ?', (tenant + '%',))
        store._run(tenant, cleanup, write=True)


def transitions(tenant):
    return store._run(tenant, lambda sql: [dict(row) for row in sql.execute(
        'SELECT * FROM lab_git_transitions WHERE tenant_id=? ORDER BY created_at,transition_id', (tenant,)).fetchall()])


def expire_transition(tenant):
    store._run(tenant, lambda sql: sql.execute('UPDATE lab_git_transitions SET lease_expires_at=? WHERE tenant_id=?',
        ((store._now() - timedelta(seconds=1)).isoformat(), tenant)), write=True)


@pytest.mark.parametrize('guard', ['allowlist', 'attestation', 'ready', 'mode', 'pending'])
def test_cutover_preconditions(world, monkeypatch, guard):
    tenant, harness = world
    if guard == 'allowlist':
        monkeypatch.setenv('LEAF_LAB_GIT_TENANTS', '')
    elif guard == 'attestation':
        monkeypatch.setenv('LEAF_LAB_GIT_SINGLE_WRITER', 'yes')
    elif guard == 'ready':
        monkeypatch.setattr(store, 'GIT_AUTHORITY_READY', False)
    elif guard == 'mode':
        ledger._run(tenant, lambda sql: git._update(sql, tenant, mode='ledger'), write=True)
    else:
        store.stage(tenant, uuid.uuid4().hex, 'pending', 1, None, 'graph', '{}', 'a' * 64,
                    expected_parents=[harness.head])
    with pytest.raises(git.MirrorError):
        git.cutover(tenant, 'operator')
    assert store.authority_get(tenant) is None
    assert not any(endpoint == 'fence' for endpoint, _ in harness.calls)


@pytest.mark.parametrize('endpoint', ['generation', 'inspect', 'changes', 'fence'])
def test_cutover_requires_capability_on_every_response(world, monkeypatch, endpoint):
    tenant, harness = world
    original = harness.post
    def missing(t, route, body):
        result = original(t, route, body)
        if route == endpoint:
            result['_capability'] = None
        return result
    monkeypatch.setattr(git, '_post', missing)
    with pytest.raises(git.MirrorError, match='lab_harness_capability_missing'):
        git.cutover(tenant, 'operator')
    assert store.authority_get(tenant) is None
    assert not git.mirror_status(tenant)['frozen']


@pytest.mark.parametrize('divergence', ['digest', 'foreign', 'barrier-digest'])
def test_cutover_refuses_divergence_and_enqueues_once(world, divergence):
    tenant, harness = world
    if divergence == 'digest':
        harness.documents['lab/graphs/seed.json'] = '{}'
    elif divergence == 'foreign':
        harness.foreign('lab/graphs/seed.json', harness.documents['lab/graphs/seed.json'])
    else:
        harness.after_fence = lambda: harness.documents.update({'lab/graphs/seed.json': '{}'})
    with pytest.raises(git.MirrorError):
        git.cutover(tenant, 'operator')
    assert store.authority_get(tenant) is None
    assert transitions(tenant)[-1]['state'] == 'reverted'
    assert not git.mirror_status(tenant)['frozen']
    assert len(outbox(tenant)) == 1
    assert outbox(tenant)[0]['payload']['event'] == 'lab_replay_divergence'


def test_cutover_barrier_and_successful_rollback(world):
    tenant, harness = world
    active = git.cutover(tenant, 'operator')
    barrier = harness.head
    assert active['generation'] == int(harness.generation) == 1
    assert active['base_head'] == active['verified_head'] == barrier
    assert git.mirror_status(tenant)['frozen']
    assert transitions(tenant)[-1]['state'] == 'done'
    assert store.GIT_AUTHORITY_READY is True
    harness.calls.clear()
    git.catch_up(tenant)
    assert harness.calls == []
    save(tenant)
    assert git.flip_back(tenant, 'operator')['mode'] == 'ledger'
    assert store.authority_get(tenant) is None
    assert not git.mirror_status(tenant)['frozen']
    assert transitions(tenant)[-1]['state'] == 'done'
    assert harness.remote == harness.head and int(harness.generation) == 2
    assert all(row['done_at'] for row in outbox(tenant))


@pytest.mark.parametrize('crash', ['planned', 'receipt', 'fenced', 'proven', 'done'])
def test_cutover_crash_takeover(world, monkeypatch, crash):
    tenant, harness = world
    original = git._step
    def step(t, transition, target, operation=lambda sql, row: None):
        if crash == 'planned' and target == 'fenced':
            raise SystemExit('planned crash')
        result = original(t, transition, target, operation)
        if target == crash:
            raise SystemExit('state crash')
        return result
    if crash == 'receipt':
        def die():
            raise SystemExit('lost fence receipt')
        harness.after_fence = die
    monkeypatch.setattr(git, '_step', step)
    with pytest.raises(SystemExit):
        git.cutover(tenant, 'operator')
    original_transition = store.get_active(tenant)
    if crash == 'done':
        assert original_transition is None
        assert transitions(tenant)[-1]['state'] == 'done'
        assert store.authority_get(tenant)['base_head'] == harness.head
        return
    assert original_transition is not None
    monkeypatch.setattr(git, '_step', original)
    expire_transition(tenant)
    git.background_tick(0)
    assert store.get_active(tenant) is None
    terminal = transitions(tenant)[-1]
    assert terminal['owner'] != original_transition['owner']
    if crash == 'planned':
        assert terminal['state'] == 'reverted' and store.authority_get(tenant) is None
        assert not git.mirror_status(tenant)['frozen']
    else:
        assert terminal['state'] == 'done'
        assert store.authority_get(tenant)['base_head'] == harness.head
    if crash == 'receipt':
        fences = [body for endpoint, body in harness.calls if endpoint == 'fence']
        assert len(fences) == 2 and fences[0] == fences[1]
    assert not store.advance(tenant, original_transition['transition_id'], original_transition['owner'],
                             original_transition['state'], 'done')


@pytest.mark.parametrize('landed', [False, True])
def test_rollback_resolves_pending_against_barrier(world, landed):
    tenant, harness = world
    git.cutover(tenant, 'operator')
    if landed:
        harness.crash_after_commit = True
    else:
        def die():
            raise SystemExit('staged')
        harness.before_commit = die
    with pytest.raises(SystemExit):
        save(tenant)
    row = pending(tenant)[0]
    commits_before = len(harness.commits)
    git.flip_back(tenant, 'operator')
    assert pending(tenant) == [] and store.authority_get(tenant) is None
    assert len(harness.commits) == commits_before
    if landed:
        assert store.get(tenant, row['op_id']) is None
        assert ledger.get(tenant, 'x')['revision'] == 1
        assert store.commit_get(tenant, harness.head)['op_id'] == row['op_id']
    else:
        assert store.get(tenant, row['op_id'])['state'] == 'abandoned'
        with pytest.raises(ledger.NotFound):
            ledger.get(tenant, 'x')


@pytest.mark.parametrize('divergence', ['foreign', 'digest'])
def test_rollback_refuses_and_returns_to_git_with_another_fence(world, divergence):
    tenant, harness = world
    git.cutover(tenant, 'operator')
    save(tenant)
    if divergence == 'foreign':
        harness.foreign('lab/graphs/x.json', harness.documents['lab/graphs/x.json'])
    else:
        harness.documents['lab/graphs/x.json'] = '{}'
    with pytest.raises(git.MirrorError, match='^lab_flip_back_refused$') as caught:
        git.flip_back(tenant, 'operator')
    assert caught.value.observed == ('foreign_commits_need_import' if divergence == 'foreign' else 'lab_replay_divergence')
    active = store.authority_get(tenant)
    assert active['mode'] == 'git' and active['generation'] == int(harness.generation) == 3
    assert git.mirror_status(tenant)['frozen']
    assert transitions(tenant)[-1]['state'] == 'reverted'
    notices = [row for row in outbox(tenant) if row['payload'].get('event') == 'lab_flip_back_refused']
    assert len(notices) == 1 and notices[0]['payload']['reason'] == caught.value.observed


@pytest.mark.parametrize('crash', ['planned', 'receipt', 'fenced', 'resolved', 'drained', 'proven', 'compensation'])
def test_rollback_takeover(world, monkeypatch, crash):
    tenant, harness = world
    git.cutover(tenant, 'operator')
    save(tenant)
    original = git._step
    def step(t, transition, target, operation=lambda sql, row: None):
        if crash == 'planned' and target == 'fenced':
            raise SystemExit('planned crash')
        result = original(t, transition, target, operation)
        if target == crash:
            raise SystemExit('state crash')
        return result
    monkeypatch.setattr(git, '_step', step)
    if crash in {'receipt', 'compensation'}:
        def die():
            raise SystemExit('lost receipt')
        if crash == 'compensation':
            harness.foreign('lab/graphs/x.json', harness.documents['lab/graphs/x.json'])
            original_post = harness.post
            count = [0]
            def post(t, endpoint, body):
                if endpoint == 'fence':
                    count[0] += 1
                    if count[0] == 2:
                        harness.after_fence = die
                return original_post(t, endpoint, body)
            monkeypatch.setattr(git, '_post', post)
        else:
            harness.after_fence = die
    with pytest.raises(SystemExit):
        git.flip_back(tenant, 'operator')
    monkeypatch.setattr(git, '_step', original)
    expire_transition(tenant)
    git.background_tick(0)
    assert store.get_active(tenant) is None
    if crash in {'planned', 'compensation'}:
        assert store.authority_get(tenant)['mode'] == 'git'
        assert int(harness.generation) == (1 if crash == 'planned' else 3)
        assert transitions(tenant)[-1]['state'] == 'reverted'
    else:
        assert store.authority_get(tenant) is None
        assert transitions(tenant)[-1]['state'] == 'done'


def test_untrusted_ops_never_abandons_a_landed_write(world, monkeypatch):
    tenant, harness = world
    git.cutover(tenant, 'operator')
    harness.crash_after_commit = True
    with pytest.raises(SystemExit):
        save(tenant)
    row = pending(tenant)[0]
    original = harness.post
    def post(t, endpoint, body):
        result = original(t, endpoint, body)
        if endpoint == 'ops':
            result['ops'][row['op_id']]['sha256'] = '0' * 64
        return result
    monkeypatch.setattr(git, '_post', post)
    with pytest.raises(git.MirrorError, match='lab_ops_ambiguous'):
        git.flip_back(tenant, 'operator')
    assert pending(tenant)[0]['op_id'] == row['op_id']
    assert store.authority_get(tenant)['mode'] == 'draining'
    monkeypatch.setattr(git, '_post', original)
    expire_transition(tenant)
    git.background_tick(0)
    assert store.get(tenant, row['op_id']) is None
    assert ledger.get(tenant, 'x')['revision'] == 1


def test_background_isolates_each_operation_and_tenant(world, monkeypatch, caplog):
    tenant, _ = world
    git.cutover(tenant, 'operator')
    other = tenant + '-other'
    calls = []
    monkeypatch.setattr(store, 'background_tenants', lambda limit: [tenant, other])
    def reconcile(t):
        calls.append(('reconcile', t))
        if t == tenant:
            raise ValueError('private-reconcile')
    monkeypatch.setattr(store, 'get_active', reconcile)
    def recover(t):
        calls.append(('recover', t))
        if t == tenant:
            raise RuntimeError('private-recover')
    monkeypatch.setattr(git, 'recover_pending', recover)
    monkeypatch.setattr(git, 'process_outbox', lambda t: calls.append(('outbox', t)))
    git.background_tick(0)
    assert calls == [(name, t) for t in (tenant, other) for name in ('reconcile', 'recover', 'outbox')]
    assert 'ValueError' in caplog.text and 'RuntimeError' in caplog.text
    assert 'private-' not in caplog.text


def test_background_tenants_are_bounded_and_oldest_first(world):
    tenant, harness = world
    git.cutover(tenant, 'operator')
    others = [tenant + '-' + str(i) for i in range(21)]
    for index, other in enumerate(others):
        def insert(sql, other=other, index=index):
            store._insert(sql, 'lab_git_authority', dict(tenant_id=other, mode='git', generation=1,
                base_head=BASE, verified_head=BASE, updated_at='2000-01-01T00:00:%02d+00:00' % index))
        store._run(other, insert, write=True)
    batch = store.background_tenants()
    # PostgreSQL twins can share a database with other suites. These inserted
    # timestamps still precede live test work, and the query stays bounded.
    assert len(batch) == 20
    assert batch == others[:20]


def test_exit_requires_authorization_and_is_tenant_bound(world):
    tenant, _ = world
    git.cutover(tenant, 'operator')
    with pytest.raises(Exception, match='lab_git_exit_refused'):
        store._run(tenant, lambda sql: git._update(sql, tenant, frozen=False, mode='ledger'), write=True)
    other = tenant + '-other'
    assert store.authority_get(other) is None
    with pytest.raises(git.MirrorError):
        git.cutover(other, 'operator')
    assert store.authority_get(tenant)['mode'] == 'git'


def test_rollback_uses_exit_token_in_the_deletion_transaction(world, monkeypatch):
    tenant, _ = world
    git.cutover(tenant, 'operator')
    original = store.authorize_exit
    calls = []
    def authorize(sql, t, tid):
        calls.append((t, tid, store.authority_get_sql(sql, t)['mode']))
        return original(sql, t, tid)
    monkeypatch.setattr(store, 'authorize_exit', authorize)
    git.flip_back(tenant, 'operator')
    assert calls == [(tenant, transitions(tenant)[-1]['transition_id'], 'draining')]


@pytest.mark.parametrize('commit_count', [2, 3])
def test_rollback_bounds_verified_walk_and_continues_on_takeover(world, monkeypatch, commit_count):
    tenant, harness = world
    base = git.cutover(tenant, 'operator')['verified_head']
    commits = []
    for index in range(commit_count):
        save(tenant, 'bounded-' + str(index))
        commits.append(harness.head)
    monkeypatch.setattr(git, '_ROLLBACK_ADVANCE_BATCHES', 1)
    original_step = git._step
    original_advance = git.advance_verified_head
    original_authorize = store.authorize_exit
    lagged, batches, exits = [], [], []
    def step(t, transition, target, operation=lambda sql, row: None):
        result = original_step(t, transition, target, operation)
        if target == 'drained' and not lagged:
            # Simulate a recorded backlog at the drained boundary, after the
            # outbox's independent cursor batch has already run.
            store._run(t, lambda sql: sql.execute(
                'UPDATE lab_git_authority SET verified_head=? WHERE tenant_id=?', (base, t)), write=True)
            lagged.append(True)
        return result
    def advance(sql, t):
        active = store.authority_get_sql(sql, t)
        result = original_advance(sql, t, limit=2)
        if lagged and active is not None and active['mode'] == 'draining':
            batches.append(result)
        return result
    def authorize(sql, t, tid):
        exits.append((t, tid, store.authority_get_sql(sql, t)['mode']))
        return original_authorize(sql, t, tid)
    monkeypatch.setattr(git, '_step', step)
    monkeypatch.setattr(git, 'advance_verified_head', advance)
    monkeypatch.setattr(store, 'authorize_exit', authorize)
    if commit_count > 2:
        with pytest.raises(git.MirrorError, match='^lab_rollback_continuing$'):
            git.flip_back(tenant, 'operator')
        transition = store.get_active(tenant)
        assert transition['state'] == 'drained'
        assert transition['barrier_head'] == commits[-1]
        assert store._date(transition['lease_expires_at']) > store._now()
        active = store.authority_get(tenant)
        assert active['mode'] == 'draining' and active['verified_head'] == commits[1]
        assert git.mirror_status(tenant)['frozen']
        assert batches == [False] and exits == []
        expire_transition(tenant)
        git.background_tick(0)
        assert transitions(tenant)[-1]['owner'] != transition['owner']
        assert batches == [False, True]
    else:
        assert git.flip_back(tenant, 'operator')['mode'] == 'ledger'
        assert batches == [True]
    assert store.authority_get(tenant) is None
    assert store.get_active(tenant) is None
    terminal = transitions(tenant)[-1]
    assert terminal['state'] == 'done'
    assert exits == [(tenant, terminal['transition_id'], 'draining')]
    assert not git.mirror_status(tenant)['frozen']


def test_rollback_completes_landed_pending_in_parent_order(world):
    tenant, harness = world
    git.cutover(tenant, 'operator')
    # Staging order differs from commit order. Both land before completion;
    # the later commit retains its charged rebase parent in durable staging.
    seed = ledger.get(tenant, 'seed')
    staged = {doc: store.stage(tenant, uuid.uuid4().hex, doc, 1, None, 'graph',
        seed['canonical_text'], seed['sha256'], expected_parents=[harness.head]) for doc in ('second', 'first')}
    for doc in ('first', 'second'):
        row = staged[doc]
        if row['expected_parents'][-1] != harness.head:
            assert store.charge_rebase(tenant, row['op_id'], row['fence'], harness.head)
            row = store.get(tenant, row['op_id'])
        harness.foreign(git._path(row), row['canonical_text'], git._pending_message(row, 'git-recovery'))
    git.flip_back(tenant, 'operator')
    assert ledger.get(tenant, 'first')['revision'] == ledger.get(tenant, 'second')['revision'] == 1
    assert pending(tenant) == [] and store.authority_get(tenant) is None


@pytest.mark.parametrize('verified', [False, True])
def test_rollback_capped_walk_requires_verified_equality(world, monkeypatch, verified):
    tenant, harness = world
    git.cutover(tenant, 'operator')
    save(tenant)
    if not verified:
        # A missing record prevents the cursor from bridging this foreign head.
        harness.foreign('lab/graphs/x.json', harness.documents['lab/graphs/x.json'])
    def capped(*args):
        raise git.MirrorError('lab_history_too_long')
    monkeypatch.setattr(git, '_changes', capped)
    if verified:
        git.flip_back(tenant, 'operator')
        assert store.authority_get(tenant) is None
    else:
        with pytest.raises(git.MirrorError, match='lab_flip_back_refused'):
            git.flip_back(tenant, 'operator')
        assert store.authority_get(tenant)['mode'] == 'git'


def test_rollback_exit_and_unfreeze_are_atomic(world, monkeypatch):
    tenant, _ = world
    git.cutover(tenant, 'operator')
    original = ledger._SQL.execute
    def crash(sql, query, params=()):
        if query.startswith('DELETE FROM lab_git_authority') and params == (tenant,):
            raise SystemExit('exit crash')
        return original(sql, query, params)
    monkeypatch.setattr(ledger._SQL, 'execute', crash)
    with pytest.raises(SystemExit):
        git.flip_back(tenant, 'operator')
    assert store.authority_get(tenant)['mode'] == 'draining'
    assert git.mirror_status(tenant)['frozen']
    assert transitions(tenant)[-1]['state'] == 'proven'
    monkeypatch.setattr(ledger._SQL, 'execute', original)
    expire_transition(tenant)
    git.background_tick(0)
    assert store.authority_get(tenant) is None
    assert not git.mirror_status(tenant)['frozen']


def test_background_recovers_pending_and_publishes(world):
    tenant, harness = world
    git.cutover(tenant, 'operator')
    harness.unknown_after_commit = True
    with pytest.raises(ledger.GitUnavailable):
        save(tenant)
    assert pending(tenant) and harness.remote != harness.head
    store._run(tenant, lambda sql: sql.execute('UPDATE lab_git_pending SET lease_expires_at=? WHERE tenant_id=?',
        ((store._now() - timedelta(seconds=1)).isoformat(), tenant)), write=True)
    git.background_tick(0)
    assert pending(tenant) == []
    assert ledger.get(tenant, 'x')['revision'] == 1
    assert harness.remote == harness.head
    assert all(row['done_at'] for row in outbox(tenant))


def test_empty_mirror_cutover_and_rollback_without_null_history_requests(world, monkeypatch):
    tenant, _ = world
    tenant += '-empty'
    harness = Harness()
    harness.head = None
    harness.commits = []
    monkeypatch.setattr(git, '_post', harness.post)
    monkeypatch.setenv('LEAF_LAB_GIT_TENANTS', tenant)
    git.enable_mirror(tenant, 'operator')
    active = git.cutover(tenant, 'operator')
    assert active['generation'] == 1 and active['base_head'] is active['verified_head'] is None
    git.flip_back(tenant, 'operator')
    assert store.authority_get(tenant) is None
    assert not git.mirror_status(tenant)['frozen']
    assert not any(endpoint in {'changes', 'ops'} for endpoint, _ in harness.calls)
