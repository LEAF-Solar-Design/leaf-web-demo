"""PostgreSQL proofs for campaign admission and single-use decisions."""
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier

import psycopg
import pytest

from leaf_platform import campaigns, db, store


def _seed(make_org):
    org = make_org()
    project = store.create_project(org.org_id, 'Campaign project')
    binding = store.create_identity_binding(
        org.org_id, 'auth0', f'auth0|campaign-{uuid.uuid4()}', role='owner')
    with campaigns.connection() as conn, conn.cursor() as cur:
        cur.execute(
            'INSERT INTO project_member_bindings '
            '(membership_id, org_id, project_id, binding_id, role, invited_by_binding_id) '
            "VALUES (%s, %s, %s, %s, 'owner', %s)",
            (uuid.uuid4(), org.org_id, project.project_id, binding.binding_id, binding.binding_id))
    campaign = campaigns.submit_campaign(
        org.org_id, project.project_id, str(org.org_id), binding.binding_id,
        title='ReciPDF', prompt='Organize the recipe collection', idempotency_key='submit-1')
    question = campaigns.ask_question(
        org.org_id, project.project_id, campaign['campaign_id'], question_key='organization',
        prompt='How should recipes be organized?', options=['tags', 'collections'],
        asked_by='operator', blocks_dispatch=True)
    return org, project, binding, campaign, question


def test_store_has_no_ddl_or_job_submission():
    source = (Path(__file__).resolve().parents[1] / 'campaigns.py').read_text(encoding='utf-8')
    assert not re.search(r'\b(?:CREATE|ALTER|DROP)\s+(?:TABLE|INDEX|TRIGGER|FUNCTION)', source, re.I)
    assert not re.search(r'\bINSERT\s+INTO\s+jobs\b', source, re.I)


def test_submission_replay_conflict_and_dispatch(make_org):
    org, project, binding, first, _ = _seed(make_org)
    args = (org.org_id, project.project_id, str(org.org_id), binding.binding_id)
    replay = campaigns.submit_campaign(*args, title='ReciPDF',
        prompt='Organize the recipe collection', idempotency_key='submit-1')
    assert replay['campaign_id'] == first['campaign_id']
    assert replay['replayed'] is True
    with pytest.raises(campaigns.CampaignConflict) as exc:
        campaigns.submit_campaign(*args, title='Different', prompt='Other', idempotency_key='submit-1')
    assert exc.value.code == 'idempotency_conflict'
    rows = campaigns.list_campaigns(org.org_id, project.project_id, limit=0)
    assert len(rows) == 1
    assert rows[0]['status'] == 'accepted' and rows[0]['dispatch_ref'] is None
    assert rows[0]['dispatch'] == {'available': False, 'action': 'mount-fleet-adapter'}
    assert campaigns.get_campaign(org.org_id, project.project_id, first['campaign_id'])['campaign_id'] == first['campaign_id']


def test_cross_project_and_unbound_principal_refused_without_answer(make_org):
    org, project, binding, campaign, question = _seed(make_org)
    other = store.create_project(org.org_id, 'Other project')
    outsider = store.create_identity_binding(
        org.org_id, 'auth0', f'auth0|outsider-{uuid.uuid4()}', role='owner')
    with campaigns.connection() as conn, conn.cursor() as cur:
        cur.execute(
            'INSERT INTO project_member_bindings '
            '(membership_id, org_id, project_id, binding_id, role, invited_by_binding_id) '
            "VALUES (%s, %s, %s, %s, 'owner', %s)",
            (uuid.uuid4(), org.org_id, other.project_id, outsider.binding_id, outsider.binding_id))
    assert campaigns.get_campaign(org.org_id, other.project_id, campaign['campaign_id']) is None
    for target, principal in [(other.project_id, outsider.binding_id),
                              (project.project_id, outsider.binding_id)]:
        with pytest.raises(campaigns.CampaignUnavailable) as exc:
            campaigns.answer_question(org.org_id, target, campaign['campaign_id'],
                                      question['question_id'], principal, answer='use tags')
        assert exc.value.code == 'project_unavailable'
    with pytest.raises(campaigns.CampaignUnavailable):
        campaigns.ask_question(org.org_id, other.project_id, campaign['campaign_id'],
                               question_key='foreign', prompt='Not visible')
    with campaigns.connection() as conn, conn.cursor() as cur:
        cur.execute('SELECT count(*) AS n FROM campaign_answers WHERE question_id=%s',
                    (uuid.UUID(question['question_id']),))
        assert cur.fetchone()['n'] == 0
    assert campaigns.list_questions(org.org_id, project.project_id, campaign['campaign_id'])[0]['status'] == 'open'


def test_duplicate_answer_is_single_use_and_atomic(make_org):
    org, project, binding, campaign, question = _seed(make_org)
    args = (org.org_id, project.project_id, campaign['campaign_id'], question['question_id'], binding.binding_id)
    first = campaigns.answer_question(*args, answer='use tags')
    replay = campaigns.answer_question(*args, answer='use tags')
    assert first['replayed'] is False
    assert replay['replayed'] is True and replay['answer_id'] == first['answer_id']
    with pytest.raises(campaigns.CampaignConflict) as exc:
        campaigns.answer_question(*args, answer='use collections')
    assert exc.value.code == 'answer_conflict'
    with campaigns.connection() as conn, conn.cursor() as cur:
        cur.execute('SELECT count(*) AS n FROM campaign_answers WHERE question_id=%s',
                    (uuid.UUID(question['question_id']),))
        assert cur.fetchone()['n'] == 1
    assert campaigns.list_questions(org.org_id, project.project_id, campaign['campaign_id'])[0]['status'] == 'answered'


def test_answer_ledger_rejects_update_and_delete(make_org):
    org, project, binding, campaign, question = _seed(make_org)
    answer = campaigns.answer_question(org.org_id, project.project_id, campaign['campaign_id'],
                                      question['question_id'], binding.binding_id, answer='use tags')
    for sql in ["UPDATE campaign_answers SET answer='tampered' WHERE answer_id=%s",
                'DELETE FROM campaign_answers WHERE answer_id=%s']:
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            with campaigns.connection() as conn, conn.cursor() as cur:
                cur.execute(sql, (uuid.UUID(answer['answer_id']),))
    replay = campaigns.answer_question(org.org_id, project.project_id, campaign['campaign_id'],
                                      question['question_id'], binding.binding_id, answer='use tags')
    assert replay['answer_id'] == answer['answer_id']


def test_list_questions_recovers_answer_after_pool_reset(make_org):
    org, project, binding, campaign, question = _seed(make_org)
    other = store.create_project(org.org_id, 'Other project')
    open_question = campaigns.list_questions(org.org_id, project.project_id, campaign['campaign_id'])[0]
    assert open_question['answer'] is None
    assert open_question['answer_id'] is None
    assert open_question['answered_at'] is None
    answer = campaigns.answer_question(org.org_id, project.project_id, campaign['campaign_id'],
                                      question['question_id'], binding.binding_id, answer='use tags')
    db.reset_pool()
    refreshed = campaigns.list_questions(org.org_id, project.project_id, campaign['campaign_id'])
    assert len(refreshed) == 1
    assert refreshed[0]['question_id'] == question['question_id']
    assert refreshed[0]['status'] == 'answered'
    assert refreshed[0]['answer'] == 'use tags'
    assert refreshed[0]['answer_id'] == answer['answer_id']
    assert refreshed[0]['answered_at'] == answer['created_at']
    with pytest.raises(campaigns.CampaignUnavailable) as exc:
        campaigns.list_questions(org.org_id, other.project_id, campaign['campaign_id'])
    assert exc.value.code == 'project_unavailable'


def test_concurrent_distinct_answers_have_one_durable_winner(make_org, monkeypatch):
    org, project, binding, campaign, question = _seed(make_org)
    args = (org.org_id, project.project_id, campaign['campaign_id'], question['question_id'], binding.binding_id)
    original_connection = campaigns.connection
    ready = Barrier(2)
    backend_pids = []

    @contextmanager
    def concurrent_connection():
        with original_connection() as conn:
            conn.execute("SET LOCAL lock_timeout = '10s'")
            conn.execute("SET LOCAL statement_timeout = '15s'")
            backend_pids.append(conn.info.backend_pid)
            ready.wait(timeout=10)
            yield conn

    def answer_once(text):
        try:
            return campaigns.answer_question(*args, answer=text)
        except campaigns.CampaignConflict as exc:
            return exc.code

    with monkeypatch.context() as patch:
        patch.setattr(campaigns, 'connection', concurrent_connection)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(answer_once, text) for text in ('use tags', 'use collections')]
            results = [future.result(timeout=30) for future in futures]
    assert len(set(backend_pids)) == 2
    winners = [result for result in results if isinstance(result, dict)]
    assert len(winners) == 1
    assert results.count('answer_conflict') == 1
    winner = winners[0]
    assert winner['replayed'] is False
    with campaigns.connection() as conn, conn.cursor() as cur:
        cur.execute('SELECT answer_id, answer FROM campaign_answers WHERE org_id=%s '
                    'AND project_id=%s AND campaign_id=%s AND question_id=%s',
                    (org.org_id, project.project_id, uuid.UUID(campaign['campaign_id']),
                     uuid.UUID(question['question_id'])))
        rows = cur.fetchall()
    assert len(rows) == 1
    assert str(rows[0]['answer_id']) == winner['answer_id']
    assert rows[0]['answer'] == winner['answer']
    db.reset_pool()
    refreshed = campaigns.list_questions(org.org_id, project.project_id, campaign['campaign_id'])
    assert len(refreshed) == 1
    assert refreshed[0]['status'] == 'answered'
    assert refreshed[0]['answer'] == winner['answer']
    assert refreshed[0]['answer_id'] == winner['answer_id']


def test_question_replay_and_conflict(make_org):
    org, project, _, campaign, first = _seed(make_org)
    replay = campaigns.ask_question(org.org_id, project.project_id, campaign['campaign_id'],
        question_key='organization', prompt='How should recipes be organized?')
    assert replay['question_id'] == first['question_id'] and replay['replayed'] is True
    with pytest.raises(campaigns.CampaignConflict) as exc:
        campaigns.ask_question(org.org_id, project.project_id, campaign['campaign_id'],
                               question_key='organization', prompt='Different prompt')
    assert exc.value.code == 'question_conflict'


def test_migration_is_idempotent_on_existing_campaigns(make_org):
    org, project, _, campaign, _ = _seed(make_org)
    sql = (Path(__file__).resolve().parents[1] / 'migrations' / '0055_campaigns.sql').read_text(encoding='utf-8')
    with campaigns.connection() as conn, conn.cursor() as cur:
        cur.execute(sql)
        cur.execute(sql)
    assert campaigns.get_campaign(org.org_id, project.project_id, campaign['campaign_id']) is not None


def test_secret_shaped_strings_and_invalid_options_are_rejected():
    org, project, principal, campaign = [uuid.uuid4() for _ in range(4)]
    with pytest.raises(campaigns.CampaignError) as exc:
        campaigns.submit_campaign(org, project, 'tenant', principal, title='Title',
            prompt='-----BEGIN PRIVATE KEY-----', idempotency_key='key')
    assert exc.value.code == 'secret_shaped_field'
    for options in [['-----BEGIN PRIVATE KEY-----'], [1], ['x'] * 17, {}]:
        with pytest.raises(campaigns.CampaignError):
            campaigns.ask_question(org, project, campaign, question_key='key', prompt='Question', options=options)
