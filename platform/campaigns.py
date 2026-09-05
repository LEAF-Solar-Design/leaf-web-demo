"""Project campaign authority for durable admission and accepted decisions."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from datetime import datetime

from psycopg.types.json import Jsonb

from .db import connection
from .ios_ship import reject_secret_shaped, SecretShapedFieldRejected


class CampaignError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class CampaignConflict(CampaignError):
    pass


class CampaignUnavailable(CampaignError):
    pass


def _secret(value):
    try:
        reject_secret_shaped(value)
    except SecretShapedFieldRejected as exc:
        raise CampaignError(exc.code, str(exc)) from exc


def _text(value, name, maximum):
    _secret(value)
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or '\x00' in value:
        raise CampaignError('invalid_request', f'{name} must contain 1 to {maximum} characters')
    return value


def _uuid(value):
    _secret(value)
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise CampaignError('invalid_request', 'invalid UUID') from exc


def _scope(org_id, project_id):
    return {'org': _uuid(org_id), 'project': _uuid(project_id)}


def _fingerprint(domain, value):
    payload = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(domain.encode() + b'\0' + payload).hexdigest()


def _lock(cur, value):
    key = int.from_bytes(hashlib.sha256(str(value).encode()).digest()[:8], 'big', signed=True)
    cur.execute('SELECT pg_advisory_xact_lock(%s)', (key,))


def _row(row, *, replayed=False):
    if row is None:
        return None
    result = {key: str(value) if isinstance(value, uuid.UUID) else
              value.isoformat() if isinstance(value, datetime) else value
              for key, value in dict(row).items()}
    result['replayed'] = replayed
    if result.get('dispatch_ref') is None:
        result['dispatch'] = {'available': False, 'action': 'mount-fleet-adapter'}
    return result


@contextmanager
def _cursor():
    try:
        with connection() as conn, conn.cursor() as cur:
            yield cur
    except CampaignError:
        raise
    except Exception as exc:
        raise CampaignUnavailable('campaigns_unavailable', 'campaign store is unavailable') from exc


def _missing():
    raise CampaignUnavailable('project_unavailable', 'project is unavailable')


def _principal(cur, scope, principal_id):
    cur.execute(
        'SELECT m.binding_id FROM project_member_bindings m '
        'JOIN identity_bindings b ON b.platform_tenant_id=m.org_id AND b.binding_id=m.binding_id '
        'JOIN projects p ON p.org_id=m.org_id AND p.project_id=m.project_id '
        "WHERE m.org_id=%(org)s AND m.project_id=%(project)s AND m.binding_id=%(principal)s "
        "AND m.status='active' AND b.status='active' AND m.role IN ('owner','editor') "
        "AND p.deleted_at IS NULL AND p.status='active'",
        {**scope, 'principal': principal_id})
    if cur.fetchone() is None:
        _missing()


def _campaign(cur, scope):
    cur.execute('SELECT * FROM campaigns WHERE org_id=%(org)s AND project_id=%(project)s '
                'AND campaign_id=%(campaign)s', scope)
    return cur.fetchone()


def submit_campaign(org_id, project_id, tenant_id, principal_id, *, title, prompt, idempotency_key):
    scope = _scope(org_id, project_id)
    principal = _uuid(principal_id)
    _text(tenant_id, 'tenant_id', 32768)
    _text(title, 'title', 200)
    _text(prompt, 'prompt', 32768)
    _text(idempotency_key, 'idempotency_key', 128)
    fingerprint = _fingerprint('leaf.campaign.v1', {
        'project_id': str(scope['project']), 'title': title, 'prompt': prompt})
    with _cursor() as cur:
        _lock(cur, f"{scope['org']}:{scope['project']}:{idempotency_key}")
        _principal(cur, scope, principal)
        cur.execute('SELECT * FROM campaigns WHERE org_id=%(org)s AND project_id=%(project)s '
                    'AND idempotency_key=%(key)s', {**scope, 'key': idempotency_key})
        existing = cur.fetchone()
        if existing:
            if existing['submission_fingerprint'] != fingerprint:
                raise CampaignConflict('idempotency_conflict', 'idempotency key has a different submission')
            return _row(existing, replayed=True)
        cur.execute(
            'INSERT INTO campaigns (campaign_id, org_id, project_id, tenant_id, principal_id, '
            'title, prompt, idempotency_key, submission_fingerprint) '
            'VALUES (%(id)s, %(org)s, %(project)s, %(tenant)s, %(principal)s, '
            '%(title)s, %(prompt)s, %(key)s, %(fingerprint)s) RETURNING *',
            {**scope, 'id': uuid.uuid4(), 'tenant': tenant_id, 'principal': principal,
             'title': title, 'prompt': prompt, 'key': idempotency_key, 'fingerprint': fingerprint})
        return _row(cur.fetchone())


def get_campaign(org_id, project_id, campaign_id):
    scope = {**_scope(org_id, project_id), 'campaign': _uuid(campaign_id)}
    with _cursor() as cur:
        return _row(_campaign(cur, scope))


def list_campaigns(org_id, project_id, limit=50):
    scope = _scope(org_id, project_id)
    _secret(limit)
    try:
        limit = max(1, min(200, int(limit)))
    except (ValueError, TypeError, OverflowError) as exc:
        raise CampaignError('invalid_request', 'limit must be an integer') from exc
    with _cursor() as cur:
        cur.execute('SELECT * FROM campaigns WHERE org_id=%(org)s AND project_id=%(project)s '
                    'ORDER BY created_at DESC, campaign_id DESC LIMIT %(limit)s', {**scope, 'limit': limit})
        return [_row(row) for row in cur.fetchall()]


def ask_question(org_id, project_id, campaign_id, *, question_key, prompt,
                 options=None, asked_by='operator', blocks_dispatch=True):
    scope = {**_scope(org_id, project_id), 'campaign': _uuid(campaign_id)}
    _text(question_key, 'question_key', 128)
    if re.fullmatch(r'[A-Za-z0-9._-]+', question_key) is None:
        raise CampaignError('invalid_request', 'invalid question key')
    _text(prompt, 'prompt', 4096)
    _secret(options)
    _secret(asked_by)
    if options is not None and (not isinstance(options, list) or len(options) > 16
                                or any(not isinstance(item, str) or '\x00' in item for item in options)):
        raise CampaignError('invalid_request', 'options must be at most 16 strings')
    if asked_by not in ('operator', 'worker') or not isinstance(blocks_dispatch, bool):
        raise CampaignError('invalid_request', 'invalid question fields')
    with _cursor() as cur:
        _lock(cur, f"{scope['campaign']}:{question_key}")
        if _campaign(cur, scope) is None:
            _missing()
        cur.execute('SELECT * FROM campaign_questions WHERE org_id=%(org)s AND project_id=%(project)s '
                    'AND campaign_id=%(campaign)s AND question_key=%(key)s', {**scope, 'key': question_key})
        existing = cur.fetchone()
        if existing:
            if existing['prompt'] != prompt:
                raise CampaignConflict('question_conflict', 'question key has a different prompt')
            return _row(existing, replayed=True)
        cur.execute(
            'INSERT INTO campaign_questions (question_id, campaign_id, org_id, project_id, '
            'question_key, prompt, options, asked_by, blocks_dispatch) '
            'VALUES (%(id)s, %(campaign)s, %(org)s, %(project)s, %(key)s, %(prompt)s, '
            '%(options)s, %(asked_by)s, %(blocks)s) RETURNING *',
            {**scope, 'id': uuid.uuid4(), 'key': question_key, 'prompt': prompt,
             'options': Jsonb(options) if options is not None else None,
             'asked_by': asked_by, 'blocks': blocks_dispatch})
        return _row(cur.fetchone())


def answer_question(org_id, project_id, campaign_id, question_id, principal_id, *, answer):
    scope = {**_scope(org_id, project_id), 'campaign': _uuid(campaign_id), 'question': _uuid(question_id)}
    principal = _uuid(principal_id)
    _text(answer, 'answer', 8192)
    fingerprint = _fingerprint('leaf.campaign.answer.v1', {'answer': answer})
    with _cursor() as cur:
        _lock(cur, scope['question'])
        if _campaign(cur, scope) is None:
            _missing()
        _principal(cur, scope, principal)
        cur.execute('SELECT * FROM campaign_questions WHERE org_id=%(org)s AND project_id=%(project)s '
                    'AND campaign_id=%(campaign)s AND question_id=%(question)s FOR UPDATE', scope)
        if cur.fetchone() is None:
            _missing()
        cur.execute('SELECT * FROM campaign_answers WHERE org_id=%(org)s AND project_id=%(project)s '
                    'AND campaign_id=%(campaign)s AND question_id=%(question)s', scope)
        existing = cur.fetchone()
        if existing:
            if existing['answer_fingerprint'] != fingerprint:
                raise CampaignConflict('answer_conflict', 'question already has a different answer')
            return _row(existing, replayed=True)
        cur.execute(
            'INSERT INTO campaign_answers (answer_id, question_id, campaign_id, org_id, project_id, '
            'principal_id, answer, answer_fingerprint) VALUES (%(id)s, %(question)s, %(campaign)s, '
            '%(org)s, %(project)s, %(principal)s, %(answer)s, %(fingerprint)s) RETURNING *',
            {**scope, 'id': uuid.uuid4(), 'principal': principal, 'answer': answer, 'fingerprint': fingerprint})
        result = _row(cur.fetchone())
        cur.execute("UPDATE campaign_questions SET status='answered' WHERE org_id=%(org)s "
                    'AND project_id=%(project)s AND campaign_id=%(campaign)s AND question_id=%(question)s', scope)
        return result


def list_questions(org_id, project_id, campaign_id):
    scope = {**_scope(org_id, project_id), 'campaign': _uuid(campaign_id)}
    with _cursor() as cur:
        if _campaign(cur, scope) is None:
            _missing()
        cur.execute('SELECT q.*, a.answer, a.answer_id, a.created_at AS answered_at '
                    'FROM campaign_questions q LEFT JOIN campaign_answers a '
                    'ON a.question_id=q.question_id AND a.campaign_id=q.campaign_id '
                    'AND a.org_id=q.org_id AND a.project_id=q.project_id '
                    'WHERE q.org_id=%(org)s AND q.project_id=%(project)s '
                    'AND q.campaign_id=%(campaign)s ORDER BY q.created_at, q.question_id', scope)
        return [_row(row) for row in cur.fetchall()]
