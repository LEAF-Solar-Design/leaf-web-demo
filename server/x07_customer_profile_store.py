"""Injected local SQLite store for explicit customer role maps and project pins.

No default path, identity discovery, network, or drawing mutation. Customer and
project keys are explicit, case-sensitive application keys. New profile versions
never rewrite an existing version or a project's first applied version.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

MAX_PROFILE_VERSION = 2**53 - 1  # exact JSON/JavaScript integer boundary


class ProfileSchemaError(ValueError):
    pass


class ProfileScopeError(ValueError):
    pass


def _check_connection(db):
    if (db.execute('PRAGMA foreign_keys').fetchone()[0] != 1
            or db.execute('PRAGMA read_uncommitted').fetchone()[0] != 0):
        raise ProfileSchemaError('unsafe SQLite connection settings')


def _key(value):
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 200:
        raise ValueError('an explicit nonempty key of at most 200 characters is required')
    return value


class CustomerProfileStore:
    def __init__(self, path, *, read_only=False):
        if str(path) == ':memory:':
            raise ValueError('inject a local SQLite file path')
        self.path = Path(path)
        self.read_only = read_only
        with self._connection() as db:
            _check_connection(db)
            if not read_only: db.execute('BEGIN IMMEDIATE')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if version not in (0, 1):
                raise ProfileSchemaError('unsupported profile schema version: ' + str(version))
            expected = {'customers': ['customer_key', 'profile_id'],
                        'profile_versions': ['customer_key', 'profile_id', 'version', 'roles_json', 'created_at'],
                        'project_applications': ['project_key', 'customer_key', 'profile_id', 'version']}
            triggers = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            required = {'versions_no_update', 'versions_no_delete', 'applications_no_update', 'applications_no_delete'}
            shape_ok = tables == set(expected) and required <= triggers
            if shape_ok:
                shape_ok = all([row[1] for row in db.execute('PRAGMA table_info(' + name + ')')] == fields
                               for name, fields in expected.items())
            if shape_ok:
                shape_ok = self._schema_signature(db) == self._known_schema()
            if version == 1 and not shape_ok:
                raise ProfileSchemaError('supported version has incompatible schema shape')
            if version == 0 and tables and not shape_ok:
                raise ProfileSchemaError('unversioned database is not the supported legacy shape')
            decision = 'supported' if version == 1 else ('adopt_legacy' if tables else 'initialize')
            if read_only and decision != 'supported':
                raise ProfileSchemaError('read-only store requires supported versioned schema')
            if not tables:
                self._create_schema(db)
            if version == 0:
                db.execute('PRAGMA user_version=1')
            self.schema_status = {'previous_version': version, 'version': 1, 'decision': decision}

    @staticmethod
    def _schema_signature(db):
        return tuple((row[0], row[1], ' '.join(row[2].split()) if row[2] else None)
                     for row in db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"))

    @staticmethod
    @lru_cache(maxsize=1)
    def _known_schema():
        # Compare with actual SQLite output from this producer, not names alone.
        # This reference contains no customer data and is never persisted.
        db = sqlite3.connect(':memory:')
        try:
            CustomerProfileStore._create_schema(db)
            return CustomerProfileStore._schema_signature(db)
        finally:
            db.close()

    @staticmethod
    def _create_schema(db):
        # Execute complete statements individually inside the caller's transaction.
        # executescript would implicitly commit before DDL and break rollback.
        sql = '''
              CREATE TABLE IF NOT EXISTS customers (
                customer_key TEXT PRIMARY KEY, profile_id TEXT NOT NULL UNIQUE,
                UNIQUE(customer_key, profile_id));
              CREATE TABLE IF NOT EXISTS profile_versions (
                customer_key TEXT NOT NULL, profile_id TEXT NOT NULL,
                version INTEGER NOT NULL CHECK(version > 0), roles_json TEXT NOT NULL,
                created_at TEXT NOT NULL, PRIMARY KEY(customer_key, version),
                FOREIGN KEY(customer_key, profile_id) REFERENCES customers(customer_key, profile_id));
              CREATE TABLE IF NOT EXISTS project_applications (
                project_key TEXT PRIMARY KEY, customer_key TEXT NOT NULL,
                profile_id TEXT NOT NULL, version INTEGER NOT NULL,
                FOREIGN KEY(customer_key, profile_id) REFERENCES customers(customer_key, profile_id),
                FOREIGN KEY(customer_key, version) REFERENCES profile_versions(customer_key, version));
              CREATE TRIGGER IF NOT EXISTS versions_no_update BEFORE UPDATE ON profile_versions
                BEGIN SELECT RAISE(ABORT, 'profile versions are immutable'); END;
              CREATE TRIGGER IF NOT EXISTS versions_no_delete BEFORE DELETE ON profile_versions
                BEGIN SELECT RAISE(ABORT, 'profile versions are immutable'); END;
              CREATE TRIGGER IF NOT EXISTS applications_no_update BEFORE UPDATE ON project_applications
                BEGIN SELECT RAISE(ABORT, 'project applications are pinned'); END;
              CREATE TRIGGER IF NOT EXISTS applications_no_delete BEFORE DELETE ON project_applications
                BEGIN SELECT RAISE(ABORT, 'project applications are pinned'); END;
                    '''
        statement = ''
        for line in sql.splitlines(True):
            statement += line
            if sqlite3.complete_statement(statement):
                db.execute(statement)
                statement = ''
        if statement.strip():
            raise ProfileSchemaError('incomplete initialization SQL')

    @contextmanager
    def _connection(self):
        db = (sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro', uri=True, timeout=5)
              if self.read_only else sqlite3.connect(self.path, timeout=5))
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA read_uncommitted=OFF')
            _check_connection(db)
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _profile(row):
        if row is None:
            return None
        if type(row['version']) is not int or not 1 <= row['version'] <= MAX_PROFILE_VERSION:
            raise ValueError('profile version must be a positive safe integer')
        result = dict(row)
        encoded = result.pop('roles_json')
        try:
            roles = json.loads(encoded, parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
            if (not isinstance(encoded, str) or len(encoded.encode('utf-8')) > 131072
                    or not isinstance(roles, dict) or 'module' not in roles
                    or set(roles)-{'module','roof','obstruction'}
                    or any(not isinstance(role, dict) or not isinstance(role.get('layer'),str) or not role['layer']
                           or ('block' in role and (not isinstance(role['block'],str) or not role['block'])) for role in roles.values())):
                raise ValueError('invalid role shape')
        except (TypeError, ValueError, RecursionError) as error:
            raise ValueError('stored profile roles are invalid') from error
        result['roles'] = roles
        return result

    def save_version(self, customer_key, roles, *, expected_version=None):
        if self.read_only: raise sqlite3.OperationalError('profile store is read-only')
        customer_key = _key(customer_key)
        if not isinstance(roles, dict):
            raise ValueError('roles must be a JSON object')
        try:
            encoded = json.dumps(roles, sort_keys=True, separators=(',', ':'), allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError('roles must be finite JSON') from exc
        if len(encoded.encode('utf-8')) > 131072:
            raise ValueError('role map exceeds 128 KiB')
        if expected_version is not None and (type(expected_version) is not int or not 0 <= expected_version <= MAX_PROFILE_VERSION):
            raise ValueError('expected version must be a nonnegative safe integer')
        with self._connection() as db:
            _check_connection(db)
            db.execute('BEGIN IMMEDIATE')
            latest = db.execute('SELECT * FROM profile_versions WHERE customer_key=? ORDER BY version DESC LIMIT 1',
                                (customer_key,)).fetchone()
            current = latest['version'] if latest else 0
            if type(current) is not int or current >= MAX_PROFILE_VERSION:
                raise ValueError('profile version limit reached')
            if expected_version is not None and expected_version != current:
                raise ValueError('profile version changed')
            profile_id = latest['profile_id'] if latest else str(uuid4())
            if latest is None:
                db.execute('INSERT INTO customers VALUES (?, ?)', (customer_key, profile_id))
            version = current + 1
            db.execute('INSERT INTO profile_versions VALUES (?, ?, ?, ?, ?)',
                       (customer_key, profile_id, version, encoded, datetime.now(timezone.utc).isoformat()))
            return self._profile(db.execute('SELECT * FROM profile_versions WHERE customer_key=? AND version=?',
                                           (customer_key, version)).fetchone())

    def latest(self, customer_key):
        with self._connection() as db:
            _check_connection(db)
            return self._profile(db.execute('SELECT * FROM profile_versions WHERE customer_key=? ORDER BY version DESC LIMIT 1',
                                           (_key(customer_key),)).fetchone())

    def apply_to_project(self, project_key, customer_key, *, expected_version=None):
        project_key, customer_key = _key(project_key), _key(customer_key)
        if expected_version is not None and (type(expected_version) is not int or not 1 <= expected_version <= MAX_PROFILE_VERSION):
            raise ValueError('expected version must be a positive safe integer')
        if self.read_only:
            result = self.application_record(project_key, customer_key)
            if result is None: raise sqlite3.OperationalError('profile store is read-only; new pin refused')
            if expected_version is not None and result['version'] != expected_version:
                raise ValueError('project is pinned to another version')
            return result
        with self._connection() as db:
            _check_connection(db)
            db.execute('BEGIN IMMEDIATE')
            applied = db.execute('SELECT * FROM project_applications WHERE project_key=?', (project_key,)).fetchone()
            if applied and applied['customer_key'] != customer_key:
                raise ProfileScopeError('project belongs to another customer')
            if applied is None:
                latest = db.execute('SELECT * FROM profile_versions WHERE customer_key=? ORDER BY version DESC LIMIT 1',
                                    (customer_key,)).fetchone()
                if latest is None:
                    return None
                if type(latest['version']) is not int or not 1 <= latest['version'] <= MAX_PROFILE_VERSION:
                    raise ValueError('profile version must be a positive safe integer')
                if expected_version is not None and latest['version'] != expected_version:
                    raise ValueError('profile version changed before project pin')
                db.execute('INSERT INTO project_applications VALUES (?, ?, ?, ?)',
                           (project_key, customer_key, latest['profile_id'], latest['version']))
            result = self._application(db, project_key, customer_key)
            if expected_version is not None and result['version'] != expected_version:
                raise ValueError('project is pinned to another version')
            return result

    def _application(self, db, project_key, customer_key):
        pin = db.execute('SELECT * FROM project_applications WHERE project_key=?', (project_key,)).fetchone()
        if pin is None: return None
        if pin['customer_key'] != customer_key:
            raise ProfileScopeError('project belongs to another customer')
        row = db.execute('SELECT * FROM profile_versions WHERE customer_key=? AND version=?',
                         (customer_key, pin['version'])).fetchone()
        owner = db.execute('SELECT profile_id FROM customers WHERE customer_key=?',(customer_key,)).fetchone()
        if row is None or owner is None or row['profile_id'] != pin['profile_id'] or owner['profile_id'] != pin['profile_id']:
            raise ValueError('stored project pin is invalid')
        return self._profile({**dict(row), 'project_key': project_key})

    def application_record(self, project_key, customer_key):
        with self._connection() as db:
            _check_connection(db)
            return self._application(db, _key(project_key), _key(customer_key))
