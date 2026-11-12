"""Read-only customer boundary around the private repository bundle producer."""
from __future__ import annotations

import hashlib
import re
from copy import deepcopy

import browser_project_flow as flow
import project_repository_source as source

MAX_BUNDLE_BYTES = 64 * 1024 * 1024
_WITNESSES = ('source_commit', 'source_tree', 'receipt_digest', 'version')


def parse_query(pairs):
    values = {}
    for key, value in pairs:
        if key not in _WITNESSES or key in values:
            raise flow.FlowError(422)
        values[key] = value
    if (set(values) != set(_WITNESSES)
            or not flow._sha(values['source_commit'], 40)
            or not flow._sha(values['source_tree'], 40)
            or not flow._sha(values['receipt_digest'])
            or re.fullmatch(r'[1-9][0-9]{0,9}', values['version']) is None
            or int(values['version']) > 2147483647):
        raise flow.FlowError(422)
    values['version'] = int(values['version'])
    return values


def export_bundle(tenant, project_id, request_id, query_pairs):
    current = flow.scope(tenant, project_id)
    principal = (str(tenant), tenant.subject)
    witnesses = parse_query(query_pairs)
    try:
        flow._uuid(request_id)
    except (ValueError, TypeError, AttributeError):
        raise flow.FlowError(422) from None
    authority = deepcopy(flow.repository(current))
    if authority is None:
        raise flow.FlowError(409)
    def read_candidate():
        try:
            return flow.read(tenant, project_id, request_id)
        except flow.FlowError as exc:
            if exc.status == 404:
                raise flow.FlowError(409) from None
            raise

    row = deepcopy(read_candidate())
    if (row.get('state') != 'published' or row.get('project_id') != project_id
            or row.get('source_request_id') != request_id
            or type(row.get('version')) is not int
            or any(row.get(key) != witnesses[key] for key in _WITNESSES)):
        raise flow.FlowError(409)

    def recheck():
        if ((str(tenant), tenant.subject) != principal or flow.scope(tenant, project_id) != current
                or flow.repository(current) != authority):
            raise flow.FlowError(403)
        if read_candidate() != row:
            raise flow.FlowError(409)
        if ((str(tenant), tenant.subject) != principal or flow.scope(tenant, project_id) != current
                or flow.repository(current) != authority):
            raise flow.FlowError(403)

    recheck()
    try:
        result = source.export_project_source_bundle(
            authority['tenant_id'], authority['organization_id'], authority['project_id'],
            row['source_commit'], row['source_tree'], max_bytes=MAX_BUNDLE_BYTES)
    except source.SourceConflict:
        raise flow.FlowError(409) from None
    except source.SourceUnavailable:
        raise flow.FlowError(503) from None
    if type(result) is not dict:
        raise flow.FlowError(503)
    bundle = result.get('bundle')
    if (type(bundle) is not bytes or not 1 <= len(bundle) <= MAX_BUNDLE_BYTES
            or not bundle.startswith((b'# v2 git bundle\n', b'# v3 git bundle\n'))
            or type(result.get('size_bytes')) is not int or result['size_bytes'] != len(bundle)
            or not flow._sha(result.get('bundle_sha256'))
            or hashlib.sha256(bundle).hexdigest() != result['bundle_sha256']
            or any(result.get(key) != row[key] for key in ('source_commit', 'source_tree'))):
        raise flow.FlowError(503)
    recheck()
    return bundle, {
        'Content-Disposition': f'attachment; filename="project-source-{row["source_commit"]}.bundle"',
        'Content-Length': str(len(bundle)),
        'X-Leaf-Source-Commit': row['source_commit'],
        'X-Leaf-Source-Tree': row['source_tree'],
        'X-Leaf-Bundle-Sha256': result['bundle_sha256'],
        'X-Leaf-Source-Receipt-Digest': row['receipt_digest'],
        'X-Leaf-Source-Version': str(row['version']),
        'Cache-Control': 'private, no-store',
        'X-Content-Type-Options': 'nosniff',
    }
