"""Managed web recipe delivery within the authoritative campaign release."""
from __future__ import annotations

import hashlib
import uuid
from urllib.parse import quote

from fastapi.responses import Response

import campaign_delivery_service as delivery
import campaign_web_tool_static as static
import campaign_web_tool_producer as producer

WORKFLOW = 'Open the managed records converter, review the source records, convert them, and download the validated CSV file.'
HTML_NAME = 'records-to-csv.html'


def compile_recipe(snapshot, refs):
    candidates = [refs] if refs else [[row['path']] for row in sorted(
        snapshot.get('files', []), key=lambda row: row['path'])
        if not row['path'].startswith('releases/') and row['path'].lower().endswith('.json')]
    for candidate in candidates:
        try:
            source = static.select_source(snapshot, candidate)
            source = {k: v for k, v in source.items() if k not in ('recipe_id', 'recipe_version')}
            raw = delivery.file_bytes(snapshot, source['path'])
            html = static.render(raw)
            static.expected_output(raw)
            if len(html) > delivery.MAX_BYTES:
                raise delivery.DeliveryConflict('Managed tool exceeds the project file limit')
            generated = static.validate_generated(html, raw)
            artifact = {k: v for k, v in generated.items() if k not in ('recipe_id', 'recipe_version')}
            artifact.update(path=HTML_NAME, name=HTML_NAME)
            return artifact, {'recipe_id': static.RECIPE_ID, 'recipe_version': static.RECIPE_VERSION,
                              'source_artifact': source}
        except (ValueError, UnicodeError):
            continue
    return None, None


def prefix(release):
    return 'releases/' + str(uuid.UUID(str(release['release_id']))) + '/v' + str(release['contract_version']) + '/'


def _recipe(release):
    recipe = release['contract'].get('web_recipe')
    if not recipe or recipe['recipe_id'] != static.RECIPE_ID or recipe['recipe_version'] != static.RECIPE_VERSION:
        raise delivery.DeliveryConflict('The selected web recipe is unavailable')
    return recipe


def _source(release):
    return dict(_recipe(release)['source_artifact'], path=prefix(release) + 'source.json', name='source.json')


def _html(release):
    return dict(release['contract']['selected_artifact'], path=prefix(release) + HTML_NAME)


def _input(snapshot, release):
    return delivery.read_verified(snapshot, _source(release))[0]


def read(snapshot, release, name):
    source = _input(snapshot, release)
    if name == HTML_NAME:
        artifact = _html(release)
        raw = delivery.file_bytes(snapshot, artifact['path'])
        static.validate_generated(raw, source)
        if len(raw) != artifact['size_bytes'] or hashlib.sha256(raw).hexdigest() != artifact['sha256']:
            raise delivery.DeliveryConflict('Saved tool version changed')
        if not delivery.receipt_for(snapshot, artifact['path'], artifact['media_type'], artifact['sha256']):
            raise delivery.DeliveryConflict('Matching tool publication receipt is unavailable')
        return raw, dict(artifact, retrieved=True, observed_revision=artifact['sha256'])
    if name == 'records.csv':
        expected = static.expected_output(source)
        artifact = delivery.validate_bytes(prefix(release) + name, expected)
        return delivery.read_verified(snapshot, artifact)
    if name == 'source.json':
        return delivery.read_verified(snapshot, _source(release))
    raise LookupError('Artifact unavailable')


def response(raw, metadata):
    """The route and deployment producer share this exact response builder."""
    html = metadata['media_type'] == 'text/html'
    headers = {'Content-Disposition': ('inline' if html else 'attachment') + "; filename*=UTF-8''" +
               quote(metadata['name'], safe=''), 'ETag': '"' + metadata['sha256'] + '"',
               'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'}
    if html:
        headers['Content-Security-Policy'] = static.CSP
    return Response(content=raw, media_type=metadata['media_type'], headers=headers)


def _put(runtime, tenant, project_id, campaign_id, release, artifact, raw):
    org, project, actor = runtime.authority(tenant, project_id)
    key = 'release-' + runtime._digest([release['release_id'], release['contract_version'],
                                      artifact['path'], artifact['sha256']])
    runtime._store().record_decision(org, project, campaign_id, release['release_id'],
        decision_key=key, kind='external_dependency', decided_by=str(actor),
        payload={'operation': 'project_file_put', 'operation_key': key, 'path': artifact['path'],
                 'source_revision': artifact['sha256']})
    state = runtime._lifecycle().project_snapshot(org, project, actor)
    receipt = delivery.receipt_for(state, artifact['path'], artifact['media_type'], artifact['sha256'])
    if receipt is None:
        runtime.authority(tenant, project_id)
        runtime._lifecycle().put_project_file(org, project, actor, path=artifact['path'],
            media_type=artifact['media_type'], content=raw.decode('utf-8'), idempotency_key=key)
        state = runtime._lifecycle().project_snapshot(org, project, actor)
        receipt = delivery.receipt_for(state, artifact['path'], artifact['media_type'], artifact['sha256'])
    if not receipt or delivery.file_bytes(state, artifact['path']) != raw:
        raise delivery.DeliveryConflict('Published bytes or lifecycle receipt do not match')
    return receipt


def run_stage(runtime, tenant, project_id, campaign_id, completion, stage):
    release = completion['release']
    recipe = _recipe(release)
    org, project, actor = runtime.authority(tenant, project_id)
    state = runtime._lifecycle().project_snapshot(org, project, actor)
    original = recipe['source_artifact']
    html_meta = _html(release)
    if stage in ('implementation', 'publication'):
        # A partial publication may already hold the frozen input, even if the
        # original was subsequently edited. Preserve that successful write.
        if delivery.receipt_for(state, _source(release)['path'], original['media_type'], original['sha256']):
            raw = _input(state, release)
        else:
            raw = delivery.file_bytes(state, original['path'])
            observed = delivery.validate_bytes(original['path'], raw)
            if any(observed[k] != original[k] for k in ('sha256', 'size_bytes', 'media_type')):
                raise delivery.DeliveryConflict('Source records changed before publication')
        html = static.render(raw)
        if hashlib.sha256(html).hexdigest() != html_meta['sha256']:
            raise delivery.DeliveryConflict('Managed recipe version changed')
        if stage == 'implementation':
            return {'artifact': html_meta, 'input_sha256': original['sha256'],
                    'recipe_id': recipe['recipe_id'], 'recipe_version': recipe['recipe_version']}
        receipts = [_put(runtime, tenant, project_id, campaign_id, release, _source(release), raw),
                    _put(runtime, tenant, project_id, campaign_id, release, html_meta, html)]
        return {'artifact': html_meta, 'receipts': receipts}
    html, observed = read(state, release, HTML_NAME)
    if stage == 'deployment':
        served = response(html, observed)
        if served.body != html or served.headers.get('content-security-policy') != static.CSP:
            raise delivery.DeliveryConflict('Artifact response does not serve the selected version')
        return {'observed_revision': hashlib.sha256(served.body).hexdigest(),
                'resource_identity': html_meta['path'] + '@' + html_meta['sha256'],
                'rollback_identity': 'Versioned project files preserve all earlier releases; cancel this release to stop further work.',
                'response_headers': dict(served.headers), 'artifact': observed}
    if stage == 'user_verification':
        proof = producer.verify(html, _input(state, release))
        if proof['source_revision'] != html_meta['sha256']:
            raise producer.WebToolVerificationError('Browser exercised a different version')
        output = proof['output']['content'].encode('utf-8')
        if output != static.expected_output(_input(state, release)):
            raise producer.WebToolVerificationError('Downloaded output does not match source records')
        output_meta = delivery.validate_bytes(prefix(release) + 'records.csv', output)
        receipt = _put(runtime, tenant, project_id, campaign_id, release, output_meta, output)
        return {'workflow': release['contract']['workflow'],
                'observations': proof['observations'] + proof['workflow'],
                'output': output_meta, 'receipt': receipt, 'browser_source_revision': proof['source_revision']}
    artifacts = []
    for name in (HTML_NAME, 'records.csv'):
        raw, meta = read(state, release, name)
        url = ('/api/campaigns/' + str(campaign_id) + '/releases/' + str(release['release_id']) +
               '/artifacts/' + quote(name, safe='') + '?project_id=' + str(project))
        artifacts.append({'artifact_ref': original['path'], 'name': name, 'sha256': meta['sha256'],
                          'byte_count': len(raw), 'retrieved': True, 'valid': True,
                          'access_path': url, 'media_type': meta['media_type']})
    return {'artifacts': artifacts,
            'replay_recipe': 'Open this project release and its records converter. Review the frozen copy of ' +
                original['path'] + ', select Convert, then Download CSV. Recipe ' + static.RECIPE_ID +
                ' version ' + str(static.RECIPE_VERSION) + '. Flat JSON records only, at most 1000 records and 100 columns. Spreadsheet formula text is escaped.',
            'known_limits': release['contract']['deferred_items']}
