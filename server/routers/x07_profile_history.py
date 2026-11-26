"""Read immutable customer profile revisions through an authorized drawing."""
import base64
import json
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query

import deps
import platform_link
import solar_project_context as context_service
from routers.project_drawings import _dispatch, _headers
from routers.x07_profiles import PATH
from x07_customer_profile_store import CustomerProfileStore
from x07_postgres_profile_store import PostgresProfileStore

router = APIRouter()
HISTORY_PATH = PATH + '/history'


def _encode_cursor(customer, profile_id, through, before):
    raw = json.dumps([customer, profile_id, through, before], separators=(',', ':')).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _cursor_bounds(cursor, customer, latest):
    if cursor is None:
        return latest['version'], latest['version'] + 1
    try:
        raw = base64.b64decode(cursor, altchars=b'-_', validate=True)
        owner, profile_id, through, before = json.loads(raw)
        if type(through) is not int or type(before) is not int:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(400, 'Invalid profile history cursor.') from None
    if owner != customer or profile_id != latest['profile_id']:
        raise HTTPException(404, 'Profile history cursor unavailable.')
    if not 1 < before <= through <= latest['version']:
        raise HTTPException(400, 'Invalid profile history cursor.')
    return through, before


def _fields(roles):
    fields = {}
    for role, value in roles.items():
        if isinstance(value, dict):
            fields.update({(role, name): field for name, field in value.items()})
        else:
            fields[(role, '')] = value
    return fields


def _changes(previous, current):
    old, new = _fields(previous), _fields(current)
    changes = []
    for key in sorted(old.keys() | new.keys()):
        role, field = key
        if key not in old:
            operation = 'added'
        elif key not in new:
            operation = 'removed'
        elif old[key] != new[key]:
            operation = 'changed'
        else:
            continue
        changes.append({'role': role, 'field': field or None, 'operation': operation,
                        'old': old.get(key), 'new': new.get(key)})
    return changes


@router.get(HISTORY_PATH)
def history(project_id: UUID, drawing_id: UUID, input_version_id: UUID,
            limit: int = Query(20, ge=1, le=50), cursor: Optional[str] = Query(None, max_length=256),
            tenant=Depends(deps.require_active_tenant), x_org_id: Optional[UUID] = Header(None),
            x_project_id: Optional[UUID] = Header(None)):
    def operation():
        _headers(tenant, project_id, x_org_id, x_project_id)
        context = context_service.resolve_context(
            tenant, project_id, input_version_id, drawing_id=drawing_id, write=False)
        customer = str(context.binding.organization_id)
        identity = {'customer_key': customer, 'project_key': str(project_id),
                    'drawing_key': str(drawing_id), 'input_version_id': str(input_version_id)}
        with platform_link.platform_db().transaction(isolation='repeatable read', read_only=True) as connection:
            store = PostgresProfileStore(connection, customer)
            latest = store.latest(customer)
            if latest is None:
                if cursor is not None:
                    raise HTTPException(404, 'Profile history cursor unavailable.')
                return {**identity, 'profile_id': None, 'through_version': None, 'rows': [], 'next_cursor': None}
            through, before = _cursor_bounds(cursor, customer, latest)
            profile_id = latest['profile_id']
            raw_rows = connection.execute(
                'SELECT * FROM x07_profile_versions WHERE customer_key=%s AND profile_id=%s '
                'AND version<=%s AND version<%s ORDER BY version DESC LIMIT %s',
                (customer, profile_id, through, before, limit + 1),
            ).fetchall()
            versions = [CustomerProfileStore._profile(dict(row)) for row in raw_rows]
            expected = min(through, before - 1)
            if [row['version'] for row in versions] != list(
                range(expected, max(0, expected - limit - 1), -1)
            ):
                raise ValueError('profile history is incomplete')
            rows = []
            for index, row in enumerate(versions[:limit]):
                predecessor = versions[index + 1]['roles'] if row['version'] > 1 else {}
                rows.append({'version': row['version'], 'created_at': row['created_at'],
                             'changes': _changes(predecessor, row['roles'])})
            next_cursor = None
            if len(versions) > limit:
                next_cursor = _encode_cursor(customer, profile_id, through, rows[-1]['version'])
            return {**identity, 'profile_id': profile_id, 'through_version': through, 'rows': rows,
                    'next_cursor': next_cursor}

    return _dispatch(operation)
