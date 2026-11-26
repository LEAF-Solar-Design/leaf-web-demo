from contextlib import contextmanager
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from uuid import UUID

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dxf_intake import parse_dxf_bytes
from test_x07_customer_profile_store import connection
from x07_role_selection import SelectionError, compare_placement_override, select_modules


def insert(handle, name, layer, x, y, *extra):
    return ((0, 'INSERT'), (5, handle), (2, name), (8, layer),
            (10, x), (20, y), *extra)


def block(name, x, y, *entities):
    return ((0, 'BLOCK'), (2, name), (10, x), (20, y),
            *entities, (0, 'ENDBLK'))


def parse(definitions, entities):
    pairs = ((0, 'SECTION'), (2, 'BLOCKS'), *definitions, (0, 'ENDSEC'),
             (0, 'SECTION'), (2, 'ENTITIES'), *entities, (0, 'ENDSEC'), (0, 'EOF'))
    return parse_dxf_bytes(''.join(f'{code}\n{value}\n' for code, value in pairs).encode())


def outline(handle, vertices, layer='ROOF'):
    coordinates = tuple(pair for x, y in vertices for pair in ((10, x), (20, y)))
    return ((0, 'LWPOLYLINE'), (5, handle), (8, layer), (90, len(vertices)), (70, 1), *coordinates)


@pytest.mark.parametrize(('kind', 'layers'), [
    ('outline', ('Deck East', 'Deck West')),
    ('outline', ('Roof Lower', 'Roof Upper')),
    ('insert', ('Roof East', 'Roof West')),
])
def test_mounted_roof_inheritance_candidates_preserve_original_pin(
        connection, monkeypatch, kind, layers):
    from fastapi.testclient import TestClient
    from app import app
    from routers import x07_profiles as profiles
    from x07_postgres_profile_store import PostgresProfileStore

    org, project, drawing, before, after = (UUID(int=num) for num in range(1, 6))
    module = insert('F1', 'Panel', 'MODULES', 1, 1)
    original_outline = outline('E1', [(0, 0), (20, 0), (20, 5), (0, 5)], 'Roof Middle')
    entities, expected = [], []
    names = ('Level East', 'Level West')
    for x, handle, layer, name in zip((0, 10), ('E1', 'E2'), layers, names):
        role = {'layer': layer}
        if kind == 'insert':
            entities.extend(insert(handle, name, layer, x, 0))
            bounds = [x, 0, x, 0]
            role['block'] = name
        else:
            entities.extend(outline(handle, [(x, 0), (x + 10, 0), (x + 10, 5), (x, 5)], layer))
            bounds = [x, 0, x + 10, 5]
        expected.append({'handle': handle, 'kind': kind, 'role': role, 'bounds': bounds})

    # Shared labels cannot establish an exact successor for a layer-only role.
    clip = [(30, 0), (35, 0), (35, 5), (30, 5)]
    entities.extend(outline('A3', clip, 'Unrelated'))
    entities.extend(outline('A4', [(x + 10, y) for x, y in clip], 'Unrelated'))
    definitions = (*block('Panel', 0, 0), *block(names[0], 0, 0), *block(names[1], 0, 0))
    intakes = {before: parse(definitions, (*module, *original_outline)),
               after: parse(definitions, (*module, *entities))}

    store = PostgresProfileStore(connection, str(org))
    roles = {'module': {'block': 'Panel', 'layer': 'MODULES'}, 'roof': {'layer': 'Roof Middle'}}
    with connection.db:
        store.save_version(str(org), roles, expected_version=0)
        pin = store.apply_to_project(str(project), str(org), expected_version=1)

    @contextmanager
    def transaction():
        with connection.db:
            yield connection

    def resolve(tenant, requested_project, revision, *, drawing_id, write):
        assert (tenant, requested_project, drawing_id) == (org, project, drawing)
        assert not write or revision == after
        return SimpleNamespace(binding=SimpleNamespace(organization_id=org), intake=intakes[revision])

    monkeypatch.setitem(app.dependency_overrides, profiles.deps.require_active_tenant, lambda: org)
    monkeypatch.setattr(profiles.context_service, 'resolve_context', resolve)
    monkeypatch.setattr(profiles.platform_link, 'platform_db', lambda: SimpleNamespace(transaction=transaction))
    client = TestClient(app)
    older, newer = (profiles.PATH.format(project_id=project, drawing_id=drawing, input_version_id=revision)
                    for revision in (before, after))
    connection.statements.clear()
    old = client.get(older)
    new = client.get(newer)
    assert old.status_code == new.status_code == 200, new.text
    assert old.json()['roof_inheritance_candidates'] == []
    assert new.json()['input_version_id'] == str(after)
    assert new.json()['roof_inheritance_candidates'] == expected
    assert new.json()['application_record'] == old.json()['application_record'] == pin
    assert all(sql.startswith('SELECT') for sql in connection.statements)

    selected = {**roles, 'roof': expected[1]['role']}
    saved = client.put(newer, json={'expected_version': 1, 'roles': selected})
    assert saved.status_code == 200, saved.text
    assert saved.json()['profile']['roles'] == selected
    assert client.get(newer).json()['roof_inheritance_candidates'] == []
    assert store.application_record(str(project), str(org)) == pin


@pytest.mark.parametrize(('rotation', 'offset'), [(90, 0), (180, 120), (270, -65)])
def test_mounted_placement_changes_keep_literal_role_tags(monkeypatch, rotation, offset):
    from fastapi.testclient import TestClient
    from app import app
    from routers import x07_profile_portfolio as portfolio

    org, project, drawing, before, after = (UUID(int=num) for num in range(1, 6))
    first, second, layer = 'PV.A+1', 'PV-A(1)', 'PV-South.(A)+'
    common = [('A1', first, 1, 1), ('B1', second, 3, 3),
              ('C1', 'PVxAA1', 1, 3), ('D1', 'PV-A1', 3, 1)]
    definitions = tuple(pair for tag in (first, second, 'PVxAA1', 'PV-A1')
                        for pair in block(tag, 0, 0))

    def intake(rows, angle):
        entities = []
        for handle, tag, x, y in rows:
            x, y = {0: (x, y), 90: (-y, x), 180: (-x, -y), 270: (y, -x)}[angle]
            entities.extend(insert(handle, tag, layer, x + offset, y + offset, (50, angle)))
        return parse(definitions, entities)

    intakes = {
        before: intake([*common, ('A2', first, 2, 2)], 0),
        after: intake([*common, ('A2', first, 9, 9), ('B2', second, 2, 3)], rotation),
    }
    monkeypatch.setitem(app.dependency_overrides, portfolio.deps.require_active_tenant, lambda: org)
    monkeypatch.setattr(portfolio.context_service, '_access', lambda *_args, **_kwargs: (org, None))
    monkeypatch.setattr(portfolio.context_service, 'resolve_context',
                        lambda _tenant, _project, version, **_kwargs: SimpleNamespace(
                            binding=SimpleNamespace(organization_id=org), intake=intakes[version]))
    path = portfolio.PLACEMENT_CHANGE_PREVIEW_PATH.format(
        project_id=project, drawing_id=drawing, input_version_id=after)
    client = TestClient(app)
    expected = [
        {'previous_count': 2, 'current_count': 1, 'count_delta': -1, 'retained_count': 1,
         'removed_paths': ['A2[0,0]'], 'added_paths': []},
        {'previous_count': 1, 'current_count': 2, 'count_delta': 1, 'retained_count': 1,
         'removed_paths': [], 'added_paths': ['B2[0,0]']},
    ]
    for tag, row in zip((first, second), expected):
        response = client.post(path, json={
            'previous_input_version_id': str(before), 'block': tag.lower(), 'layer': layer.lower(),
            'windows': [{'within': [offset - 4, offset - 4, offset + 4, offset + 4]}],
        })
        assert response.status_code == 200, response.text
        assert response.json() == {'previous_input_version_id': str(before),
                                   'input_version_id': str(after), 'windows': [row],
                                   'degraded_mode': False, 'error': None}


@pytest.mark.parametrize(('current_role', 'expected'), [
    ({'block': 'Other', 'layer': 'MODULES'}, [['C1[0,0]'], ['C2[0,0]']]),
    ({'block': 'Panel', 'layer': 'ALT'}, [['D1[0,0]'], []]),
    ({'block': 'Panel', 'layer': 'MODULES'}, [[], []]),
], ids=['new-block', 'new-layer-one-copy', 'same-role'])
def test_mounted_placement_changes_compare_roles_per_copy(monkeypatch, current_role, expected):
    from fastapi.testclient import TestClient
    from app import app
    from routers import x07_profile_portfolio as portfolio

    org, project, drawing, version = (UUID(int=num) for num in range(1, 5))
    intake = parse((*block('Panel', 0, 0), *block('Other', 0, 0)), (
        *outline('E1', [(0, 0), (10, 0), (10, 10), (0, 10)]),
        *outline('E2', [(20, 0), (30, 0), (30, 10), (20, 10)]),
        *insert('A1', 'Panel', 'MODULES', 1, 1), *insert('A2', 'Panel', 'MODULES', 21, 1),
        *insert('C1', 'Other', 'MODULES', 2, 2), *insert('C2', 'Other', 'MODULES', 22, 2),
        *insert('D1', 'Panel', 'ALT', 3, 3), *insert('D2', 'Panel', 'ALT', 40, 3),
    ))
    resolutions = []

    def resolve(_tenant, _project, revision, **_kwargs):
        resolutions.append(revision)
        return SimpleNamespace(binding=SimpleNamespace(organization_id=org), intake=intake)

    def reject_storage():
        pytest.fail('Role placement preview touched profile storage')

    monkeypatch.setitem(app.dependency_overrides, portfolio.deps.require_active_tenant, lambda: org)
    monkeypatch.setattr(portfolio.context_service, '_access', lambda *_args, **_kwargs: (org, None))
    monkeypatch.setattr(portfolio.context_service, 'resolve_context', resolve)
    monkeypatch.setattr(portfolio.platform_link, 'platform_db', reject_storage)
    path = portfolio.PLACEMENT_CHANGE_PREVIEW_PATH.format(
        project_id=project, drawing_id=drawing, input_version_id=version)
    payload = {'previous_input_version_id': str(version), **current_role,
               'previous_role': {'block': 'Panel', 'layer': 'MODULES'},
               'windows': [{'within': {'outline_handle': handle}} for handle in ('E1', 'E2')]}
    client = TestClient(app)
    response = client.post(path, json=payload)
    assert response.status_code == 200, response.text
    rows = response.json()['windows']
    assert [row['added_paths'] for row in rows] == expected
    same_role = current_role == payload['previous_role']
    assert [row['removed_paths'] for row in rows] == ([[], []] if same_role else [['A1[0,0]'], ['A2[0,0]']])
    assert [row['count_delta'] for row in rows] == ([0, 0] if same_role else [len(paths) - 1 for paths in expected])
    assert [row['retained_count'] for row in rows] == ([1, 1] if same_role else [0, 0])
    for invalid in ({'block': 'Panel'}, {'block': 'Panel', 'layer': 7},
                    {**payload['previous_role'], 'window': 'E2'}):
        assert client.post(path, json={**payload, 'previous_role': invalid}).status_code == 422
    assert resolutions == [version]


@pytest.mark.parametrize(('revised_name', 'rotation', 'shift', 'columns', 'expected',
                          'panel_shift', 'outline_shift'), [
    ('Rack', 0, 0, 1, ['unchanged', 'missing'], 0, 8),
    ('Rack', 90, 2, 2, ['moved', 'moved'], 8, 0),
    ('RevisedRack', 180, 0, 2, ['missing', 'missing'], 16, 8),
])
def test_mounted_override_preview_and_confirmation_use_exact_revisions(
        connection, monkeypatch, revised_name, rotation, shift, columns, expected,
        panel_shift, outline_shift):
    from fastapi.testclient import TestClient
    from app import app
    from routers import x07_profile_portfolio as portfolio, x07_profiles as profiles

    org, project, drawing, before, after, absent = (UUID(int=num) for num in range(1, 7))
    definitions = (*block('Panel', 0, 0),
                   *block('Rack', 0, 0, *insert('C1', 'Panel', '0', 2, 1)),
                   *block('RevisedRack', 0, 0, *insert('C2', 'Panel', '0', 2, 1)))
    independent = insert('B1', 'Rack', 'MODULES', 40, 0)
    original = insert('A1', 'Rack', 'MODULES', 0, 3, (50, rotation), (70, 2), (44, 10))
    updated = insert('A1', revised_name, 'MODULES', shift, 3,
                     (50, rotation), (70, columns), (44, 10))
    roof = outline('E1', [(996, -4), (1004, -4), (1004, 4), (996, 4)])
    revised_roof = outline('E1', [(x + outline_shift, y) for x, y in
                                  [(1004, 4), (1004, -4), (1000, -4), (996, -4), (996, 4)]])
    intakes = {
        before: parse(definitions, (*original, *independent, *roof,
                                    *insert('F1', 'Panel', 'MODULES', 1000, 0))),
        after: parse(definitions, (*updated, *independent, *revised_roof,
                                   *insert('F1', 'Panel', 'MODULES', 1000 + panel_shift, 0))),
    }
    resolutions = []
    confirmation_enabled = False

    def authorize(tenant, project_id, *, write):
        assert tenant == org and write is False
        if project_id != project:
            portfolio.context_service.refuse('CONTEXT_NOT_FOUND')
        return org, None

    def resolve(tenant, project_id, version, *, drawing_id, write):
        assert write is confirmation_enabled
        authorize(tenant, project_id, write=False)
        resolutions.append(version)
        if drawing_id != drawing:
            portfolio.context_service.refuse('CONTEXT_NOT_FOUND')
        if version == absent:
            portfolio.context_service.refuse('INTAKE_UNAVAILABLE')
        return SimpleNamespace(binding=SimpleNamespace(organization_id=org), intake=intakes[version])

    def unexpected_store_access():
        pytest.fail('read-only preview opened the profile database')

    monkeypatch.setitem(app.dependency_overrides, portfolio.deps.require_active_tenant, lambda: org)
    monkeypatch.setattr(portfolio.context_service, '_access', authorize)
    monkeypatch.setattr(portfolio.context_service, 'resolve_context', resolve)
    monkeypatch.setattr(portfolio.platform_link, 'platform_db', unexpected_store_access)
    client = TestClient(app)
    payload = {'previous_input_version_id': str(before), 'block': 'Panel', 'layer': 'MODULES',
               'overrides': [
                   {'within': [-100, -100, 100, 100],
                    'placement_paths': ['A1[0,0]/C1[0,0]', 'a1[0,1]/c1[0,0]']},
                   {'within': [35, -2, 45, 2], 'placement_paths': ['B1[0,0]/C1[0,0]']},
               ]}

    def request(version, body=payload, *, draw=drawing, headers=None,
                route=portfolio.PLACEMENT_OVERRIDE_PREVIEW_PATH):
        path = route.format(project_id=project, drawing_id=draw, input_version_id=version)
        return client.post(path, json=body, headers=headers)

    response = request(after, headers={'X-Org-Id': str(org), 'X-Project-Id': str(project)})
    assert response.status_code == 200, response.text
    assert response.headers['Cache-Control'] == 'no-store'
    result = response.json()
    assert (result['previous_input_version_id'], result['input_version_id']) == (str(before), str(after))
    first, second = result['overrides']
    assert [placement['status'] for placement in first['placements']] == expected
    assert first['all_paths_match'] == ('missing' not in expected)
    assert first['selection']['count'] == len(expected) - expected.count('missing')
    assert second['placements'] == [{
        'path': 'B1[0,0]/C1[0,0]', 'status': 'unchanged',
        'previous_origin': [42.0, 1.0], 'current_origin': [42.0, 1.0],
        'current_drawing_origin': [42.0, 1.0],
    }]
    assert resolutions == [before, after]

    assert request(before).json()['overrides'][0]['all_paths_match']
    assert resolutions == [before, after, before]
    motion = request(after, dict(payload, overrides=[
        {'within': {'outline_handle': 'E1'}, 'placement_paths': ['F1[0,0]']},
    ]))
    assert motion.status_code == 200, motion.text
    [roof_review] = motion.json()['overrides']
    assert roof_review['boundary_changed'] is bool(outline_shift)
    assert roof_review['placements'] == [{
        'path': 'F1[0,0]', 'status': 'missing', 'previous_origin': [1000, 0],
        'current_origin': None, 'current_drawing_origin': [1000 + panel_shift, 0],
    }]
    assert request(after, draw=absent).status_code == 404
    assert request(after, headers={'X-Org-Id': str(absent)}).status_code == 404
    response = request(absent)
    assert response.status_code == 409
    assert response.json()['detail']['reason_code'] == 'SIP_R1_INTAKE_UNAVAILABLE'
    invalid = {'within': [0, 0, 1, 1], 'placement_paths': ['no-such-path']}
    response = request(after, dict(payload, overrides=[invalid]))
    assert response.status_code == 409
    assert response.json()['detail']['reason_code'] == 'PLACEMENT_OVERRIDE_INVALID'
    assert request(after, dict(payload, overrides=payload['overrides'] * 3)).status_code == 422

    windows = [[-20, -20, 25, 25], [35, -2, 45, 2], [-20, -20, 45, 25]]
    changes = {key: payload[key] for key in ('previous_input_version_id', 'block', 'layer')}
    changes['windows'] = [{'within': coords} for coords in windows]
    route = portfolio.PLACEMENT_CHANGE_PREVIEW_PATH
    response = request(after, changes, route=route)
    assert response.status_code == 200, response.text
    assert response.headers['Cache-Control'] == 'no-store'
    report = response.json()
    assert (report['previous_input_version_id'], report['input_version_id']) == (str(before), str(after))
    removed = [f'A1[0,{index}]/C1[0,0]' for index, status in enumerate(expected)
               if status == 'missing']
    added = ['A1[0,0]/C2[0,0]', 'A1[0,1]/C2[0,0]'] if revised_name == 'RevisedRack' else []
    assert report['windows'] == [
        {'previous_count': 2, 'current_count': columns, 'count_delta': columns - 2,
         'retained_count': 2 - len(removed), 'removed_paths': removed, 'added_paths': added},
        {'previous_count': 1, 'current_count': 1, 'count_delta': 0,
         'retained_count': 1, 'removed_paths': [], 'added_paths': []},
        {'previous_count': 3, 'current_count': columns + 1, 'count_delta': columns - 2,
         'retained_count': 3 - len(removed), 'removed_paths': removed, 'added_paths': added},
    ]
    assert request(after, changes, route=route, draw=absent).status_code == 404
    assert request(absent, changes, route=route).status_code == 409

    @contextmanager
    def transaction():
        with connection.db:
            yield connection

    monkeypatch.setattr(profiles.platform_link, 'platform_db',
                        lambda: SimpleNamespace(transaction=transaction))
    confirmation_enabled = True
    confirmation = {key: payload[key] for key in ('block', 'layer')}
    confirmation.update(expected_version=0, design_window=payload['overrides'][0])
    malformed = {**confirmation, 'design_window': {'within': [True, 0, 1, 1],
                                                   'placement_paths': ['A1[0,0]/C1[0,0]']}}
    assert request(after, malformed, route=profiles.PATH).status_code == 422

    if removed:
        response = request(after, confirmation, route=profiles.PATH)
        assert response.status_code == 409
        assert response.json()['error']['reason_code'] == 'X07_PROFILE_CONFLICT'
    assert connection.db.execute('SELECT COUNT(*) FROM x07_profile_versions').fetchone()[0] == 0
    assert connection.db.execute('SELECT COUNT(*) FROM x07_project_applications').fetchone()[0] == 0
    assert connection.db.execute('SELECT COUNT(*) FROM x07_confirmation_evidence').fetchone()[0] == 0

    chosen_paths = [f'A1[0,{index}]/{"C2" if added else "C1"}[0,0]' for index in range(columns)]
    window = {**confirmation['design_window'], 'placement_paths': chosen_paths}
    refreshed = {**confirmation, 'design_window': {**window, 'placement_paths': [
        path.lower() for path in chosen_paths
    ]}}
    response = request(after, refreshed, route=profiles.PATH)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['input_version_id'] == str(after)
    assert result['selection']['count'] == columns
    assert [item['path'] for item in result['selection']['placements']] == chosen_paths
    assert result['selection']['design_window'] == window
    evidence = connection.db.execute(
        'SELECT input_version_id, design_window_json FROM x07_confirmation_evidence'
    ).fetchall()
    assert [(row[0], json.loads(row[1])) for row in evidence] == [(str(after), window)]

    normalized = {**refreshed, 'design_window': window}
    assert request(after, normalized, route=profiles.PATH).json() == result
    different = {**normalized, 'design_window': payload['overrides'][1]}
    assert request(after, different, route=profiles.PATH).status_code == 409
    assert request(before, normalized, route=profiles.PATH).status_code == 409
    assert connection.db.execute('SELECT COUNT(*) FROM x07_profile_versions').fetchone()[0] == 1
    assert connection.db.execute('SELECT COUNT(*) FROM x07_project_applications').fetchone()[0] == 1


class SelectionTests(unittest.TestCase):
    def select(self, rows, window=(0, 0, 2, 2)):
        return select_modules({'inserts': rows}, block='Panel', layer='MODULES', window=window)

    def row(self, x, y, **extra):
        return {'name': 'Panel', 'layer': 'MODULES', 'x': x, 'y': y, **extra}

    def test_three_repeated_copies_pick_exactly_one(self):
        rows = [self.row(x + offset, y) for offset in (0, 10, 20)
                for x, y in ((0, 0), (1, 1), (2, 2))]
        self.assertEqual(self.select(rows, (10, 0, 12, 2))['insert_indices'], [3, 4, 5])

    def test_inclusive_edges_and_no_implicit_tolerance(self):
        rows = [self.row(x, y) for x, y in ((0, 0), (0, 2), (2, 0), (2, 2),
                                            (-0.000001, 1), (2.000001, 1))]
        self.assertEqual(self.select(rows)['count'], 4)

    def test_case_insensitive_symbols_but_both_must_match(self):
        rows = [self.row(1, 1, name='pAnEl', layer='modules'), self.row(1, 1, name='Other'),
                self.row(1, 1, layer='Other')]
        self.assertEqual(self.select(rows)['count'], 1)

    def test_point_window_and_ignored_transform(self):
        self.assertEqual(self.select([self.row(1, 1, rot=5, scale=[9, 9], z=200)],
                                     (1, 1, 1, 1))['count'], 1)

    def test_invalid_windows_and_matching_origins_refuse(self):
        for window in ((2, 0, 1, 2), (0, 0, float('inf'), 2), (0, 0, True, 2), (0, 0, 2)):
            with self.subTest(window=window), self.assertRaises(SelectionError):
                self.select([], window)
        for value in (float('nan'), float('inf'), True, '1', None):
            with self.subTest(value=value), self.assertRaises(SelectionError):
                self.select([self.row(value, 1)])

    def test_no_input_mutation_or_output_alias(self):
        intake = {'inserts': [self.row(1, 1, handle='A')]}
        window = [0, 0, 2, 2]
        before = copy.deepcopy(intake)
        result = select_modules(intake, block='Panel', layer='MODULES', window=window)
        result['design_window'][0] = 90
        self.assertEqual(window, [0, 0, 2, 2])
        self.assertEqual(intake, before)

    def test_placement_override_preserves_rack_and_array_identity_across_revisions(self):
        retained = 'A1[0,0]/C1[0,0]'
        removed = 'A1[0,1]/C1[0,0]'
        alternative = 'B1[0,0]/C1[0,0]'
        definitions = (*block('Panel', 0, 0),
                       *block('Rack', 0, 0, *insert('C1', 'Panel', '0', 2, 1)),
                       *block('RevisedRack', 0, 0, *insert('C2', 'Panel', '0', 2, 1)))
        cases = (
            ('reduced array', 0, 0, 'Rack', 0, 1, ('unchanged', 'missing')),
            ('moved rack', 90, 30, 'Rack', 2, 2, ('moved', 'moved')),
            ('replaced child', 180, 60, 'RevisedRack', 0, 2, ('missing', 'missing')),
        )
        for change, rotation, offset, revised_block, shift, columns, expected in cases:
            with self.subTest(change=change):
                other = insert('B1', 'Rack', 'MODULES', offset, 3, (50, rotation), (70, 2), (44, 10))
                original = insert('A1', 'Rack', 'MODULES', offset, 3, (50, rotation), (70, 2), (44, 10))
                revised = insert('A1', revised_block, 'MODULES', offset + shift, 3,
                                 (50, rotation), (70, columns), (44, 10))
                before = parse(definitions, (*original, *other))
                after = parse(definitions, (*revised, *other))

                scope = {'within': [-100, -100, 100, 100]}
                pick_a = dict(scope, placement_paths=[removed.lower(), retained])
                pick_b = dict(scope, placement_paths=[alternative])
                role = {'block': 'Panel', 'layer': 'MODULES'}
                a = select_modules(before, **role, window=pick_a)
                b = select_modules(before, **role, window=pick_b)
                self.assertEqual([item['path'] for item in a['placements']], [retained, removed])
                self.assertEqual([item['path'] for item in b['placements']], [alternative])
                self.assertEqual(a['design_window']['placement_paths'], [retained, removed])
                self.assertEqual(select_modules(after, **role, window=pick_b), b)

                reopened = compare_placement_override(before, after, **role, window=pick_a)
                self.assertEqual([item['status'] for item in reopened['placements']], list(expected))
                self.assertEqual(reopened['all_paths_match'], change == 'moved rack')
                for item, selected in zip(reopened['placements'], a['placements']):
                    x, y = selected['origin']
                    self.assertEqual(item['previous_origin'], [x, y])
                    if item['status'] == 'missing':
                        self.assertIsNone(item['current_origin'])
                    else:
                        actual_x, actual_y = item['current_origin']
                        self.assertAlmostEqual(actual_x, x + shift)
                        self.assertAlmostEqual(actual_y, y)
                if 'missing' in expected:
                    with self.assertRaisesRegex(SelectionError, 'no longer matches'):
                        select_modules(after, **role, window=pick_a)
                else:
                    self.assertEqual(select_modules(after, **role, window=pick_a), reopened['selection'])

    def test_native_dxf_uses_occurrence_paths_and_world_origins(self):
        panel = block('Panel', 0, 0)
        rack = block('Rack', 0, 0, *insert('C1', 'Panel', '0', 2, 3))
        based_rack = block('Rack', 1, 2, *insert('C1', 'Panel', '0', 3, 5))
        bracket = block('Bracket', 1, 0, *insert('D1', 'Panel', '0', 3, 0))
        layered_rack = block('Rack', 0, 0, *insert('C1', 'Bracket', '0', 2, 3))
        cases = (
            ('mirrored', (*panel, *rack),
             (*insert('A1', 'Rack', 'MODULES', 0, 0, (41, -1)),
              *insert('B1', 'Rack', 'MODULES', 30, 0, (41, -1))),
             (((-2, 3), 'A1[0,0]/C1[0,0]'), ((28, 3), 'B1[0,0]/C1[0,0]'))),
            ('rotated with a nonzero base', (*panel, *based_rack),
             insert('A1', 'Rack', 'MODULES', 20, 10, (50, 90)),
             (((17, 12), 'A1[0,0]/C1[0,0]'),)),
            ('three levels with a row and column array', (*panel, *bracket, *layered_rack),
             (*insert('A1', 'Rack', 'MODULES', 0, 0, (70, 2), (71, 2), (44, 10), (45, 20)),
              *insert('B1', 'Rack', 'MODULES', 0, 0, (67, 1))),
             (((4, 3), 'A1[0,0]/C1[0,0]/D1[0,0]'),
              ((14, 3), 'A1[0,1]/C1[0,0]/D1[0,0]'),
              ((4, 23), 'A1[1,0]/C1[0,0]/D1[0,0]'),
              ((14, 23), 'A1[1,1]/C1[0,0]/D1[0,0]'))),
        )
        for case, definitions, entities, expected in cases:
            with self.subTest(case=case):
                intake = parse(definitions, entities)
                for (x, y), path in expected:
                    result = select_modules(intake, block='Panel', layer='MODULES',
                                            window=(x - 0.001, y - 0.001, x + 0.001, y + 0.001))
                    self.assertEqual(result['count'], 1)
                    self.assertEqual(result['placements'][0]['path'], path)
                    actual_x, actual_y = result['placements'][0]['origin']
                    self.assertAlmostEqual(actual_x, x, delta=0.000002)
                    self.assertAlmostEqual(actual_y, y, delta=0.000002)


class OutlineValidationTests(unittest.TestCase):
    def test_unsupported_outlines_fail_closed(self):
        square = [[0, 0], [2, 0], [2, 2], [0, 2]]
        valid = {'handle': 'A', 'closed': True, 'pts': square}
        cases = [
            [],
            [valid, {**valid, 'handle': 'a'}],
            [{**valid, 'closed': False}],
            [{**valid, 'space': 'paper'}],
            [{**valid, 'bulges': [0, 1, 0, 0]}],
            [{**valid, 'pts': [[0, 0, 0], [2, 0, 0], [0, 2, 1]]}],
            [{**valid, 'pts': [square[0], square[2], square[1], square[3]]}],
            [{**valid, 'pts': [[0, 0], [4, 0], [2, 0], [2, 3], [0, 3]]}],
        ]
        for polylines in cases:
            with self.subTest(polylines=polylines), self.assertRaises(SelectionError):
                select_modules({'inserts': [], 'polylines': polylines}, block='Panel',
                               layer='PV', window={'outline_handle': 'A'})
