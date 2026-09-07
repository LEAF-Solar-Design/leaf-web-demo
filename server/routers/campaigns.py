"""Authenticated project campaign admission and durable single-use Q&A."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

import deps
import platform_link
import project_repository_source
import campaign_worker_service
import campaign_bridge
import campaign_capability_api

router = APIRouter()
_STORE = None
_EXECUTION = None
_ENROLLMENT = None


def set_enrollment_store(obj):
    global _ENROLLMENT
    _ENROLLMENT = obj


def _enrollment_store():
    if _ENROLLMENT is not None:
        return _ENROLLMENT
    _store()
    from leaf_platform import campaign_enrollment
    return campaign_enrollment


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
    except project_repository_source.SourceConflict:
        return _failure(409, 'source_conflict', 'project source conflicts')
    except project_repository_source.SourceUnavailable:
        return _failure(503, 'source_unavailable', 'project source is unavailable')
    except LookupError:
        return _failure(404, 'project_unavailable', 'project is unavailable')
    except store.CampaignConflict as exc:
        return _failure(409, exc.code, 'Campaign request conflicts')
    except store.CampaignUnavailable as exc:
        if exc.code == 'project_unavailable':
            return _failure(404, 'project_unavailable', 'project is unavailable')
        if exc.code in ('source_unavailable', 'worker_unavailable'):
            return _failure(503, exc.code, 'Project source is unavailable' if exc.code == 'source_unavailable'
                            else 'Campaign worker is unavailable')
        return _failure(503, 'campaigns_unavailable', 'campaign store is unavailable')
    except store.CampaignError as exc:
        return _failure(400, exc.code, 'Invalid campaign request')
    except Exception:
        return _failure(503, 'campaigns_unavailable', 'campaign store is unavailable')


_COMPLETION_UNAVAILABLE = object()


def _completion(org, project, campaign_id):
    """Best-effort completion projection for a legacy read.

    Uses the already-authorized ``org`` and the raw store's ``release_snapshot``
    directly, skipping campaign_release_service's own redundant caller-binding
    resolution. A genuine authorization failure still propagates; any other
    failure (the completion projection being unavailable) is reported as the
    sentinel so the caller can omit the field rather than fake an empty state
    or 503 an otherwise-successful legacy read.
    """
    import campaign_release_service as releases
    try:
        return releases._store().release_snapshot(org, project, campaign_id)
    except platform_link.ProjectSessionForbidden:
        raise
    except Exception:
        return _COMPLETION_UNAVAILABLE


def _principal(tenant):
    binding = platform_link.resolve_caller_binding(tenant)
    if binding is None:
        raise platform_link.ProjectSessionForbidden('identity binding is unavailable')
    return binding.binding_id


@router.get('/api/campaigns/{campaign_id}/enrollments')
def enrollments(campaign_id: str, request: Request, tenant: Any = Depends(deps.require_tenant)):
    try:
        project = _canonical_id(request.query_params.get('project_id'))
        campaign_id = _canonical_id(campaign_id)
    except ValueError:
        return _failure(400, 'invalid_request', 'Invalid campaign request')
    return _capability_call('enrollment', campaign_capability_api.enrollment_snapshot,
                            tenant, project, campaign_id)


@router.post('/api/campaigns/{campaign_id}/enrollments')
async def enroll(campaign_id: str, request: Request, tenant: Any = Depends(deps.require_tenant)):
    try:
        body = await _body(request)
        if 'capability' in body and set(body) != {'project_id', 'machine_id', 'capability'}:
            raise ValueError('Invalid fields')
        capability = body.get('capability', 'campaign.host-enrollment')
        if capability not in ('campaign.host-enrollment', 'campaign.native-release'):
            raise ValueError('Invalid capability')
        project, campaign_id = _id(body.get('project_id')), _id(campaign_id)
        machine = _text(body.get('machine_id'), 'machine_id', 200)
    except ValueError:
        return _failure(400, 'invalid_request', 'Invalid campaign request')
    return _execute(tenant, project, lambda store, org: _enrollment_store().request_enrollment(
        org, project, campaign_id, _principal(tenant), machine_id=machine,
        **({'capability': capability} if 'capability' in body else {})),
        'enrollment', created=True, project=lambda row: row)


def _capability_call(key, function, *args):
    try:
        return {'ok': True, key: function(*args)}
    except campaign_capability_api.CapabilityError as exc:
        return _failure(exc.status, exc.code, 'Campaign capability request failed')
    except Exception:
        return _failure(503, 'capability_unavailable', 'Campaign capability request failed')


def _canonical_id(value):
    if not isinstance(value, str) or _id(value) != value:
        raise ValueError('Invalid id')
    return value


async def _capability_body(request, fields):
    length = request.headers.get('content-length')
    if length is not None and (not length.isdecimal() or int(length) > 4096):
        raise ValueError('Invalid size')
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 4096:
            raise ValueError('Invalid size')
        raw.extend(chunk)
    body = json.loads(raw, object_pairs_hook=project_repository_source._closed_pairs)
    if not isinstance(body, dict) or set(body) != set(fields):
        raise ValueError('Invalid fields')
    return body


@router.get('/api/campaigns/{campaign_id}/capabilities')
def capabilities(campaign_id: str, request: Request, tenant: Any = Depends(deps.require_tenant)):
    try:
        project = _canonical_id(request.query_params.get('project_id'))
        campaign_id = _canonical_id(campaign_id)
    except ValueError:
        return _failure(400, 'invalid_request', 'Invalid campaign request')
    return _capability_call('capabilities', campaign_capability_api.list_capabilities,
                            tenant, project, campaign_id)


@router.post('/api/campaigns/{campaign_id}/enrollments/{enrollment_id}/publication')
async def publication(campaign_id: str, enrollment_id: str, request: Request,
                      tenant: Any = Depends(deps.require_tenant)):
    try:
        body = await _capability_body(request, ('project_id', 'change_set_id'))
        project = _canonical_id(body['project_id'])
        campaign_id, enrollment_id = _canonical_id(campaign_id), _canonical_id(enrollment_id)
        change = _text(body['change_set_id'], 'change_set_id', 200)
        if not change.isprintable():
            raise ValueError('Invalid change')
    except (ValueError, UnicodeError):
        return _failure(400, 'invalid_request', 'Invalid campaign request')
    return await run_in_threadpool(_capability_call, 'enrollment', campaign_capability_api.bind_publication,
                                   tenant, project, campaign_id, enrollment_id, change)


@router.post('/api/campaigns/{campaign_id}/enrollments/{enrollment_id}/invoke')
async def invoke(campaign_id: str, enrollment_id: str, request: Request,
                 tenant: Any = Depends(deps.require_tenant)):
    try:
        body = await _capability_body(request, ('project_id', 'effective_catalog_digest'))
        project = _canonical_id(body['project_id'])
        campaign_id, enrollment_id = _canonical_id(campaign_id), _canonical_id(enrollment_id)
        digest = body['effective_catalog_digest']
        if not isinstance(digest, str) or re.fullmatch('[0-9a-f]{64}', digest) is None:
            raise ValueError('Invalid digest')
        key = _text(request.headers.get('Idempotency-Key'), 'Idempotency-Key', 128)
        if not key.isprintable():
            raise ValueError('Invalid key')
    except (ValueError, UnicodeError):
        return _failure(400, 'invalid_request', 'Invalid campaign request')
    return await run_in_threadpool(_capability_call, 'invocation', campaign_capability_api.invoke,
                                   tenant, project, campaign_id, enrollment_id, digest, key)


@router.post('/api/campaigns/{campaign_id}/enrollments/{enrollment_id}/{action}')
async def change_enrollment(campaign_id: str, enrollment_id: str, action: str, request: Request,
                            tenant: Any = Depends(deps.require_tenant)):
    try:
        body = await _body(request)
        project = _id(body.get('project_id'))
        campaign_id, enrollment_id = _id(campaign_id), _id(enrollment_id)
        if action not in ('enable', 'revoke'):
            raise ValueError('Unknown enrollment action')
    except ValueError:
        return _failure(400, 'invalid_request', 'Invalid campaign request')
    return _execute(tenant, project, lambda store, org: getattr(
        _enrollment_store(), action + '_enrollment')(
            org, project, campaign_id, enrollment_id, _principal(tenant)),
        'enrollment', project=lambda row: row)


@router.post('/internal/campaigns/bridge/{op}')
async def campaign_bridge_operation(op: str, request: Request,
                                    subject: str = Depends(deps.require_campaign_worker)):
    limit = 6 * 1024 * 1024 if op == 'product' else 512 * 1024 if op == 'plan' else 128 * 1024
    try:
        length = request.headers.get('content-length')
        if length is not None and (not length.isdecimal() or int(length) > limit):
            return _failure(413, 'request_too_large', 'Campaign bridge request failed')
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > limit:
                return _failure(413, 'request_too_large', 'Campaign bridge request failed')
            raw.extend(chunk)
        body = json.loads(raw, object_pairs_hook=project_repository_source._closed_pairs)
        if not isinstance(body, dict):
            raise ValueError('Invalid body')
    except (ValueError, UnicodeError):
        return _failure(400, 'invalid_request', 'Invalid campaign bridge request')
    try:
        _store()
        return campaign_bridge.handle(op, body, subject)
    except campaign_bridge.BridgeError as exc:
        status = exc.status
    except Exception:
        status = 503
    code = {400: 'invalid_request', 403: 'worker_forbidden', 409: 'bridge_conflict',
            413: 'request_too_large', 422: 'invalid_plan'}.get(
        status, 'bridge_unavailable')
    return _failure(status, code, 'Campaign bridge request failed')


@router.post('/internal/campaign-worker/recover')
async def recover_worker(request: Request, subject: str = Depends(deps.require_campaign_worker)):
    try:
        body = await _body(request)
        if set(body) != {'enrollment_id'}:
            raise ValueError('Only enrollment_id is accepted')
        enrollment_id = _id(body.get('enrollment_id'))
    except ValueError:
        return _failure(400, 'invalid_request', 'Invalid campaign request')
    try:
        store = _store()
    except Exception:
        return _failure(503, 'campaigns_unavailable', 'Campaign recovery is unavailable')
    try:
        bindings = _enrollment_store().resolve_worker_enrollment(enrollment_id, subject)
        return {'ok': True, 'pending_remote_bindings': bindings}
    except store.CampaignError as exc:
        if exc.code in ('worker_forbidden', 'project_unavailable'):
            return _failure(403, 'worker_forbidden', 'Campaign worker is not authorized')
        return _failure(503, 'campaigns_unavailable', 'Campaign recovery is unavailable')
    except Exception:
        return _failure(503, 'campaigns_unavailable', 'Campaign recovery is unavailable')


@router.post('/internal/campaign-worker/next')
async def next_worker(request: Request, subject: str = Depends(deps.require_campaign_worker)):
    if os.environ.get('LEAF_CAMPAIGN_FIRST_TASK_PRODUCER', '') != 'on':
        return _failure(503, 'producer_disabled', 'Campaign first-task producer is disabled')
    try:
        body = await _body(request)
        if set(body) != {'enrollment_id'}:
            raise ValueError('Only enrollment_id is accepted')
        enrollment_id = _id(body.get('enrollment_id'))
    except ValueError:
        return _failure(400, 'invalid_request', 'Invalid campaign request')
    try:
        store = _store()
    except Exception:
        return _failure(503, 'campaigns_unavailable', 'Campaign planning is unavailable')
    try:
        return campaign_worker_service.next_work(enrollment_id, subject)
    except project_repository_source.SourceConflict:
        return _failure(409, 'source_conflict', 'Project source conflicts')
    except project_repository_source.SourceUnavailable:
        return _failure(503, 'source_unavailable', 'Project source is unavailable')
    except store.CampaignError as exc:
        if exc.code in ('worker_forbidden', 'project_unavailable'):
            return _failure(403, 'worker_forbidden', 'Campaign worker is not authorized')
        if exc.code in ('plan_source_conflict', 'task_conflict', 'source_conflict'):
            return _failure(409, 'plan_source_conflict', 'Planning task source conflicts')
        if exc.code == 'prompt_too_large':
            return _failure(409, exc.code, 'Shorten the accepted prompt to at most 12000 UTF-8 bytes')
        if exc.code in ('source_unavailable', 'producer_disabled'):
            return _failure(503, exc.code, 'Campaign planning is unavailable')
        if exc.code == 'invalid_request':
            return _failure(400, exc.code, 'Invalid planning request')
        return _failure(503, 'campaigns_unavailable', 'Campaign planning is unavailable')
    except Exception:
        return _failure(503, 'campaigns_unavailable', 'Campaign planning is unavailable')


@router.post('/api/campaigns')
async def submit(request: Request, tenant: Any = Depends(deps.require_tenant)):
    try:
        body = await _body(request)
        project = _id(body.get('project_id'))
        title = _text(body.get('title'), 'title', 200)
        prompt = _text(body.get('prompt'), 'prompt', 32768)
        key = _text(request.headers.get('Idempotency-Key'), 'Idempotency-Key', 128)
        if 'mode' in body or 'finish' in body:
            import campaign_release_service as releases
            if body.get('mode') != 'finish' or set(body) != {'project_id', 'title', 'prompt', 'mode', 'finish'}:
                raise ValueError('Invalid finish request')
            releases.validate_finish(body['finish'])
    except ValueError:
        return _failure(400, 'invalid_request', 'Invalid campaign request')
    if body.get('mode') == 'finish':
        return await run_in_threadpool(_release_call, 'campaign', _finish_campaign,
                                       tenant, project, title, prompt, body['finish'], key)
    def admit(store, org):
        tenant_id = str(getattr(tenant, 'tenant_id', tenant))
        row = store.submit_campaign(org, project, tenant_id, _principal(tenant),
                                    title=title, prompt=prompt, idempotency_key=key)
        if row is not None and os.environ.get('LEAF_PROJECT_SOURCE_PRODUCER', '').strip().lower() == 'on':
            source = project_repository_source.initialize_project_source(
                tenant_id, str(org), project, row['prompt'])
            row = {**row, 'source': source}
        return row
    return _execute(tenant, project, admit, 'campaign', created=True)


def _finish_campaign(tenant, project, title, prompt, finish, key):
    import campaign_release_service as releases
    org, _, actor = releases.authority(tenant, project)
    row = _store().submit_campaign(org, project, str(getattr(tenant, 'tenant_id', tenant)),
                                   actor, title=title, prompt=prompt, idempotency_key=key)
    if row is None:
        raise LookupError('Campaign unavailable')
    completion = releases.create(tenant, project, row['campaign_id'], finish, key)
    return dict(row, completion=completion)


def _release_call(key, function, *args):
    import campaign_delivery_service as delivery
    try:
        return {'ok': True, key: function(*args)}
    except (platform_link.ProjectSessionForbidden, PermissionError):
        return _failure(403, 'forbidden', 'Project access denied')
    except LookupError:
        return _failure(404, 'release_unavailable', 'Release unavailable')
    except delivery.DeliveryConflict:
        return _failure(409, 'release_conflict', 'Artifact or release version conflicts')
    except Exception as exc:
        store = _store()
        if isinstance(exc, store.CampaignConflict):
            return _failure(409, 'release_conflict', 'Release request conflicts')
        if isinstance(exc, store.CampaignUnavailable):
            return _failure(503, 'release_unavailable', 'Release service unavailable')
        if isinstance(exc, (ValueError, TypeError, KeyError)):
            return _failure(400, 'invalid_request', 'Invalid release request')
        return _failure(503, 'release_unavailable', 'Release service unavailable')


@router.get('/api/campaigns/{campaign_id}/releases')
def releases_list(campaign_id: str, request: Request, tenant: Any = Depends(deps.require_tenant)):
    import campaign_release_service as releases
    return _release_call('releases', releases.list_releases, tenant,
                         request.query_params.get('project_id'), campaign_id)


@router.post('/api/campaigns/{campaign_id}/releases')
async def release_create(campaign_id: str, request: Request, tenant: Any = Depends(deps.require_tenant)):
    import campaign_release_service as releases
    try:
        body = await _capability_body(request, ('project_id', 'finish'))
        project, campaign_id = _id(body['project_id']), _id(campaign_id)
        releases.validate_finish(body['finish'])
        key = _text(request.headers.get('Idempotency-Key'), 'Idempotency-Key', 128)
    except (ValueError, UnicodeError):
        return _failure(400, 'invalid_request', 'Invalid release request')
    return await run_in_threadpool(_release_call, 'completion', releases.create,
                                   tenant, project, campaign_id, body['finish'], key)


@router.get('/api/campaigns/{campaign_id}/releases/{release_id}')
def release_get(campaign_id: str, release_id: str, request: Request,
                tenant: Any = Depends(deps.require_tenant)):
    import campaign_release_service as releases
    return _release_call('completion', releases.snapshot, tenant,
                         request.query_params.get('project_id'), campaign_id, release_id)


@router.post('/api/campaigns/{campaign_id}/releases/{release_id}/{action}')
async def release_action(campaign_id: str, release_id: str, action: str, request: Request,
                         tenant: Any = Depends(deps.require_tenant)):
    import campaign_release_service as releases
    try:
        if action not in ('pause', 'resume', 'cancel', 'retry', 'advance'):
            raise ValueError('Invalid action')
        fields = ('project_id', 'stage') if action == 'retry' else ('project_id',)
        body = await _capability_body(request, fields)
        project, campaign_id, release_id = _id(body['project_id']), _id(campaign_id), _id(release_id)
    except (ValueError, UnicodeError):
        return _failure(400, 'invalid_request', 'Invalid release request')
    function = releases.advance if action == 'advance' else releases.retry if action == 'retry' else releases.transition
    args = () if action == 'advance' else (body['stage'],) if action == 'retry' else (action,)
    return await run_in_threadpool(_release_call, 'completion', function,
                                   tenant, project, campaign_id, release_id, *args)


@router.get('/api/campaigns/{campaign_id}/releases/{release_id}/artifacts/{name}')
def release_artifact(campaign_id: str, release_id: str, name: str, request: Request,
                     tenant: Any = Depends(deps.require_tenant)):
    import campaign_release_service as releases
    from urllib.parse import quote
    result = _release_call('artifact', releases.read_artifact, tenant,
                           request.query_params.get('project_id'), campaign_id, release_id, name)
    if isinstance(result, JSONResponse):
        return result
    raw, metadata = result['artifact']
    return Response(content=raw, media_type=metadata['media_type'], headers={
        'Content-Disposition': "attachment; filename*=UTF-8''" + quote(name, safe=''),
        'ETag': '"' + metadata['sha256'] + '"', 'Cache-Control': 'private, no-store'})


@router.get('/api/campaigns')
def list_campaigns(request: Request, tenant: Any = Depends(deps.require_tenant)):
    try:
        project = _id(request.query_params.get('project_id'))
        limit = max(1, min(200, int(request.query_params.get('limit', '50'))))
    except ValueError:
        return _failure(400, 'invalid_request', 'Invalid campaign request')
    return _execute(tenant, project, lambda store, org: store.list_campaigns(org, project, limit), 'campaigns')


@router.get('/api/campaigns/{campaign_id}')
def get_campaign(campaign_id: str, request: Request, tenant: Any = Depends(deps.require_tenant)):
    try:
        project = _id(request.query_params.get('project_id'))
        campaign_id = _id(campaign_id)
    except ValueError:
        return _failure(400, 'invalid_request', 'Invalid campaign request')
    availability = []
    def read(store, org):
        row = store.get_campaign(org, project, campaign_id)
        if row is None:
            return None
        completion = _completion(org, project, campaign_id)
        availability.append(completion is not _COMPLETION_UNAVAILABLE)
        return dict(row, completion=completion) if completion is not _COMPLETION_UNAVAILABLE else row
    response = _execute(tenant, project, read, 'campaign')
    if availability == [False]:
        response.headers['X-Completion-Status'] = 'unavailable'
    return response


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
    except ValueError:
        return _failure(400, 'invalid_request', 'Invalid campaign request')
    availability = []
    def read(store, org):
        result = _project(_execution_store().read_execution(org, project, campaign_id, limit=limit))
        completion = _completion(org, project, campaign_id)
        availability.append(completion is not _COMPLETION_UNAVAILABLE)
        return dict(result, completion=completion) if completion is not _COMPLETION_UNAVAILABLE else result
    response = _execute(tenant, project, read, 'execution', project=lambda row: row)
    if availability == [False]:
        response.headers['X-Completion-Status'] = 'unavailable'
    return response


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
    except ValueError:
        return _failure(400, 'invalid_request', 'Invalid campaign request')
    return _execute(tenant, project, lambda store, org: store.ask_question(
        org, project, campaign_id, question_key=key, prompt=prompt, options=options,
        asked_by='operator', blocks_dispatch=blocks), 'question', created=True)


@router.get('/api/campaigns/{campaign_id}/questions')
def questions(campaign_id: str, request: Request, tenant: Any = Depends(deps.require_tenant)):
    try:
        project, campaign_id = _id(request.query_params.get('project_id')), _id(campaign_id)
    except ValueError:
        return _failure(400, 'invalid_request', 'Invalid campaign request')
    return _execute(tenant, project, lambda store, org: store.list_questions(org, project, campaign_id), 'questions')


@router.post('/api/campaigns/{campaign_id}/questions/{question_id}/answer')
async def answer(campaign_id: str, question_id: str, request: Request,
                 tenant: Any = Depends(deps.require_tenant)):
    try:
        body = await _body(request)
        project = _id(body.get('project_id'))
        campaign_id, question_id = _id(campaign_id), _id(question_id)
        value = _text(body.get('answer'), 'answer', 8192)
    except ValueError:
        return _failure(400, 'invalid_request', 'Invalid campaign request')
    return _execute(tenant, project, lambda store, org: store.answer_question(
        org, project, campaign_id, question_id, _principal(tenant), answer=value), 'answer', created=True)
