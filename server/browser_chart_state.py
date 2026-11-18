"""Current-source/preview boundary for canonical project chart preferences."""
from __future__ import annotations
from copy import deepcopy
import json
from uuid import UUID
import platform_link
import browser_project_flow as flow
import browser_project_source_export as source_export
import browser_static_build as build

FIELDS = set(source_export._WITNESSES) | {'artifact_id','manifest_sha256'}


def _store():
    platform_link._ensure_platform_package()
    from leaf_platform import browser_chart_state
    return browser_chart_state


def descriptor(request_id, job_id, pairs):
    values = {}
    for key, value in pairs:
        if key in values or key not in FIELDS:
            raise flow.FlowError(422)
        values[key] = value
    if set(values) != FIELDS:
        raise flow.FlowError(422)
    try:
        for value in (request_id, job_id, values['artifact_id']):
            flow._uuid(value)
        if not flow._sha(values['manifest_sha256']):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise flow.FlowError(422) from None
    witnesses = source_export.parse_query([(k,v) for k,v in values.items() if k in source_export._WITNESSES])
    return dict(witnesses, source_request_id=request_id, job_id=job_id,
                artifact_id=values['artifact_id'], manifest_sha256=values['manifest_sha256'])


def parse_save(request_id, job_id, raw):
    try:
        if len(raw) > 32768:
            raise ValueError()
        body = json.loads(raw.decode('utf-8'), object_pairs_hook=flow._pairs, parse_constant=flow._nonfinite)
        if (type(body) is not dict or set(body) != {'descriptor','state','expected_revision','save_id'}
                or type(body['descriptor']) is not dict or type(body['descriptor'].get('version')) is not int
                or type(body['expected_revision']) is not int or not 0 <= body['expected_revision'] < 2147483647):
            raise ValueError()
        flow._uuid(body['save_id'])
        d = descriptor(request_id, job_id, [(k,str(v)) for k,v in body['descriptor'].items()])
        state = _store().state_copy(body['state'])
        return dict(descriptor=d, state=state, expected_revision=body['expected_revision'], save_id=body['save_id'])
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise flow.FlowError(422) from None


def exchange(tenant, project_id, d, *, save=None):
    d, save = deepcopy(d), deepcopy(save)
    write = save is not None
    current = flow.scope(tenant, project_id, write=write)
    principal = (str(tenant), tenant.subject)
    authority = deepcopy(flow.repository(current))
    if authority is None:
        raise flow.FlowError(409)
    row = deepcopy(flow.read(tenant, project_id, d['source_request_id']))
    if (row.get('state') != 'published' or row.get('project_id') != project_id
            or row.get('source_request_id') != d['source_request_id'] or type(row.get('version')) is not int
            or any(row.get(k) != d[k] for k in source_export._WITNESSES)):
        raise flow.FlowError(409)

    def recheck():
        if ((str(tenant), tenant.subject) != principal or flow.scope(tenant,project_id,write=write) != current
                or flow.repository(current) != authority):
            raise flow.FlowError(403)
        if flow.read(tenant,project_id,d['source_request_id']) != row:
            raise flow.FlowError(409)
        if flow.scope(tenant,project_id,write=write) != current:
            raise flow.FlowError(403)

    recheck()
    # Observation is read-only. Never start/reconcile by resubmitting a build.
    observed = build.exchange(tenant,project_id,d['source_request_id'],d['job_id'],
                              {k:d[k] for k in source_export._WITNESSES})
    if (observed.get('status') != 'ready'
            or observed.get('jobId') != d['job_id']
            or observed.get('result',{}).get('preview',{}).get('artifactId') != d['artifact_id']
            or observed.get('result',{}).get('preview',{}).get('manifestSha256') != d['manifest_sha256']):
        raise flow.FlowError(409)
    recheck()
    backend = _store()
    args = (UUID(current[0]), UUID(project_id), UUID(current[2]), d)
    try:
        if write:
            value = backend.save(*args, deepcopy(save['state']), expected_revision=save['expected_revision'],save_id=save['save_id'])
        else:
            value = backend.read(*args)
    except backend.lifecycle.LifecycleForbidden:
        raise flow.FlowError(403) from None
    except backend.lifecycle.LifecycleConflict:
        raise flow.FlowError(409) from None
    except backend.lifecycle.LifecycleUnavailable:
        raise flow.FlowError(503) from None
    except ValueError:
        raise flow.FlowError(422) from None
    value = deepcopy(value)
    recheck()
    if write and value is None:
        raise flow.FlowError(503)
    if value is not None:
        if (type(value) is not dict or set(value) != {'revision','digest','descriptor','state'}
                or type(value['revision']) is not int or not 1 <= value['revision'] <= 2147483647
                or not flow._sha(value['digest']) or value['descriptor'] != d):
            raise flow.FlowError(503)
        try:
            value['state'] = backend.state_copy(value['state'])
        except (ValueError,TypeError,UnicodeError):
            raise flow.FlowError(503) from None
    return {'chart':value}
