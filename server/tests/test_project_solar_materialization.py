"""Canonical layout publication, with licensed runtime explicitly out of scope."""
from uuid import uuid4
from copy import deepcopy
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import project_solar_materialization as layout
import project_drawing_editor as editor
import solar_physical_state as ps
from solar_solve_results import digest
from test_w1_design_graph import graph

from test_project_drawing_editor import lane, private_pg, checkout


def test_synthetic_layout_replay_over_canonical_save_restore(fresh):
    # Keep the replay on this existing database-backed selected-CI gate.
    from solar_layout_replay import replay_http_mixed_generation_save_reopen_inspect_and_restore
    replay_http_mixed_generation_save_reopen_inspect_and_restore(fresh)


def test_synthetic_layout_export_reopen_keeps_generation_models():
    from solar_layout_replay import replay_synthetic_export_reopen_keeps_each_generations_attributes
    replay_synthetic_export_reopen_keeps_each_generations_attributes()


def canonical_start(fresh):
    lane, project = fresh
    response = lane.http.post(f'/api/projects/{project}/solar-layout', json=initial_body(),
                              headers={'Idempotency-Key': str(uuid4())})
    assert response.status_code == 200, response.text
    receipt = response.json()
    base = f"/api/projects/{project}/drawings/{receipt['drawing_id']}"
    return lane, project, receipt, base


def test_canonical_initial_physical_continuity_and_exact_restore(fresh):
    import solar_native_layout_dxf as native
    lane, project, first, base = canonical_start(fresh)
    parent = first['drawing_version']['version_id']
    source = lane.http.get(base + f'/versions/{parent}/solar-layout')
    assert source.status_code == 200, source.text
    assert source.json()['physical_source_kind'] == 'canonical_version_intake'
    physical_source = source.json()['physical_source']
    assert physical_source['physical_head'] == first['intake_sha256']
    cap = lane.http.post(base + '/checkout').json()['checkout_capability']
    request = str(uuid4())
    parameters = initial_body()
    parameters['boundary'] = [[x + 1000, y] for x, y in parameters['boundary']]
    body = {'physical_source': physical_source, 'operation': 'frame-generate', 'parameters': parameters}
    route = base + f'/versions/{parent}/solar-layout/operations'
    response = lane.http.post(route, json=body, headers={'Idempotency-Key': request, 'X-Checkout-Capability': cap})
    assert response.status_code == 200, response.text
    receipt = response.json()
    successor = receipt['drawing_version']['version_id']
    before = lane.http.get(base + f'/versions/{parent}/dxf').content
    after = lane.http.get(base + f'/versions/{successor}/dxf').content
    original, continued = native.decode_native_layout(before), native.decode_native_layout(after)
    assert len(continued.frames) == 2 * len(original.frames)
    assert continued.document['state']['frames'][:len(original.frames)] == original.document['state']['frames']
    assert continued.parameters['generation_history'] == [original.parameters['generation'], continued.parameters['generation']]
    assert continued.parameters['generation']['boundary'] == parameters['boundary']
    assert lane.http.post(route, json=body, headers={'Idempotency-Key': request, 'X-Checkout-Capability': cap}).json() == receipt
    reopened = lane.http.get(base + f'/versions/{successor}/solar-layout')
    assert reopened.status_code == 200 and reopened.json()['physical_source']['physical_head'] == receipt['intake_sha256']
    conflict = lane.http.post(route, json={**body, 'parameters': initial_body()},
        headers={'Idempotency-Key': request, 'X-Checkout-Capability': cap})
    assert conflict.status_code == 409 and conflict.json()['error']['reason_code'] == 'SIP_R2_IDEMPOTENCY_CONFLICT'
    boolean_request = deepcopy(body)
    assert boolean_request['parameters']['boundary'][0][1] == 0
    boolean_request['parameters']['boundary'][0][1] = False
    conflict = lane.http.post(route, json=boolean_request,
        headers={'Idempotency-Key': request, 'X-Checkout-Capability': cap})
    assert conflict.status_code == 409 and conflict.json()['error']['reason_code'] == 'SIP_R2_IDEMPOTENCY_CONFLICT'
    restored = lane.http.post(base + f'/versions/{successor}/editor/restore', json={'version_id': parent},
        headers={'Idempotency-Key': str(uuid4()), 'X-Checkout-Capability': cap})
    assert restored.status_code == 200, restored.text
    restored_id = restored.json()['drawing_version']['version_id']
    raw = lane.http.get(base + f'/versions/{restored_id}/dxf').content
    assert raw == before and native.decode_native_layout(raw).parameters == original.parameters
    assert lane.http.get(base + f'/versions/{restored_id}/solar-layout').status_code == 200
    assert lane.http.post(route, json=body, headers={'Idempotency-Key': request, 'X-Checkout-Capability': cap}).json() == receipt


@pytest.mark.parametrize('failure', ['stale_version', 'stale_head', 'foreign_project', 'foreign_tenant',
                                     'foreign_metadata', 'tampered_metadata', 'no_checkout'])
def test_canonical_continuity_refuses_before_mutation(fresh, failure):
    import deps
    import solar_frames_piles as fp
    import solar_native_layout_dxf as native
    from leaf_platform import store
    lane, project, first, base = canonical_start(fresh)
    drawing, parent = layout.UUID(first['drawing_id']), layout.UUID(first['drawing_version']['version_id'])
    source = lane.http.get(base + f'/versions/{parent}/solar-layout').json()['physical_source']
    context = layout.project.resolve_context(lane.tenant, project, parent, drawing_id=drawing)
    raw = lane.http.get(base + f'/versions/{parent}/dxf').content
    cap = None if failure == 'no_checkout' else lane.http.post(base + '/checkout').json()['checkout_capability']
    expected = None
    if failure in ('stale_version', 'foreign_metadata', 'tampered_metadata'):
        if failure != 'stale_version':
            decoded = native.decode_native_layout(raw)
            scope = {'organization_id': lane.org, 'project_id': project, 'drawing_id': drawing}
            document = decoded.document
            parameters = decoded.parameters
            if failure == 'foreign_metadata':
                scope['drawing_id'] = uuid4()
            else:
                values = initial_body()
                values['boundary'] = [[x + 1000, y] for x, y in values['boundary']]
                document, parameters = fp.prepare_initial_frames(**values)
            raw = native.materialize_native_layout(scope, None, document, None, parameters=parameters)
        saved = editor.save_dxf(lane.tenant, project, drawing, parent, raw, request_id=uuid4(), capability=cap)
        if failure != 'stale_version':
            parent = layout.UUID(saved['drawing_version']['version_id'])
            expected = 'PROJECT_LAYOUT_NATIVE_SCOPE_MISMATCH' if failure == 'foreign_metadata' else 'PROJECT_LAYOUT_NATIVE_METADATA_INVALID'
        else:
            expected = 'SIP_R1_STALE_VERSION'
    if failure == 'stale_head':
        source['physical_head'] = '0' * 64
        expected = 'PROJECT_LAYOUT_PHYSICAL_HEAD_STALE'
    if failure == 'foreign_project':
        base = f'/api/projects/{lane.project}/drawings/{drawing}'
    if failure == 'foreign_tenant':
        org = store.create_org('Foreign continuity tenant')
        subject = 'auth0|continuity-' + uuid4().hex
        store.create_identity_binding(org.org_id, 'auth0', subject, role='editor')
        lane.app.dependency_overrides[deps.require_active_tenant] = lambda: deps.TenantContext(
            str(org.org_id), org_id=str(org.org_id), subject=subject, authority_resolved=True)
    if failure == 'no_checkout':
        expected = 'SIP_R1_CHECKOUT_REQUIRED'
    before = [v.version_id for v in store.list_drawing_versions(lane.org, project)]
    body = {'physical_source': source, 'operation': 'frame-generate', 'parameters': initial_body()}
    headers = {'Idempotency-Key': str(uuid4())}
    if cap:
        headers['X-Checkout-Capability'] = cap
    response = lane.http.post(base + f'/versions/{parent}/solar-layout/operations', json=body, headers=headers)
    assert response.status_code in (403, 404, 409), response.text
    if expected:
        assert response.json()['error']['reason_code'] == expected
    assert [v.version_id for v in store.list_drawing_versions(lane.org, project)] == before


@pytest.fixture
def fresh(lane):
    from leaf_platform import db, store
    project = store.create_project(lane.org, 'Parameter-only initial layout', authority_mode='postgres_canonical')
    with db.cursor() as cur:
        cur.execute('INSERT INTO project_member_bindings '
                    '(membership_id,org_id,project_id,binding_id,role,invited_by_binding_id) '
                    "VALUES (%s,%s,%s,%s,'editor',%s)",
                    (uuid4(), lane.org, project.project_id, lane.actor, lane.actor))
    return lane, project.project_id


def initial_body():
    from test_solar_frames_piles import GEN, PRESET
    return {'boundary': deepcopy(GEN['boundary']), 'preset': deepcopy(PRESET), 'drawing_units': 'm'}


def test_connected_initial_layout_needs_no_import_or_dummy_drawing(fresh):
    import solar_native_layout_dxf as native
    lane, project = fresh
    route = f'/api/projects/{project}/solar-layout'
    response = lane.http.get(route)
    assert response.status_code == 200
    assert response.json()['drawing_id'] is None
    assert response.json()['parent_version_id'] is None
    assert response.json()['writer_available'] is True
    assert response.json()['serialization_available'] is True
    response = lane.http.post(route, json=initial_body(), headers={'Idempotency-Key': str(uuid4())})
    assert response.status_code == 200, response.text
    receipt = response.json()
    drawing, version = receipt['drawing_id'], receipt['drawing_version']['version_id']
    assert receipt['parent_version_id'] is None and receipt['drawing_version']['seq'] == 1
    base = f'/api/projects/{project}/drawings/{drawing}'
    source = lane.http.get(base + f'/versions/{version}/dxf')
    assert source.status_code == 200
    decoded = native.decode_native_layout(source.content)
    expected, parameters = layout.state.decode_document(decoded.xrecord['document'].encode()), decoded.parameters
    assert decoded.frames and len(decoded.frames) == len(expected['state']['frames'])
    assert parameters['generation']['boundary'] == initial_body()['boundary']
    assert parameters['generation']['preset'] == initial_body()['preset']
    assert decoded.scope == {'organization_id': str(lane.org), 'project_id': str(project), 'drawing_id': drawing}
    context = layout.project.resolve_context(lane.tenant, project, layout.UUID(version), drawing_id=layout.UUID(drawing))
    metadata = context.intake[layout.KEY]
    assert ps.decode_document(metadata['document'].encode()) == decoded.document
    assert metadata['parameters'] == decoded.parameters
    assert len(context.intake['polylines']) == len(decoded.frames)
    cap = lane.http.post(base + '/checkout').json()['checkout_capability']
    saved = lane.http.post(base + f'/versions/{version}/editor', content=source.content,
        headers={'Content-Type': 'application/dxf', 'Idempotency-Key': str(uuid4()), 'X-Checkout-Capability': cap})
    assert saved.status_code == 200, saved.text
    successor = saved.json()['drawing_version']['version_id']
    saved_source = lane.http.get(base + f'/versions/{successor}/dxf')
    assert saved_source.content == source.content
    assert native.decode_native_layout(saved_source.content).document == decoded.document
    restored = lane.http.post(base + f'/versions/{successor}/editor/restore', json={'version_id': version},
        headers={'Idempotency-Key': str(uuid4()), 'X-Checkout-Capability': cap})
    assert restored.status_code == 200, restored.text
    restored_id = restored.json()['drawing_version']['version_id']
    reopened = lane.http.get(base + f'/versions/{restored_id}/dxf')
    assert reopened.content == source.content
    assert native.decode_native_layout(reopened.content).parameters == decoded.parameters
    followup = lane.http.get(base + f'/versions/{restored_id}/solar-layout')
    assert followup.status_code == 200
    assert followup.json()['physical_source_kind'] == 'canonical_version_intake'


def test_initial_real_storage_metadata_replay_and_normal_history(fresh, monkeypatch):
    lane, project = fresh
    route, request, calls = f'/api/projects/{project}/solar-layout', uuid4(), []
    body = initial_body()
    def synthetic_writer(context, head, document, source, *, parameters):
        assert context['project_id'] == project and head is None and source is None
        assert parameters['generation']['boundary'] == body['boundary']
        assert parameters['generation']['preset'] == body['preset']
        assert document['state']['frames']
        calls.append(deepcopy(document))
        return lane.raw
    monkeypatch.setattr(layout, 'materialize_native_layout', synthetic_writer)
    first = lane.http.post(route, json=body, headers={'Idempotency-Key': str(request)})
    assert first.status_code == 200, first.text
    receipt = first.json()
    drawing, version = receipt['drawing_id'], receipt['drawing_version']['version_id']
    assert receipt['parent_version_id'] is None and receipt['mode'] == 'initial'
    assert receipt['request_id'] == str(request)
    assert receipt['drawing_version']['seq'] == 1
    base = f'/api/projects/{project}/drawings/{drawing}'
    source = lane.http.get(base + f'/versions/{version}/dxf')
    assert source.status_code == 200 and source.content == lane.raw
    context = layout.project.resolve_context(lane.tenant, project, layout.UUID(version), drawing_id=layout.UUID(drawing))
    metadata = context.intake[layout.KEY]
    assert ps.decode_document(metadata['document'].encode()) == calls[0]
    assert metadata['request'] == body
    assert metadata['generation_source']['parameters_sha256'] == digest(body)
    replay = lane.http.post(route, json=body, headers={'Idempotency-Key': str(request)})
    assert replay.json() == receipt and len(calls) == 1
    conflict = lane.http.post(route, json={**body, 'drawing_units': 'ft'},
                             headers={'Idempotency-Key': str(request)})
    assert conflict.status_code == 409 and conflict.json()['error']['reason_code'] == 'SIP_R2_IDEMPOTENCY_CONFLICT'
    cap = lane.http.post(base + '/checkout').json()['checkout_capability']
    saved = lane.http.post(base + f'/versions/{version}/editor', content=lane.raw,
        headers={'Content-Type': 'application/dxf', 'Idempotency-Key': str(uuid4()), 'X-Checkout-Capability': cap})
    assert saved.status_code == 200, saved.text
    successor = saved.json()['drawing_version']['version_id']
    restored = lane.http.post(base + f'/versions/{successor}/editor/restore', json={'version_id': version},
        headers={'Idempotency-Key': str(uuid4()), 'X-Checkout-Capability': cap})
    assert restored.status_code == 200, restored.text
    restored_context = layout.project.resolve_context(lane.tenant, project,
        layout.UUID(restored.json()['drawing_version']['version_id']), drawing_id=layout.UUID(drawing))
    assert restored_context.intake[layout.KEY] == metadata
    assert lane.http.post(route, json=body, headers={'Idempotency-Key': str(request)}).json() == receipt


@pytest.mark.parametrize('semantics', [None, 'current-native', 'historical-list-reuse'])
def test_original_intake_custody_in_canonical_layout_export(fresh, semantics):
    import solar_frames_piles as fp
    import solar_native_layout_dxf as native

    lane, project = fresh
    base = initial_body()
    template = {'Name': 'Supplied', 'PlacementMode': 'Grid', 'HorizontalDistancesM': [1.0, 2.0],
                'VerticalDistancesM': [0.5], 'Stations': [], 'PileDiameterM': 0.0,
                'RevealBucketBoundariesM': [0.9144, 1.2192, 0.9144]}
    preset = {**base['preset'], 'PileTemplateName': template['Name']}
    original = {'units': 'm', 'boundary': base['boundary'], 'active_preset': preset,
                'pile_template': template}
    text = json.dumps(original, indent=2).replace('\n', '\r\n') + '\r\n'
    body = {'original_intake': text, 'grid_long_axis': 150}
    if semantics is not None:
        body['producer_semantics'] = semantics
    original_sha = hashlib.sha256(text.encode('utf-8')).hexdigest()
    headers = {'Idempotency-Key': str(uuid4())}
    route = f'/api/projects/{project}/solar-layout'
    first = lane.http.post(route, json=body, headers=headers)
    assert first.status_code == 200, first.text
    receipt = first.json()
    assert receipt['generation_source']['original_intake_sha256'] == original_sha
    assert receipt['generation_source']['producer_semantics'] == (semantics or 'current-native')
    assert lane.http.post(route, json=body, headers=headers).json() == receipt
    for changed in ({**body, 'grid_long_axis': 151}, {**body, 'original_intake': text.replace('\r\n', '\n')}):
        assert lane.http.post(route, json=changed, headers=headers).status_code == 409
    changed_semantics = 'current-native' if semantics == 'historical-list-reuse' else 'historical-list-reuse'
    assert lane.http.post(route, json={**body, 'producer_semantics': changed_semantics}, headers=headers).status_code == 409

    drawing_id = receipt['drawing_id']
    version_id = receipt['drawing_version']['version_id']
    drawing_route = f'/api/projects/{project}/drawings/{drawing_id}'
    reopened = lane.http.get(f'{drawing_route}/versions/{version_id}/solar-layout')
    assert reopened.status_code == 200, reopened.text
    exported = lane.http.get(f'{drawing_route}/versions/{version_id}/dxf')
    assert exported.status_code == 200, exported.text
    decoded = native.decode_native_layout(exported.content)
    assert decoded.parameters['original_intake'] == {
        'sha256': original_sha, 'pile_template': template,
        'requested_grid_long_axis': 150, 'terrain_grid_applied': False,
    }
    assert decoded.parameters['generation']['preset'] == preset
    assert set(decoded.parameters['generation']) == {'operation', 'base', 'drawing_units', 'boundary', 'preset'}
    assert decoded.document['units'] == {'drawing_units': 'm', 'meters_per_unit': 1.0}
    state = decoded.document['state']
    assert state['grid'] is None and state['piles'] == [] and state['frames']
    current_native = semantics != 'historical-list-reuse'
    assert decoded.parameters['producer_semantics'] == (semantics or 'current-native')
    loaded, _ = (fp.load_current_preset if current_native else fp.load_preset)(preset)
    from dataclasses import asdict
    assert decoded.parameters['generation_models'] == [asdict(loaded)]
    assert decoded.parameters['frame_generations'] == {frame['handle']: 0 for frame in state['frames']}
    changed_generation, _ = layout._original_generation({**body, 'grid_long_axis': 151})
    comparison, _ = fp.prepare_initial_frames(**changed_generation, current_native=current_native)
    assert comparison == decoded.document
    other, _ = fp.prepare_initial_frames(**changed_generation, current_native=not current_native)
    assert other['state']['frames'] != state['frames']
    context = layout.project.resolve_context(lane.tenant, project, layout.UUID(version_id),
                                             drawing_id=layout.UUID(drawing_id))
    assert context.intake[layout.KEY]['request']['original_intake'].encode('utf-8') == text.encode('utf-8')


@pytest.mark.parametrize('semantics', [None, True, [], {}, 'unknown'])
def test_initial_rejects_unknown_producer_semantics(semantics):
    with pytest.raises(editor.EditorError, match='PROJECT_LAYOUT_REQUEST_INVALID'):
        layout.initial_parameters({'original_intake': '{}', 'grid_long_axis': 150,
                                   'producer_semantics': semantics})


@pytest.mark.parametrize('semantics', [None, 'current-native', 'historical-list-reuse'])
def test_initial_preparation_binds_loader_geometry_models_and_export(semantics):
    from dataclasses import asdict
    import solar_frames_piles as fp
    import solar_native_layout_dxf as native

    values = initial_body()
    preset = {**values['preset'], 'PileTemplateName': 'Synthetic piles'}
    original = {'units': values['drawing_units'], 'boundary': values['boundary'],
                'active_preset': preset, 'pile_template': {'Name': 'Synthetic piles'}}
    raw = json.dumps(original, indent=2).replace('\n', '\r\n') + '\r\n'
    body = {'original_intake': raw, 'grid_long_axis': 150}
    if semantics is not None:
        body['producer_semantics'] = semantics
    document, inputs, source = layout._prepare_initial_layout(layout.initial_parameters(body))
    current = semantics != 'historical-list-reuse'
    expected, _ = fp.prepare_initial_frames(boundary=values['boundary'], preset=preset,
                                           drawing_units=values['drawing_units'], current_native=current)
    opposite, _ = fp.prepare_initial_frames(boundary=values['boundary'], preset=preset,
                                           drawing_units=values['drawing_units'], current_native=not current)
    assert document == expected
    assert document['state']['frames'] != opposite['state']['frames']
    loaded, _ = (fp.load_current_preset if current else fp.load_preset)(preset)
    assert inputs['generation_models'] == [asdict(loaded)]
    assert inputs['generation']['preset'] == preset
    assert inputs['producer_semantics'] == source['producer_semantics'] == (semantics or 'current-native')
    assert source['original_intake_sha256'] == hashlib.sha256(raw.encode()).hexdigest()
    scope = {key: uuid4() for key in ('organization_id', 'project_id', 'drawing_id')}
    exported = layout.materialize_native_layout(scope, None, document, None, parameters=inputs)
    decoded = native.decode_native_layout(exported)
    assert decoded.document == document and decoded.parameters == inputs


def _inspection_fixture():
    import solar_native_layout_dxf as native
    document, inputs, _ = layout._prepare_initial_layout(initial_body())
    scope = {key: uuid4() for key in ('organization_id', 'project_id', 'drawing_id')}
    raw = layout.materialize_native_layout(scope, None, document, None, parameters=inputs)
    return native.decode_native_layout(raw)


def test_saved_frame_pages_preserve_native_identity_cells_and_model_pose():
    decoded = _inspection_fixture()
    observed = []
    for offset in range(0, len(decoded.frames), layout.FRAME_PAGE_SIZE):
        page = layout._frame_page(decoded, offset)
        assert page['total'] == len(decoded.frames) and page['offset'] == offset
        assert page['page_size'] == layout.FRAME_PAGE_SIZE
        assert page['drawing_units'] == decoded.document['units']['drawing_units']
        assert len(json.dumps(page).encode()) < 65536
        for frame, native in zip(page['frames'], decoded.frames[offset:offset + layout.FRAME_PAGE_SIZE]):
            logical = next(key for key, value in decoded.handles.items() if value == native['handle'])
            model = decoded.parameters['generation_models'][decoded.parameters['frame_generations'][logical]]
            assert frame['handle'] == native['handle']
            assert (frame['row'], frame['column'], frame['elevation']) == (native['row'], native['col'], native['elevation'])
            assert frame['vertices'] == [list(point) for point in native['vertices']]
            assert frame['preset'] == model['name'] and frame['pile_template'] == model['pile_template_name']
            assert frame['pose'] == {key: model[key] for key in (
                'tilt_degrees', 'axis_azimuth_deg', 'height_at_low_pose_m', 'height_at_high_pose_m')}
            observed.append(frame['handle'])
    assert observed == [frame['handle'] for frame in decoded.frames]
    assert len(observed) == len(set(observed))


@pytest.mark.parametrize('offset', [-1, True, 1.5, '0', None, 10**9])
def test_saved_frame_page_refuses_invalid_offsets(offset):
    with pytest.raises(editor.EditorError, match='PROJECT_LAYOUT_REQUEST_INVALID'):
        layout._frame_page(_inspection_fixture(), offset)


@pytest.mark.parametrize('failure', ['missing', 'out_of_range', 'pose'])
def test_saved_frame_page_refuses_unbound_model_attributes(failure):
    decoded = _inspection_fixture()
    logical = next(key for key, value in decoded.handles.items() if value == decoded.frames[0]['handle'])
    if failure == 'missing':
        decoded.parameters['frame_generations'].pop(logical)
    elif failure == 'out_of_range':
        decoded.parameters['frame_generations'][logical] = len(decoded.parameters['generation_models'])
    else:
        decoded.parameters['generation_models'][0]['tilt_degrees'] = None
    with pytest.raises(editor.EditorError, match='PROJECT_LAYOUT_NATIVE_METADATA_INVALID'):
        layout._frame_page(decoded, 0)


def test_canonical_frame_inspection_preserves_route_scope_and_stale_version_refusal(fresh):
    lane, project, receipt, base = canonical_start(fresh)
    parent = receipt['drawing_version']['version_id']
    route = base + f'/versions/{parent}/solar-layout'
    assert lane.http.get(route).json()['schema'] == layout.SCHEMA
    response = lane.http.get(route + '?frame_offset=0')
    assert response.status_code == 200, response.text
    page = response.json()
    assert page['schema'] == layout.FRAME_SCHEMA and page['frames']
    assert page['parent_version_id'] == parent and page['project_id'] == str(project)
    assert page['drawing_id'] == receipt['drawing_id']
    assert lane.http.get(route + '?frame_offset=-1').status_code == 400
    foreign = route.replace(str(project), str(uuid4()))
    refused = lane.http.get(foreign + '?frame_offset=0')
    assert refused.status_code in (403, 404) and 'frames' not in refused.json()
    raw = lane.http.get(base + f'/versions/{parent}/dxf').content
    cap = lane.http.post(base + '/checkout').json()['checkout_capability']
    saved = editor.save_dxf(lane.tenant, project, layout.UUID(receipt['drawing_id']), layout.UUID(parent),
                            raw, request_id=uuid4(), capability=cap)
    assert saved['drawing_version']['version_id'] != parent
    stale = lane.http.get(route + '?frame_offset=0')
    assert stale.status_code == 409 and 'frames' not in stale.json()


def test_initial_existing_project_refuses_before_writer(lane, monkeypatch):
    monkeypatch.setattr(layout, 'materialize_native_layout', lambda *_a, **_k: pytest.fail('unexpected writer'))
    response = lane.http.post(f'/api/projects/{lane.project}/solar-layout', json=initial_body(),
                              headers={'Idempotency-Key': str(uuid4())})
    assert response.status_code == 409
    assert response.json()['error']['reason_code'] == 'PROJECT_LAYOUT_INITIAL_REQUIRES_EMPTY_PROJECT'


def test_initial_drain_refuses_before_writer(fresh, monkeypatch):
    lane, project = fresh
    monkeypatch.setattr(layout.project.write_loop, 'drawing_mutations_refusal', lambda: 'synthetic-drain')
    monkeypatch.setattr(layout, 'materialize_native_layout', lambda *_a, **_k: pytest.fail('unexpected writer'))
    response = lane.http.post(f'/api/projects/{project}/solar-layout', json=initial_body(),
                              headers={'Idempotency-Key': str(uuid4())})
    assert response.status_code == 503
    assert response.json()['error']['reason_code'] == 'SIP_R1_WRITES_DRAINED'


def test_initial_late_result_preserves_concurrent_drawing(fresh, monkeypatch):
    from leaf_platform import store
    lane, project = fresh
    winners = []
    def late_writer(*_a, **_k):
        winners.append(store.create_drawing_artifact(lane.org, project, 'Concurrent winner'))
        return lane.raw
    monkeypatch.setattr(layout, 'materialize_native_layout', late_writer)
    response = lane.http.post(f'/api/projects/{project}/solar-layout', json=initial_body(),
                              headers={'Idempotency-Key': str(uuid4())})
    assert response.status_code == 409
    assert response.json()['error']['reason_code'] == 'PROJECT_LAYOUT_INITIAL_REQUIRES_EMPTY_PROJECT'
    assert store.list_drawing_versions(lane.org, project) == []
    assert len(winners) == 1


def test_initial_foreign_project_refuses_before_writer(lane, monkeypatch):
    from leaf_platform import store
    foreign = store.create_project(store.create_org('Foreign initial layout').org_id,
                                   'Foreign', authority_mode='postgres_canonical')
    monkeypatch.setattr(layout, 'materialize_native_layout', lambda *_a, **_k: pytest.fail('unexpected writer'))
    response = lane.http.post(f'/api/projects/{foreign.project_id}/solar-layout', json=initial_body(),
                              headers={'Idempotency-Key': str(uuid4())})
    assert response.status_code in (403, 404)


def test_initial_failed_source_storage_creates_no_canonical_rows(fresh, monkeypatch):
    from leaf_platform import store, db
    lane, project = fresh
    monkeypatch.setattr(layout, 'materialize_native_layout', lambda *_a, **_k: lane.raw)
    def failed_put(*_a):
        raise OSError('synthetic immutable storage outage')
    monkeypatch.setattr(lane.backend, 'put_if_absent_or_verify', failed_put)
    response = lane.http.post(f'/api/projects/{project}/solar-layout', json=initial_body(),
                              headers={'Idempotency-Key': str(uuid4())})
    assert response.status_code == 503
    assert store.list_drawing_versions(lane.org, project) == []
    with db.cursor() as cur:
        cur.execute('SELECT count(*) AS n FROM drawing_artifacts WHERE project_id=%s', (project,))
        assert cur.fetchone()['n'] == 0


def test_connected_layout_route_rejects_a_parent_without_a_solar_graph(lane):
    response = lane.http.post(lane.base + f'/versions/{lane.parent.version_id}/solar-layout',
        json={'physical_head': 'a' * 64, 'physical_content_sha256': 'b' * 64, 'physical_index': 0},
        headers={'X-Checkout-Capability': checkout(lane), 'Idempotency-Key': str(uuid4())})
    assert response.status_code == 409
    assert response.json()['error']['reason_code'] == 'PROJECT_LAYOUT_GRAPH_REQUIRED'
    source = lane.http.get(lane.base + f'/versions/{lane.parent.version_id}/solar-layout')
    assert source.status_code == 409
    assert source.json()['error']['reason_code'] == 'PROJECT_LAYOUT_GRAPH_REQUIRED'


def body():
    return {'physical_head': 'a' * 64, 'physical_content_sha256': 'b' * 64, 'physical_index': 0}


def test_native_writer_refusal_is_named_and_payload_free():
    with pytest.raises(editor.EditorError) as error:
        layout.materialize_native_layout(None, None, None, None, parameters={})
    assert (error.value.code, error.value.status) == ('NATIVE_LAYOUT_SCOPE_INVALID', 422)


@pytest.mark.parametrize('change', [
    {'geometry': []}, {'physical_index': True}, {'physical_head': 'bad'},
    {'physical_content_sha256': 'A' * 64}, {'physical_index': -1},
])
def test_request_does_not_admit_geometry_or_invalid_identity(change):
    with pytest.raises(editor.EditorError, match='PROJECT_LAYOUT_REQUEST_INVALID'):
        layout.parameters({**body(), **change})


def test_generation_parameters_round_trip_without_changing_the_request(graph, tmp_path, monkeypatch):
    import solar_frames_piles as fp
    import solar_physical_head as ph
    from test_w1_solve_commit import seed
    from test_solar_frames_piles import GEN, PRESET, TENANT, DRAWING, PROJECT
    backend, _ = seed(tmp_path, monkeypatch, graph)
    before = deepcopy(PRESET)
    result = fp.generate_frames(backend, TENANT, DRAWING, base=None,
        boundary=GEN['boundary'], preset=PRESET, drawing_units='m', project_id=PROJECT)
    head, document = ph.load_physical_head(backend, TENANT, DRAWING, project_id=PROJECT)
    meta, _ = layout.state.solar_artifacts.read_artifact(backend, TENANT, DRAWING, head['state']['artifact_id'])
    parameters = ps.load_operation_parameters(backend, TENANT, DRAWING, meta, document)
    request = parameters['generation']
    assert request['boundary'] == GEN['boundary'] and request['preset'] == PRESET
    assert request == parameters['operation']
    assert digest(request) == document['source']['sha256']
    assert ps.decode_document(ps.encode_document(document)) == document
    assert PRESET == before and head == result['head']


@pytest.fixture
def prepared(lane, monkeypatch):
    """Synthetic licensed-writer seam; actual PG/editor publication stays real.

    This does not attest native metadata encoding or the physical artifact IO
    bridge. Those production boundaries remain explicit and are tested apart.
    """
    cap = checkout(lane)
    context = layout.project.resolve_context(lane.tenant, lane.project, lane.parent.version_id,
                                            drawing_id=lane.drawing, write=True)
    request = {'operation': 'frame-generate', 'boundary': [[0, 0], [1, 0], [1, 2]],
               'preset': {'Name': 'Synthetic', 'Rows': 2, 'Columns': 1}}
    document = ps.physical_document({'frames': [], 'piles': []}, drawing_units='m', source_sha256=digest(request), capability='frame-generate')
    parameters = {'schema': ps.PARAMETERS_SCHEMA, 'operation': request, 'generation': request, 'piling': None}
    head = {'state': {'artifact_id': 'a' * 64, 'content_sha256': editor._sha(ps.encode_document(document))},
            'index': 0, 'project_id': str(lane.project), 'drawing_id': lane.source}
    value = body(); value['physical_content_sha256'] = head['state']['content_sha256']
    monkeypatch.setattr(layout, '_load', lambda *_: (deepcopy(head), deepcopy(document), deepcopy(parameters)))
    calls = []
    def synthetic_writer(context, supplied_head, supplied_document, raw, *, parameters):
        assert supplied_document == document and supplied_head == head and raw == lane.raw
        calls.append(deepcopy(supplied_document))
        return lane.raw
    monkeypatch.setattr(layout, 'materialize_native_layout', synthetic_writer)
    return lane, cap, document, value, calls


def publish(prepared, request=None, value=None, parent=None):
    lane, cap, _, expected, _ = prepared
    return layout.publish_layout(lane.tenant, lane.project, lane.drawing,
        parent or lane.parent.version_id, expected if value is None else value,
        request_id=request or uuid4(), capability=cap)


def test_real_canonical_publication_preserves_complete_metadata_and_exact_replay(prepared):
    lane, _, document, value, calls = prepared
    request = uuid4()
    first = publish(prepared, request)
    assert first['drawing_version']['drawing_id'] == str(lane.drawing)
    assert first['drawing_version']['project_id'] == str(lane.project)
    context = layout.project.resolve_context(lane.tenant, lane.project,
        layout.UUID(first['drawing_version']['version_id']), drawing_id=lane.drawing)
    stored = context.intake[layout.KEY]
    assert stored['request'] == value
    assert ps.decode_document(stored['document'].encode('utf-8')) == document
    assert first['physical_source'] == value
    assert publish(prepared, request) == first
    assert len(calls) == 1
    with pytest.raises(layout.project.ProjectContextError, match='IDEMPOTENCY_CONFLICT'):
        publish(prepared, request, {**value, 'physical_index': 1})
    assert len(calls) == 1
    restored = editor.restore_dxf(lane.tenant, lane.project, lane.drawing,
        layout.UUID(first['drawing_version']['version_id']), lane.parent.version_id,
        request_id=uuid4(), capability=prepared[1])
    restored_context = layout.project.resolve_context(lane.tenant, lane.project,
        layout.UUID(restored['drawing_version']['version_id']), drawing_id=lane.drawing)
    assert layout.KEY not in restored_context.intake
    assert publish(prepared, request) == first
    assert len(calls) == 1


def test_late_selected_version_preserves_the_newer_history(prepared):
    lane, _, _, _, calls = prepared
    newer = publish(prepared)
    with pytest.raises(layout.project.ProjectContextError, match='STALE_VERSION'):
        publish(prepared)
    assert len(calls) == 1
    context = layout.project.resolve_context(lane.tenant, lane.project,
        layout.UUID(newer['drawing_version']['version_id']), drawing_id=lane.drawing)
    assert context.binding.is_head


def test_foreign_parent_refuses_before_the_writer(prepared):
    _, _, _, _, calls = prepared
    with pytest.raises(layout.project.ProjectContextError):
        publish(prepared, parent=uuid4())
    assert calls == []


def test_writer_failure_does_not_publish(prepared, monkeypatch):
    lane, _, _, _, _ = prepared
    def unavailable(*_, **kwargs):
        layout.fail('LICENSED_WRITER_UNAVAILABLE', 503)
    monkeypatch.setattr(layout, 'materialize_native_layout', unavailable)
    with pytest.raises(editor.EditorError, match='LICENSED_WRITER_UNAVAILABLE'):
        publish(prepared)
    assert layout.project.resolve_context(lane.tenant, lane.project, lane.parent.version_id,
        drawing_id=lane.drawing).binding.is_head


@pytest.mark.parametrize('change', [{'index': 1}, {'project_id': 'foreign'}])
def test_foreign_or_stale_physical_head_refuses(graph, monkeypatch, change):
    from types import SimpleNamespace
    org = uuid4()
    request = {'operation': 'frame-generate', 'preset': {}, 'boundary': []}
    document = ps.physical_document({'frames': []}, drawing_units='m', source_sha256=digest(request), capability='frame-generate')
    value = body(); value['physical_content_sha256'] = editor._sha(ps.encode_document(document))
    head = {'drawing_id': 'source', 'project_id': graph['project']['id'], 'index': 0,
            'state': {'artifact_id': value['physical_head'], 'content_sha256': value['physical_content_sha256']}}
    head.update(change)
    context = SimpleNamespace(intake={'solar_design_graph': graph, 'solar_design_graph_sha256': digest(graph)},
                              binding=SimpleNamespace(organization_id=org))
    monkeypatch.setattr(layout, '_source', lambda _: ('source', 1))
    monkeypatch.setattr(editor, '_backend', lambda _: object())
    monkeypatch.setattr(layout.physical, 'load_physical_head', lambda *a, **k: (head, document))
    monkeypatch.setattr(ps, 'load_physical_state', lambda *a, **k: ({'source_version': 1, 'graph_sha256': digest(graph)}, document))
    with pytest.raises(editor.EditorError, match='PHYSICAL_HEAD_STALE'):
        layout._load(context, value)


def test_physical_head_moved_during_writer_does_not_publish(prepared, monkeypatch):
    lane, _, _, _, calls = prepared
    original = layout._load
    count = 0
    def moved(*args):
        nonlocal count
        count += 1
        if count > 1:
            layout.fail('PHYSICAL_HEAD_STALE')
        return original(*args)
    monkeypatch.setattr(layout, '_load', moved)
    with pytest.raises(editor.EditorError, match='PHYSICAL_HEAD_STALE'):
        publish(prepared)
    assert len(calls) == 1
    assert layout.project.resolve_context(lane.tenant, lane.project, lane.parent.version_id,
        drawing_id=lane.drawing).binding.is_head
