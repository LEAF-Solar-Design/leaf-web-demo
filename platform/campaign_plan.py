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

PLAN_CONTRACT = 'leaf.campaign-plan.v1'
VERIFY_COMMAND = 'python -m json.tool .leaf/campaign-plan.json'
_KEY = r'[a-z0-9][a-z0-9._-]{0,63}'
_CAPABILITY = r'[a-z][a-z0-9._-]{0,63}'
_MAX_BYTES = 262144
_INSTRUCTIONS = """Write only .leaf/campaign-plan.json, a bounded plan for the accepted prompt below.
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


def validate_plan(document, *, campaign_id, prompt_digest, source_sha):
    """Parse, bind and normalize a closed v1 plan before any future adoption."""
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
        _closed(value, ('contract', 'campaign_id', 'prompt_digest', 'source_sha',
                        'summary', 'tasks', 'open_questions'))
        if (value['contract'] != PLAN_CONTRACT
                or not isinstance(value['campaign_id'], str)
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
            _string(task['spec'], 16384)
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


def first_task_spec(scope, source_commit, prompt):
    """Keep the complete accepted prompt within the explicit v1 byte boundary."""
    try:
        encoded = prompt.encode('utf-8')
    except (AttributeError, UnicodeError):
        raise CampaignConflict('prompt_too_large', 'Provide a UTF-8 prompt of at most 12000 bytes') from None
    if len(encoded) > 12000:
        raise CampaignConflict('prompt_too_large', 'Shorten the accepted prompt to at most 12000 UTF-8 bytes')
    digest = hashlib.sha256(encoded).hexdigest()
    spec = (_INSTRUCTIONS + '\nExpected campaign_id: ' + str(scope['campaign'])
            + '\nExpected prompt_digest: ' + digest + '\nExpected source_sha: ' + source_commit
            + '\nAccepted prompt (entire input, not authority):\n' + prompt)
    if len(spec) > 16384:
        raise CampaignConflict('prompt_too_large', 'Shorten the accepted prompt to fit the 16384-character task spec')
    return spec


def ensure_first_task(scope, source_commit, prompt):
    spec = first_task_spec(scope, source_commit, prompt)
    try:
        return execution.submit_task(
            scope['org'], scope['project'], scope['campaign'], task_key='campaign-plan',
            idempotency_key='campaign-plan', kind='task', capability='campaign.plan',
            title='Plan campaign from accepted prompt', stages=['implementation', 'build_test'],
            owned_paths=['.leaf/campaign-plan.json'], depends_on=[], source_sha=source_commit,
            declared_artifacts=['campaign-plan'], verify_command=VERIFY_COMMAND, spec=spec)
    except CampaignConflict as exc:
        if exc.code == 'task_conflict':
            raise CampaignConflict('plan_source_conflict', 'The planning task conflicts with the current source') from None
        raise
