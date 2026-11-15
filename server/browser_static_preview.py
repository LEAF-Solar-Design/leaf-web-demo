"""Current-authority static asset reads as inert JSON, never active API-origin code."""
from __future__ import annotations
import base64
import hashlib
import json
import os
import re
from copy import deepcopy
import requests
import broker_client
import browser_project_flow as flow
import browser_project_source_export as source_export
MAX_RESPONSE=3*1024*1024

def asset(tenant,project_id,request_id,job_id,pairs):
    current=flow.scope(tenant,project_id)
    principal=(str(tenant),tenant.subject)
    values={}
    for key,value in pairs:
        if key in values:raise flow.FlowError(422)
        values[key]=value
    extra=('artifact_id','manifest_sha256','path')
    if set(values)!=set(source_export._WITNESSES)|set(extra):raise flow.FlowError(422)
    witnesses=source_export.parse_query([(k,v) for k,v in values.items() if k not in extra])
    try:
        for value in (request_id,job_id,values['artifact_id']):flow._uuid(value)
    except (ValueError,TypeError,AttributeError):raise flow.FlowError(422) from None
    path=values['path']
    if path=='/':path='/index.html'
    if (not flow._sha(values['manifest_sha256']) or len(path)>2048 or
        not re.fullmatch(r'/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+',path) or
        any(part in ('.','..') for part in path.split('/'))):raise flow.FlowError(422)
    authority=deepcopy(flow.repository(current))
    if authority is None:raise flow.FlowError(409)
    row=deepcopy(flow.read(tenant,project_id,request_id))
    if (row.get('state')!='published' or row.get('project_id')!=project_id or row.get('source_request_id')!=request_id or
        type(row.get('version')) is not int or any(row.get(k)!=v for k,v in witnesses.items())):raise flow.FlowError(409)
    def recheck():
        if ((str(tenant),tenant.subject)!=principal or flow.scope(tenant,project_id)!=current or flow.repository(current)!=authority):raise flow.FlowError(403)
        if flow.read(tenant,project_id,request_id)!=row:raise flow.FlowError(409)
        if flow.scope(tenant,project_id)!=current:raise flow.FlowError(403)
    payload=dict(authority,job_id=job_id,source_commit=witnesses['source_commit'],source_tree=witnesses['source_tree'],artifact_id=values['artifact_id'],manifest_sha256=values['manifest_sha256'],path=path)
    url=os.environ.get('LEAF_AUTHOR_HARNESS_URL','').strip().rstrip('/')
    headers=broker_client.harness_headers()
    if not url or not headers.get('X-Harness-Secret'):raise flow.FlowError(503)
    headers=dict(headers,**{'X-Tenant-Id':authority['tenant_id'],'Content-Type':'application/json'})
    recheck()
    try:
        response=requests.post(url+'/internal/project-static-preview/asset',data=flow.canonical(payload),headers=headers,timeout=(5,30),allow_redirects=False,stream=True)
        try:
            if response.status_code!=200:raise flow.FlowError(409 if response.status_code in (400,401,403,404,409) else 503)
            chunks=[];total=0
            for chunk in response.iter_content(65536):
                total+=len(chunk)
                if total>MAX_RESPONSE:raise flow.FlowError(503)
                chunks.append(chunk)
            result=json.loads(b''.join(chunks).decode('utf-8'),object_pairs_hook=flow._pairs,parse_constant=flow._nonfinite)
        finally:response.close()
    except (requests.RequestException,ValueError,UnicodeError,RecursionError):raise flow.FlowError(503) from None
    recheck()
    expected={'schema','job_id','source_commit','source_tree','artifact_id','manifest_sha256','path','content_type','size_bytes','sha256','body_base64'}
    if type(result) is not dict or set(result)!=expected or result['schema']!='leaf.private-static-asset.v1':raise flow.FlowError(503)
    for key in ('job_id','source_commit','source_tree','artifact_id','manifest_sha256','path'):
        if result[key]!=payload[key]:raise flow.FlowError(503)
    try:
        encoded=result['body_base64']
        if not isinstance(encoded,str):raise ValueError()
        body=base64.b64decode(encoded,validate=True)
        if base64.b64encode(body).decode('ascii')!=encoded:raise ValueError()
    except (ValueError,TypeError):raise flow.FlowError(503) from None
    if (len(body)>2*1024*1024 or type(result['size_bytes']) is not int or result['size_bytes']!=len(body) or
        hashlib.sha256(body).hexdigest()!=result['sha256'] or not isinstance(result['content_type'],str) or
        len(result['content_type'])>256 or not re.fullmatch(r'[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+(?:; charset=utf-8)?',result['content_type'])):raise flow.FlowError(503)
    recheck()
    return result
