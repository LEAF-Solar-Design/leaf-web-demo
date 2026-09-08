"""Versioned, deterministic DXF layer summaries within project authority."""
from __future__ import annotations

import csv
import io
import re
import campaign_delivery_service as delivery
import campaign_web_release as files
from dxf_intake import parse_dxf_bytes

RECIPE_ID = 'dxf-layer-summary'
RECIPE_VERSION = 1
OUTPUT_NAME = 'dxf-layer-summary.csv'
WORKFLOW = 'Summarize direct supported entities by layer in the selected ASCII DXF, then download and open the verified CSV file.'
LIMITS = ['The original ambition beyond this DXF layer summary remains unproven.',
          'Block contents and unsupported entity types are excluded. Linear paths combine LINE, LWPOLYLINE and POLYLINE.',
          'Geometry repair and drawing modification are deferred. The source drawing is preserved.']
HEADER = ('layer', 'linear_paths', 'circles', 'arcs', 'text_labels', 'supported_total')


def requested(workflow):
    words = set(re.findall(r'[a-z]+', workflow.lower()))
    return {'dxf', 'layer', 'csv'} <= words and bool(words & {'summary', 'inventory'})


def _require_supported_coverage(raw, parsed):
    # The shared parser intentionally omits unusable entities for its viewer.
    # An inventory must account for every supported direct entity instead.
    buckets = {'LINE': 'polylines', 'LWPOLYLINE': 'polylines', 'POLYLINE': 'polylines',
               'CIRCLE': 'circles', 'ARC': 'arcs', 'TEXT': 'texts', 'MTEXT': 'texts'}
    expected = dict.fromkeys(buckets.values(), 0)
    lines = raw.decode('ascii').splitlines()
    section = None
    # validate_bytes has already checked group pairs and section structure.
    for index in range(0, len(lines), 2):
        code, value = int(lines[index].strip()), lines[index + 1].strip()
        if code == 0 and value == 'SECTION':
            section = lines[index + 3].strip()
        elif code == 0 and value == 'ENDSEC':
            section = None
        elif section == 'ENTITIES' and code == 0 and value in buckets:
            expected[buckets[value]] += 1
    if any(len(parsed.get(key, [])) != count for key, count in expected.items()):
        raise delivery.DeliveryConflict('DXF contains an unusable supported direct entity')


def render(raw):
    delivery.validate_bytes('source.dxf', raw)
    parsed = parse_dxf_bytes(raw, source_name='source.dxf')
    if parsed.get('parseErrors') or parsed.get('blocksCapped'):
        raise delivery.DeliveryConflict('DXF parsing is incomplete')
    _require_supported_coverage(raw, parsed)
    counts = {layer: [0, 0, 0, 0] for layer in parsed['layers']}
    for index, key in enumerate(('polylines', 'circles', 'arcs', 'texts')):
        for entity in parsed.get(key, []):
            counts.setdefault(entity['layer'], [0, 0, 0, 0])[index] += 1
    output = io.StringIO(newline='')
    writer = csv.writer(output, quoting=csv.QUOTE_ALL, lineterminator='\r\n')
    writer.writerow(HEADER)
    for layer in sorted(counts):
        # CSV quoting alone does not prevent spreadsheet formula evaluation.
        cell = "'" + layer if layer[:1] in ('=', '+', '-', '@', '\t', '\r', '\n') else layer
        writer.writerow([cell, *counts[layer], sum(counts[layer])])
    result = output.getvalue().encode('utf-8')
    delivery.validate_bytes(OUTPUT_NAME, result)
    return result


def compile_recipe(snapshot, refs):
    if len(refs) > 1:
        raise delivery.DeliveryConflict('DXF layer summary requires one source file')
    candidates = refs or sorted(row['path'] for row in snapshot.get('files', [])
                               if not row['path'].startswith('releases/')
                               and row['path'].lower().endswith('.dxf'))
    for path in candidates:
        if not path.lower().endswith('.dxf'):
            continue
        try:
            raw = delivery.file_bytes(snapshot, delivery.safe_path(path))
            source = delivery.validate_bytes(path, raw)
            artifact = delivery.validate_bytes(OUTPUT_NAME, render(raw))
            return artifact, {'recipe_id': RECIPE_ID, 'recipe_version': RECIPE_VERSION,
                              'source_artifact': source}
        except (ValueError, UnicodeError, csv.Error):
            continue
    return None, None


def _metadata(release):
    recipe = release['contract']['cad_recipe']
    if (recipe['recipe_id'] != RECIPE_ID or type(recipe['recipe_version']) is not int
            or recipe['recipe_version'] != RECIPE_VERSION):
        raise delivery.DeliveryConflict('The selected CAD recipe is unavailable')
    source = dict(recipe['source_artifact'], path=files.prefix(release) + 'source.dxf', name='source.dxf')
    output = dict(release['contract']['selected_artifact'], path=files.prefix(release) + OUTPUT_NAME)
    return recipe, source, output


def read(snapshot, release, name):
    _, source, output = _metadata(release)
    raw, source_meta = delivery.read_verified(snapshot, source)
    expected = render(raw)
    observed = delivery.validate_bytes(OUTPUT_NAME, expected)
    if any(observed[key] != output[key] for key in ('sha256', 'size_bytes', 'media_type')):
        raise delivery.DeliveryConflict('DXF recipe output differs from the frozen contract')
    if name == 'source.dxf':
        return raw, source_meta
    if name != OUTPUT_NAME:
        raise LookupError('Artifact unavailable')
    result, metadata = delivery.read_verified(snapshot, output)
    if result != expected:
        raise delivery.DeliveryConflict('Published DXF summary differs from the frozen input')
    return result, metadata


def run_stage(runtime, tenant, project_id, campaign_id, completion, stage):
    release = completion['release']
    recipe, source, output = _metadata(release)
    org, project, actor = runtime.authority(tenant, project_id)
    state = runtime._lifecycle().project_snapshot(org, project, actor)
    original = recipe['source_artifact']
    if stage in ('implementation', 'publication'):
        # Recover a committed source copy before consulting mutable project input.
        if delivery.receipt_for(state, source['path'], source['media_type'], source['sha256']):
            raw = delivery.read_verified(state, source)[0]
        else:
            raw = delivery.file_bytes(state, original['path'])
            observed = delivery.validate_bytes(original['path'], raw)
            if any(observed[key] != original[key] for key in ('sha256', 'size_bytes', 'media_type')):
                raise delivery.DeliveryConflict('DXF source changed before publication')
        result = render(raw)
        observed = delivery.validate_bytes(OUTPUT_NAME, result)
        if any(observed[key] != output[key] for key in ('sha256', 'size_bytes', 'media_type')):
            raise delivery.DeliveryConflict('DXF summary differs from the frozen contract')
        if stage == 'implementation':
            return {'artifact': observed, 'input_sha256': original['sha256'],
                    'recipe_id': RECIPE_ID, 'recipe_version': RECIPE_VERSION}
        receipts = [files._put(runtime, tenant, project_id, campaign_id, release, source, raw),
                    files._put(runtime, tenant, project_id, campaign_id, release, output, result)]
        final = runtime._lifecycle().project_snapshot(org, project, actor)
        read(final, release, OUTPUT_NAME)
        return {'artifact': observed, 'receipts': receipts}
    result, observed = read(state, release, OUTPUT_NAME)
    url = ('/api/campaigns/' + str(campaign_id) + '/releases/' + str(release['release_id']) +
           '/artifacts/' + OUTPUT_NAME + '?project_id=' + str(project))
    if stage == 'deployment':
        served = files.response(result, observed)
        if served.body != result:
            raise delivery.DeliveryConflict('Artifact response differs from the saved summary')
        return {'artifact': observed, 'observed_revision': observed['sha256'],
                'resource_identity': output['path'] + '@' + output['sha256'],
                'rollback_identity': 'Earlier versioned project files remain available.',
                'response_headers': dict(served.headers)}
    if stage == 'user_verification':
        return {'workflow': release['contract']['workflow'], 'artifact': observed,
                'observations': ['Current project authority checked', 'Saved CSV retrieved through ' + url,
                                 'CSV parsed and compared with the frozen DXF layer counts']}
    if stage != 'delivery':
        raise delivery.DeliveryConflict('Unknown CAD delivery stage')
    source_url = url.replace('/artifacts/' + OUTPUT_NAME, '/artifacts/source.dxf')
    return {'artifacts': [{'artifact_ref': original['path'], 'name': OUTPUT_NAME,
                'sha256': observed['sha256'], 'byte_count': len(result), 'retrieved': True,
                'valid': True, 'access_path': url, 'media_type': observed['media_type']}],
            'replay_recipe': 'Open this project and download ' + url + '. Retrieve the frozen DXF from ' +
                source_url + ' (' + source['path'] + '). Request DXF layer summary CSV using recipe ' +
                RECIPE_ID + ' version ' + str(RECIPE_VERSION) + '. Original input: ' + original['path'] + '.',
            'known_limits': release['contract']['deferred_items']}
