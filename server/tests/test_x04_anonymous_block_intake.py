"""Anonymous module definitions survive the normal reader/emitter/count path."""
import copy
import hashlib
import importlib.util
from pathlib import Path
import pytest
import dxf_intake
import intake_dxf

SPEC = importlib.util.spec_from_file_location('x04_anon_count', Path(__file__).resolve().parents[1] / 'builtins/count_modules.py')
count_modules = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(count_modules)


def dxf(blocks, inserts=()):
    pairs = [(0, 'SECTION'), (2, 'TABLES'), (0, 'TABLE'), (2, 'LAYER'),
             (0, 'LAYER'), (2, '0'), (0, 'LAYER'), (2, 'Panels'), (0, 'ENDTAB'), (0, 'ENDSEC'),
             (0, 'SECTION'), (2, 'BLOCKS')]
    for name, flags in blocks:
        pairs += [(0, 'BLOCK'), (2, name), (70, flags), (10, 1), (20, 2), (30, 0),
                  (0, 'LWPOLYLINE'), (8, '0'), (90, 4), (70, 1),
                  (10, 1), (20, 2), (10, 3), (20, 2), (10, 3), (20, 3), (10, 1), (20, 3), (0, 'ENDBLK')]
    pairs += [(0, 'ENDSEC'), (0, 'SECTION'), (2, 'ENTITIES')]
    for i, name in enumerate(inserts):
        pairs += [(0, 'INSERT'), (5, f'A{i+1:X}'), (2, name), (8, 'Panels'), (10, 10+i*5), (20, 20), (30, 0)]
    pairs += [(0, 'ENDSEC'), (0, 'EOF')]
    return ('\n'.join(str(value) for pair in pairs for value in pair)+'\n').encode()


@pytest.mark.parametrize('name,flags', [('*U1', 1), ('*U2', 0), ('*D1', 1), ('Flagged', 1)])
def test_anonymous_geometry_resolves_counts_and_roundtrips(name, flags, tmp_path):
    raw = dxf([(name, flags)], [name, name]); source = tmp_path / 'source.dxf'; source.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    intake = dxf_intake.parse_dxf_file(source, source_name='anonymous.dxf'); before = copy.deepcopy(intake)
    block = intake['blocks'][name]
    assert block['complete'] is True and block['count'] == 1
    assert block['children'][0]['kind'] == 'LWPOLYLINE' and block['children'][0]['closed'] is True
    assert block['children'][0]['pts'] == [[1, 2], [3, 2], [3, 3], [1, 3]]
    assert [row['name'] for row in intake['inserts']] == [name, name]
    listing, _ = count_modules.run(intake, {})
    assert len(listing['sets']) == 1 and listing['sets'][0]['block'] == name
    result, overlay = count_modules.run(intake, {'set_id': listing['sets'][0]['set_id']})
    assert result['count'] == 2 and overlay['highlight_handles'] == ['A1', 'A2']
    emitted = intake_dxf.intake_to_dxf(intake)
    reread = dxf_intake.parse_dxf_bytes(emitted, source_name='anonymous.dxf')
    assert {k: v for k, v in reread.items() if k != 'dependency_report'} == {
        k: v for k, v in intake.items() if k != 'dependency_report'}
    assert reread['dependency_report']['external_files_opened'] is False
    records = []; current = None
    for code, value in dxf_intake._group_pairs(emitted.decode()):
        if code == 0:
            current = {'kind': value}; records.append(current)
        elif current is not None: current[code] = value
    definition = next(row for row in records if row.get('kind') == 'BLOCK' and row.get(2) == name)
    assert definition[70] == ('1' if name.startswith('*') else '0')
    assert intake == before and source.read_bytes() == raw and hashlib.sha256(source.read_bytes()).hexdigest() == digest


@pytest.mark.parametrize('name', ['*Model_Space', '*Paper_Space', '*Paper_Space12'])
def test_reserved_layouts_are_not_renderable_definitions(name):
    intake = dxf_intake.parse_dxf_bytes(dxf([(name, 0)], [name]))
    assert intake['blocks'] == {} and not intake.get('inserts')
    forged = {'layers': ['0'], 'polylines': [], 'blocks': {name: {'base': [0, 0, 0], 'children': []}}}
    with pytest.raises(intake_dxf.IntakeDxfError, match='reserved layout'):
        intake_dxf.intake_to_dxf(forged)


def test_unresolved_anonymous_insert_still_refuses():
    intake = dxf_intake.parse_dxf_bytes(dxf([], ['*U9']))
    assert not intake.get('inserts') and 'unresolved block reference *U9' in intake['parseErrors'][0]


def test_anonymous_catalogue_keeps_existing_bound():
    intake = dxf_intake.parse_dxf_bytes(dxf([(f'*U{i}', 1) for i in range(201)]))
    assert len(intake['blocks']) == 200 and intake['blocksCapped'] == 201
