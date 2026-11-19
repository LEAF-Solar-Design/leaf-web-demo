"""CustomerProfileStore interface on one caller-owned PostgreSQL transaction.

The caller authenticates the project, supplies its organization and holds the
transaction through the complete workflow. Writes lock the customer row, so a
confirmation can create its version and first project pin in one transaction.
This adapter never opens a connection, commits, discovers identity or migrates.
"""
from datetime import datetime, timezone
import json
from uuid import UUID, uuid4

from x07_tracker_roles import encode_roles
from x07_customer_profile_store import CustomerProfileStore, MAX_PROFILE_VERSION, ProfileScopeError


class PostgresProfileStore:
    def __init__(self, connection, customer_key):
        self.connection = connection
        self.customer_key = str(UUID(customer_key))

    def _scope(self, customer_key):
        if customer_key != self.customer_key:
            raise ProfileScopeError('customer scope differs')

    def _one(self, sql, parameters):
        row = self.connection.execute(sql, parameters).fetchone()
        if row is None:
            return None
        return {key: str(value) if isinstance(value, UUID) else value for key, value in dict(row).items()}

    def lock(self):
        self.connection.execute(
            'INSERT INTO x07_customers (customer_key, profile_id) VALUES (%s, %s) '
            'ON CONFLICT (customer_key) DO NOTHING', (self.customer_key, str(uuid4())))
        self._one('SELECT profile_id FROM x07_customers WHERE customer_key=%s FOR UPDATE',
                  (self.customer_key,))

    def latest(self, customer_key):
        self._scope(customer_key)
        row = self._one('SELECT * FROM x07_profile_versions WHERE customer_key=%s '
                        'ORDER BY version DESC LIMIT 1', (customer_key,))
        return CustomerProfileStore._profile(row)

    def save_version(self, customer_key, roles, *, expected_version=None):
        self._scope(customer_key)
        if type(expected_version) is not int or not 0 <= expected_version < MAX_PROFILE_VERSION:
            raise ValueError('explicit expected version is required')
        encoded = encode_roles(roles)
        # Reuse the existing persisted role shape and byte-budget checks.
        candidate = CustomerProfileStore._profile({'version': expected_version + 1, 'roles_json': encoded})
        if not candidate['roles']['module'].get('block'):
            raise ValueError('module block is required')
        self.lock()
        latest = self.latest(customer_key)
        if expected_version != (latest['version'] if latest else 0):
            raise ValueError('profile version changed')
        owner = self._one('SELECT profile_id FROM x07_customers WHERE customer_key=%s', (customer_key,))
        self.connection.execute(
            'INSERT INTO x07_profile_versions (customer_key, profile_id, version, roles_json, created_at) '
            'VALUES (%s, %s, %s, %s, %s)',
            (customer_key, owner['profile_id'], expected_version + 1, encoded,
             datetime.now(timezone.utc).isoformat()))
        return self.latest(customer_key)

    def application_record(self, project_key, customer_key):
        self._scope(customer_key)
        project_key = str(UUID(project_key))
        pin = self._one('SELECT * FROM x07_project_applications WHERE project_key=%s', (project_key,))
        if pin is None:
            return None
        if pin['customer_key'] != customer_key:
            raise ProfileScopeError('project belongs to another customer')
        row = self._one('SELECT * FROM x07_profile_versions WHERE customer_key=%s AND version=%s',
                        (customer_key, pin['version']))
        if row is None or row['profile_id'] != pin['profile_id']:
            raise ValueError('stored project pin is invalid')
        return CustomerProfileStore._profile({**row, 'project_key': project_key})

    def apply_to_project(self, project_key, customer_key, *, expected_version=None):
        self._scope(customer_key)
        if type(expected_version) is not int or not 1 <= expected_version <= MAX_PROFILE_VERSION:
            raise ValueError('explicit expected version is required')
        project_key = str(UUID(project_key))
        self.lock()
        pin = self.application_record(project_key, customer_key)
        if pin is not None:
            if pin['version'] != expected_version:
                raise ValueError('project is pinned to another version')
            return pin
        latest = self.latest(customer_key)
        if latest is None or latest['version'] != expected_version:
            raise ValueError('profile version changed before project pin')
        self.connection.execute(
            'INSERT INTO x07_project_applications (project_key, customer_key, profile_id, version) '
            'VALUES (%s, %s, %s, %s)',
            (project_key, customer_key, latest['profile_id'], expected_version))
        return self.application_record(project_key, customer_key)
