"""Bounded editable pages within the existing project membership authority."""
from __future__ import annotations

from psycopg.types.json import Jsonb

from . import project_lifecycle as lifecycle
from .db import run_transaction
from .web_page_template import document_digest, validate_document


def _authorize(cur, org_id, project_id, actor_id, *, write=False):
    # All page writers serialize on the live project, including the first save.
    lifecycle._project_row(cur, org_id, project_id)
    lifecycle._require_project_role(cur, org_id, project_id, actor_id, write=write)
    cur.execute("SELECT role FROM project_member_bindings WHERE org_id=%s AND project_id=%s "
                "AND binding_id=%s AND status='active' FOR SHARE", (org_id, project_id, actor_id))
    membership = cur.fetchone()
    if membership is None or membership['role'] not in (lifecycle.WRITE_ROLES if write else lifecycle.PROJECT_ROLES):
        raise lifecycle.LifecycleForbidden('project access denied')


def _page(row):
    if row is None:
        return None
    document = validate_document(row['document'])
    if document_digest(document) != row['digest']:
        raise lifecycle.LifecycleConflict('The saved page could not be verified.')
    return {'revision': row['revision'], 'digest': row['digest'], 'document': document}


def read(org_id, project_id, actor_id, *, revision=None):
    if revision is not None and (type(revision) is not int or not 1 <= revision <= 2147483647):
        raise ValueError('Invalid page revision.')

    def operation(conn):
        with conn.cursor() as cur:
            _authorize(cur, org_id, project_id, actor_id)
            cur.execute('SELECT revision,digest,document FROM project_web_page_revisions '
                        'WHERE org_id=%s AND project_id=%s AND (%s::integer IS NULL OR revision=%s) '
                        'ORDER BY revision DESC LIMIT 1', (org_id, project_id, revision, revision))
            page = _page(cur.fetchone())
            if page is None and revision is not None:
                raise lifecycle.LifecycleUnavailable('page revision not found')
            return page
    return run_transaction(operation)


def save(org_id, project_id, actor_id, *, expected_revision, document):
    if type(expected_revision) is not int or not 0 <= expected_revision < 2147483647:
        raise ValueError('Invalid expected page revision.')
    document = validate_document(document)
    digest = document_digest(document)

    def operation(conn):
        with conn.cursor() as cur:
            _authorize(cur, org_id, project_id, actor_id, write=True)
            cur.execute('SELECT revision,digest,document FROM project_web_page_revisions '
                        'WHERE org_id=%s AND project_id=%s ORDER BY revision DESC LIMIT 1', (org_id, project_id))
            current = _page(cur.fetchone())
            head = current['revision'] if current else 0
            if current and digest == current['digest'] and expected_revision in (head, head - 1):
                return current
            if expected_revision != head:
                raise lifecycle.LifecycleConflict('This page changed elsewhere. Your draft is retained. Reload the saved page before saving again.')
            cur.execute('INSERT INTO project_web_page_revisions '
                        '(org_id,project_id,revision,document,digest,actor_binding_id) VALUES (%s,%s,%s,%s,%s,%s)',
                        (org_id, project_id, head + 1, Jsonb(document), digest, actor_id))
            return {'revision': head + 1, 'digest': digest, 'document': document}
    return run_transaction(operation, isolation='serializable')
