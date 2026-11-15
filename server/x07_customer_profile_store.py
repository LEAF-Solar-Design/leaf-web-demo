"""Injected local SQLite store for explicit customer role maps and project pins.

No default path, identity discovery, network, or drawing mutation. Customer and
project keys are explicit, case-sensitive application keys. New profile versions
never rewrite an existing version or a project's first applied version.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4


class ProfileScopeError(ValueError):
    pass


def _key(value):
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 200:
        raise ValueError('an explicit nonempty key of at most 200 characters is required')
    return value


class CustomerProfileStore:
    def __init__(self, path):
        if str(path) == ':memory:':
            raise ValueError('inject a local SQLite file path')
        self.path = Path(path)
        with self._connection() as db:
            db.executescript('''
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
            ''')

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _profile(row):
        if row is None:
            return None
        result = dict(row)
        result['roles'] = json.loads(result.pop('roles_json'))
        return result

    def save_version(self, customer_key, roles, *, expected_version=None):
        customer_key = _key(customer_key)
        if not isinstance(roles, dict):
            raise ValueError('roles must be a JSON object')
        try:
            encoded = json.dumps(roles, sort_keys=True, separators=(',', ':'), allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError('roles must be finite JSON') from exc
        if len(encoded.encode('utf-8')) > 131072:
            raise ValueError('role map exceeds 128 KiB')
        if expected_version is not None and (type(expected_version) is not int or expected_version < 0):
            raise ValueError('expected version must be a nonnegative integer')
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            latest = db.execute('SELECT * FROM profile_versions WHERE customer_key=? ORDER BY version DESC LIMIT 1',
                                (customer_key,)).fetchone()
            current = latest['version'] if latest else 0
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
            return self._profile(db.execute('SELECT * FROM profile_versions WHERE customer_key=? ORDER BY version DESC LIMIT 1',
                                           (_key(customer_key),)).fetchone())

    def apply_to_project(self, project_key, customer_key, *, expected_version=None):
        project_key, customer_key = _key(project_key), _key(customer_key)
        if expected_version is not None and (type(expected_version) is not int or expected_version < 1):
            raise ValueError('expected version must be a positive integer')
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            applied = db.execute('SELECT * FROM project_applications WHERE project_key=?', (project_key,)).fetchone()
            if applied and applied['customer_key'] != customer_key:
                raise ProfileScopeError('project belongs to another customer')
            if applied is None:
                latest = db.execute('SELECT * FROM profile_versions WHERE customer_key=? ORDER BY version DESC LIMIT 1',
                                    (customer_key,)).fetchone()
                if latest is None:
                    return None
                if expected_version is not None and latest['version'] != expected_version:
                    raise ValueError('profile version changed before project pin')
                db.execute('INSERT INTO project_applications VALUES (?, ?, ?, ?)',
                           (project_key, customer_key, latest['profile_id'], latest['version']))
            result = self._application(db, project_key, customer_key)
            if expected_version is not None and result['version'] != expected_version:
                raise ValueError('project is pinned to another version')
            return result

    def _application(self, db, project_key, customer_key):
        row = db.execute('''SELECT a.project_key, v.* FROM project_applications a
            JOIN profile_versions v ON a.customer_key=v.customer_key AND a.version=v.version
            WHERE a.project_key=?''', (project_key,)).fetchone()
        if row and row['customer_key'] != customer_key:
            raise ProfileScopeError('project belongs to another customer')
        return self._profile(row)

    def application_record(self, project_key, customer_key):
        with self._connection() as db:
            return self._application(db, _key(project_key), _key(customer_key))
