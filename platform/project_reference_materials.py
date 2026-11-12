"""Reference metadata under the existing active project membership boundary."""
from __future__ import annotations

import uuid
from . import project_lifecycle as lifecycle
from .project_materials import _authorize
from .db import run_transaction

FIELDS = ('material_id', 'org_id', 'project_id', 'actor_binding_id', 'name',
          'media_type', 'byte_size', 'sha256', 'created_at')


def metadata(row):
    result = {key: row[key] for key in FIELDS}
    for key in ('material_id', 'org_id', 'project_id', 'actor_binding_id'):
        result[key] = str(result[key])
    result['created_at'] = result['created_at'].isoformat()
    result.update(storage_status='uploaded', preview_status='unsupported', model_status='not_consumed')
    return result


def commit(org_id, project_id, actor_id, prepared):
    """Only called after the immutable original is stored. Retry dedups by scope/digest."""
    def operation(conn):
        with conn.cursor() as cur:
            _authorize(cur, org_id, project_id, actor_id, write=True)
            values = {key: prepared[key] for key in ('name', 'media_type', 'byte_size', 'sha256')}
            values.update(material_id=uuid.uuid4(), org_id=org_id, project_id=project_id, actor_binding_id=actor_id)
            columns = ', '.join(values)
            placeholders = ', '.join(f'%({key})s' for key in values)
            cur.execute(f'INSERT INTO project_reference_materials ({columns}) VALUES ({placeholders}) '
                        'ON CONFLICT (org_id, project_id, sha256) DO NOTHING', values)
            cur.execute('SELECT * FROM project_reference_materials WHERE org_id=%s AND project_id=%s AND sha256=%s',
                        (org_id, project_id, prepared['sha256']))
            return metadata(cur.fetchone())
    return run_transaction(operation, isolation='serializable')


def list_materials(org_id, project_id, actor_id, *, cursor=None, limit=200):
    if not 1 <= limit <= 200:
        raise ValueError('limit must be between 1 and 200')
    def operation(conn):
        with conn.cursor() as cur:
            _authorize(cur, org_id, project_id, actor_id)
            cur.execute('SELECT * FROM project_reference_materials WHERE org_id=%s AND project_id=%s '
                        'AND (%s::uuid IS NULL OR material_id > %s::uuid) ORDER BY material_id LIMIT %s',
                        (org_id, project_id, cursor, cursor, limit + 1))
            rows = cur.fetchall()
            return {'materials': [metadata(row) for row in rows[:limit]],
                    'next_cursor': str(rows[limit - 1]['material_id']) if len(rows) > limit else None}
    return run_transaction(operation)


def get_material(org_id, project_id, actor_id, material_id):
    def operation(conn):
        with conn.cursor() as cur:
            _authorize(cur, org_id, project_id, actor_id)
            cur.execute('SELECT * FROM project_reference_materials WHERE org_id=%s AND project_id=%s AND material_id=%s',
                        (org_id, project_id, material_id))
            row = cur.fetchone()
            if row is None:
                raise lifecycle.LifecycleUnavailable('reference not found')
            return metadata(row)
    return run_transaction(operation)
