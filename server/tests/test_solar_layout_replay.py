"""S01 hardening: synthetic multi-generation export and saved-attribute replay.

The HTTP row uses the existing verified disposable PostgreSQL fixture. The
serialization row needs no database. Neither row opens a benchmark project.
"""
from copy import deepcopy
import hashlib
import json
from uuid import uuid4

import project_solar_materialization as layout
import solar_frames_piles as frames
import solar_native_layout_dxf as native
from test_project_solar_materialization import fresh, lane, private_pg  # noqa: F401


def inputs():
    preset = {
        'Name': 'Synthetic first', 'Rows': 2, 'Columns': 3,
        'ModuleLengthM': 2.0, 'ModuleWidthM': 1.0, 'ModulePowerWp': 400,
        'Orientation': 'Portrait', 'FramingType': 'FixedTilt',
        'HorizontalGapM': 0.1, 'VerticalGapM': 0.1,
        'TiltDegrees': 15.0, 'AxisAzimuthDeg': 180.0,
        'HeightAtLowPoseM': 0.7, 'HeightAtHighPoseM': 1.7,
        'PileTemplateName': 'Synthetic piles',
    }
    original = {'units': 'm', 'boundary': [[0, 0], [20, 0], [20, 24], [0, 24]],
                'active_preset': preset, 'pile_template': {'Name': 'Synthetic piles'}}
    text = json.dumps(original, indent=2) + '\n'
    body = {'original_intake': text, 'grid_long_axis': 150,
            'producer_semantics': 'historical-list-reuse'}
    second = {'drawing_units': 'm',
              'boundary': [[40, 0], [60, 0], [60, 24], [40, 24]],
              'preset': {**preset, 'Name': 'Synthetic second', 'TiltDegrees': 30.0,
                         'AxisAzimuthDeg': 90.0, 'HeightAtLowPoseM': 1.2,
                         'HeightAtHighPoseM': 2.2}}
    return body, second


def inspected(decoded):
    rows = []
    for offset in range(0, len(decoded.frames), layout.FRAME_PAGE_SIZE):
        page = layout._frame_page(decoded, offset)
        assert page['total'] == len(decoded.frames)
        rows.extend(page['frames'])
    assert len(rows) == len(decoded.frames)
    assert len({row['handle'] for row in rows}) == len(rows)
    return rows


def assert_models(rows, first_count):
    assert 0 < first_count < len(rows)
    for group, name, tilt, azimuth, low, high in (
        (rows[:first_count], 'Synthetic first', 15.0, 180.0, 0.7, 1.7),
        (rows[first_count:], 'Synthetic second', 30.0, 90.0, 1.2, 2.2),
    ):
        assert group
        for row in group:
            assert row['preset'] == name
            assert row['pile_template'] == 'Synthetic piles'
            assert row['pose'] == {'tilt_degrees': tilt, 'axis_azimuth_deg': azimuth,
                                   'height_at_low_pose_m': low, 'height_at_high_pose_m': high}


def test_synthetic_export_reopen_keeps_each_generations_attributes():
    body, second = inputs()
    document, parameters, _ = layout._prepare_initial_layout(layout.initial_parameters(body))
    original_document, original_parameters = deepcopy(document), deepcopy(parameters)
    existing = {frame['handle'] for frame in document['state']['frames']}
    document, parameters = frames.continue_physical_document(
        document, parameters, 'frame-generate', second, current_native=True)
    layout._generation_provenance(parameters, document, second['preset'], existing=existing)
    scope = {key: uuid4() for key in ('organization_id', 'project_id', 'drawing_id')}
    first = layout.materialize_native_layout(scope, None, original_document, None,
                                             parameters=original_parameters)
    exported = layout.materialize_native_layout(scope, {'index': 0}, document, first, parameters=parameters)
    decoded = native.decode_native_layout(exported)
    assert decoded.document['state']['frames'][:len(existing)] == original_document['state']['frames']
    assert decoded.parameters['frame_generations'] == {
        frame['handle']: 0 if frame['handle'] in existing else 1
        for frame in document['state']['frames']}
    assert decoded.parameters['original_intake'] == original_parameters['original_intake']
    assert decoded.parameters['original_intake']['sha256'] == hashlib.sha256(body['original_intake'].encode()).hexdigest()
    assert decoded.parameters['generation_history'] == [original_parameters['generation'], parameters['generation']]
    assert_models(inspected(decoded), len(existing))
    # Both serializer inputs remain reusable after publication.
    assert native.decode_native_layout(first).document == original_document
    assert original_parameters['generation']['preset']['Name'] == 'Synthetic first'


def test_http_mixed_generation_save_reopen_inspect_and_restore(fresh):
    lane, project = fresh
    body, second = inputs()
    created = lane.http.post(f'/api/projects/{project}/solar-layout', json=body,
                             headers={'Idempotency-Key': str(uuid4())})
    assert created.status_code == 200, created.text
    receipt = created.json()
    base = f"/api/projects/{project}/drawings/{receipt['drawing_id']}"
    first_id = receipt['drawing_version']['version_id']
    first_route = base + f'/versions/{first_id}'
    first = lane.http.get(first_route + '/dxf')
    assert first.status_code == 200, first.text
    original = native.decode_native_layout(first.content)
    first_rows = inspected(original)
    acquired = lane.http.post(base + '/checkout')
    assert acquired.status_code == 200, acquired.text
    headers = {'Idempotency-Key': str(uuid4()),
               'X-Checkout-Capability': acquired.json()['checkout_capability']}
    source = lane.http.get(first_route + '/solar-layout')
    assert source.status_code == 200, source.text
    generated = lane.http.post(first_route + '/solar-layout/operations', headers=headers,
                               json={'physical_source': source.json()['physical_source'],
                                     'operation': 'frame-generate', 'parameters': second})
    assert generated.status_code == 200, generated.text
    second_id = generated.json()['drawing_version']['version_id']
    second_route = base + f'/versions/{second_id}'
    exported = lane.http.get(second_route + '/dxf')
    assert exported.status_code == 200, exported.text
    decoded = native.decode_native_layout(exported.content)
    expected_rows = inspected(decoded)
    assert_models(expected_rows, len(first_rows))
    # Publication allocates fresh native CAD handles. Logical frame ownership,
    # geometry and per-frame model attributes must survive that remapping.
    without_handles = lambda rows: [{key: value for key, value in row.items() if key != 'handle'} for row in rows]
    assert without_handles(expected_rows[:len(first_rows)]) == without_handles(first_rows)
    assert decoded.parameters['original_intake'] == original.parameters['original_intake']
    saved = lane.http.post(second_route + '/editor', content=exported.content,
                           headers={**headers, 'Idempotency-Key': str(uuid4()),
                                    'Content-Type': 'application/dxf'})
    assert saved.status_code == 200, saved.text
    saved_route = base + f"/versions/{saved.json()['drawing_version']['version_id']}"
    reopened = lane.http.get(saved_route + '/dxf')
    assert reopened.status_code == 200 and reopened.content == exported.content
    observed = []
    for offset in range(0, len(expected_rows), layout.FRAME_PAGE_SIZE):
        page = lane.http.get(saved_route + f'/solar-layout?frame_offset={offset}')
        assert page.status_code == 200, page.text
        observed.extend(page.json()['frames'])
    assert observed == expected_rows
    restored = lane.http.post(saved_route + '/editor/restore', json={'version_id': first_id},
                              headers={**headers, 'Idempotency-Key': str(uuid4())})
    assert restored.status_code == 200, restored.text
    restored_route = base + f"/versions/{restored.json()['drawing_version']['version_id']}"
    restored_export = lane.http.get(restored_route + '/dxf')
    assert restored_export.status_code == 200 and restored_export.content == first.content
    assert inspected(native.decode_native_layout(restored_export.content)) == first_rows
