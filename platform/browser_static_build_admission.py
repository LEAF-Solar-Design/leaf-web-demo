"""Immutable App admission for private Browser static builds."""
from __future__ import annotations

from . import db


_SCOPE = ('tenant_id', 'project_id', 'job_id')
_UUID_FIELDS = (*_SCOPE, 'organization_id', 'repo_key', 'actor_binding_id', 'source_request_id')
_FIELDS = (*_UUID_FIELDS, 'version', 'receipt_digest', 'source_commit', 'source_tree')
_COLUMNS = ', '.join(_FIELDS)
_VALUES = ', '.join(f'%({field})s' for field in _FIELDS)
_LOOKUP = f"""
    SELECT {_COLUMNS} FROM browser_static_build_admissions
    WHERE tenant_id = %(tenant_id)s AND project_id = %(project_id)s AND job_id = %(job_id)s
"""


def _find(cur, scope):
    cur.execute(_LOOKUP, scope)
    row = cur.fetchone()
    if row is None:
        return None
    return dict(row, **{field: str(row[field]) for field in _UUID_FIELDS})


def find(tenant_id, project_id, job_id):
    with db.cursor() as cur:
        return _find(cur, dict(tenant_id=tenant_id, project_id=project_id, job_id=job_id))


def admit(record):
    """Record the first admission, accepting retries only for the exact binding."""
    with db.cursor() as cur:
        cur.execute(
            f"""INSERT INTO browser_static_build_admissions ({_COLUMNS}) VALUES ({_VALUES})
                ON CONFLICT (tenant_id, project_id, job_id) DO NOTHING""",
            record,
        )
        return _find(cur, {field: record[field] for field in _SCOPE}) == record
