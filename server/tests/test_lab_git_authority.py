"""Git authority invariants, mirrored on SQLite and PostgreSQL."""
from datetime import timedelta
import os
import uuid

import pytest

import lab_artifacts as ledger
import lab_artifacts_git as mirror
import lab_git_authority as store


@pytest.fixture(params=['sqlite', 'postgres'])
def tenant(request, monkeypatch, tmp_path):
    monkeypatch.setenv('LEAF_RUNTIME_ENV', 'test')
    monkeypatch.setenv('LEAF_PLATFORM_POSTGRES_REQUIRED', '0')
    monkeypatch.setenv('LEAF_LAB_STORE', request.param)
    monkeypatch.setenv('LEAF_LAB_DB', str(tmp_path / 'authority.db'))
    if request.param == 'postgres':
        if not os.environ.get('DATABASE_URL'):
            pytest.skip('platform DB unreachable: DATABASE_URL unset')
        import platform_link
        platform_link.platform_db().apply_migration()
    return 'authority-' + uuid.uuid4().hex


def stage(tenant, doc='doc', **fields):
    return store.stage(tenant, uuid.uuid4().hex, doc, 1, None, 'graph', '{}', 'a' * 64, **fields)


def transition(tenant, **fields):
    return store.begin(tenant, uuid.uuid4().hex, 'rollback', 1, 2, 'owner', **fields)


def authority(tenant):
    generation = store.next_generation(tenant)
    mirror.mirror_status(tenant)
    ledger._run(tenant, lambda sql: mirror._update(sql, tenant, mode='mirror', frozen=True), write=True)
    return store.authority_insert(tenant, generation)


def test_generations_survive_authority_deletion(tenant):
    assert authority(tenant)['generation'] == 1
    assert store.authority_set_mode(tenant, 'draining')
    assert store.authority_get(tenant)['mode'] == 'draining'
    with pytest.raises(ValueError, match='lab_git_exit_refused'):
        store.authority_delete(tenant, uuid.uuid4().hex)
    active = transition(tenant)
    assert store.authority_delete(tenant, active['transition_id'])
    assert store.authority_get(tenant) is None
    assert store.next_generation(tenant) == 2
    assert store.next_generation(tenant) == 3
    with pytest.raises(Exception, match='lab_git_generations_retained'):
        store._run(tenant, lambda sql: sql.execute('DELETE FROM lab_git_generations WHERE tenant_id=?', (tenant,)), write=True)


def test_one_live_pending_and_abandonment(tenant):
    row = stage(tenant)
    with pytest.raises(Exception):
        stage(tenant)
    assert not store.abandon(tenant, row['op_id'], row['fence'] + 1)
    assert store.abandon(tenant, row['op_id'], row['fence'])
    assert stage(tenant)['state'] == 'staged'
    assert store.get(tenant, row['op_id'])['state'] == 'abandoned'


def test_takeover_and_conditional_completion(tenant):
    row = stage(tenant)
    op = row['op_id']
    assert store.get(tenant + '-other', op) is None
    assert not store.complete_conditional(tenant + '-other', op, row['fence'])
    assert store.take_over_expired(tenant, op, 'next', 10) is None
    past = (store._now() - timedelta(seconds=1)).isoformat()
    store._run(tenant, lambda sql: sql.execute('UPDATE lab_git_pending SET lease_expires_at=? WHERE tenant_id=? AND op_id=?',
                                            (past, tenant, op)), write=True)
    taken = store.take_over_expired(tenant, op, 'next', 10)
    assert taken['fence'] == row['fence'] + 1
    assert taken['lease_owner'] == 'next'
    assert not store.complete_conditional(tenant, op, row['fence'])
    assert not store.mark_ambiguous(tenant, op, row['fence'])
    assert store.mark_ambiguous(tenant, op, taken['fence'])
    assert store.complete_conditional(tenant, op, taken['fence'])
    assert store.get(tenant, op) is None


def test_rebase_budget_and_parent_validation(tenant):
    row = stage(tenant, expected_parents=['a' * 40])
    assert not store.charge_rebase(tenant, row['op_id'], 999, 'b' * 40)
    for parent in ('b', 'c', 'd'):
        assert store.charge_rebase(tenant, row['op_id'], row['fence'], parent * 40)
    assert not store.charge_rebase(tenant, row['op_id'], row['fence'], 'e' * 40)
    assert store.get(tenant, row['op_id'])['rebase_count'] == 3
    for parents in (['bad'], ['a' * 40] * 5, [None]):
        with pytest.raises(ValueError, match='lab_git_invalid_parents'):
            stage(tenant, 'bad', expected_parents=parents)
    with pytest.raises(Exception):
        store._run(tenant, lambda sql: sql.execute('UPDATE lab_git_pending SET expected_parents=' +
            ('CAST(? AS jsonb)' if sql.postgres else '?') + ' WHERE tenant_id=?', ('["bad"]', tenant)), write=True)


def test_reservations_only_live_rows_and_tenant(tenant):
    stage(tenant, 'one', reserved_bytes=10, new_document=True)
    stage(tenant, 'two', reserved_bytes=20)
    abandoned = stage(tenant, 'three', reserved_bytes=100, new_document=True)
    store.abandon(tenant, abandoned['op_id'], abandoned['fence'])
    stage(tenant + '-other', reserved_bytes=1000, new_document=True)
    assert store.reservations(tenant) == {'bytes': 30, 'revisions': 2, 'new_documents': 1}


def test_commits_append_only(tenant):
    commit = 'a' * 40
    store.record(tenant, commit, uuid.uuid4().hex, 'doc', 1, 'graph')
    assert store.commit_get(tenant, commit)['revision'] == 1
    assert store.commit_get(tenant + '-other', commit) is None
    for mutation in ('UPDATE lab_git_commits SET revision=2', 'DELETE FROM lab_git_commits'):
        with pytest.raises(Exception, match='lab_git_commits_append_only'):
            store._run(tenant, lambda sql: sql.execute(mutation + ' WHERE tenant_id=?', (tenant,)), write=True)


def test_outbox_identity_sequence_claim_and_retry(tenant):
    first = store.enqueue(tenant, 'publish', 'one', {'head': 'a' * 40})
    assert store.enqueue(tenant, 'publish', 'one', {}) == first
    second = store.enqueue(tenant, 'notify', 'two', {})
    assert second['sequence'] == first['sequence'] + 1
    claimed = store.claim_due(tenant, 1)
    assert [r['outbox_id'] for r in claimed] == [first['outbox_id']]
    assert claimed[0]['attempts'] == 1
    assert store.defer(tenant, first['outbox_id'], 60)
    assert [r['outbox_id'] for r in store.claim_due(tenant)] == [second['outbox_id']]
    assert not store.mark_done(tenant + '-other', second['outbox_id'])
    assert store.mark_done(tenant, second['outbox_id'])
    assert not store.defer(tenant, second['outbox_id'], 60)
    assert store.claim_due(tenant) == []


def test_transition_uniqueness_cas_and_takeover(tenant):
    row = transition(tenant)
    tid = row['transition_id']
    assert store.get_active(tenant + '-other') is None
    assert not store.advance(tenant + '-other', tid, 'owner', 'planned', 'fenced')
    with pytest.raises(Exception):
        transition(tenant)
    assert not store.advance(tenant, tid, 'other', 'planned', 'fenced')
    assert not store.advance(tenant, tid, 'owner', 'fenced', 'resolved')
    assert store.advance(tenant, tid, 'owner', 'planned', 'fenced', barrier_head='a' * 40)
    assert not store.transition_take_over_expired(tenant, tid, 'new', 30)
    past = (store._now() - timedelta(seconds=1)).isoformat()
    assert store.advance(tenant, tid, 'owner', 'fenced', 'resolved', lease_expires_at=past)
    assert store.transition_take_over_expired(tenant, tid, 'new', 30)
    assert not store.advance(tenant, tid, 'owner', 'resolved', 'done')
    assert store.advance(tenant, tid, 'new', 'resolved', 'done')
    assert store.get_active(tenant) is None
    assert transition(tenant)['state'] == 'planned'


def test_s1_refuses_unfreeze_mode_change_delete_and_tenant_move(tenant):
    authority(tenant)
    for mutation in ('UPDATE lab_git_tenants SET frozen=false', "UPDATE lab_git_tenants SET mode='ledger'",
                     'DELETE FROM lab_git_tenants', "UPDATE lab_git_tenants SET tenant_id='moved'"):
        with pytest.raises(Exception, match='lab_git_exit_refused'):
            ledger._run(tenant, lambda sql: sql.execute(mutation + ' WHERE tenant_id=?', (tenant,)), write=True)
    ledger._run(tenant, lambda sql: mirror._update(sql, tenant, local_sha='a' * 40), write=True)
    assert mirror.mirror_status(tenant)['frozen']


def test_s1_authorization_scoped_and_transaction_local(tenant):
    authority(tenant)
    tid = transition(tenant)['transition_id']
    other = tenant + '-other'
    authority(other)
    other_tid = transition(other)['transition_id']
    def wrong(sql):
        store.authorize_exit(sql, other, other_tid)
        mirror._update(sql, tenant, frozen=False)
    with pytest.raises(Exception, match='lab_git_exit_refused'):
        store._run(tenant, wrong, write=True)
    def allowed(sql):
        store.authorize_exit(sql, tenant, tid)
        mirror._update(sql, tenant, frozen=False, mode='ledger')
    store._run(tenant, allowed, write=True)
    assert not mirror.mirror_status(tenant)['frozen']
    # Token was removed at commit; another transaction cannot reuse it.
    with pytest.raises(Exception, match='lab_git_exit_refused'):
        ledger._run(tenant, lambda sql: sql.execute('DELETE FROM lab_git_tenants WHERE tenant_id=?', (tenant,)), write=True)
    for mutation in ("UPDATE lab_git_tenants SET mode='ledger'", 'DELETE FROM lab_git_tenants'):
        def allowed_again(sql):
            store.authorize_exit(sql, tenant, tid)
            sql.execute(mutation + ' WHERE tenant_id=?', (tenant,))
        store._run(tenant, allowed_again, write=True)
    assert store.advance(tenant, tid, 'owner', 'planned', 'done')
    with pytest.raises(ValueError, match='lab_git_exit_refused'):
        store.authority_delete(tenant, tid)


def test_tenants_without_authority_unaffected_and_store_disabled(tenant):
    store.next_generation(tenant)
    mirror.mirror_status(tenant)
    ledger._run(tenant, lambda sql: mirror._update(sql, tenant, frozen=False, mode='ledger'), write=True)
    ledger._run(tenant, lambda sql: sql.execute('DELETE FROM lab_git_tenants WHERE tenant_id=?', (tenant,)), write=True)
    assert store.GIT_AUTHORITY_READY is False


def test_quarantine_preserves_evidence_and_tenant_boundary(tenant):
    op = uuid.uuid4().hex
    store.quarantine_add(tenant, op, 'doc', 1, '{}', 'a' * 64, 'unknown_parent')
    row = store._run(tenant, lambda sql: store._get(sql, 'lab_git_quarantine', tenant, 'op_id', op))
    assert row['canonical_text'] == '{}'
    assert row['reason'] == 'unknown_parent'
    assert store._run(tenant + '-other', lambda sql: store._get(sql, 'lab_git_quarantine', tenant + '-other', 'op_id', op)) is None
