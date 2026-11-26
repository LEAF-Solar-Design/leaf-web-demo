from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import json
from types import SimpleNamespace
from uuid import UUID

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_customer_profile_store import CustomerProfileStore, ProfileScopeError
from x07_postgres_profile_store import PostgresProfileStore
from x07_profile_workflow import ProfileWorkflow


class ProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'profiles.sqlite'
        self.store = CustomerProfileStore(self.path)
        self.a = {'module': {'block': 'Panel A', 'layer': 'MODULES A'}}

    def test_same_customer_reuses_profile_across_projects_and_reopen(self):
        saved = self.store.save_version('customer-a', self.a)
        first = self.store.apply_to_project('project-one', 'customer-a')
        second = CustomerProfileStore(self.path).apply_to_project('project-two', 'customer-a')
        self.assertEqual(first['profile_id'], saved['profile_id'])
        self.assertEqual(second['roles'], self.a)
        self.assertEqual(second['version'], 1)

    def test_customer_isolation_and_foreign_project_refusal(self):
        a = self.store.save_version('customer-a', self.a)
        b = self.store.save_version('customer-b', {'module': {'layer': 'MODULES B'}})
        self.store.apply_to_project('project-a', 'customer-a')
        applied = self.store.apply_to_project('project-b', 'customer-b')
        self.assertEqual(applied['profile_id'], b['profile_id'])
        self.assertNotEqual(applied['profile_id'], a['profile_id'])
        for operation in (self.store.apply_to_project, self.store.application_record):
            with self.assertRaises(ProfileScopeError):
                operation('project-a', 'customer-b')
        self.assertIsNone(self.store.apply_to_project('fresh-project', 'customer-c'))

    def test_new_version_does_not_change_old_project_pin(self):
        old = self.store.save_version('customer-a', self.a)
        self.store.apply_to_project('old-project', 'customer-a')
        new = self.store.save_version('customer-a', {'module': {'layer': 'REVISED'}}, expected_version=1)
        self.assertEqual((old['profile_id'], 2), (new['profile_id'], new['version']))
        self.assertEqual(self.store.apply_to_project('old-project', 'customer-a')['roles'], self.a)
        self.assertEqual(self.store.apply_to_project('new-project', 'customer-a')['version'], 2)

    def test_stale_edit_refuses_without_creating_version(self):
        self.store.save_version('customer-a', self.a)
        with self.assertRaises(ValueError):
            self.store.save_version('customer-a', {}, expected_version=0)
        self.assertEqual(self.store.latest('customer-a')['version'], 1)

    def test_versions_and_applications_are_immutable_at_sqlite_boundary(self):
        self.store.save_version('customer-a', self.a)
        self.store.apply_to_project('project-one', 'customer-a')
        with closing(sqlite3.connect(self.path)) as db:
            for sql in ('UPDATE profile_versions SET roles_json=\'{}\'', 'DELETE FROM profile_versions',
                        'UPDATE project_applications SET version=2', 'DELETE FROM project_applications'):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                    db.execute(sql)

    def test_input_and_return_values_do_not_alias_storage(self):
        saved = self.store.save_version('customer-a', self.a)
        saved['roles']['module']['layer'] = 'changed'
        self.assertEqual(self.store.latest('customer-a')['roles'], self.a)
        with self.assertRaises(ValueError):
            self.store.save_version('', self.a)
        with self.assertRaises(ValueError):
            self.store.save_version('customer-a', {'bad': float('inf')})


# PostgreSQL adapter and authenticated route contracts use a disposable SQL shim.
# Placeholders are translated and FOR UPDATE is recorded. Native locking and
# migration execution require the integration rail.
A, B, P, Q = [str(UUID(int=n)) for n in range(1, 5)]
ROLES = {'module': {'block': 'Panel', 'layer': 'PV'}}


class Connection:
    def __init__(self):
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.statements = []
        self.db.executescript('''
            PRAGMA foreign_keys=ON;
            CREATE TABLE x07_customers(customer_key TEXT PRIMARY KEY, profile_id TEXT UNIQUE);
            CREATE TABLE x07_profile_versions(customer_key TEXT, profile_id TEXT, version INTEGER,
                roles_json TEXT, created_at TEXT, PRIMARY KEY(customer_key,version),
                UNIQUE(customer_key,profile_id,version));
            CREATE TABLE x07_project_applications(project_key TEXT PRIMARY KEY, customer_key TEXT,
                profile_id TEXT, version INTEGER, FOREIGN KEY(customer_key,profile_id,version)
                REFERENCES x07_profile_versions(customer_key,profile_id,version));
            CREATE TABLE x07_confirmation_evidence(
                project_key TEXT PRIMARY KEY REFERENCES x07_project_applications(project_key),
                drawing_key TEXT NOT NULL, input_version_id TEXT NOT NULL, design_window_json TEXT NOT NULL);
        ''')

    def execute(self, sql, parameters):
        self.statements.append(sql)
        return self.db.execute(sql.replace('%s', '?').replace(' FOR UPDATE', ''), parameters)


@pytest.fixture
def connection():
    value = Connection()
    try:
        yield value
    finally:
        value.db.close()


@pytest.fixture
def history_product(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import x07_profile_history as history

    conn = Connection()
    threaded = sqlite3.connect(':memory:', check_same_thread=False)
    threaded.row_factory = sqlite3.Row
    threaded.executescript('\n'.join(conn.db.iterdump()))
    conn.db.close()
    conn.db = threaded

    @contextmanager
    def transaction(**options):
        assert options == {'isolation': 'repeatable read', 'read_only': True}
        with conn.db:
            yield conn

    alternate_project = str(UUID(int=12))
    alternate_drawing = str(UUID(int=13))
    contexts = {
        A: {(P, Q, 5), (P, Q, 6), (alternate_project, alternate_drawing, 7)},
        B: {(Q, Q, 5)},
    }

    def resolve(tenant, project_id, input_version_id, *, drawing_id, write):
        assert write is False
        if (str(project_id), str(drawing_id), input_version_id.int) not in contexts.get(tenant, ()):
            history.context_service.refuse('CONTEXT_NOT_FOUND')
        return SimpleNamespace(binding=SimpleNamespace(organization_id=UUID(tenant)))

    monkeypatch.setattr(history.platform_link, 'platform_db', lambda: SimpleNamespace(transaction=transaction))
    monkeypatch.setattr(history, '_headers', lambda *_: None)
    monkeypatch.setattr(history.context_service, 'resolve_context', resolve)
    app = FastAPI()
    app.include_router(history.router)
    scoped = [A]
    app.dependency_overrides[history.deps.require_active_tenant] = lambda: scoped[0]
    with TestClient(app) as client:
        def request(*, scope=A, project=P, drawing=Q, input_version=5, **params):
            scoped[0] = scope
            path = history.HISTORY_PATH.format(project_id=project, drawing_id=drawing,
                                               input_version_id=UUID(int=input_version))
            snapshot = list(conn.db.iterdump())
            conn.statements.clear()
            response = client.get(path, params=params)
            assert all(statement.startswith('SELECT ') for statement in conn.statements)
            assert list(conn.db.iterdump()) == snapshot
            assert response.headers['Cache-Control'] == 'no-store'
            return response

        yield conn, request
    conn.db.close()


def save_history_version(conn, owner, expected, block, **other):
    store = PostgresProfileStore(conn, owner)
    with conn.db:
        return store.save_version(owner, {'module': {'block': block, 'layer': 'PV'}, **other},
                                  expected_version=expected)


def test_concurrent_save_keeps_page_watermarks_and_pins(history_product):
    conn, request = history_product
    other_project = str(UUID(int=12))
    other_drawing = str(UUID(int=13))
    store = PostgresProfileStore(conn, A)
    pins = {1: P, 2: other_project}
    for version in range(1, 5):
        save_history_version(conn, A, version - 1, f'M{version}')
        if version in pins:
            with conn.db:
                store.apply_to_project(pins[version], A, expected_version=version)
    scopes = (
        dict(project=P, drawing=Q, input_version=5),
        dict(project=P, drawing=Q, input_version=6),
        dict(project=other_project, drawing=other_drawing, input_version=7),
    )
    first_pages = []
    for scope in scopes:
        response = request(limit=2, **scope)
        assert response.status_code == 200
        first_pages.append(response.json())

    save_history_version(conn, A, 4, 'M5')
    for scope, first in zip(scopes, first_pages):
        response = request(limit=2, cursor=first['next_cursor'], **scope)
        assert response.status_code == 200
        resumed = response.json()
        for page in (first, resumed):
            assert page['through_version'] == 4
            assert [page[key] for key in ('customer_key', 'project_key', 'drawing_key', 'input_version_id')] == [
                A, scope['project'], scope['drawing'], str(UUID(int=scope['input_version'])),
            ]
        assert [row['version'] for row in first['rows'] + resumed['rows']] == [4, 3, 2, 1]
        assert resumed['next_cursor'] is None
        assert request(limit=2, **scope).json()['through_version'] == 5
        assert {version: store.application_record(project, A)['version']
                for version, project in pins.items()} == {1: 1, 2: 2}


def test_exact_field_changes_use_predecessor_beyond_page(history_product):
    conn, request = history_product
    save_history_version(conn, A, 0, 'M1', roof={'layer': 'EAST'})
    save_history_version(conn, A, 1, 'M2', obstruction={'layer': 'VENT'})
    response = request(limit=1)
    assert response.status_code == 200
    page = response.json()
    assert page['rows'][0]['changes'] == [
        {'role': 'module', 'field': 'block', 'operation': 'changed', 'old': 'M1', 'new': 'M2'},
        {'role': 'obstruction', 'field': 'layer', 'operation': 'added', 'old': None, 'new': 'VENT'},
        {'role': 'roof', 'field': 'layer', 'operation': 'removed', 'old': 'EAST', 'new': None},
    ]
    initial = request(limit=1, cursor=page['next_cursor']).json()['rows'][0]
    assert initial['version'] == 1
    assert [(row['field'], row['new']) for row in initial['changes']] == [
        ('block', 'M1'), ('layer', 'PV'), ('layer', 'EAST')]


def test_customer_cannot_reuse_another_customers_cursor(history_product):
    conn, request = history_product
    save_history_version(conn, A, 0, 'PRIVATE-A')
    save_history_version(conn, A, 1, 'PRIVATE-A2')
    save_history_version(conn, B, 0, 'PRIVATE-B')
    cursor = request(limit=1).json()['next_cursor']
    for response in [request(scope=B, project=Q, cursor=cursor), request(project=Q)]:
        assert response.status_code == 404
        assert 'rows' not in response.json()
        assert 'PRIVATE-' not in response.text
    own = request(scope=B, project=Q).json()['rows']
    assert len(own) == 1 and own[0]['version'] == 1
    assert 'PRIVATE-B' in json.dumps(own) and 'PRIVATE-A' not in json.dumps(own)


@pytest.mark.parametrize('variant', ['changed', 'same', 'wrong_revision', 'missing_handle', 'array'])
def test_portfolio_reuse_report_reasons_and_pins(connection, monkeypatch, tmp_path, variant):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from dxf_intake import parse_dxf_bytes
    from routers import x07_profile_portfolio as portfolio

    projects = [P, Q, str(UUID(int=6))]
    revisions = [
        {'project_id': project, 'drawing_id': str(UUID(int=20 + index)),
         'input_version_id': str(UUID(int=30 + index))}
        for index, project in enumerate(projects)
    ]
    profile = save_history_version(connection, A, 0, 'Panel')
    current_block = 'PANEL' if variant == 'same' else 'Edited'
    scope = revisions[0]
    window_input = revisions[1]['input_version_id'] if variant == 'wrong_revision' else scope['input_version_id']
    with connection.db:
        for project in (P, Q):
            PostgresProfileStore(connection, A).apply_to_project(project, A, expected_version=1)
        connection.execute('INSERT INTO x07_confirmation_evidence VALUES (%s, %s, %s, %s)',
                           (scope['project_id'], scope['drawing_id'], window_input, '[0,0,4,4]'))
    with connection.db:
        PostgresProfileStore(connection, A).save_version(
            A, {'module': {'block': current_block, 'layer': 'PV'}, 'roof': {'layer': 'NEW'}},
            expected_version=1)
    edit_during_report = False

    @contextmanager
    def transaction(**options):
        assert options == {'isolation': 'repeatable read', 'read_only': True}
        yield connection

    def authorize(tenant, project_id, *, write):
        assert tenant == A and write is False
        if str(project_id) not in projects:
            portfolio.context_service.refuse('PROJECT_FORBIDDEN')
        return UUID(A), None

    def resolve(tenant, project_id, input_version_id, *, drawing_id, write):
        authorize(tenant, project_id, write=write)
        index = projects.index(str(project_id))
        assert str(input_version_id) == revisions[index]['input_version_id']
        assert str(drawing_id) == revisions[index]['drawing_id']
        nonlocal edit_during_report
        if edit_during_report:
            edit_during_report = False
            peer = Connection()
            peer.db.close()
            with closing(sqlite3.connect(path)) as peer_db:
                peer.db = peer_db
                peer_db.row_factory = sqlite3.Row
                save_history_version(peer, A, 2, 'Peer edit')
        if index == 2:
            portfolio.context_service.refuse('INTAKE_UNAVAILABLE')

        def insert(name, layer, handle, x, y):
            handle_pair = [(5, handle)] if handle else []
            return [(0, 'INSERT'), (2, name), (8, layer), *handle_pair, (10, x), (20, y)]

        if index == 0:
            entities = insert('Panel', 'PV', 'A1', 1, 1)
            if variant == 'array':
                entities += [(71, 2), (45, 2)]
            entities += insert('Edited', 'PV', '' if variant == 'missing_handle' else 'B1', 1, 1)
            if variant == 'array':
                entities += [(70, 2), (44, 2)]
            entities += insert('Edited', 'PV', 'B2', 20, 1) + insert('Roof', 'NEW', 'C1', 3, 3)
        else:
            entities = insert('Different', 'PV', 'A1', 1, 1)
        blocks = []
        for name in ('Panel', 'Edited', 'Roof', 'Different'):
            blocks += [(0, 'BLOCK'), (2, name), (10, 0), (20, 0), (0, 'ENDBLK')]
        pairs = [(0, 'SECTION'), (2, 'BLOCKS'), *blocks, (0, 'ENDSEC'),
                 (0, 'SECTION'), (2, 'ENTITIES'), *entities, (0, 'ENDSEC'), (0, 'EOF')]
        intake = parse_dxf_bytes(''.join(f'{code}\n{value}\n' for code, value in pairs).encode())
        assert intake['inserts']
        return SimpleNamespace(binding=SimpleNamespace(organization_id=UUID(A)),
                               intake=intake)

    monkeypatch.setattr(portfolio, '_headers', lambda *_: None)
    monkeypatch.setattr(portfolio.context_service, '_access', authorize)
    monkeypatch.setattr(portfolio.context_service, 'resolve_context', resolve)
    monkeypatch.setattr(portfolio.platform_link, 'platform_db', lambda: SimpleNamespace(transaction=transaction))
    # Give the ASGI worker and peer edit distinct connections to the same fixture.
    path = tmp_path / 'portfolio.sqlite'
    threaded = sqlite3.connect(path, check_same_thread=False)
    threaded.row_factory = sqlite3.Row
    threaded.executescript('\n'.join(connection.db.iterdump()))
    connection.db.close()
    connection.db = threaded

    app = FastAPI()
    app.include_router(portfolio.router)
    app.dependency_overrides[portfolio.deps.require_active_tenant] = lambda: A
    before = list(connection.db.iterdump())
    connection.statements.clear()
    comparisons = [
        {'role': 'module', 'field': 'block', 'pinned': 'Panel', 'current': current_block},
        {'role': 'module', 'field': 'layer', 'pinned': 'PV', 'current': 'PV'},
        {'role': 'roof', 'field': 'layer', 'pinned': None, 'current': 'NEW'},
    ]
    no_window = {'status': 'unavailable', 'reason': 'CONFIRMED_WINDOW_UNAVAILABLE'}
    exact_comparison = {
        'changed': {'status': 'different', 'added': 1, 'removed': 1},
        'same': {'status': 'same', 'added': 0, 'removed': 0},
        'wrong_revision': no_window,
        'missing_handle': {'status': 'unavailable', 'reason': 'PLACEMENT_IDENTITY_UNAVAILABLE'},
        'array': {'status': 'different', 'added': 2, 'removed': 2},
    }[variant]
    expected = [
        {**revision, 'status': status, 'reason': reason,
         'profile_version': version, 'profile_id': profile['profile_id'], 'pinned': pinned,
         'role_comparison': comparisons if pinned else None, 'panel_comparison': panels}
        for revision, status, reason, version, pinned, panels in zip(
            revisions, ['compatible', 'needs_choice', 'unavailable'],
            [None, 'ROLE_NOT_FOUND', 'SIP_R1_INTAKE_UNAVAILABLE'], [1, 1, 2], [True, True, False],
            [exact_comparison, no_window, None],
        )
    ]
    with TestClient(app) as client:
        for ordered in [revisions, revisions[::-1], revisions[1:] + revisions[:1]]:
            response = client.post(portfolio.PORTFOLIO_PATH, json={'revisions': ordered})
            assert response.status_code == 200
            assert response.headers['Cache-Control'] == 'no-store'
            assert response.json() == {
                'customer_key': A,
                'degraded_mode': False,
                'error': None,
                'current_profile_version': 2,
                'rows': [expected[projects.index(item['project_id'])] for item in ordered],
            }
        assert all(sql.startswith('SELECT ') for sql in connection.statements)
        connection.statements.clear()
        foreign = {**revisions[0], 'project_id': B}
        response = client.post(portfolio.PORTFOLIO_PATH, json={'revisions': [revisions[0], foreign]})
        assert response.status_code == 403
        assert 'rows' not in response.json()
        assert connection.statements == []
        assert list(connection.db.iterdump()) == before

        edit_during_report = True
        concurrent = client.post(portfolio.PORTFOLIO_PATH, json={'revisions': revisions}).json()
        assert concurrent['current_profile_version'] == 2
        assert concurrent['rows'] == expected
        refreshed = client.post(portfolio.PORTFOLIO_PATH, json={'revisions': revisions}).json()
        assert refreshed['current_profile_version'] == 3
        assert [row['profile_version'] for row in refreshed['rows']] == [1, 1, 3]
        for row in refreshed['rows'][:2]:
            assert row['role_comparison'] == [
                {'role': 'module', 'field': 'block', 'pinned': 'Panel', 'current': 'Peer edit'},
                {'role': 'module', 'field': 'layer', 'pinned': 'PV', 'current': 'PV'},
            ]
        assert refreshed['rows'][2]['role_comparison'] is None
        assert refreshed['rows'][0]['panel_comparison'] == (
            no_window if variant == 'wrong_revision' else {'status': 'unavailable', 'reason': 'PROFILE_REQUIRES_REVIEW'})
        assert all(sql.startswith('SELECT ') for sql in connection.statements)
    store = PostgresProfileStore(connection, A)
    assert store.latest(A)['version'] == 3
    assert [store.application_record(project, A)['version'] for project in (P, Q)] == [1, 1]


def test_removed_roles_export_uses_the_history_watermark_and_keeps_project_pins(connection, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import x07_profile_portfolio as portfolio

    projects = [P, Q, *(str(UUID(int=n)) for n in range(6, 10))]
    revisions = [
        {'project_id': project, 'drawing_id': str(UUID(int=20 + index)),
         'input_version_id': str(UUID(int=30 + index))}
        for index, project in enumerate(projects)
    ]
    store = PostgresProfileStore(connection, A)
    roof, obstruction = {'layer': 'Roof'}, {'layer': 'Obstructions'}
    versions = [dict(roof=roof, obstruction=obstruction), dict(roof=roof), {}, dict(roof={'layer': 'New roof'})]
    for version, roles in enumerate(versions, start=1):
        profile = save_history_version(connection, A, version - 1, f'M{version}', **roles)
        with connection.db:
            store.apply_to_project(projects[version - 1], A, expected_version=version)
            if version == 1:
                store.apply_to_project(projects[-1], A, expected_version=1)
    foreign = save_history_version(connection, B, 0, 'Foreign panel')

    @contextmanager
    def transaction(**options):
        assert options == {'isolation': 'repeatable read', 'read_only': True}
        yield connection

    def authorize(tenant, project_id, *, write):
        assert tenant == A and write is False
        if str(project_id) not in projects:
            portfolio.context_service.refuse('PROJECT_FORBIDDEN')
        return UUID(A), None

    def resolve(tenant, project_id, input_version_id, *, drawing_id, write):
        authorize(tenant, project_id, write=write)
        index = projects.index(str(project_id))
        revision = revisions[index]
        if str(input_version_id) != revision['input_version_id'] or str(drawing_id) != revision['drawing_id']:
            portfolio.context_service.refuse('CONTEXT_NOT_FOUND')
        if index == len(projects) - 1:
            portfolio.context_service.refuse('INTAKE_UNAVAILABLE')
        return SimpleNamespace(binding=SimpleNamespace(organization_id=UUID(A)), intake={})

    monkeypatch.setattr(portfolio, '_headers', lambda *_: None)
    monkeypatch.setattr(portfolio.context_service, '_access', authorize)
    monkeypatch.setattr(portfolio.context_service, 'resolve_context', resolve)
    monkeypatch.setattr(portfolio.platform_link, 'platform_db', lambda: SimpleNamespace(transaction=transaction))
    app = FastAPI()
    app.include_router(portfolio.router)
    app.dependency_overrides[portfolio.deps.require_active_tenant] = lambda: A
    payload = {'profile_id': profile['profile_id'], 'through_version': 3, 'revisions': revisions}

    with TestClient(app) as client:
        def export(**overrides):
            before = list(connection.db.iterdump())
            connection.statements.clear()
            response = client.post(portfolio.REMOVED_ROLES_EXPORT_PATH, json={**payload, **overrides})
            assert response.headers['Cache-Control'] == 'no-store'
            assert all(sql.startswith('SELECT ') for sql in connection.statements)
            assert list(connection.db.iterdump()) == before
            return response

        initial = export()
        assert initial.status_code == 200
        assert initial.headers['Content-Type'] == 'application/json'
        assert initial.headers['Content-Disposition'] == 'attachment; filename="removed-profile-roles.json"'
        data = initial.json()
        assert (data['customer_key'], data['profile_id'], data['through_version']) == (A, profile['profile_id'], 3)
        expected = [
            ('uses_removed_roles', 1, [('obstruction', obstruction), ('roof', roof)], None),
            ('uses_removed_roles', 2, [('roof', roof)], None),
            ('no_removed_roles', 3, [], None),
            ('outside_snapshot', 4, [], None),
            ('unpinned', None, [], None),
            ('unavailable', 1, [], 'SIP_R1_INTAKE_UNAVAILABLE'),
        ]
        assert data['rows'] == [
            {**revision, 'status': status, 'pinned_profile_version': pinned, 'reason': reason,
             'removed_roles': [{'role': role, 'applied': value} for role, value in removed]}
            for revision, (status, pinned, removed, reason) in zip(revisions, expected)
        ]
        assert export(revisions=revisions[::-1]).json()['rows'] == data['rows'][::-1]
        restored = export(through_version=4).json()['rows']
        assert restored[0]['removed_roles'] == [{'role': 'obstruction', 'applied': obstruction}]
        assert restored[1]['removed_roles'] == []
        assert restored[3]['status'] == 'no_removed_roles'
        save_history_version(connection, A, 4, 'Peer edit')
        assert export().content == initial.content

        invalid = [
            (dict(profile_id=foreign['profile_id'], through_version=1), 404),
            (dict(through_version=100), 404),
            (dict(through_version=True), 422),
            (dict(revisions=[revisions[0], {**revisions[1], 'project_id': B}]), 403),
            (dict(revisions=[{**revisions[0], 'drawing_id': revisions[1]['drawing_id']}]), 404),
        ]
        for overrides, code in invalid:
            failure = client.post(portfolio.REMOVED_ROLES_EXPORT_PATH, json={**payload, **overrides})
            assert failure.status_code == code
            assert 'Content-Disposition' not in failure.headers
            assert 'rows' not in failure.json()


def test_versions_old_pin_and_foreign_customer(connection):
    store = PostgresProfileStore(connection, A)
    assert store.latest(A) is None
    assert connection.statements == ['SELECT * FROM x07_profile_versions WHERE customer_key=%s ORDER BY version DESC LIMIT 1']
    first = store.save_version(A, ROLES, expected_version=0)
    pin = store.apply_to_project(P, A, expected_version=1)
    edited = store.save_version(A, {'module': {'block': 'New', 'layer': 'PV'}}, expected_version=1)
    assert edited['version'] == 2
    assert edited['profile_id'] == first['profile_id']
    assert store.application_record(P, A) == pin
    assert pin['roles'] == ROLES
    assert store.apply_to_project(Q, A, expected_version=2)['version'] == 2
    with pytest.raises(ValueError, match='changed'):
        store.save_version(A, ROLES, expected_version=1)
    with pytest.raises(ProfileScopeError):
        store.latest(B)
    foreign = PostgresProfileStore(connection, B)
    assert foreign.latest(B) is None
    with pytest.raises(ProfileScopeError):
        foreign.application_record(P, B)
    assert any(sql.endswith('FOR UPDATE') for sql in connection.statements)


def test_caller_rollback_covers_confirmation_version_and_pin(connection):
    intake = {'inserts': [{'name': 'Panel', 'layer': 'PV', 'x': 1, 'y': 1}]}
    store = PostgresProfileStore(connection, A)
    with pytest.raises(RuntimeError):
        with connection.db:
            result = ProfileWorkflow(store).confirm(intake, customer_key=A, project_key=P,
                drawing_key='drawing:version', roles=ROLES, window=[0, 0, 2, 2], expected_version=0)
            assert result['selection']['count'] == 1
            raise RuntimeError('abort after pin')
    assert store.latest(A) is None
    assert store.application_record(P, A) is None
    with connection.db:
        result = ProfileWorkflow(store).confirm(intake, customer_key=A, project_key=P,
            drawing_key='drawing:version', roles=ROLES, window=[0, 0, 2, 2], expected_version=0)
    assert result['applied_profile']['version'] == 1
    assert store.application_record(P, A)['roles'] == ROLES


@pytest.mark.parametrize('expected', [None, True, -1, 2**53 - 1, 1.5])
def test_invalid_expected_version_refuses_before_write(connection, expected):
    store = PostgresProfileStore(connection, A)
    with pytest.raises(ValueError):
        store.save_version(A, ROLES, expected_version=expected)
    assert connection.statements == []


@pytest.mark.parametrize('roles', [{}, {'module': {'layer': 'PV'}}, {'module': {'block': 'P', 'layer': ''}},
                                   {'module': {'block': 'P', 'layer': 'x' * 131073}}])
def test_invalid_roles_refuse_before_write(connection, roles):
    with pytest.raises(ValueError):
        PostgresProfileStore(connection, A).save_version(A, roles, expected_version=0)
    assert connection.statements == []


@pytest.fixture
def routes(connection, monkeypatch):
    from routers import x07_profiles as routes
    @contextmanager
    def transaction(**options):
        if options:
            assert options == {'isolation': 'repeatable read', 'read_only': True}
        with connection.db:
            yield connection
    monkeypatch.setattr(routes.platform_link, 'platform_db', lambda: SimpleNamespace(transaction=transaction))
    monkeypatch.setattr(routes, '_headers', lambda *args: None)
    monkeypatch.setattr(routes.context_service, 'resolve_context', lambda *args, **kwargs: SimpleNamespace(
        binding=SimpleNamespace(organization_id=UUID(A)),
        intake={'inserts': [{'name': 'Panel', 'layer': 'PV', 'x': 1, 'y': 1},
                            {'name': 'Edited', 'layer': 'PV', 'x': 2, 'y': 2}]}))
    return routes


def call_route(routes, action, payload=None):
    response = routes._operate(A, UUID(P), UUID(Q), UUID(int=5), None, None, action, payload)
    return response.status_code, json.loads(response.body)


def test_routes_confirm_replay_edit_and_old_pin(routes, connection):
    request = routes.Confirmation(block='Panel', layer='PV', design_window=[0, 0, 3, 3], expected_version=0)
    status, data = call_route(routes, 'read')
    assert status == 200 and data['expected_version'] == 0 and data['application_record'] is None
    assert not connection.db.in_transaction  # GET does not create a customer.
    status, confirmed = call_route(routes, 'confirm', request)
    assert status == 200 and confirmed['ok'] is True
    assert confirmed['applied_profile']['roles'] == ROLES
    assert confirmed['profile']['roles'] == ROLES
    assert confirmed['profile']['version'] == 1
    assert confirmed['profile']['profile_id'] == confirmed['applied_profile']['profile_id']
    status, replay = call_route(routes, 'confirm', request)
    assert status == 200 and replay == confirmed
    assert connection.db.execute('SELECT COUNT(*) FROM x07_profile_versions').fetchone()[0] == 1
    status, edited = call_route(routes, 'edit', routes.Edit(
        roles={'module': {'block': 'Edited', 'layer': 'PV'}}, expected_version=1))
    assert status == 200 and edited['profile']['version'] == 2
    status, after_edit = call_route(routes, 'confirm', request)
    assert status == 200
    assert after_edit['applied_profile'] == confirmed['applied_profile']
    assert after_edit['profile'] == edited['profile']
    assert connection.db.execute('SELECT COUNT(*) FROM x07_project_applications').fetchone()[0] == 1
    assert connection.db.execute('SELECT COUNT(*) FROM x07_profile_versions').fetchone()[0] == 2
    status, reopened = call_route(routes, 'read')
    assert status == 200
    assert reopened['profile']['version'] == 2
    assert reopened['application_record'] == confirmed['applied_profile']
    status, stale = call_route(routes, 'edit', routes.Edit(roles=ROLES, expected_version=1))
    assert status == 409 and stale['error']['reason_code'] == 'X07_PROFILE_CONFLICT'
    assert 'roles' not in json.dumps(stale)


def test_authorization_failure_precedes_store_access(routes, connection, monkeypatch):
    def refuse(*args, **kwargs):
        routes.context_service.refuse('PROJECT_FORBIDDEN')
    monkeypatch.setattr(routes.context_service, 'resolve_context', refuse)
    status, data = call_route(routes, 'read')
    assert status == 403
    assert connection.statements == []
    assert 'profile_id' not in json.dumps(data)


def test_transaction_failure_returns_fixed_refusal_and_rolls_back(routes, connection, monkeypatch):
    original = routes.PostgresProfileStore.apply_to_project
    def fail(store, *args, **kwargs):
        original(store, *args, **kwargs)
        raise RuntimeError('private database sentinel')
    monkeypatch.setattr(routes.PostgresProfileStore, 'apply_to_project', fail)
    status, data = call_route(routes, 'confirm', routes.Confirmation(
        block='Panel', layer='PV', design_window=[0, 0, 3, 3], expected_version=0))
    assert status == 503
    assert 'sentinel' not in json.dumps(data)
    assert connection.db.execute('SELECT COUNT(*) FROM x07_profile_versions').fetchone()[0] == 0
    assert connection.db.execute('SELECT COUNT(*) FROM x07_project_applications').fetchone()[0] == 0


@pytest.mark.parametrize('window', [[True, 0, 2, 2], [float('inf'), 0, 2, 2], ['0', 0, 2, 2]])
def test_route_models_refuse_nonfinite_or_coerced_windows(routes, window):
    with pytest.raises(ValueError):
        routes.Confirmation(block='Panel', layer='PV', design_window=window, expected_version=0)


def test_migration_uses_canonical_foreign_keys():
    from pathlib import Path
    ddl = (Path(__file__).resolve().parents[2] / 'platform/migrations/0107_x07_customer_roles.sql').read_text()
    assert 'REFERENCES orgs(org_id)' in ddl
    assert 'REFERENCES projects(org_id, project_id)' in ddl
    assert 'BEFORE UPDATE OR DELETE ON x07_profile_versions' in ddl
    assert 'BEFORE UPDATE OR DELETE ON x07_project_applications' in ddl


@pytest.mark.parametrize('scenario', ['edited-profile', 'legacy-and-other-revision', 'foreign-project'])
def test_recovery_report(routes, connection, monkeypatch, scenario):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.deps.require_active_tenant] = lambda: A
    endpoint = routes.RECOVERY_PATH
    revision = str(UUID(int=5))
    scope = {'project_id': P, 'drawing_id': Q, 'input_version_id': revision}
    scopes = [scope]
    expected = []
    if scenario == 'edited-profile':
        request = routes.Confirmation(block='Panel', layer='PV', design_window=[0, 0, 3, 3], expected_version=0)
        status, confirmed = call_route(routes, 'confirm', request)
        assert status == 200
        assert call_route(routes, 'confirm', request)[0] == 200
        assert connection.db.execute('SELECT COUNT(*) FROM x07_confirmation_evidence').fetchone()[0] == 1
        assert call_route(routes, 'edit', routes.Edit(
            roles={'module': {'block': 'Edited', 'layer': 'PV'}}, expected_version=1))[0] == 200
        other = {**scope, 'input_version_id': str(UUID(int=6))}
        scopes.append(other)
        expected = [{**scope, 'customer_key': A, 'profile_id': confirmed['applied_profile']['profile_id'],
                     'pinned_version': 1, 'evidence_status': 'confirmed',
                     'evidence': {'design_window': [0, 0, 3, 3]}}]
        expected.append({**expected[0], **other, 'evidence_status': 'unavailable', 'evidence': None})
    elif scenario == 'legacy-and-other-revision':
        store = PostgresProfileStore(connection, A)
        profile = store.save_version(A, ROLES, expected_version=0)
        store.apply_to_project(P, A, expected_version=1)
        scopes = [scope, {**scope, 'input_version_id': str(UUID(int=6))}]
        expected = [{**requested, 'customer_key': A, 'profile_id': profile['profile_id'],
                     'pinned_version': 1, 'evidence_status': 'unavailable', 'evidence': None}
                    for requested in scopes]
    else:
        scopes = [scope, {**scope, 'project_id': B}]

        def authorize(_tenant, project_id, *_headers):
            if str(project_id) == B:
                routes.context_service.refuse('CONTEXT_NOT_FOUND')

        monkeypatch.setattr(routes, '_headers', authorize)

    snapshot = list(connection.db.iterdump())
    connection.statements.clear()
    with TestClient(app) as client:
        first = client.post(endpoint, json={'revisions': scopes})
        second = client.post(endpoint, json={'revisions': scopes})
    assert first.json() == second.json()
    assert first.headers['Cache-Control'] == 'no-store'
    if scenario == 'foreign-project':
        assert first.status_code == 404
        assert 'rows' not in first.json()
        assert connection.statements == []
    else:
        assert first.status_code == 200
        assert first.json()['rows'] == expected
        assert all(sql.startswith('SELECT ') for sql in connection.statements)
    assert list(connection.db.iterdump()) == snapshot


@pytest.mark.parametrize('turn,offset', [(0, (0, 0)), (1, (500, -75)), (2, (-120, 220))])
def test_confirm_courtyard_island_from_exact_dxf_handles(routes, connection, monkeypatch, turn, offset):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from dxf_intake import parse_dxf_bytes

    def transform(point):
        x, y = point
        for _ in range(turn):
            x, y = -y, x
        return x + offset[0], y + offset[1]

    outlines = {
        'E1': [(0, 0), (30, 0), (30, 30), (0, 30)],
        'E2': [(6, 6), (25, 6), (25, 25), (6, 25)],
        'E3': [(10, 10), (20, 10), (20, 13), (13, 13), (13, 20), (10, 20)],
        'E4': [(15, 9), (23, 9), (23, 23), (15, 23)],
    }
    pairs = [(0, 'SECTION'), (2, 'BLOCKS'), (0, 'BLOCK'), (2, 'Panel'), (70, 0),
             (10, 0), (20, 0), (30, 0), (0, 'POINT'), (10, 0), (20, 0), (30, 0),
             (0, 'ENDBLK'), (0, 'ENDSEC'), (0, 'SECTION'), (2, 'ENTITIES')]
    for handle, vertices in outlines.items():
        pairs.extend([(0, 'LWPOLYLINE'), (5, handle), (8, 'Roof Outlines'),
                      (90, len(vertices)), (70, 1)])
        for vertex in vertices:
            x, y = transform(vertex)
            pairs.extend([(10, x), (20, y)])
    for handle, origin in {'A1': (11, 17), 'A2': (12, 11), 'A3': (17, 17),
                           'A4': (26, 26), 'A5': (16, 11), 'A6': (15, 11),
                           'A7': (10, 18)}.items():
        x, y = transform(origin)
        pairs.extend([(0, 'INSERT'), (5, handle), (2, 'Panel'), (8, 'PV'),
                      (10, x), (20, y), (30, 0)])
    pairs.extend([(0, 'ENDSEC'), (0, 'EOF')])
    raw = ('\n'.join(str(value) for pair in pairs for value in pair) + '\n').encode()
    intake = parse_dxf_bytes(raw, source_name='concave-roof-island.dxf')
    revised = parse_dxf_bytes(raw.replace(b'5\nE3\n', b'5\nE5\n'), source_name='revised-island.dxf')
    monkeypatch.setattr(routes.context_service, 'resolve_context', lambda _tenant, _project, version, **kwargs:
        SimpleNamespace(binding=SimpleNamespace(organization_id=UUID(A)),
                        intake=intake if version == UUID(int=5) else revised))

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.deps.require_active_tenant] = lambda: A
    scope = {'project_id': P, 'drawing_id': Q, 'input_version_id': str(UUID(int=5))}
    url = routes.PATH.format(**scope)
    other_scope = {**scope, 'input_version_id': str(UUID(int=6))}
    other_url = routes.PATH.format(**other_scope)
    window = {'outline_handle': 'e3', 'exclude_handles': ['e4']}
    normalized = {'outline_handle': 'E3', 'exclude_handles': ['E4']}
    request = {'block': 'Panel', 'layer': 'PV', 'design_window': window, 'expected_version': 0}
    with TestClient(app) as client:
        response = client.get(url)
        assert response.status_code == 200
        assert response.json()['outline_choices'] == [
            {'handle': handle, 'layer': 'Roof Outlines'} for handle in outlines
        ]
        assert {row['handle'] for row in client.get(other_url).json()['outline_choices']} == {'E1', 'E2', 'E4', 'E5'}
        assert client.post(other_url, json=request).status_code == 409
        invalid = client.post(url, json={**request, 'design_window': {'outline_handle': 'E5'}})
        assert invalid.status_code == 409
        assert connection.db.execute('SELECT COUNT(*) FROM x07_profile_versions').fetchone()[0] == 0
        confirmed = client.post(url, json=request)
        assert confirmed.status_code == 200
        selection = confirmed.json()['selection']
        # A3 is in the island bounding box only. A5/A6 sit in or on the
        # neighboring outline. A7 is on the island's outer boundary.
        assert selection['handles'] == ['A1', 'A2', 'A7']
        assert selection['design_window'] == normalized
        assert client.post(url, json=request).json() == confirmed.json()
        assert client.post(url, json={**request, 'design_window': {'outline_handle': 'E3'}}).status_code == 409
        assert client.post(url, json={**request, 'design_window': [0, 0, 50, 50]}).status_code == 409
        assert client.post(other_url, json={**request, 'design_window': {'outline_handle': 'E5'}}).status_code == 409
        recovered = client.post(routes.RECOVERY_PATH, json={'revisions': [
            scope, other_scope,
        ]}).json()['rows']
    assert recovered[0]['evidence'] == {'design_window': normalized}
    assert recovered[1]['evidence_status'] == 'unavailable'
    assert connection.db.execute('SELECT COUNT(*) FROM x07_confirmation_evidence').fetchone()[0] == 1
