"""Canonical project-graph chart-state persistence, using the real test database."""
import uuid
from concurrent.futures import ThreadPoolExecutor
import pytest
from leaf_platform import browser_chart_state as chart, project_lifecycle as lifecycle, store

STATE = dict(version=1,range='all',scale='Log',reference='2023-01-01',averaging=56,timeAxis='expanded2026',series=['a','b'],groups=['g'])
def descriptor():
    return dict(source_request_id=str(uuid.uuid4()),job_id=str(uuid.uuid4()),artifact_id=str(uuid.uuid4()),version=1,
                receipt_digest='a'*64,source_commit='b'*40,source_tree='c'*40,manifest_sha256='d'*64)

@pytest.fixture
def project(make_org):
    org=make_org();actor=store.create_identity_binding(org.org_id,'auth0',str(uuid.uuid4()),role='owner')
    made=lifecycle.create_blank_project(org.org_id,actor.binding_id,name='Chart',idempotency_key=str(uuid.uuid4()))
    project=uuid.UUID(made['project']['project_id'])
    store.set_project_authority_mode(org.org_id,project,'postgres_canonical')
    return org.org_id,project,actor.binding_id

def test_save_retry_reload_restore_and_graph_revision(project):
    d=descriptor();save_id=str(uuid.uuid4());assert chart.read(*project,d) is None
    first=chart.save(*project,d,STATE,expected_revision=0,save_id=save_id)
    assert first['revision']==1 and chart.save(*project,d,STATE,expected_revision=0,save_id=save_id)==first
    changed=chart.save(*project,d,dict(STATE,scale='Linear'),expected_revision=1,save_id=str(uuid.uuid4()))
    assert changed['revision']==2 and chart.read(*project,d)==changed
    assert chart.save(*project,d,STATE,expected_revision=0,save_id=save_id)==first
    assert chart.read(*project,d)==changed
    restored=chart.save(*project,d,STATE,expected_revision=2,save_id=str(uuid.uuid4()))
    assert restored['revision']==3 and restored['state']==STATE

def test_concurrent_stale_saves_have_one_winner(project):
    d=descriptor()
    def save(scale):
        try:return chart.save(*project,d,dict(STATE,scale=scale),expected_revision=0,save_id=str(uuid.uuid4()))
        except lifecycle.LifecycleConflict:return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(save,['Log','Linear']))
    assert results.count('conflict')==1 and chart.read(*project,d)['revision']==1

def test_identity_and_idempotency_cannot_relabel_saved_state(project):
    d=descriptor();save_id=str(uuid.uuid4());chart.save(*project,d,STATE,expected_revision=0,save_id=save_id)
    with pytest.raises(lifecycle.LifecycleConflict):chart.read(*project,dict(d,source_tree='f'*40))
    with pytest.raises(lifecycle.LifecycleConflict):chart.save(*project,d,dict(STATE,scale='Linear'),expected_revision=0,save_id=save_id)

def test_foreign_actor_cannot_read_or_write(project,make_org):
    org=make_org();actor=store.create_identity_binding(org.org_id,'auth0',str(uuid.uuid4()),role='owner');d=descriptor()
    with pytest.raises(lifecycle.LifecycleForbidden):chart.read(project[0],project[1],actor.binding_id,d)
    with pytest.raises(lifecycle.LifecycleForbidden):chart.save(project[0],project[1],actor.binding_id,d,STATE,expected_revision=0,save_id=str(uuid.uuid4()))

@pytest.mark.parametrize('state',[dict(STATE,version=True),dict(STATE,series=['a','a']),dict(STATE,groups=['x'*201]),dict(STATE,extra=True)])
def test_invalid_state_is_refused(state):
    with pytest.raises(ValueError):chart.state_copy(state)
