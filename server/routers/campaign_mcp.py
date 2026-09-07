"""JSON-RPC MCP tools for the same tenant-authorized campaign release service."""
import json

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

import deps
import campaign_release_service as releases
from routers import campaigns

router = APIRouter()
_BASE = {'project_id': {'type': 'string', 'format': 'uuid'},
         'campaign_id': {'type': 'string', 'format': 'uuid'}}
_FINISH = {'type': 'object', 'additionalProperties': False,
           'required': ['delivery_profile', 'intended_user', 'workflow', 'artifact_refs'],
           'properties': {'delivery_profile': {'type': 'string'}, 'intended_user': {'type': 'string'},
                          'workflow': {'type': 'string'},
                          'deadline_at': {'type': 'string', 'format': 'date-time'},
                          'artifact_refs': {'type': 'array', 'maxItems': 32, 'items': {'type': 'string'}}}}


def tools_list():
    tools = []
    for name in ('campaign.finish', 'campaign.release.get', 'campaign.release.pause',
                 'campaign.release.resume', 'campaign.release.cancel',
                 'campaign.release.retry', 'campaign.release.advance'):
        props = dict(_BASE)
        required = ['project_id', 'campaign_id']
        if name == 'campaign.finish':
            props.update(finish=_FINISH, idempotency_key={'type': 'string', 'maxLength': 128},
                         title={'type': 'string'}, prompt={'type': 'string'})
            required = ['project_id', 'finish', 'idempotency_key']
        else:
            props['release_id'] = {'type': 'string', 'format': 'uuid'}
            required.append('release_id')
        if name == 'campaign.release.retry':
            props['stage'] = {'type': 'string', 'enum': list(releases.STAGES)}
            required.append('stage')
        tools.append({'name': name, 'description': 'Authorized campaign completion operation',
                      'inputSchema': {'type': 'object', 'additionalProperties': False,
                                      'properties': props, 'required': required}})
    return tools


def call_tool(tenant, name, args, authority_headers=None):
    authority_headers = authority_headers or {}
    definition = next((t for t in tools_list() if t['name'] == name), None)
    if definition is None:
        raise LookupError('Unknown tool')
    schema = definition['inputSchema']
    if (not isinstance(args, dict) or set(args) - set(schema['properties'])
            or set(schema['required']) - set(args)):
        raise ValueError('Invalid tool arguments')
    project = campaigns._id(args['project_id'])
    if name == 'campaign.finish':
        releases.validate_finish(args['finish'])
        key = campaigns._text(args['idempotency_key'], 'idempotency_key', 128)
        if 'campaign_id' not in args:
            title = campaigns._text(args.get('title'), 'title', 200)
            prompt = campaigns._text(args.get('prompt'), 'prompt', 32768)
            return campaigns._release_call('campaign', campaigns._finish_campaign,
                                           tenant, project, title, prompt, args['finish'], key, **authority_headers)
        if 'title' in args or 'prompt' in args:
            raise ValueError('Existing campaign finish does not accept admission fields')
        campaign = campaigns._id(args['campaign_id'])
        return campaigns._release_call('completion', releases.create, tenant, project,
                                       campaign, args['finish'], key, **authority_headers)
    campaign, release = campaigns._id(args['campaign_id']), campaigns._id(args['release_id'])
    if name == 'campaign.release.get':
        return campaigns._release_call('completion', releases.snapshot, tenant, project, campaign, release)
    if name == 'campaign.release.advance':
        return campaigns._release_call('completion', releases.advance, tenant, project, campaign, release, **authority_headers)
    if name == 'campaign.release.retry':
        return campaigns._release_call('completion', releases.retry, tenant, project, campaign, release, args['stage'])
    actions = {'campaign.release.pause': 'pause', 'campaign.release.resume': 'resume',
               'campaign.release.cancel': 'cancel'}
    return campaigns._release_call('completion', releases.transition, tenant, project, campaign, release, actions[name],
                                   **(authority_headers if name == 'campaign.release.resume' else {}))


def _error(identifier, code, message):
    return JSONResponse({'jsonrpc': '2.0', 'id': identifier, 'error': {'code': code, 'message': message}})


@router.post('/api/mcp/campaigns')
async def mcp(request: Request, tenant=Depends(deps.require_tenant)):
    try:
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 65536:
                return _error(None, -32600, 'Request too large')
        body = json.loads(raw)
    except (ValueError, UnicodeError):
        return _error(None, -32700, 'Parse error')
    if (not isinstance(body, dict) or body.get('jsonrpc') != '2.0'
            or not isinstance(body.get('method'), str)
            or set(body) - {'jsonrpc', 'id', 'method', 'params'}
            or isinstance(body.get('id'), (bool, dict, list))):
        return _error(None, -32600, 'Invalid Request')
    identifier = body.get('id')
    method = body['method']
    params = body.get('params', {})
    if method == 'notifications/initialized' and 'id' not in body:
        return Response(status_code=202)
    if not isinstance(params, dict):
        return _error(identifier, -32602, 'Invalid params')
    if method == 'initialize':
        result = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},
                  'serverInfo': {'name': 'campaign-completion', 'version': '1.0'}}
    elif method == 'tools/list':
        result = {'tools': tools_list()}
    elif method == 'tools/call':
        if set(params) != {'name', 'arguments'}:
            return _error(identifier, -32602, 'Invalid params')
        try:
            output = await run_in_threadpool(call_tool, tenant, params['name'], params['arguments'],
                                             campaigns._release_authority(request))
        except LookupError:
            return _error(identifier, -32602, 'Unknown tool')
        except (ValueError, TypeError, KeyError):
            return _error(identifier, -32602, 'Invalid tool arguments')
        if isinstance(output, JSONResponse):
            output = json.loads(output.body)
        result = {'content': [{'type': 'text', 'text': json.dumps(output)}],
                  'isError': not output.get('ok', False)}
    else:
        return _error(identifier, -32601, 'Method not found')
    if 'id' not in body:
        return Response(status_code=202)
    return {'jsonrpc': '2.0', 'id': identifier, 'result': result}
