"""Canonical layout publication, with licensed runtime explicitly out of scope."""
from uuid import uuid4
from copy import deepcopy
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


def test_real_native_writer_absence_is_explicit():
    with pytest.raises(editor.EditorError) as error:
        layout.materialize_native_layout(None, None, None, None, parameters={})
    assert (error.value.code, error.value.status) == ('PROJECT_LAYOUT_LICENSED_WRITER_UNAVAILABLE', 503)


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
