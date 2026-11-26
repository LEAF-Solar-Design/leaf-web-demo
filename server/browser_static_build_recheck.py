"""Dedicated App dispatch boundary for rechecking private build admission."""
from __future__ import annotations

import json
import os
import uuid
from functools import partial

import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

import browser_project_flow as flow
import browser_project_source_export as source_export
import deps
import platform_link


PATH = '/internal/project-static-preview/candidate-recheck'
router = APIRouter()
_MAX_BODY = 16384
_LOOKUP = (*flow.AUTHORITY_FIELDS, 'job_id', 'source_commit', 'source_tree')
_PUBLICATION = ('source_request_id', 'version', 'receipt_digest')
_RECORD = (*_LOOKUP, *_PUBLICATION, 'actor_binding_id')
# Member requests can be waiting on the Harness while it calls back into App.
_recheck_slots = anyio.CapacityLimiter(8)


def enabled():
    return os.environ.get('LEAF_STATIC_NEXT_EXECUTOR') == 'enabled'


def _store():
    platform_link._ensure_platform_package()
    from leaf_platform import browser_static_build_admission
    return browser_static_build_admission


def _require_role(current, *, write):
    platform_link._ensure_platform_package()
    from leaf_platform import project_lifecycle
    try:
        project_lifecycle.require_project_role(*(uuid.UUID(value) for value in current), write=write)
    except (project_lifecycle.LifecycleUnavailable, project_lifecycle.LifecycleForbidden):
        raise flow.FlowError(403) from None


def _parse(raw, tenant_id):
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=flow._pairs, parse_constant=flow._nonfinite)
        if type(value) is not dict:
            raise ValueError()
        mode = value.pop('operation')
        if mode not in ('read', 'write'):
            raise ValueError()
        complete = set(value) == {*_LOOKUP, *_PUBLICATION}
        if not complete and set(value) != set(_LOOKUP):
            raise ValueError()
        for key in (*flow.AUTHORITY_FIELDS, 'job_id'):
            flow._uuid(value[key])
        if not all(flow._sha(value[key], 40) for key in ('source_commit', 'source_tree')):
            raise ValueError()
        if complete:
            flow._uuid(value['source_request_id'])
            if type(value['version']) is not int or not flow._sha(value['receipt_digest']):
                raise ValueError()
            source_export.parse_query([(key, str(value[key])) for key in source_export._WITNESSES])
    except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
        raise flow.FlowError(422) from None
    if value['tenant_id'] != tenant_id or value['organization_id'] != tenant_id:
        raise flow.FlowError(403)
    return value, mode == 'write'


def admit(authority, job_id, actor_binding_id, source_request_id, witnesses):
    """Called only from the App's already-authorized member submission."""
    record = dict(authority, **witnesses, job_id=job_id, actor_binding_id=actor_binding_id,
                  source_request_id=source_request_id)
    try:
        admitted = _store().admit(record)
    except Exception:
        raise flow.FlowError(503) from None
    if not admitted:
        raise flow.FlowError(409)


def _recheck(expected, *, write):
    # Imported here because the Browser submission calls admit() in this module.
    from browser_static_build import _execution_digest

    try:
        record = _store().find(*(expected[key] for key in ('tenant_id', 'project_id', 'job_id')))
        if record is None or set(record) != set(_RECORD) or any(record[k] != v for k, v in expected.items()):
            raise flow.FlowError(404)
        authority = {key: record[key] for key in flow.AUTHORITY_FIELDS}
        current = tuple(record[key] for key in ('organization_id', 'project_id', 'actor_binding_id'))
        for value in current:
            flow._uuid(value)
        _require_role(current, write=write)
        if flow.repository(current) != authority:
            raise flow.FlowError(403)
        row = flow._read(authority, record['source_request_id'])
        if row is None:
            raise flow.FlowError(404)
        flow._membership(row, authority, current, flow.WEB_PROFILE)
        published = flow.representation(row, authority)
        candidate_fields = ('source_request_id', *source_export._WITNESSES)
        if (published['state'] != 'published' or published['project_id'] != authority['project_id']
                or any(published[key] != record[key] for key in candidate_fields)):
            raise flow.FlowError(409)
        if flow.repository(current) != authority:
            raise flow.FlowError(403)
        _require_role(current, write=write)
        return dict(authority, schema='leaf.browser-static-candidate-recheck.v1',
                    job_id=record['job_id'], execution_id=_execution_digest(authority, record['job_id'], record),
                    **{key: record[key] for key in candidate_fields})
    except flow.FlowError:
        raise
    except Exception:
        raise flow.FlowError(503) from None


@router.post(PATH, include_in_schema=False)
async def candidate_recheck(request: Request):
    if not enabled() or request.scope.get('raw_path', PATH.encode()) != PATH.encode():
        raise HTTPException(404)
    dispatch = request.headers.getlist('x-dispatch-secret')
    tenants = request.headers.getlist('x-tenant-id')
    if len(dispatch) != 1 or len(tenants) != 1 or not deps._dispatch_secret_ok(dispatch[0]):
        raise HTTPException(403)
    try:
        tenant_id = flow._uuid(tenants[0])
    except ValueError:
        raise HTTPException(403) from None
    try:
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > _MAX_BODY:
                raise flow.FlowError(422)
            raw.extend(chunk)
        expected, write = _parse(raw, tenant_id)
        result = await anyio.to_thread.run_sync(partial(_recheck, expected, write=write), limiter=_recheck_slots)
        return JSONResponse(result, headers={'Cache-Control': 'private, no-store'})
    except flow.FlowError as error:
        raise HTTPException(error.status) from None
