"""Versioned finish contracts within the campaign authority.

Only trusted producer adapters call record_stage and finish_release. Evidence
does not grant execution authority, and release completion never completes the
original campaign ambition.
"""
from __future__ import annotations

import json
import re
import uuid

from psycopg.types.json import Jsonb

from .campaigns import (
    CampaignError, CampaignConflict, CampaignUnavailable, _cursor, _scope,
    _principal, _lock, _uuid, _text, _secret, _fingerprint, _row, _campaign, _missing,
)

STAGES = ('implementation', 'publication', 'deployment', 'user_verification', 'delivery')
PRODUCERS = dict(zip(STAGES, ('task_ledger', 'task_ledger', 'deployment_adapter',
                             'user_workflow', 'artifact_reader')))
SCOPE = 'org_id=%(org)s AND project_id=%(project)s AND campaign_id=%(campaign)s'
RELEASE = SCOPE + ' AND release_id=%(release)s'


def _invalid(message='invalid release request'):
    raise CampaignError('invalid_request', message)


def _conflict(code):
    raise CampaignConflict(code, code.replace('_', ' '))


def _string(value, name, maximum=1024):
    _text(value, name, maximum)
    if not value.strip():
        _invalid(name + ' must not be blank')
    return value


def _json(value):
    _secret(value)
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False)
        size = len(encoded.encode())
    except (ValueError, TypeError, RecursionError, UnicodeError):
        _invalid()
    if size > 60000:
        _invalid('release data is too large')
    # Commands, grants and credentials must never enter public snapshots.
    def visit(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or '\x00' in key or key.lower() in {
                    'command', 'commands', 'grant', 'grants', 'credentials',
                    'password', 'token', 'access_token', 'authorization',
                    'api_key', 'secret', 'provider_endpoint', 'endpoint_override',
                    'verify_command', 'shell', 'script', 'grant_ref', 'opaque_grant',
                }:
                    _invalid('executable or credential material is not release evidence')
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, str) and '\x00' in item:
            _invalid('release text contains a null character')
    visit(value)
    return value


def _strings(value, name, maximum=64):
    if not isinstance(value, list) or len(value) > maximum:
        _invalid(name + ' must be a bounded list')
    for item in value:
        _string(item, name)
    return value


def _contract(value, profile):
    _json(value)
    if not isinstance(value, dict) or set(value) - {
        'original_goal', 'intended_user', 'workflow', 'release_boundary',
        'deferred_items', 'artifact_refs', 'required_checks',
    }:
        _invalid('invalid contract fields')
    for field in ('original_goal', 'intended_user', 'workflow', 'release_boundary'):
        _string(value.get(field), field, 16384)
    _strings(value.get('deferred_items', []), 'deferred_items')
    _strings(value.get('artifact_refs', []), 'artifact_refs')
    checks = value.get('required_checks')
    if not isinstance(checks, list) or not 1 <= len(checks) <= 128:
        _invalid('required_checks must not be empty')
    ids, stages = set(), set()
    for check in checks:
        if not isinstance(check, dict) or set(check) != {'check_id', 'stage', 'description'}:
            _invalid('invalid required check')
        key = _string(check['check_id'], 'check_id', 128)
        if not re.fullmatch(r'[A-Za-z0-9._-]+', key) or key in ids:
            _invalid('check ids must be unique')
        if check['stage'] not in STAGES:
            _invalid('unknown check stage')
        _string(check['description'], 'description', 2048)
        ids.add(key)
        stages.add(check['stage'])
    if stages != set(STAGES):
        _invalid('each release stage requires checks')
    return value


def _params(org_id, project_id, campaign_id, release_id=None):
    scope = {**_scope(org_id, project_id), 'campaign': _uuid(campaign_id)}
    if release_id is not None:
        scope['release'] = _uuid(release_id)
    return scope


def _check(cur, scope, principal=None):
    if _campaign(cur, scope) is None:
        _missing()
    if principal is not None:
        _principal(cur, scope, _uuid(principal))


def _get(cur, scope):
    cur.execute('SELECT * FROM campaign_releases WHERE ' + RELEASE + ' FOR UPDATE', scope)
    row = cur.fetchone()
    if row is None:
        _missing()
    return row


def _org_lock(cur, scope):
    _lock(cur, 'campaign-release-org:' + str(scope['org']))


def _slot(cur, scope):
    # A paused release yields only once outstanding work has settled. Read
    # attempts without locking them: all claimers already hold the org lock.
    cur.execute("SELECT 1 FROM campaign_releases r WHERE r.org_id=%(org)s "
                "AND r.release_id<>%(release)s AND (r.status='active' OR EXISTS ("
                'SELECT 1 FROM campaign_tasks t WHERE t.org_id=r.org_id '
                'AND t.project_id=r.project_id AND t.campaign_id=r.campaign_id '
                "AND t.status IN ('claimed','reconcile_required'))) LIMIT 1", scope)
    return 'queued' if cur.fetchone() else 'active'


def _public(row, replayed=False):
    result = _row(row, replayed=replayed)
    result.pop('dispatch', None)
    result.pop('payload_fingerprint', None)
    if 'contract' in result:
        result['scope_summary'] = result['contract']['release_boundary']
        result['deferred_items'] = result['contract'].get('deferred_items', [])
    return result


def _update(cur, scope, status, next_action=None):
    cur.execute('UPDATE campaign_releases SET status=%(status)s, next_action=%(next)s, '
                'updated_at=NOW() WHERE ' + RELEASE + ' RETURNING *',
                {**scope, 'status': status, 'next': Jsonb(next_action) if next_action else None})
    return cur.fetchone()


def create_release(org_id, project_id, campaign_id, principal_id, *, contract,
                   delivery_profile, idempotency_key):
    _string(delivery_profile, 'delivery_profile', 64)
    if not re.fullmatch(r'[a-z][a-z0-9_]*', delivery_profile):
        _invalid()
    _contract(contract, delivery_profile)
    _string(idempotency_key, 'idempotency_key', 128)
    scope = _params(org_id, project_id, campaign_id, uuid.uuid4())
    fingerprint = _fingerprint('release', dict(contract=contract, delivery_profile=delivery_profile))
    with _cursor() as cur:
        _org_lock(cur, scope)
        _check(cur, scope, principal_id)
        cur.execute('SELECT * FROM campaign_releases WHERE ' + SCOPE +
                    ' AND idempotency_key=%(key)s', {**scope, 'key': idempotency_key})
        old = cur.fetchone()
        if old:
            if old['payload_fingerprint'] != fingerprint:
                _conflict('idempotency_conflict')
            return _public(old, True)
        status = _slot(cur, scope)
        params = {**scope, 'principal': _uuid(principal_id), 'profile': delivery_profile,
                  'contract': Jsonb(contract), 'key': idempotency_key,
                  'fingerprint': fingerprint, 'status': status}
        cur.execute('INSERT INTO campaign_releases (org_id,project_id,campaign_id,release_id,'
                    'principal_id,delivery_profile,status,contract_version,contract,idempotency_key,'
                    'payload_fingerprint) VALUES (%(org)s,%(project)s,%(campaign)s,%(release)s,'
                    '%(principal)s,%(profile)s,%(status)s,1,%(contract)s,%(key)s,%(fingerprint)s) RETURNING *', params)
        row = cur.fetchone()
        cur.execute('INSERT INTO campaign_release_contracts (org_id,project_id,campaign_id,release_id,'
                    'contract_version,contract,reason,principal_id,idempotency_key,payload_fingerprint) '
                    "VALUES (%(org)s,%(project)s,%(campaign)s,%(release)s,1,%(contract)s,'initial',"
                    '%(principal)s,%(key)s,%(fingerprint)s)', params)
        return _public(row)


def list_releases(org_id, project_id, campaign_id, limit=50):
    if type(limit) is not int or not 1 <= limit <= 200:
        _invalid('invalid limit')
    scope = _params(org_id, project_id, campaign_id)
    with _cursor() as cur:
        _check(cur, scope)
        cur.execute('SELECT * FROM campaign_releases WHERE ' + SCOPE +
                    ' ORDER BY created_at DESC, release_id DESC LIMIT %(limit)s', {**scope, 'limit': limit})
        return [_public(row) for row in cur.fetchall()]


def _history(cur, scope, version=None):
    cur.execute('SELECT * FROM campaign_release_stages WHERE ' + RELEASE +
                (' AND contract_version=%(version)s' if version is not None else '') + ' ORDER BY seq',
                {**scope, 'version': version})
    return cur.fetchall()


def _current(rows):
    return {row['stage']: row for row in rows}


def _snapshot(cur, scope, row):
    if row is None:
        return dict(release=None, stages=[], decisions=[], coverage=[], remaining=[],
                    deliverables=[], next_action=None)
    scope = {**scope, 'release': row['release_id']}
    history = _history(cur, scope)
    current = _current([item for item in history if item['contract_version'] == row['contract_version']])
    coverage = []
    for check in row['contract']['required_checks']:
        stage = current.get(check['stage'])
        result = next((item for item in stage['evidence'].get('checks', [])
                       if item['check_id'] == check['check_id']), None) if stage else None
        status = result['status'] if result else 'unavailable'
        if status == 'passed' and stage['status'] != 'passed':
            status = stage['status']
        coverage.append({**check, 'status': status})
    remaining = [dict(item) for item in coverage if item['status'] != 'passed']
    delivery = current.get('delivery')
    artifacts = delivery['evidence'].get('artifacts', []) if delivery and delivery['status'] == 'passed' else []
    cur.execute('SELECT * FROM campaign_release_decisions WHERE ' + RELEASE +
                ' ORDER BY created_at, decision_id', scope)
    decisions = [_public(item) for item in cur.fetchall()]
    return dict(release=_public(row), stages=[_public(item) for item in history],
                decisions=decisions, coverage=coverage, remaining=remaining,
                deliverables=artifacts, next_action=row['next_action'])


def get_release(org_id, project_id, campaign_id, release_id):
    scope = _params(org_id, project_id, campaign_id, release_id)
    with _cursor() as cur:
        _check(cur, scope)
        return _snapshot(cur, scope, _get(cur, scope))


def release_snapshot(org_id, project_id, campaign_id):
    scope = _params(org_id, project_id, campaign_id)
    with _cursor() as cur:
        _check(cur, scope)
        cur.execute('SELECT * FROM campaign_releases WHERE ' + SCOPE +
                    ' ORDER BY created_at DESC, release_id DESC LIMIT 1 FOR UPDATE', scope)
        return _snapshot(cur, scope, cur.fetchone())


def transition_release(org_id, project_id, campaign_id, release_id, principal_id, *, action):
    if action not in ('pause', 'resume', 'cancel', 'wait'):
        _invalid()
    scope = _params(org_id, project_id, campaign_id, release_id)
    with _cursor() as cur:
        _org_lock(cur, scope)
        _check(cur, scope, principal_id)
        row = _get(cur, scope)
        target = {'pause': 'paused', 'cancel': 'cancelled', 'wait': 'waiting'}.get(action)
        if row['status'] in ('finished', 'cancelled'):
            if target == row['status']:
                return _public(row, True)
            _conflict('release_terminal')
        if row['status'] == 'needs_approach' and action != 'cancel':
            _conflict('needs_approach')
        if action == 'resume':
            target = _slot(cur, scope)
        return _public(_update(cur, scope, target))


def revise_contract(org_id, project_id, campaign_id, release_id, principal_id, *,
                    contract, reason, idempotency_key):
    _string(reason, 'reason', 4096)
    _string(idempotency_key, 'idempotency_key', 128)
    scope = _params(org_id, project_id, campaign_id, release_id)
    with _cursor() as cur:
        _org_lock(cur, scope)
        _check(cur, scope, principal_id)
        row = _get(cur, scope)
        _contract(contract, row['delivery_profile'])
        fingerprint = _fingerprint('revision', dict(contract=contract, reason=reason))
        cur.execute('SELECT * FROM campaign_release_contracts WHERE ' + RELEASE +
                    ' AND idempotency_key=%(key)s', {**scope, 'key': idempotency_key})
        old = cur.fetchone()
        if old:
            if old['payload_fingerprint'] != fingerprint:
                _conflict('idempotency_conflict')
            return _public(row, True)
        if row['status'] in ('finished', 'cancelled'):
            _conflict('release_terminal')
        if contract['original_goal'] != row['contract']['original_goal']:
            _conflict('original_goal_immutable')
        if contract == row['contract']:
            _conflict('approach_change_required')
        version = row['contract_version'] + 1
        params = {**scope, 'version': version, 'contract': Jsonb(contract), 'reason': reason,
                  'principal': _uuid(principal_id), 'key': idempotency_key, 'fingerprint': fingerprint}
        cur.execute('INSERT INTO campaign_release_contracts (org_id,project_id,campaign_id,release_id,'
                    'contract_version,contract,reason,principal_id,idempotency_key,payload_fingerprint) '
                    'VALUES (%(org)s,%(project)s,%(campaign)s,%(release)s,%(version)s,%(contract)s,'
                    '%(reason)s,%(principal)s,%(key)s,%(fingerprint)s)', params)
        cur.execute('UPDATE campaign_releases SET contract=%(contract)s, contract_version=%(version)s, '
                    'updated_at=NOW() WHERE ' + RELEASE, params)
        _decision(cur, scope, 'revision:' + idempotency_key, 'revision',
                  dict(contract_version=version, reason=reason, release_boundary=contract['release_boundary'],
                       deferred_items=contract.get('deferred_items', [])), str(principal_id))
        status = _slot(cur, scope) if row['status'] == 'needs_approach' else row['status']
        return _public(_update(cur, scope, status))


def _decision(cur, scope, key, kind, payload, decided_by):
    fingerprint = _fingerprint('decision', dict(kind=kind, payload=payload, decided_by=decided_by))
    cur.execute('SELECT * FROM campaign_release_decisions WHERE ' + RELEASE +
                ' AND decision_key=%(key)s', {**scope, 'key': key})
    old = cur.fetchone()
    if old:
        if old['payload_fingerprint'] != fingerprint:
            _conflict('decision_conflict')
        return _public(old, True)
    cur.execute('INSERT INTO campaign_release_decisions (decision_id,org_id,project_id,campaign_id,'
                'release_id,decision_key,kind,payload,decided_by,payload_fingerprint) VALUES '
                '(%(id)s,%(org)s,%(project)s,%(campaign)s,%(release)s,%(key)s,%(kind)s,%(payload)s,'
                '%(by)s,%(fingerprint)s) RETURNING *',
                {**scope, 'id': uuid.uuid4(), 'key': key, 'kind': kind, 'payload': Jsonb(payload),
                 'by': decided_by, 'fingerprint': fingerprint})
    return _public(cur.fetchone())


def record_decision(org_id, project_id, campaign_id, release_id, *, decision_key,
                    kind, payload, decided_by):
    _string(decision_key, 'decision_key', 128)
    _string(decided_by, 'decided_by', 128)
    _json(payload)
    if kind not in ('scope', 'capability_selection', 'revision', 'external_dependency', 'answer') or not isinstance(payload, dict):
        _invalid()
    scope = _params(org_id, project_id, campaign_id, release_id)
    with _cursor() as cur:
        _check(cur, scope)
        _get(cur, scope)
        return _decision(cur, scope, decision_key, kind, payload, decided_by)


def _evidence(row, stage, status, evidence):
    _json(evidence)
    if not isinstance(evidence, dict):
        _invalid()
    version = evidence.get('contract_version')
    if type(version) is not int or version != row['contract_version']:
        _conflict('contract_version_mismatch')
    source = evidence.get('source_revision')
    if status == 'passed' or source is not None:
        _string(source, 'source_revision', 256)
    checks = evidence.get('checks', [])
    if not isinstance(checks, list) or len(checks) > 128:
        _invalid()
    required = {item['check_id'] for item in row['contract']['required_checks'] if item['stage'] == stage}
    observed = {}
    for check in checks:
        if not isinstance(check, dict) or set(check) != {'check_id', 'status', 'evidence'}:
            _invalid('invalid check observation')
        key = _string(check['check_id'], 'check_id', 128)
        if key not in required or key in observed or check['status'] not in ('passed', 'failed', 'unavailable'):
            _invalid('invalid check observation')
        if not isinstance(check['evidence'], dict) or (check['status'] == 'passed' and not check['evidence']):
            _invalid('passing checks need observations')
        observed[key] = check['status']
    if status != 'passed':
        return
    if set(observed) != required or any(value != 'passed' for value in observed.values()):
        _conflict('insufficient_evidence')
    if stage == 'deployment':
        if evidence.get('observed_revision') != source:
            _conflict('source_revision_mismatch')
        for name in ('resource_identity', 'rollback_identity'):
            _string(evidence.get(name), name)
    if stage == 'user_verification':
        if evidence.get('workflow') != row['contract']['workflow']:
            _conflict('workflow_mismatch')
        observations = evidence.get('observations')
        if not isinstance(observations, (dict, list)) or not observations:
            _conflict('insufficient_evidence')
    if stage == 'delivery':
        _string(evidence.get('replay_recipe'), 'replay_recipe', 8192)
        artifacts = evidence.get('artifacts')
        if not isinstance(artifacts, list) or len(artifacts) > 64:
            _invalid('delivery requires artifacts list')
        refs = row['contract'].get('artifact_refs', [])
        if (refs or row['delivery_profile'] == 'cad_file') and not artifacts:
            _conflict('insufficient_evidence')
        found = set()
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                _invalid()
            for name in ('artifact_ref', 'name', 'sha256', 'access_path'):
                _string(artifact.get(name), name)
            if (not re.fullmatch(r'[0-9a-f]{64}', artifact['sha256'])
                    or type(artifact.get('byte_count')) is not int
                    or not 0 < artifact['byte_count'] <= 9223372036854775807
                    or artifact.get('retrieved') is not True or artifact.get('valid') is not True
                    or artifact['artifact_ref'] in found):
                _conflict('insufficient_evidence')
            found.add(artifact['artifact_ref'])
        if not set(refs).issubset(found):
            _conflict('insufficient_evidence')


def record_stage(org_id, project_id, campaign_id, release_id, *, stage, status,
                 evidence, producer, operation_key):
    _string(operation_key, 'operation_key', 128)
    if stage not in STAGES or status not in ('passed', 'failed', 'unavailable'):
        _invalid()
    if producer != PRODUCERS[stage]:
        _conflict('invalid_producer')
    scope = _params(org_id, project_id, campaign_id, release_id)
    with _cursor() as cur:
        _org_lock(cur, scope)
        row = _get(cur, scope)
        _check(cur, scope, row['principal_id'])
        _evidence(row, stage, status, evidence)
        fingerprint = _fingerprint('stage', dict(stage=stage, status=status, evidence=evidence, producer=producer))
        history = _history(cur, scope, row['contract_version'])
        old = next((item for item in history if item['stage'] == stage and item['operation_key'] == operation_key), None)
        if old:
            if old['payload_fingerprint'] != fingerprint:
                _conflict('stage_conflict')
            return _public(old, True)
        current = _current(history)
        if stage in current and current[stage]['status'] == 'passed':
            _conflict('stage_already_passed')
        if row['status'] in ('finished', 'cancelled'):
            _conflict('release_terminal')
        if row['status'] == 'needs_approach':
            _conflict('needs_approach')
        predecessors = [current.get(name) for name in STAGES[:STAGES.index(stage)]]
        if any(item is None or item['status'] != 'passed' for item in predecessors):
            _conflict('predecessor_required')
        sources = {item['evidence']['source_revision'] for item in predecessors}
        source = evidence.get('source_revision')
        if sources and source is not None and sources != {source}:
            _conflict('source_revision_mismatch')
        cur.execute('INSERT INTO campaign_release_stages (stage_id,org_id,project_id,campaign_id,'
                    'release_id,contract_version,stage,status,evidence,producer,operation_key,payload_fingerprint) '
                    'VALUES (%(id)s,%(org)s,%(project)s,%(campaign)s,%(release)s,%(version)s,%(stage)s,'
                    '%(status)s,%(evidence)s,%(producer)s,%(key)s,%(fingerprint)s) RETURNING *',
                    {**scope, 'id': uuid.uuid4(), 'version': row['contract_version'], 'stage': stage,
                     'status': status, 'evidence': Jsonb(evidence), 'producer': producer,
                     'key': operation_key, 'fingerprint': fingerprint})
        result = cur.fetchone()
        if status != 'passed':
            # A claimed new source in a failed receipt is not accepted product
            # change. Only a passed stage or explicit contract revision resets.
            failures = [item for item in history if item['stage'] == stage and item['status'] != 'passed']
            if len(failures) >= 2:
                _update(cur, scope, 'needs_approach', dict(action='revise_contract', stage=stage,
                        reason='Two corrections produced no accepted product change'))
        return _public(result)


def retry_stage(org_id, project_id, campaign_id, release_id, principal_id, *, stage):
    if stage not in STAGES:
        _invalid()
    scope = _params(org_id, project_id, campaign_id, release_id)
    with _cursor() as cur:
        _org_lock(cur, scope)
        _check(cur, scope, principal_id)
        row = _get(cur, scope)
        if row['status'] == 'needs_approach':
            _conflict('needs_approach')
        if row['status'] in ('cancelled', 'finished'):
            _conflict('release_terminal')
        history = _history(cur, scope, row['contract_version'])
        if not history or history[-1]['stage'] != stage or history[-1]['status'] == 'passed':
            _conflict('stage_not_retryable')
        return _public(_update(cur, scope, _slot(cur, scope), dict(action='retry_stage', stage=stage)))


def finish_release(org_id, project_id, campaign_id, release_id):
    scope = _params(org_id, project_id, campaign_id, release_id)
    with _cursor() as cur:
        _org_lock(cur, scope)
        row = _get(cur, scope)
        _check(cur, scope, row['principal_id'])
        if row['status'] == 'finished':
            return _public(row, True)
        if row['status'] in ('cancelled', 'needs_approach'):
            _conflict('release_terminal')
        current = _current(_history(cur, scope, row['contract_version']))
        if any(name not in current or current[name]['status'] != 'passed' for name in STAGES):
            _conflict('insufficient_evidence')
        for name in STAGES:
            _evidence(row, name, 'passed', current[name]['evidence'])
            if current[name]['producer'] != PRODUCERS[name]:
                _conflict('insufficient_evidence')
        if len({item['evidence']['source_revision'] for item in current.values()}) != 1:
            _conflict('insufficient_evidence')
        return _public(_update(cur, scope, 'finished'))


def admits_claim(cur, scope, worker_id):
    """Serialize release admission before any task lock, including legacy claims."""
    _org_lock(cur, scope)
    cur.execute('SELECT status FROM campaign_releases WHERE ' + SCOPE +
                ' ORDER BY created_at DESC, release_id DESC LIMIT 1', scope)
    release = cur.fetchone()
    if release is None:
        return True
    # Preserve the execution ledger's expiry/recovery path even while admission
    # is paused or full. Use its receipt producers; never invent a rollback.
    from . import campaign_execution as execution
    cur.execute('SELECT t.* FROM campaign_tasks t WHERE t.org_id=%(org)s '
                'AND t.project_id=%(project)s AND t.campaign_id=%(campaign)s '
                "AND t.status='claimed' AND EXISTS (SELECT 1 FROM campaign_task_attempts a "
                'WHERE a.org_id=t.org_id AND a.project_id=t.project_id AND a.campaign_id=t.campaign_id '
                "AND a.task_id=t.task_id AND a.status='active' AND a.deadline_at<=clock_timestamp() "
                "AND (a.stage IN ('publication','deployment','cleanup') OR EXISTS ("
                'SELECT 1 FROM campaign_dispatch_bindings b WHERE b.org_id=a.org_id '
                'AND b.project_id=a.project_id AND b.campaign_id=a.campaign_id AND b.attempt_id=a.attempt_id))) '
                'ORDER BY t.task_id FOR UPDATE OF t SKIP LOCKED', scope)
    uncertain = cur.fetchall()
    for task in uncertain:
        params = {**scope, 'task': task['task_id']}
        cur.execute("UPDATE campaign_task_attempts SET status='expired' WHERE " + SCOPE +
                    " AND task_id=%(task)s AND status='active' AND deadline_at<=clock_timestamp() RETURNING *", params)
        attempt = cur.fetchone()
        if attempt is None:
            continue
        execution._event(cur, scope, task, 'attempt_expired', attempt)
        binding = execution._dispatch_binding(cur, scope, attempt)
        key = attempt['outward_operation_key'] or (binding['request_id'] if binding else None)
        values = execution._values('unknown', {'reason': 'lease_expired'}, None, key, None, None, False)
        execution._receipt(cur, scope, task, attempt, values)
        execution._advance(cur, scope, task, 'unknown')
        execution._event(cur, scope, task, 'outcome_unknown', attempt, {'outward_operation_key': key})
    if release['status'] != 'active':
        return False
    cur.execute('SELECT 1 FROM campaign_tasks WHERE ' + SCOPE +
                " AND status='reconcile_required' LIMIT 1", scope)
    if cur.fetchone():
        return False
    # Uncertain external attempts occupy capacity until the existing recovery
    # authority settles them. Expired local-only work can still be reclaimed.
    cur.execute("SELECT count(*) AS n FROM campaign_task_attempts a WHERE a.org_id=%(org)s AND ("
                "(a.status='active' AND (a.deadline_at>clock_timestamp() OR "
                "a.stage IN ('publication','deployment','cleanup') OR EXISTS ("
                'SELECT 1 FROM campaign_dispatch_bindings b WHERE b.org_id=a.org_id '
                'AND b.project_id=a.project_id AND b.campaign_id=a.campaign_id '
                "AND b.attempt_id=a.attempt_id))) OR (a.status='expired' AND EXISTS ("
                'SELECT 1 FROM campaign_tasks t WHERE t.org_id=a.org_id AND t.project_id=a.project_id '
                'AND t.campaign_id=a.campaign_id AND t.task_id=a.task_id '
                "AND t.status='reconcile_required' AND t.fence IN (a.fence,a.fence+1))))", scope)
    return cur.fetchone()['n'] < 3
