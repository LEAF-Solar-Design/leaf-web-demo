"""Authenticated Browser build submission and read-only observation bridge."""
from __future__ import annotations
import hashlib
import json
import os
import re
from copy import deepcopy
import requests
import broker_client
import browser_project_flow as flow
import browser_project_source_export as source_export

MAX_RESPONSE = 2 * 1024 * 1024
_CAMEL = dict(source_request_id='sourceRequestId', version='version', receipt_digest='receiptDigest',
              source_commit='sourceCommit', source_tree='sourceTree')


def parse_body(raw):
    try:
        if len(raw) > 16384:
            raise ValueError()
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=flow._pairs, parse_constant=flow._nonfinite)
        if (type(value) is not dict or set(value) != set(source_export._WITNESSES)
                or type(value['version']) is not int):
            raise ValueError()
        return source_export.parse_query([(k, str(v)) for k, v in value.items()])
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise flow.FlowError(422) from None


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _uuid(value):
    try:
        flow._uuid(value)
    except (ValueError, TypeError, AttributeError):
        raise flow.FlowError(503) from None


def _validate(value, authority, request_id, job_id, witnesses):
    def need(condition):
        if not condition:
            raise flow.FlowError(503)
    need(type(value) is dict and value.get('schema') == 'leaf.private-static-build.v1'
         and value.get('jobId') == job_id and value.get('status') in ('unknown', 'building', 'failed', 'ready'))
    need(set(value) == {'schema', 'jobId', 'status'} | ({'result'} if value['status'] == 'ready' else set()))
    if value['status'] != 'ready':
        return value
    result = value['result']
    candidate = {_CAMEL[k]: v for k, v in dict(witnesses, source_request_id=request_id).items()}
    need(type(result) is dict and set(result) == {'schema', 'candidate', 'preview'}
         and result['schema'] == 'leaf.browser-candidate-preview.v1' and type(result['candidate']) is dict
         and type(result['candidate'].get('version')) is int and result['candidate'] == candidate)
    p = result['preview']
    fields = {'jobId', 'sourceCommit', 'sourceTree', 'bundleSha256', 'bindingId', 'toolchainSha256',
              'artifactBindingId', 'artifactId', 'manifestSha256', 'manifest'}
    need(type(p) is dict and set(p) == fields and p['jobId'] == job_id
         and p['sourceCommit'] == witnesses['source_commit'] and p['sourceTree'] == witnesses['source_tree'])
    for key in ('bindingId', 'artifactBindingId', 'artifactId'):
        _uuid(p[key])
    need(all(flow._sha(p[k]) for k in ('bundleSha256', 'toolchainSha256', 'manifestSha256')))
    m = p['manifest']
    need(type(m) is dict and set(m) == {'requestDigest', 'bundleSha256', 'bindingId', 'toolchainSha256', 'files'})
    need(all(m[k] == p[k] for k in ('bundleSha256', 'bindingId', 'toolchainSha256')))
    need(m['requestDigest'] == _digest([authority[k] for k in ('tenant_id', 'organization_id', 'project_id', 'repo_key')]
         + [job_id, witnesses['source_commit'], witnesses['source_tree']]))
    need(type(m['files']) is list and 1 <= len(m['files']) <= 4096)
    paths, total, files = set(), 0, []
    for f in m['files']:
        need(type(f) is dict and set(f) == {'path', 'sha256', 'sizeBytes', 'contentType'})
        path = f['path']
        need(isinstance(path, str) and len(path) <= 2048
             and re.fullmatch(r'/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+', path)
             and not any(part in ('.', '..') for part in path.split('/')) and path not in paths)
        need(flow._sha(f['sha256']) and type(f['sizeBytes']) is int and 0 <= f['sizeBytes'] <= 2 * 1024 * 1024
             and isinstance(f['contentType'], str) and len(f['contentType']) <= 256
             and re.fullmatch(r'[a-z0-9.+-]+/[a-z0-9.+-]+(?:; charset=utf-8)?', f['contentType'], re.I))
        paths.add(path)
        total += f['sizeBytes']
        files.append([path, f['sha256'], f['sizeBytes'], f['contentType']])
    need('/index.html' in paths and total <= 32 * 1024 * 1024)
    need(p['manifestSha256'] == _digest([m[k] for k in ('requestDigest', 'bundleSha256', 'bindingId', 'toolchainSha256')] + [files]))
    return value


def exchange(tenant, project_id, request_id, job_id, witnesses, *, submit=False):
    current = flow.scope(tenant, project_id, write=submit)
    principal = (str(tenant), tenant.subject)
    try:
        flow._uuid(request_id)
        flow._uuid(job_id)
    except (ValueError, TypeError, AttributeError):
        raise flow.FlowError(422) from None
    authority = deepcopy(flow.repository(current))
    if authority is None:
        raise flow.FlowError(409)
    row = deepcopy(flow.read(tenant, project_id, request_id))
    if (row.get('state') != 'published' or row.get('project_id') != project_id
            or row.get('source_request_id') != request_id or type(row.get('version')) is not int
            or any(row.get(k) != v for k, v in witnesses.items())):
        raise flow.FlowError(409)

    def recheck():
        if ((str(tenant), tenant.subject) != principal or flow.scope(tenant, project_id, write=submit) != current
                or flow.repository(current) != authority):
            raise flow.FlowError(403)
        if flow.read(tenant, project_id, request_id) != row:
            raise flow.FlowError(409)
        if flow.scope(tenant, project_id, write=submit) != current:
            raise flow.FlowError(403)

    payload = dict(authority, source_request_id=request_id, job_id=job_id, **witnesses)
    url = os.environ.get('LEAF_AUTHOR_HARNESS_URL', '').strip().rstrip('/')
    headers = broker_client.harness_headers()
    if not url or not headers.get('X-Harness-Secret'):
        raise flow.FlowError(503)
    headers = dict(headers, **{'X-Tenant-Id': authority['tenant_id'], 'Content-Type': 'application/json'})
    recheck()
    try:
        response = requests.post(url + '/internal/project-static-preview/' + ('build' if submit else 'status'),
            data=flow.canonical(payload), headers=headers, timeout=(5, 20), allow_redirects=False, stream=True)
        try:
            if response.status_code != 200:
                raise flow.FlowError(409 if response.status_code in (400, 401, 403, 404, 409) else 503)
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > MAX_RESPONSE:
                    raise flow.FlowError(503)
                chunks.append(chunk)
            value = json.loads(b''.join(chunks).decode('utf-8'), object_pairs_hook=flow._pairs, parse_constant=flow._nonfinite)
        finally:
            response.close()
    except (requests.RequestException, ValueError, UnicodeError, RecursionError):
        raise flow.FlowError(503) from None
    recheck()
    try:
        return _validate(value, authority, request_id, job_id, witnesses)
    except (KeyError, TypeError, ValueError, RecursionError):
        raise flow.FlowError(503) from None
