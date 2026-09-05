"""Authenticated project campaign admission and durable single-use Q&A."""
from __future__ import annotations

import importlib.util
import re
import sys
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

import deps
import platform_link

router = APIRouter()
_STORE = None
_EXECUTION = None


def set_execution_store(obj):
    global _EXECUTION
    _EXECUTION = obj


def _execution_store():
    if _EXECUTION is not None:
        return _EXECUTION
    _store()
    from leaf_platform import campaign_execution
    return campaign_execution


def set_store(obj):
    global _STORE
    _STORE = obj


def _store():
    if _STORE is not None:
        return _STORE
    if 'leaf_platform' not in sys.modules:
        pkg_dir = Path(__file__).resolve().parent.parent.parent / 'platform'
        spec = importlib.util.spec_from_file_location(
            'leaf_platform', pkg_dir / '__init__.py', submodule_search_locations=[str(pkg_dir)])
        mod = importlib.util.module_from_spec(spec)
        sys.modules['leaf_platform'] = mod
        spec.loader.exec_module(mod)
    from leaf_platform import campaigns
    return campaigns


def _failure(status, code, message):
    return JSONResponse(status_code=status, content={'ok': False, 'error': {
        'error_code': code, 'message': message, 'retryable': status >= 500}})


def _text(value, name, maximum):
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or '\x00' in value:
        raise ValueError(f'{name} must contain 1 to {maximum} characters')
    return value


def _id(value):
    return str(uuid.UUID(_text(value, 'id', 36)))


async def _body(request):
    try:
        body = await request.json()
    except (ValueError, UnicodeError) as exc:
        raise ValueError('body must be a JSON object') from exc
    if not isinstance(body, dict):
        raise ValueError('body must be a JSON object')
    return body


def _dispatch(row):
    result = dict(row)
    if result.get('dispatch_ref') is None:
        result['dispatch'] = {'available': False, 'action': 'mount-fleet-adapter'}
    return result


def _execute(tenant, project_id, operation, key, *, created=False, project=_dispatch):
    try:
        store = _store()
    except Exception:
        return _failure(503, 'campaigns_unavailable', 'campaign store is unavailable')
    try:
        org_id = platform_link.require_project_access(tenant, project_id, write=True)
        result = operation(store, org_id)
        if result is None:
            return _failure(404, 'project_unavailable', 'project is unavailable')
        status = 201 if created and not result.get('replayed', False) else 200
        projected = [project(row) for row in result] if isinstance(result, list) else project(result)
        return JSONResponse(status_code=status, content={'ok': True, key: projected})
    except platform_link.ProjectSessionForbidden:
        return _failure(403, 'forbidden', 'project role does not permit access')
    except LookupError:
        return _failure(404, 'project_unavailable', 'project is unavailable')
    except store.CampaignConflict as exc:
        return _failure(409, exc.code, str(exc))
    except store.CampaignUnavailable as exc:
        if exc.code == 'project_unavailable':
            return _failure(404, 'project_unavailable', 'project is unavailable')
        return _failure(503, 'campaigns_unavailable', 'campaign store is unavailable')
    except store.CampaignError as exc:
        return _failure(400, exc.code, str(exc))
    except Exception:
        return _failure(503, 'campaigns_unavailable', 'campaign store is unavailable')


def _principal(tenant):
    binding = platform_link.resolve_caller_binding(tenant)
    if binding is None:
        raise platform_link.ProjectSessionForbidden('identity binding is unavailable')
    return binding.binding_id


@router.post('/api/campaigns')
async def submit(request: Request, tenant: Any = Depends(deps.require_tenant)):
    try:
        body = await _body(request)
        project = _id(body.get('project_id'))
        title = _text(body.get('title'), 'title', 200)
        prompt = _text(body.get('prompt'), 'prompt', 32768)
        key = _text(request.headers.get('Idempotency-Key'), 'Idempotency-Key', 128)
    except ValueError as exc:
        return _failure(400, 'invalid_request', str(exc))
    return _execute(tenant, project, lambda store, org: store.submit_campaign(
        org, project, str(getattr(tenant, 'tenant_id', tenant)), _principal(tenant),
        title=title, prompt=prompt, idempotency_key=key), 'campaign', created=True)


@router.get('/api/campaigns')
def list_campaigns(request: Request, tenant: Any = Depends(deps.require_tenant)):
    try:
        project = _id(request.query_params.get('project_id'))
        limit = max(1, min(200, int(request.query_params.get('limit', '50'))))
    except ValueError as exc:
        return _failure(400, 'invalid_request', str(exc))
    return _execute(tenant, project, lambda store, org: store.list_campaigns(org, project, limit), 'campaigns')


@router.get('/api/campaigns/{campaign_id}')
def get_campaign(campaign_id: str, request: Request, tenant: Any = Depends(deps.require_tenant)):
    try:
        project = _id(request.query_params.get('project_id'))
        campaign_id = _id(campaign_id)
    except ValueError as exc:
        return _failure(400, 'invalid_request', str(exc))
    return _execute(tenant, project, lambda store, org: store.get_campaign(org, project, campaign_id), 'campaign')


def _project(snapshot):
    fields = {
        'tasks': ('tasks', ('task_id', 'task_key', 'title', 'kind', 'status', 'stages',
                           'current_stage', 'depends_on', 'blocked_by_questions', 'created_at', 'updated_at')),
        'questions': ('pending_questions', ('question_id', 'question_key', 'prompt', 'options',
                                            'status', 'blocks_dispatch', 'task_ids', 'created_at')),
        'receipts': ('receipts', ('receipt_id', 'task_id', 'stage', 'outcome', 'verified',
                                 'created_at', 'reconciles_receipt_id')),
        'events': ('events', ('event_id', 'task_id', 'event_type', 'created_at')),
    }
    return {name: [{key: row[key] for key in keys if key in row} for row in snapshot.get(source, [])]
            for name, (source, keys) in fields.items()}


@router.get('/api/campaigns/{campaign_id}/execution')
def execution(campaign_id: str, request: Request, tenant: Any = Depends(deps.require_tenant)):
    try:
        project, campaign_id = _id(request.query_params.get('project_id')), _id(campaign_id)
        limit = max(1, min(200, int(request.query_params.get('limit', '50'))))
    except ValueError as exc:
        return _failure(400, 'invalid_request', str(exc))
    return _execute(tenant, project, lambda store, org: _project(
        _execution_store().read_execution(org, project, campaign_id, limit=limit)),
        'execution', project=lambda row: row)


@router.post('/api/campaigns/{campaign_id}/questions')
async def ask(campaign_id: str, request: Request, tenant: Any = Depends(deps.require_tenant)):
    try:
        body = await _body(request)
        project, campaign_id = _id(body.get('project_id')), _id(campaign_id)
        key = _text(body.get('question_key'), 'question_key', 128)
        prompt = _text(body.get('prompt'), 'prompt', 4096)
        options, blocks = body.get('options'), body.get('blocks_dispatch', True)
        if re.fullmatch(r'[A-Za-z0-9._-]+', key) is None:
            raise ValueError('invalid question key')
        if options is not None and (not isinstance(options, list) or len(options) > 16
                                    or any(not isinstance(item, str) for item in options)):
            raise ValueError('options must be at most 16 strings')
        if not isinstance(blocks, bool):
            raise ValueError('blocks_dispatch must be a boolean')
    except ValueError as exc:
        return _failure(400, 'invalid_request', str(exc))
    return _execute(tenant, project, lambda store, org: store.ask_question(
        org, project, campaign_id, question_key=key, prompt=prompt, options=options,
        asked_by='operator', blocks_dispatch=blocks), 'question', created=True)


@router.get('/api/campaigns/{campaign_id}/questions')
def questions(campaign_id: str, request: Request, tenant: Any = Depends(deps.require_tenant)):
    try:
        project, campaign_id = _id(request.query_params.get('project_id')), _id(campaign_id)
    except ValueError as exc:
        return _failure(400, 'invalid_request', str(exc))
    return _execute(tenant, project, lambda store, org: store.list_questions(org, project, campaign_id), 'questions')


@router.post('/api/campaigns/{campaign_id}/questions/{question_id}/answer')
async def answer(campaign_id: str, question_id: str, request: Request,
                 tenant: Any = Depends(deps.require_tenant)):
    try:
        body = await _body(request)
        project = _id(body.get('project_id'))
        campaign_id, question_id = _id(campaign_id), _id(question_id)
        value = _text(body.get('answer'), 'answer', 8192)
    except ValueError as exc:
        return _failure(400, 'invalid_request', str(exc))
    return _execute(tenant, project, lambda store, org: store.answer_question(
        org, project, campaign_id, question_id, _principal(tenant), answer=value), 'answer', created=True)
