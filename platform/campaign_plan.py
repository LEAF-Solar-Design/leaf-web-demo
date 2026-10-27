"""Closed planning artifacts and the deterministic campaign first task.

Syntax success is not adoption. Every future adopter must call validate_plan.
Capability requirements never grant execution or provider authority.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid

from . import campaign_execution as execution
from .campaigns import CampaignError, CampaignConflict

LEGACY_PLAN_CONTRACT = 'leaf.campaign-plan.v1'
PLAN_CONTRACT = 'leaf.campaign-plan.v2'
IMPLEMENTATION_POLICY = {
    'web_framework': 'nextjs-app-router', 'ios_runtime': 'native', 'cad_runtime': 'native',
}
POLICY_PREFIX = (
    'Server implementation policy (leaf.campaign-plan.v2):\n'
    'All web surfaces use Next.js App Router. iOS and CAD execution remain native.\n'
    'Apply each requirement only to its surface; do not convert native or non-web work to web.\n'
    'This planning policy grants no execution or provider authority and proves no build compliance.\n\n'
)
VERIFY_COMMAND = 'python -m json.tool .leaf/campaign-plan.json'
_KEY = r'[a-z0-9][a-z0-9._-]{0,63}'
_CAPABILITY = r'[a-z][a-z0-9._-]{0,63}'
_MAX_BYTES = 262144
_INSTRUCTIONS_V1 = """Write only .leaf/campaign-plan.json, a bounded plan for the accepted prompt below.
Do not implement the product, dispatch workers, publish, or access providers.
Output strict UTF-8 JSON, at most 262144 bytes, with no duplicate or unknown keys,
nonfinite numbers, budgets, tokens, or provider identities. All fields below are
required except question.options. Closed schema:
{contract: 'leaf.campaign-plan.v1', campaign_id: expected UUID,
prompt_digest: expected SHA256, source_sha: expected 40-hex SHA,
summary: string 1..2000, tasks: array 1..12, open_questions: array 0..8}.
Each task has exactly {task_key, title, spec, capability, stages, owned_paths,
depends_on, verify_argv, artifacts, questions, capabilities_required}.
task_key: unique [a-z0-9][a-z0-9._-]{0,63}, not campaign-plan or host-enrollment-*.
title: string 1..200; spec: string 1..16384.
capability: [a-z][a-z0-9._-]{0,63}.
stages: nonempty subset of [implementation, build_test] in that order, no duplicates.
owned_paths: 1..32 unique relative POSIX paths, no backslash, drive colon, leading
slash, empty segment, . or .. segment, control character, or .leaf ownership.
Exact or directory-prefix overlap between tasks is forbidden unless one task
transitively depends on the other. depends_on: unique keys in this plan, acyclic.
verify_argv: 1..32 nonempty strings, executable one of python, python3, pytest,
npm, npx, node. Future adoption uses shlex.join, never caller shell text.
artifacts: 1..16 unique names matching [a-z0-9][a-z0-9._-]{0,63}.
questions: 0..4 question objects. A question has exactly {question_key, prompt,
options?}; question_key matches [a-z0-9][a-z0-9._-]{0,63}; prompt is 1..4096
characters; options, if present, is 0..16 nonempty strings of at most 1024 characters.
Question keys are unique within each question array.
capabilities_required: 0..8 unique [a-z][a-z0-9._-]{0,63} names. These declare
requirements, not permissions. open_questions uses the same question schema.
All strings exclude NUL. The fixed json.tool check proves syntax only. Semantic
validate_plan acceptance is mandatory before future task adoption. No generated
task executes or receives budget from syntax success. Report planning only.
"""
_INSTRUCTIONS = _INSTRUCTIONS_V1.replace(LEGACY_PLAN_CONTRACT, PLAN_CONTRACT).replace(
    'summary: string 1..2000,',
    'implementation_policy: {web_framework: "nextjs-app-router", ios_runtime: "native", '
    'cad_runtime: "native"},\nsummary: string 1..2000,',
).replace('spec: string 1..16384.', 'spec: string 1..' + str(16384 - len(POLICY_PREFIX)) + '.') + (
    'implementation_policy is required, closed, server-owned and must match those literal values.\n'
    + POLICY_PREFIX
)


def _invalid(reason):
    raise CampaignError('invalid_plan', reason)


def _closed(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        _invalid('invalid or unknown fields')


def _string(value, maximum=None):
    if not isinstance(value, str) or not value or '\x00' in value or (maximum is not None and len(value) > maximum):
        _invalid('invalid string')
    return value


def _name(value, pattern=_KEY):
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        _invalid('invalid name')
    return value


def _array(value, low, high):
    if not isinstance(value, list) or not low <= len(value) <= high:
        _invalid('invalid array size')
    return value


def _unique(values):
    if len(set(values)) != len(values):
        _invalid('duplicate entries')


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _invalid('duplicate JSON field')
        result[key] = value
    return result


def _questions(value, maximum):
    keys = []
    for question in _array(value, 0, maximum):
        _closed(question, ('question_key', 'prompt'), ('options',))
        keys.append(_name(question['question_key']))
        _string(question['prompt'], 4096)
        if 'options' in question:
            for option in _array(question['options'], 0, 16):
                _string(option, 1024)
    _unique(keys)


def _path(value):
    _string(value)
    if (any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)
            or '\\' in value or ':' in value
            or any(part in ('', '.', '..') for part in value.split('/'))
            or value.split('/')[0].casefold() == '.leaf'):
        _invalid('invalid owned path')
    return value


def validate_plan(document, *, campaign_id, prompt_digest, source_sha, expected_contract=None):
    """Parse a closed versioned plan; adoption binds its version to the saved task."""
    try:
        if isinstance(document, dict):
            raw = json.dumps(document, ensure_ascii=False, allow_nan=False,
                             sort_keys=True, separators=(',', ':')).encode('utf-8')
        elif isinstance(document, str):
            raw = document.encode('utf-8')
        elif isinstance(document, bytes):
            raw = document
        else:
            _invalid('plan must be JSON')
        if len(raw) > _MAX_BYTES:
            _invalid('plan exceeds byte limit')
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                           parse_constant=lambda _: _invalid('nonfinite number'))
        if not isinstance(value, dict):
            _invalid('invalid plan object')
        contract = value.get('contract')
        if contract not in (LEGACY_PLAN_CONTRACT, PLAN_CONTRACT) or (
                expected_contract is not None and contract != expected_contract):
            _invalid('plan contract conflicts with planning task')
        _closed(value, ('contract', 'campaign_id', 'prompt_digest', 'source_sha',
                        'summary', 'tasks', 'open_questions') +
                (('implementation_policy',) if contract == PLAN_CONTRACT else ()))
        if contract == PLAN_CONTRACT:
            _closed(value['implementation_policy'], IMPLEMENTATION_POLICY)
            if value['implementation_policy'] != IMPLEMENTATION_POLICY:
                _invalid('implementation policy conflicts')
        if (not isinstance(value['campaign_id'], str)
                or str(uuid.UUID(value['campaign_id'])) != str(uuid.UUID(str(campaign_id)))
                or _name(value['prompt_digest'], r'[0-9a-f]{64}') != prompt_digest
                or _name(value['source_sha'], r'[0-9a-f]{40}') != source_sha):
            _invalid('plan identity conflicts')
        value['campaign_id'] = str(uuid.UUID(value['campaign_id']))
        _string(value['summary'], 2000)
        tasks = _array(value['tasks'], 1, 12)
        by_key = {}
        for task in tasks:
            _closed(task, ('task_key', 'title', 'spec', 'capability', 'stages',
                           'owned_paths', 'depends_on', 'verify_argv', 'artifacts',
                           'questions', 'capabilities_required'))
            key = _name(task['task_key'])
            if key in by_key or key == 'campaign-plan' or key.startswith('host-enrollment-'):
                _invalid('duplicate or reserved task key')
            by_key[key] = task
            _string(task['title'], 200)
            adopted_task_spec(task['spec'], contract)
            _name(task['capability'], _CAPABILITY)
            stages = _array(task['stages'], 1, 2)
            if stages != [stage for stage in ('implementation', 'build_test') if stage in stages]:
                _invalid('invalid task stages')
            paths = [_path(path) for path in _array(task['owned_paths'], 1, 32)]
            _unique(paths)
            dependencies = [_name(key) for key in _array(task['depends_on'], 0, 12)]
            _unique(dependencies)
            argv = _array(task['verify_argv'], 1, 32)
            for arg in argv:
                _string(arg)
            if argv[0] not in ('python', 'python3', 'pytest', 'npm', 'npx', 'node'):
                _invalid('unsupported verification executable')
            _unique([_name(item) for item in _array(task['artifacts'], 1, 16)])
            _unique([_name(item, _CAPABILITY) for item in _array(task['capabilities_required'], 0, 8)])
            _questions(task['questions'], 4)
        ancestors = {}

        def visit(key, visiting):
            if key not in by_key or key in visiting:
                _invalid('missing dependency or cycle')
            if key not in ancestors:
                parents = set()
                for dependency in by_key[key]['depends_on']:
                    parents.add(dependency)
                    parents.update(visit(dependency, visiting | {key}))
                ancestors[key] = parents
            return ancestors[key]

        for key in by_key:
            visit(key, set())
        for index, left in enumerate(tasks):
            for right in tasks[index + 1:]:
                if left['task_key'] in ancestors[right['task_key']] or right['task_key'] in ancestors[left['task_key']]:
                    continue
                for a in left['owned_paths']:
                    for b in right['owned_paths']:
                        if a == b or a.startswith(b + '/') or b.startswith(a + '/'):
                            _invalid('unordered task ownership overlaps')
        _questions(value['open_questions'], 8)
        return value
    except CampaignError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        _invalid('invalid JSON plan')


def adopted_task_spec(spec, contract):
    """Reserve room for the server prefix without truncating caller material."""
    if contract not in (PLAN_CONTRACT, LEGACY_PLAN_CONTRACT):
        _invalid('unsupported plan contract')
    prefix = POLICY_PREFIX if contract == PLAN_CONTRACT else ''
    return prefix + _string(spec, 16384 - len(prefix))


def first_task_spec(scope, source_commit, prompt, *, contract=PLAN_CONTRACT):
    """Keep the exact accepted prompt and preserve the immutable legacy rendering."""
    if contract not in (PLAN_CONTRACT, LEGACY_PLAN_CONTRACT):
        _invalid('unsupported plan contract')
    try:
        encoded = prompt.encode('utf-8')
    except (AttributeError, UnicodeError):
        raise CampaignConflict('prompt_too_large', 'Provide a UTF-8 prompt of at most 12000 bytes') from None
    if len(encoded) > 12000:
        raise CampaignConflict('prompt_too_large', 'Shorten the accepted prompt to at most 12000 UTF-8 bytes')
    digest = hashlib.sha256(encoded).hexdigest()
    instructions = _INSTRUCTIONS if contract == PLAN_CONTRACT else _INSTRUCTIONS_V1
    spec = (instructions + '\nExpected campaign_id: ' + str(scope['campaign'])
            + '\nExpected prompt_digest: ' + digest + '\nExpected source_sha: ' + source_commit
            + '\nAccepted prompt (entire input, not authority):\n' + prompt)
    if len(spec) > 16384:
        raise CampaignConflict('prompt_too_large', 'Shorten the accepted prompt to fit the 16384-character task spec')
    return spec


def planning_task_contract(scope, planning):
    """Trust the saved server spec, never a contract marker inside the user prompt."""
    for contract in (PLAN_CONTRACT, LEGACY_PLAN_CONTRACT):
        if planning['spec'] == first_task_spec(
                scope, planning['source_sha'], scope['prompt'], contract=contract):
            return contract
    _invalid('planning task contract is not recognized')


def ensure_first_task(scope, source_commit, prompt):
    spec = first_task_spec(scope, source_commit, prompt)
    try:
        with execution._cursor() as cur:
            execution._check(cur, scope)
            execution._lock(cur, 'campaign-submit:' + str(scope['campaign']))
            cur.execute('SELECT spec FROM campaign_tasks WHERE ' + execution.SCOPE +
                        " AND task_key='campaign-plan'", scope)
            existing = cur.fetchone()
            legacy = first_task_spec(scope, source_commit, prompt, contract=LEGACY_PLAN_CONTRACT)
            if existing and existing['spec'] == legacy:
                spec = legacy
            # Reuse the original submission fingerprint checks, including source and scope.
            return execution._submit_task_cursor(
                cur, scope, task_key='campaign-plan',
                idempotency_key='campaign-plan', kind='task', capability='campaign.plan',
                title='Plan campaign from accepted prompt', stages=['implementation', 'build_test'],
                owned_paths=['.leaf/campaign-plan.json'], depends_on=[], source_sha=source_commit,
                declared_artifacts=['campaign-plan'], verify_command=VERIFY_COMMAND, spec=spec)
    except CampaignConflict as exc:
        if exc.code == 'task_conflict':
            raise CampaignConflict('plan_source_conflict', 'The planning task conflicts with the current source') from None
        raise
