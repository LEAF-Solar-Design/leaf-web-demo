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
        self.db = sqlite3.connect(':memory:')
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
    def transaction():
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
