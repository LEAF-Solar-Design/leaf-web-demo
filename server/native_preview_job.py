"""Dormant native preview contract. Importing this module registers no executor.

Executors are trusted in-process adapters, never client-selected code. Synthetic
receipts exercise custody and leasing only; they are not simulator proof.
"""
from __future__ import annotations

import copy
import math
import time

import platform_link

platform_link._ensure_platform_package()
import project_preview_source as source

SCHEMA = 'leaf.native-preview-job.v1'
REMOTE_SCHEMA = 'leaf.native-preview-job.v2'
OPERATION = 'internal.native-preview'
RESULT_SCHEMA = 'leaf.native-preview-result.v1'
_executor = None
_OWNER = object()


def register_executor(executor):
    """Explicit trusted bootstrap/test injection; None disables future execution."""
    if executor is not None and not callable(executor):
        raise ValueError('Native executor must be callable.')
    global _executor
    _executor = executor


def require_executor():
    if _executor is None:
        raise ValueError('Native preview executor is unavailable.')
    return _executor


def validate_context(value):
    remote = type(value) is dict and value.get('schema') == REMOTE_SCHEMA
    fields = {'schema', 'tenant_id', 'org_id', 'project_id', 'actor_id', 'source'}
    if remote:
        fields |= {'dispatch_kind', 'worker_binding', 'budget_s'}
    if type(value) is not dict or set(value) != fields:
        raise ValueError('Invalid native context.')
    if type(value['schema']) is not str or value['schema'] not in (SCHEMA, REMOTE_SCHEMA):
        raise ValueError('Invalid native schema.')
    for key in ('tenant_id', 'org_id', 'project_id', 'actor_id'):
        if type(value[key]) is not str:
            raise ValueError('Invalid native scope.')
        source._uuid(value[key])
    # Browser project tenants are canonical platform org UUIDs (platform_link),
    # not an actor subject, a workspace slug, or an inferred user equivalence.
    if value['tenant_id'] != value['org_id']:
        raise ValueError('Native tenant and organization conflict.')
    if remote:
        from native_preview_remote import validate_binding
        binding = validate_binding(value['worker_binding'])
        if (value['dispatch_kind'] != 'remote-worker'
                or any(binding[k] != value[k] for k in ('org_id', 'project_id'))
                or type(value['budget_s']) not in (int, float)
                or not math.isfinite(value['budget_s']) or not 0 < value['budget_s'] <= 3600):
            raise ValueError('Invalid remote native context.')
    return {**value, 'source': source._descriptor(
        value['source'], value['org_id'], value['project_id'])}


def execution_context(execution):
    if type(execution) is not dict:
        raise ValueError('Invalid durable native execution.')
    if 'native_preview' not in execution and execution.get('operation') != OPERATION:
        return None
    if set(execution) != {'operation', 'native_preview'} or execution['operation'] != OPERATION:
        raise ValueError('Conflicting native execution kinds.')
    return validate_context(execution['native_preview'])


def record_context(execution, record):
    context = execution_context(execution)
    if context is None and record.get('tool') == OPERATION:
        raise ValueError('Missing durable native context.')
    if context is not None and (record.get('tool') != OPERATION or any(
            record.get(key) != context[key] for key in ('tenant_id', 'org_id', 'project_id'))):
        raise ValueError('Durable native scope mismatch.')
    return context


def read_source(context):
    context = validate_context(context)
    return source.read_source(context['org_id'], context['project_id'],
                              context['actor_id'], context['source'])


def validate_result(context, job_id, worker_id, attempt, value):
    context = validate_context(context)
    if type(job_id) is not str:
        raise ValueError('Invalid native job identity.')
    source._uuid(job_id)
    if type(worker_id) is not str or not 1 <= len(worker_id) <= 128:
        raise ValueError('Invalid native worker.')
    if type(attempt) is not int or attempt < 1:
        raise ValueError('Invalid native attempt.')
    expected = {'schema': RESULT_SCHEMA, 'target': 'ios-simulator',
                'job_id': job_id, 'worker_id': worker_id, 'attempt': attempt,
                'archive_sha256': context['source']['archive_sha256']}
    if (type(value) is not dict or set(value) != set(expected) | {'receipt'}
            or any(type(value[k]) is not type(v) or value[k] != v for k, v in expected.items())):
        raise ValueError('Native result identity mismatch.')
    receipt = value['receipt']
    if type(receipt) is not dict or set(receipt) != {'kind', 'receipt_id', 'sha256'}:
        raise ValueError('Invalid native receipt.')
    if type(receipt['kind']) is not str or receipt['kind'] not in ('synthetic', 'simulator'):
        raise ValueError('Invalid native evidence kind.')
    if context['schema'] == REMOTE_SCHEMA and receipt['kind'] != 'simulator':
        raise ValueError('Remote preview requires worker-reported simulator evidence.')
    if type(receipt['receipt_id']) is not str:
        raise ValueError('Invalid native receipt identity.')
    source._uuid(receipt['receipt_id'])
    source._digest(receipt['sha256'])
    # Opaque receipt identity/digest only: no paths, URLs, logs, or source bytes.
    return copy.deepcopy(value)


def guard(heartbeat, cancelled, deadline):
    if (not math.isfinite(deadline) or time.monotonic() >= deadline
            or cancelled() or not heartbeat() or cancelled()
            or time.monotonic() >= deadline):
        raise ValueError('Native attempt no longer owns its execution budget.')


def run(job_id, worker_id, attempt, context, heartbeat, cancelled, deadline):
    executor = require_executor()
    context = validate_context(context)
    if context['schema'] == REMOTE_SCHEMA:
        raise ValueError('Remote native jobs cannot use a local executor.')
    guard(heartbeat, cancelled, deadline)
    raw = read_source(context)
    guard(heartbeat, cancelled, deadline)
    result = executor(job_id=job_id, worker_id=worker_id, attempt=attempt,
                      source_bytes=raw, descriptor=copy.deepcopy(context['source']),
                      heartbeat=heartbeat, cancelled=cancelled, deadline=deadline)
    guard(heartbeat, cancelled, deadline)
    result = validate_result(context, job_id, worker_id, attempt, result)
    read_source(context)
    guard(heartbeat, cancelled, deadline)
    return result
