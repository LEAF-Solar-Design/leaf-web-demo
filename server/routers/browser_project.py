"""Authenticated, bounded public Browser source routes."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

import browser_project_flow as flow
import deps

router = APIRouter(prefix='/api/projects/{project_id}/browser/source')
_PATH = re.compile(r'^/api/projects/[^/]+/browser/source(?:/.*)?$')
_PRIVATE = {'Cache-Control': 'private, no-store'}


async def no_store_responses(request, call_next):
    response = await call_next(request)
    if _PATH.fullmatch(request.url.path):
        if response.status_code >= 400:
            status = response.status_code
            status = 403 if status == 401 else (status if status in flow._ERRORS else 503)
            return _failure(flow.FlowError(status))
        response.headers['Cache-Control'] = 'private, no-store'
    return response


def _response(value, status=200):
    raw = flow.canonical(value)
    if len(raw) > flow.MAX_RESPONSE_BYTES:
        raise flow.FlowError(503)
    return JSONResponse(value, status_code=status, headers=_PRIVATE)


def _failure(exc):
    return _response({'error': {'code': exc.code, 'message': str(exc)}}, exc.status)


async def _raw(request):
    # Authorization has already run. Never let a JSON model consume the body first.
    encodings = request.headers.getlist('content-encoding')
    if len(encodings) > 1 or (encodings and encodings[0].lower() != 'identity'):
        raise flow.FlowError(422)
    if request.headers.get('content-type', '').lower().strip() not in (
            'application/json', 'application/json; charset=utf-8'):
        raise flow.FlowError(422)
    lengths = request.headers.getlist('content-length')
    declared = None
    if lengths:
        if len(lengths) != 1 or not re.fullmatch(r'[0-9]+', lengths[0]) or len(lengths[0]) > 10:
            raise flow.FlowError(422)
        declared = int(lengths[0])
        if declared > flow.MAX_INPUT_BYTES:
            raise flow.FlowError(413)
    transfer = request.headers.getlist('transfer-encoding')
    if transfer and (len(transfer) != 1 or transfer[0].lower() != 'chunked' or declared is not None):
        raise flow.FlowError(422)
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > flow.MAX_INPUT_BYTES:
            raise flow.FlowError(413)
        chunks.append(chunk)
    if declared is not None and total != declared:
        raise flow.FlowError(422)
    return b''.join(chunks)


async def _get(tenant, project_id, edit_id=None):
    try:
        value = await run_in_threadpool(flow.read, tenant, project_id, edit_id)
        return _response(value)
    except flow.FlowError as exc:
        return _failure(exc)
    except Exception:
        return _failure(flow.FlowError(503))


@router.get('/requests')
async def list_requests(project_id: str, tenant=Depends(deps.require_active_tenant)):
    return await _get(tenant, project_id)


@router.get('/requests/{source_request_id}')
async def get_request(project_id: str, source_request_id: str, tenant=Depends(deps.require_active_tenant)):
    return await _get(tenant, project_id, source_request_id)


async def _post(project_id, request, tenant, action, edit_id=None):
    try:
        # Deny revoked/project-foreign callers before reading even one body chunk.
        current = await run_in_threadpool(flow.scope, tenant, project_id, write=True)
        session_id = request.headers.get('x-authority-session-id')
        turn_id = request.headers.get('x-authority-turn-id')
        held = await run_in_threadpool(flow.inspection.authority, tenant, session_id, session_id, turn_id)
        if tuple(map(str, held[:3])) != current:
            raise flow.FlowError(403)
        body = flow.parse_request(await _raw(request), action)
        value = await run_in_threadpool(flow.mutate, tenant, project_id, session_id, turn_id, action, edit_id, body)
        # Finishing authority can promote queued intent. Re-resolve the current
        # repository mapping and durable candidate before returning source state.
        value = await run_in_threadpool(flow.read, tenant, project_id,
                                       body['source_request_id'] if action == 'request' else edit_id)
        # The winning call has ended its authority lifetime. Project access must
        # still be current before its private result leaves the server.
        if await run_in_threadpool(flow.scope, tenant, project_id, write=True) != current:
            raise flow.FlowError(403)
        return _response(value)
    except flow.inspection.InspectionError:
        return _failure(flow.FlowError(403))
    except flow.FlowError as exc:
        return _failure(exc)
    except Exception:
        return _failure(flow.FlowError(503))


@router.post('/requests')
async def create_request(project_id: str, request: Request, tenant=Depends(deps.require_active_tenant)):
    return await _post(project_id, request, tenant, 'request')


@router.post('/requests/{source_request_id}/confirm')
async def confirm_request(project_id: str, source_request_id: str, request: Request,
                          tenant=Depends(deps.require_active_tenant)):
    return await _post(project_id, request, tenant, 'confirm', source_request_id)


@router.post('/requests/{source_request_id}/recover')
async def recover_request(project_id: str, source_request_id: str, request: Request,
                          tenant=Depends(deps.require_active_tenant)):
    return await _post(project_id, request, tenant, 'recover', source_request_id)


@router.post('/requests/{source_request_id}/reject')
async def reject_request(project_id: str, source_request_id: str, request: Request,
                         tenant=Depends(deps.require_active_tenant)):
    return await _post(project_id, request, tenant, 'reject', source_request_id)
