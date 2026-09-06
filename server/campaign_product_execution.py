"""ReciPDF campaign products use the canonical project repository edit producer."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import uuid
from datetime import datetime, timedelta, timezone

import requests

import broker_client
import project_repository_source as source_service

SOURCE_LIMIT = 8388608
PRODUCT_LIMIT = 6291456
FILE_LIMIT = 1048576
TOTAL_LIMIT = 4194304
RECEIPT_LIMIT = 65536
RECEIPTS = {name: 'findings/pair/' + path + '.json' for name, path in (
    ('source_binding', 'source-binding'), ('edit', 'edit'), ('accept', 'accept'), ('final', 'receipt'))}
META = {'run_id', 'milestone_id', 'attempt', 'requested_model', 'observed_model',
        'cli_version', 'cli_path', 'usage', 'evidence_kind'}
BINDING = {'requested_sha', 'initial_head', 'fleet_runtime_sha', 'bound_input'}
OUTPUT = {'kind', 'leaf_id', 'run_id', 'fencing_token', 'result_fingerprint', 'task_id',
          'payload_fingerprint', 'source_ref', 'base_sha', 'result_sha', 'changed_paths', 'files', 'receipts'}


def closed(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError('invalid product fields')
    return value


def counter(value, low=0, high=9007199254740991):
    return type(value) is int and low <= value <= high


def product_paths(paths):
    if not isinstance(paths, list) or not 1 <= len(paths) <= 64:
        raise ValueError('invalid product paths')
    seen = set()
    for path in paths:
        if (not isinstance(path, str) or not 1 <= len(path) <= 240
                or any(c in path for c in '\\:?#%')
                or any(ord(c) < 32 or ord(c) == 127 for c in path)
                or any(p in ('', '.', '..') for p in path.split('/'))
                or path.split('/')[0].lower() in {
                    '.git', '.claude', '.codex', '.ssh', '.aws', '_marathon_runs', '.leaf', 'findings'}
                or path.lower() == 'prompt.md' or path.lower() in seen):
            raise ValueError('invalid product path')
        seen.add(path.lower())
    return paths


def verify_argv(command):
    if (not isinstance(command, str) or not 1 <= len(command) <= 4096
            or any(ord(c) < 32 or ord(c) == 127 for c in command)
            or any(c in command for c in '|&;<>`$')):
        raise ValueError('invalid product verification')
    argv = shlex.split(command, posix=True)
    if (not 1 <= len(argv) <= 128 or any(not arg.strip() for arg in argv)
            or sum(len(arg.encode('utf-8')) for arg in argv) > 4096):
        raise ValueError('invalid product verification')
    executable = argv[0].replace('\\', '/').rsplit('/', 1)[-1].lower().removesuffix('.exe')
    if executable in {'sh', 'bash', 'dash', 'zsh', 'ksh', 'fish', 'csh', 'tcsh',
                      'cmd', 'powershell', 'pwsh', 'env', 'busybox', 'wsl'}:
        raise ValueError('shell verification is unsupported')
    return argv


def task_wire(task):
    product_paths(task['owned_paths'])
    result = {key: str(task[key]) if key == 'task_id' else task[key] for key in (
        'task_id', 'payload_fingerprint', 'task_key', 'capability', 'spec', 'owned_paths',
        'verify_command', 'declared_artifacts', 'source_sha')}
    result['verify_argv'] = verify_argv(task['verify_command'])
    return result


def validate_output(output, task=None, saved=None):
    """Shared ingress validation, before lookup or side effects, then saved binding checks."""
    closed(output, OUTPUT)
    if len(json.dumps(output, ensure_ascii=False, allow_nan=False).encode('utf-8')) > PRODUCT_LIMIT:
        raise ValueError('product response too large')
    if (output['kind'] != 'files' or not source_service._uuid(output['task_id'])
            or not counter(output['fencing_token'], 1)
            or any(not source_service._sha(output[k], 64) for k in ('result_fingerprint', 'payload_fingerprint'))
            or any(not source_service._sha(output[k], 40) for k in ('source_ref', 'base_sha', 'result_sha'))
            or any(not isinstance(output[k], str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,200}', output[k])
                   for k in ('leaf_id', 'run_id'))):
        raise ValueError('invalid product identity')
    if saved is not None:
        expected = {k: saved[k] for k in ('task_id', 'payload_fingerprint', 'source_ref',
                                         'result_fingerprint', 'run_id', 'leaf_id')}
        expected['fencing_token'] = saved['remote_fencing_token']
        if any(output[k] != value for k, value in expected.items()):
            raise ValueError('product binding mismatch')
    files = output['files']
    if not isinstance(files, list):
        raise ValueError('invalid product files')
    paths = product_paths([f.get('path') if isinstance(f, dict) else None for f in files])
    if paths != sorted(paths) or (task is not None and paths != sorted(task['owned_paths'])):
        raise ValueError('product ownership mismatch')
    prefix = f"leaf-results/{output['run_id']}/{output['leaf_id']}/{output['fencing_token']}/"

    def read(entry, maximum, file=False):
        closed(entry, {'path', 'key', 'sha256', 'size_bytes', 'product_b64'} |
               ({'before_sha256'} if file else set()))
        if (not isinstance(entry['path'], str) or not isinstance(entry['key'], str)
                or len(entry['key']) > 1024 or entry['key'] != prefix + entry['path']
                or not counter(entry['size_bytes'], 0 if file else 1, maximum)
                or not source_service._sha(entry['sha256'], 64)
                or not isinstance(entry['product_b64'], str)
                or len(entry['product_b64']) > 4 * ((maximum + 2) // 3)
                or (file and entry['before_sha256'] is not None
                    and not source_service._sha(entry['before_sha256'], 64))):
            raise ValueError('invalid product bytes')
        raw = base64.b64decode(entry['product_b64'], validate=True)
        if (base64.b64encode(raw).decode('ascii') != entry['product_b64']
                or len(raw) != entry['size_bytes'] or hashlib.sha256(raw).hexdigest() != entry['sha256']):
            raise ValueError('product hash mismatch')
        return raw

    total = 0
    for file in files:
        total += len(read(file, FILE_LIMIT, True))
        if total > TOTAL_LIMIT:
            raise ValueError('product too large')
    receipts = closed(output['receipts'], RECEIPTS)
    docs = {}
    for name, path in RECEIPTS.items():
        if not isinstance(receipts[name], dict) or receipts[name].get('path') != path:
            raise ValueError('receipt path mismatch')
        docs[name] = json.loads(read(receipts[name], RECEIPT_LIMIT),
                               object_pairs_hook=source_service._closed_pairs)
    binding = closed(docs['source_binding'], BINDING)
    edit = closed(docs['edit'], META | {'base_sha', 'result_sha', 'changed_paths', 'before', 'after'})
    accept = closed(docs['accept'], META | {'argv', 'rc', 'verdict', 'log_sha256'})
    final = closed(docs['final'], BINDING | {'schema_version', 'run_id', 'leaf_id', 'fencing_token',
        'plan_sha256', 'stage_receipt_digests', 'calls', 'acceptance', 'owned_files_before',
        'owned_files_after', 'evidence_kind'})
    if (binding['requested_sha'] != output['source_ref'] or binding['initial_head'] != output['source_ref']
            or not source_service._sha(binding['fleet_runtime_sha'], 40)
            or any(final[k] != binding[k] for k in BINDING)
            or not counter(final['schema_version'], 1, 1)
            or any(final[k] != output[k] for k in ('run_id', 'leaf_id', 'fencing_token'))
            or not counter(final['fencing_token'], 1) or final['evidence_kind'] != 'native_cli'
            or any(edit[k] != output[k] for k in ('base_sha', 'result_sha', 'changed_paths'))):
        raise ValueError('receipt identity mismatch')
    bound = binding['bound_input']
    if bound is not None:
        closed(bound, {'path', 'sha256'})
        if (not isinstance(bound['path'], str) or len(bound['path']) > 240
                or not re.fullmatch(r'[A-Za-z0-9._/-]+', bound['path'])
                or any(p in ('', '.', '..') for p in bound['path'].split('/'))
                or bound['path'] in paths or not source_service._sha(bound['sha256'], 64)):
            raise ValueError('invalid bound input')
    for document, stage in ((edit, 'edit'), (accept, 'accept')):
        if (document['run_id'] != output['run_id'] or document['milestone_id'] != 'pair-' + stage
                or not counter(document['attempt'], 1) or document['evidence_kind'] != 'native_cli'
                or not isinstance(document['requested_model'], str) or not document['requested_model']
                or document['observed_model'] != document['requested_model']):
            raise ValueError('invalid stage producer')
    stages = closed(final['stage_receipt_digests'], {'findings/pair/plan.json', RECEIPTS['edit'], RECEIPTS['accept']})
    if (any(not source_service._sha(v, 64) for v in stages.values())
            or stages[RECEIPTS['edit']] != receipts['edit']['sha256']
            or stages[RECEIPTS['accept']] != receipts['accept']['sha256']
            or final['plan_sha256'] != stages['findings/pair/plan.json']
            or not source_service._sha(accept['log_sha256'], 64)
            or not counter(accept['rc'], 0, 0) or accept['verdict'] != 'accept'
            or final['acceptance'] != accept
            or (task is not None and accept['argv'] != verify_argv(task['verify_command']))):
        raise ValueError('invalid build evidence')
    calls = final['calls']
    if not isinstance(calls, list) or len(calls) != 3:
        raise ValueError('invalid calls')
    for call in calls:
        closed(call, META)
        if not counter(call['attempt'], 1):
            raise ValueError('invalid call attempt')
    if (calls[1] != {k: edit[k] for k in META} or calls[2] != {k: accept[k] for k in META}
            or calls[0]['run_id'] != output['run_id'] or calls[0]['milestone_id'] != 'pair-plan'
            or calls[0]['evidence_kind'] != 'native_cli'
            or calls[0]['requested_model'] != accept['requested_model']
            or calls[0]['observed_model'] != accept['requested_model']):
        raise ValueError('invalid producer calls')
    before, after = closed(edit['before'], paths), closed(edit['after'], paths)
    changed = output['changed_paths']
    if (not isinstance(changed, list) or any(not isinstance(p, str) for p in changed)
            or len(set(changed)) != len(changed) or not set(changed) <= set(paths)
            or set(changed) != {p for p in paths if before[p] != after[p]}
            or final['owned_files_before'] != before or final['owned_files_after'] != after
            or any(f['sha256'] != after[f['path']] or f['before_sha256'] != before[f['path']] for f in files)):
        raise ValueError('invalid file evidence')
    return docs


def authority(scope, actor):
    from project_repository_edit_contract import RepositoryAuthorityKey
    from project_repository_edit_coordination import RepositoryEditCoordinationState
    identity = (scope['tenant_id'], str(scope['org']), str(scope['project']))
    value = source_service.platform_link.platform_store().resolve_project_repository_authority(*identity)
    closed(value, source_service._AUTHORITY)
    if (any(not source_service._uuid(v) for v in value.values())
            or tuple(value[k] for k in ('tenant_id', 'organization_id', 'project_id')) != identity):
        raise source_service.SourceConflict('source authority conflicts')
    RepositoryEditCoordinationState()._require_writer(actor, RepositoryAuthorityKey(**value))
    return value


def export(enrollment, execution, body, subject, scope, task, attempt):
    from leaf_platform import campaign_product_publication as publication
    wire = task_wire(task)
    with execution._cursor() as cur:
        actor = publication.principal(cur, scope)
    auth = authority(scope, actor)
    args = (scope['org'], scope['project'], scope['campaign'], str(attempt['attempt_id']))
    recorded = execution.read_attempt_sources(*args)['input_source']
    identity = (scope['tenant_id'], str(scope['org']), str(scope['project']))
    head = source_service.initialize_project_source(*identity, scope['prompt'])
    if (not isinstance(head, dict) or not source_service._sha(head.get('source_commit'), 40)
            or not source_service._sha(head.get('source_tree'), 40)
            or head.get('seed_digest') != hashlib.sha256(scope['prompt'].encode()).hexdigest()
            or (recorded and (recorded['repository_id'] != auth['repo_key']
                or recorded['commit_sha'] != head['source_commit'] or recorded['tree_sha'] != head['source_tree']))):
        raise source_service.SourceConflict('source conflicts with recorded input')
    bundle = source_service.export_project_source_bundle(*identity, head['source_commit'],
                                                         head['source_tree'], max_bytes=SOURCE_LIMIT)
    raw = bundle['bundle']
    if (not isinstance(raw, bytes) or not 1 <= len(raw) <= SOURCE_LIMIT
            or bundle['size_bytes'] != len(raw) or type(bundle['size_bytes']) is not int
            or bundle['bundle_sha256'] != hashlib.sha256(raw).hexdigest()
            or bundle['source_commit'] != head['source_commit'] or bundle['source_tree'] != head['source_tree']):
        raise source_service.SourceConflict('source bundle conflicts')
    with execution._cursor() as cur:
        current = enrollment.resolve_worker_scope(cur, body['enrollment_id'], subject)
        if current != scope or publication.principal(cur, current) != actor:
            execution._conflict('source_authority_conflict')
    execution.bind_attempt_input_source(*args, fence=attempt['fence'], repository_id=auth['repo_key'],
        commit_sha=head['source_commit'], tree_sha=head['source_tree'],
        bundle_sha256=bundle['bundle_sha256'], bundle_bytes=len(raw))
    return dict(ok=True, enrollment_id=body['enrollment_id'], scope=dict(org_id=str(scope['org']),
        project_id=str(scope['project']), campaign_id=str(scope['campaign']), machine_id=scope['machine_id']),
        task=wire, attempt=dict(attempt_id=str(attempt['attempt_id']), fence=attempt['fence'], stage='implementation'),
        source=dict(source_commit=head['source_commit'], source_tree=head['source_tree'],
                    seed_digest=head['seed_digest'], repository_key=auth['repo_key'],
                    bundle_sha256=bundle['bundle_sha256'], size_bytes=len(raw),
                    bundle_b64=base64.b64encode(raw).decode('ascii')))


def _post(op, body):
    base = os.environ.get('LEAF_AUTHOR_HARNESS_URL', '').strip().rstrip('/')
    headers = broker_client.harness_headers()
    if not base or not headers.get('X-Harness-Secret'):
        raise source_service.SourceUnavailable('source integration unavailable')
    raw = json.dumps(body, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(raw) > PRODUCT_LIMIT:
        raise ValueError('product request too large')
    response = None
    try:
        response = requests.post(base + '/internal/project-repository-source/' + op, data=raw,
            headers=dict(headers, **{'X-Tenant-Id': body['tenant_id'], 'Content-Type': 'application/json',
                                    'Accept-Encoding': 'identity'}),
            timeout=(10, 60), stream=True, allow_redirects=False)
        if response.status_code == 409:
            raise source_service.SourceConflict('source integration conflicts')
        if response.status_code != 200 or response.headers.get('Content-Encoding', 'identity') != 'identity':
            raise source_service.SourceUnavailable('source integration unavailable')
        length = response.headers.get('Content-Length')
        if length is not None and (not length.isdecimal() or int(length) > PRODUCT_LIMIT):
            raise source_service.SourceUnavailable('source response too large')
        data = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            if len(data) + len(chunk) > PRODUCT_LIMIT:
                raise source_service.SourceUnavailable('source response too large')
            data.extend(chunk)
        if length is not None and len(data) != int(length):
            raise source_service.SourceUnavailable('source response incomplete')
        return json.loads(data, object_pairs_hook=source_service._closed_pairs)
    except (requests.RequestException, ValueError):
        raise source_service.SourceUnavailable('source integration unavailable') from None
    finally:
        if response is not None:
            response.close()


def _edit_row(edit_id):
    from leaf_platform import db, repository_edit_store
    with db.connection() as conn:
        row = conn.execute('SELECT * FROM project_repository_edits WHERE edit_id=%s',
                           (uuid.UUID(edit_id),)).fetchone()
        return repository_edit_store._mapping(row)


def publish(body, subject):
    from leaf_platform import campaign_enrollment as enrollment, campaign_execution as execution
    from leaf_platform import campaign_product_publication as publication, repository_edit_store
    from project_repository_edit_contract import parse_staged_receipt, staged_receipt_digest
    from project_repository_edit_contract import parse_confirmation, CONFIRMATION_CONTRACT
    with execution._cursor() as cur:
        scope = enrollment.resolve_worker_scope(cur, body['enrollment_id'], subject)
        if scope['machine_id'] not in enrollment.allowed_machines():
            execution._conflict('product_identity_mismatch')
        actor = publication.principal(cur, scope)
        task, attempt, _, saved = publication.saved_context(cur, scope, body['task_id'])
    if any(body[k] != saved[k] for k in ('task_id', 'attempt_id', 'fence', 'result_fingerprint')):
        execution._conflict('product_identity_mismatch')
    try:
        validate_output(body['output'], task, saved)
    except (ValueError, TypeError, KeyError):
        execution._conflict('product_identity_mismatch')
    auth = authority(scope, actor)
    args = (scope['org'], scope['project'], scope['campaign'], saved['attempt_id'])
    sources = execution.read_attempt_sources(*args)
    if sources['input_source']['repository_id'] != auth['repo_key']:
        execution._conflict('source_authority_conflict')
    if sources['result_source']:
        return publication.settle_publication(body['enrollment_id'], subject, body['task_id'], saved)
    output = body['output']
    publication.settle_build(body['enrollment_id'], subject, body['task_id'], saved,
        accept_sha256=output['receipts']['accept']['sha256'], artifact_ref=output['receipts']['accept']['key'])
    name = ':'.join(str(v) for v in (scope['campaign'], task['task_id'], attempt['attempt_id'], saved['result_fingerprint']))
    edit_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'leaf.campaign.product:' + name))
    key = lambda action: str(uuid.uuid5(uuid.UUID(edit_id), action))
    common = dict(auth, edit_id=edit_id, actor_binding_id=actor)
    def recheck():
        with execution._cursor() as cur:
            current = enrollment.resolve_worker_scope(cur, body['enrollment_id'], subject)
            if (current != scope or current['machine_id'] not in enrollment.allowed_machines()
                    or publication.principal(cur, current) != actor):
                execution._conflict('source_authority_conflict')
        if authority(scope, actor) != auth:
            execution._conflict('source_authority_conflict')

    row = _edit_row(edit_id)
    if row is None:
        recheck()
        staged = _post('stage', dict(common, expected_base_commit=saved['source_ref'],
            instruction_digest=saved['result_fingerprint'], idempotency_key=key('stage'),
            commit_message='Campaign product ' + task['task_key'], files=output['files']))
        closed(staged, {'witness', 'receipt', 'receiptDigest', 'version'})
        receipt = parse_staged_receipt(staged['receipt'])
        if (staged_receipt_digest(receipt) != staged['receiptDigest']
                or any(staged['receipt'].get(k) != v for k, v in common.items())
                or receipt.base_commit != saved['source_ref']
                or receipt.instruction_digest != saved['result_fingerprint']
                or receipt.idempotency_key != key('stage')
                or set(receipt.changed_paths) != set(output['changed_paths'])
                or not counter(staged['version'], 1)):
            execution._conflict('publication_evidence_mismatch')
        row = _edit_row(edit_id)
        if row is None or row['receipt_digest'] != staged['receiptDigest'] or row['version'] != staged['version']:
            execution._conflict('publication_evidence_mismatch')
    if (row is None or any(row.get(k) != v for k, v in common.items())
            or row['base_commit'] != saved['source_ref']
            or row['instruction_digest'] != saved['result_fingerprint']
            or row['idempotency_key'] != key('stage')):
        execution._conflict('publication_evidence_mismatch')
    if row['state'] == 'staged':
        row = repository_edit_store.await_confirmation(edit_id, expected_version=row['version'],
                                                       transition_key=key('await'))
    if row['state'] == 'awaiting_confirmation':
        recheck()
        now = datetime.now(timezone.utc)
        confirmation = parse_confirmation(dict(auth, contract=CONFIRMATION_CONTRACT,
            confirmation_id=key('confirmation'), receipt_digest=row['receipt_digest'],
            approver_binding_id=actor, edit_id=edit_id, writer_lease_id=row['writer_lease_id'],
            writer_lease_generation=row['writer_lease_generation'], staged_tree=row['staged_tree'],
            issued_at=now.isoformat(), expires_at=(now + timedelta(minutes=30)).isoformat()))
        repository_edit_store.put_confirmation(confirmation, expected_edit_version=row['version'],
                                                transition_key=key('confirmation'))
        _post('publish', dict(common, confirmation_id=key('confirmation'),
              receipt_digest=row['receipt_digest'], expected_version=row['version'], transition_key=key('publish')))
        row = _edit_row(edit_id)
    if row['state'] == 'publishing':
        recheck()
        _post('recover', dict(common, expected_main_commit=row['expected_main_commit'],
              staged_head_commit=row['staged_head_commit'], staged_tree=row['staged_tree'],
              expected_version=row['version'], transition_key=key('recover'), reason_code='campaign_publication_retry'))
        row = _edit_row(edit_id)
    if (row['state'] != 'published' or row['observed_private_ref_commit'] != row['staged_head_commit']
            or row['observed_main_commit'] != row['staged_head_commit']
            or row['observed_main_tree'] != row['staged_tree']):
        execution._conflict('publication_not_settled')
    # The P8 producer has committed its CAS observation. L2 binds that durable proof
    # to the original implementation attempt, independent of its former lease.
    proof = dict(acceptance=publication.ACCEPTANCE, source=publication.source_evidence(saved), edit_id=edit_id,
        receipt_digest=row['receipt_digest'], source_commit=row['observed_main_commit'],
        source_tree=row['observed_main_tree'], version=row['version'])
    recheck()
    execution.record_attempt_result_source(*args, fence=saved['fence'], repository_id=auth['repo_key'],
        commit_sha=proof['source_commit'], tree_sha=proof['source_tree'], publication_receipt=proof)
    return publication.settle_publication(body['enrollment_id'], subject, body['task_id'], saved)
