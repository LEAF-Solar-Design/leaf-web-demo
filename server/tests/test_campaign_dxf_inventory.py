"""CAD adapter bytes and HTTP/runtime proof; authority and stores are fixtures."""
import copy
import csv
import importlib
import io
from types import SimpleNamespace
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import campaign_capability_resolver as capabilities
import campaign_delivery_service as delivery
import campaign_dxf_inventory as adapter
import campaign_release_service as runtime
import deps
from routers import campaigns
from test_campaign_delivery_service import Lifecycle, Store


# Independently specified drawing: two declared layers, one intentionally empty.
DXF = b'''0
SECTION
2
TABLES
0
LAYER
2
Geometry
0
LAYER
2
=EMPTY
0
ENDSEC
0
SECTION
2
ENTITIES
0
LINE
8
Geometry
10
0
20
0
11
10
21
10
0
LWPOLYLINE
8
Geometry
90
2
10
0
20
0
10
5
20
5
0
CIRCLE
8
Geometry
10
2
20
2
40
1
0
ARC
8
Geometry
10
3
20
3
40
2
50
0
51
90
0
TEXT
8
Geometry
10
0
20
0
1
Label
0
MTEXT
8
Geometry
10
1
20
1
1
Second label
0
ENDSEC
0
EOF
'''
EXPECTED = (b'"layer","linear_paths","circles","arcs","text_labels","supported_total"\r\n'
            b'"\'=EMPTY","0","0","0","0","0"\r\n'
            b'"Geometry","2","1","1","2","6"\r\n')
FINISH = {'delivery_profile': 'cad_file', 'intended_user': 'Project owner',
          'workflow': 'DXF layer inventory CSV', 'artifact_refs': ['drawing.dxf']}


class Files(Lifecycle):
    interrupt_at = None

    def put_project_file(self, *args, **kwargs):
        result = super().put_project_file(*args, **kwargs)
        if self.writes == self.interrupt_at:
            self.interrupt_at = None
            raise RuntimeError('lost response after committed file')
        return result


@pytest.fixture
def release(monkeypatch):
    files, store = Files('drawing.dxf', DXF), Store('drawing.dxf', DXF)
    project, org, actor = (uuid.uuid4() for _ in range(3))
    authorized = {'active': True}

    def authority(tenant, project_id):
        if not authorized['active'] or str(project_id) != str(project):
            raise PermissionError('Current project access denied')
        return org, project, actor

    monkeypatch.setattr(runtime, 'authority', authority)
    monkeypatch.setattr(runtime, '_STORE', store)
    monkeypatch.setattr(runtime, '_LIFECYCLE', files)
    monkeypatch.setattr(campaigns, '_STORE', SimpleNamespace(get_campaign=lambda *args:
                        {'prompt': 'Finish the larger design', 'status': 'accepted'}))
    store.release.update(delivery_profile='cad_file', contract=runtime.compile_finish(
        'tenant', project, 'campaign', copy.deepcopy(FINISH)))
    app = FastAPI()
    app.include_router(campaigns.router)
    app.dependency_overrides[deps.require_tenant] = lambda: 'tenant'
    return store, files, project, authorized, TestClient(app)


def test_exact_two_layer_counts_and_spreadsheet_safe_bytes():
    assert adapter.render(DXF) == EXPECTED
    rows = list(csv.reader(io.StringIO(EXPECTED.decode(), newline='')))
    assert rows[1] == ["'=EMPTY", '0', '0', '0', '0', '0']
    assert rows[2] == ['Geometry', '2', '1', '1', '2', '6']
    other = DXF.replace(b'CIRCLE\n8\nGeometry', b'CIRCLE\n8\n=EMPTY')
    changed = list(csv.reader(io.StringIO(adapter.render(other).decode(), newline='')))
    assert changed[1][2] == '1' and changed[2][2] == '0'


@pytest.mark.parametrize('raw', [DXF.replace(b'0\nEOF\n', b''), DXF + b'0\n',
    DXF.replace(b'11\n10', b'11\nnan'), b'AutoCAD Binary DXF\x00', b'\xff',
    b'x' * (delivery.MAX_BYTES + 1), DXF.replace(b'11\n10\n', b'')],
    ids=['truncated', 'odd-pair', 'nonfinite', 'binary', 'encoding', 'oversized', 'missing-coordinate'])
def test_invalid_dxf_cannot_render(raw):
    with pytest.raises((ValueError, UnicodeError)):
        adapter.render(raw)


@pytest.mark.parametrize('raw', [
    DXF.replace(b'40\n1\n0\nARC', b'40\n-1\n0\nARC'),
    DXF.replace(b'40\n1\n0\nARC', b'40\n0\n0\nARC'),
    DXF.replace(b'40\n2\n50\n0', b'40\n-2\n50\n0'),
    DXF.replace(b'40\n2\n50\n0', b'40\n0\n50\n0'),
    DXF.replace(b'10\n5\n20\n5\n0\nCIRCLE', b'0\nCIRCLE'),
    DXF.replace(b'1\nLabel\n0\nMTEXT', b'1\n\n0\nMTEXT'),
    DXF.replace(b'1\nSecond label\n0\nENDSEC', b'1\n\n0\nENDSEC'),
    DXF.replace(b'0\nENDSEC\n0\nEOF',
        b'0\nPOLYLINE\n8\nGeometry\n0\nVERTEX\n10\n0\n20\n0\n0\nSEQEND\n0\nENDSEC\n0\nEOF'),
], ids=['negative-circle', 'zero-circle', 'negative-arc', 'zero-arc',
        'short-lwpolyline', 'empty-text', 'empty-mtext', 'short-polyline'])
def test_silently_dropped_supported_entity_prevents_ready_recipe(raw):
    # Valid LINE geometry remains, so nonempty drawing validation alone passes.
    assert b'0\nLINE\n' in raw
    with pytest.raises(delivery.DeliveryConflict, match='unusable supported direct entity'):
        adapter.render(raw)
    snapshot = {'files': [{'path': 'drawing.dxf', 'content': raw.decode()}]}
    assert adapter.compile_recipe(snapshot, ['drawing.dxf']) == (None, None)


@pytest.mark.parametrize('diagnostic', [{'parseErrors': ['unresolved block']}, {'blocksCapped': 201}])
def test_parser_diagnostics_cannot_be_hidden(monkeypatch, diagnostic):
    real = adapter.parse_dxf_bytes
    monkeypatch.setattr(adapter, 'parse_dxf_bytes', lambda *a, **k: dict(real(*a, **k), **diagnostic))
    with pytest.raises(delivery.DeliveryConflict, match='incomplete'):
        adapter.render(DXF)


def test_unresolved_insert_is_rejected_from_real_bytes():
    bad = DXF.replace(b'0\nENDSEC\n0\nEOF', b'0\nINSERT\n2\nMissing\n0\nENDSEC\n0\nEOF')
    with pytest.raises(delivery.DeliveryConflict, match='incomplete'):
        adapter.render(bad)


def test_recipe_selection_has_one_authorized_dxf_and_no_generic_commands(release):
    store, files, project, _, _ = release
    assert store.release['contract']['cad_recipe']['recipe_version'] == 1
    assert files.writes == 0 and files.files[0]['content'].encode() == DXF
    assert adapter.compile_recipe({'files': [{'path': 'drawing.dxf', 'content': 'broken'}]}, []) == (None, None)
    with pytest.raises(delivery.DeliveryConflict, match='one source'):
        adapter.compile_recipe(files.project_snapshot(), ['drawing.dxf', 'another.dxf'])
    for workflow in ('Convert DXF to CSV', 'Repair DXF as CSV', 'DXF layer summary JSON'):
        assert not adapter.requested(workflow)
    unsupported = runtime.compile_finish('tenant', project, 'campaign', dict(FINISH, workflow='Repair DXF as CSV'))
    assert unsupported['selected_artifact'] is None and 'cad_recipe' not in unsupported


def test_runtime_http_all_stages_frozen_source_and_tampered_output(release):
    store, files, project, active, client = release
    rid = store.release['release_id']
    result = runtime.advance('tenant', project, 'campaign', rid)
    assert result['release']['status'] == 'finished'
    assert [stage['stage'] for stage in store.stages] == list(runtime.STAGES)
    assert all(stage['status'] == 'passed' and stage['evidence']['checks'] for stage in store.stages)
    assert files.writes == 2 and files.files[0]['content'].encode() == DXF
    base = f'/api/campaigns/campaign/releases/{rid}/artifacts/'
    output_url = base + adapter.OUTPUT_NAME + '?project_id=' + str(project)
    source_url = base + 'source.dxf?project_id=' + str(project)
    response = client.get(output_url)
    assert response.status_code == 200 and response.content == EXPECTED
    assert response.headers['content-disposition'].startswith('attachment;')
    assert client.get(source_url).content == DXF
    assert client.get(output_url.replace(str(project), str(uuid.uuid4()))).status_code in (403, 404)
    recipe = store.stages[-1]['evidence']['replay_recipe']
    assert 'source.dxf' in recipe and 'dxf-layer-summary version 1' in recipe
    assert store.release['contract']['original_goal'] == 'Finish the larger design'
    files.files[0]['content'] = 'Later edited original'
    assert client.get(output_url).content == EXPECTED
    assert runtime.snapshot('tenant', project, 'campaign', rid)['current_verification']['status'] == 'passed'
    runtime.advance('tenant', project, 'campaign', rid)
    assert files.writes == 2
    active['active'] = False
    assert client.get(output_url).status_code == 403 and client.get(source_url).status_code == 403
    with pytest.raises(PermissionError):
        runtime.advance('tenant', project, 'campaign', rid)
    active['active'] = True
    files.files[-1]['content'] = 'tampered\noutput\n'
    assert client.get(output_url).status_code == 409
    current = runtime.snapshot('tenant', project, 'campaign', rid)
    assert current['current_verification']['status'] == 'failed' and current['deliverables'] == []


@pytest.mark.parametrize('write', [1, 2])
def test_interrupted_publication_uses_readback_without_duplicate_files(release, write):
    store, files, project, _, _ = release
    rid = store.release['release_id']
    files.interrupt_at = write
    with pytest.raises(RuntimeError, match='lost response'):
        runtime.advance('tenant', project, 'campaign', rid)
    predecessor = copy.deepcopy(store.stages)
    assert len(predecessor) == 1 and predecessor[0]['stage'] == 'implementation'
    files.files[0]['content'] = 'Original edited after committed source copy'
    result = runtime.advance('tenant', project, 'campaign', rid)
    assert result['release']['status'] == 'finished' and files.writes == 2
    assert store.stages[:1] == predecessor
    assert len({row['path'] for row in files.files}) == 3
    assert adapter.read(files.project_snapshot(), store.release, adapter.OUTPUT_NAME)[0] == EXPECTED


def test_source_change_between_implementation_and_publication_stays_incomplete(release):
    store, files, project, _, _ = release
    proof = adapter.run_stage(runtime, 'tenant', project, 'campaign', store.get_release(), 'implementation')
    store.record_stage(stage='implementation', status='passed', producer='task_ledger',
        operation_key='initial', evidence=dict(proof, contract_version=1,
        source_revision=store.release['contract']['selected_artifact']['sha256'],
        checks=[{'check_id': 'implementation.verified', 'status': 'passed', 'evidence': proof}]))
    files.files[0]['content'] = DXF.replace(b'11\n10', b'11\n20').decode()
    result = runtime.advance('tenant', project, 'campaign', store.release['release_id'])
    assert result['release']['status'] != 'finished' and files.writes == 0
    assert store.stages[-1]['stage'] == 'publication' and store.stages[-1]['status'] == 'unavailable'


def test_versioned_capability_and_closed_contract(release, monkeypatch):
    import job_pg_store
    job_pg_store._db()  # Register the existing platform package, no DB connection.
    canonical = importlib.import_module('leaf_platform.campaign_release')
    contract = release[0].release['contract']
    canonical._contract(copy.deepcopy(contract), 'cad_file')
    for profile, mutate in [
        ('web_tool', lambda c: None),
        ('cad_file', lambda c: c['cad_recipe'].update(recipe_version=2)),
        ('cad_file', lambda c: c['cad_recipe'].update(recipe_version=True)),
        ('cad_file', lambda c: c['cad_recipe'].update(extra='unsupported')),
        ('cad_file', lambda c: c['cad_recipe']['source_artifact'].update(media_type='application/json')),
        ('cad_file', lambda c: c['selected_artifact'].update(media_type='image/vnd.dxf')),
        ('cad_file', lambda c: c.update(artifact_refs=['other.dxf'])),
        ('cad_file', lambda c: c.update(web_recipe=c['cad_recipe'])),
        ('cad_file', lambda c: c.update(transform_recipe=c['cad_recipe'])),
    ]:
        invalid = copy.deepcopy(contract)
        mutate(invalid)
        with pytest.raises(canonical.CampaignError):
            canonical._contract(invalid, profile)
    monkeypatch.setattr(capabilities.deps, 'effective_tools_with_provenance', lambda tenant: [])
    selected = capabilities.resolve('tenant', 'cad_file', existing_artifact=True, cad_recipe=True)
    assert selected['readiness'] == 'available'
    assert selected['selected_capability']['recipe_id'] == 'dxf-layer-summary'
    assert selected['selected_capability']['version'] == 1


@pytest.mark.parametrize('field', ['cad_recipe', 'command', 'status', 'grants'])
def test_public_finish_rejects_adapter_and_authority_claims(release, field):
    _, _, project, _, client = release
    response = client.post('/api/campaigns', headers={'Idempotency-Key': 'closed-cad'}, json={
        'project_id': str(project), 'title': 'CAD', 'prompt': 'Finish the drawing', 'mode': 'finish',
        'finish': dict(FINISH, **{field: 'forged'})})
    assert response.status_code == 400
