"""Real private PG transactions for BSP43 custody; authority seams stay synthetic.

Run only with the managed bsp43_pg_probe supplier. This verifies actual migration,
JSONB roundtrip, idempotent issuance and revocation, not live project publication.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import sys

import psycopg
from psycopg.rows import dict_row
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'server'), str(ROOT / 'server/tests')]
import test_project_checkout_custody as cases
import project_checkout_custody as custody


class PrivateDatabase:
    def __init__(self, dsn):
        self._dsn = dsn

    def run_transaction(self, operation):
        with psycopg.connect(self._dsn, row_factory=dict_row) as conn:
            return operation(conn)


def test_real_postgres_custody_issue_replay_revoke_and_no_credentials(monkeypatch):
    if not os.environ.get('LEAF_E364B_PG_PARENT'):
        pytest.fail('managed private PostgreSQL producer required')
    database = PrivateDatabase(os.environ['DATABASE_URL'])
    migration = (ROOT / 'platform/migrations/0097_project_checkout_custody.sql').read_text()
    def initialize(conn):
        # The normal platform autouse fixture applies the actual migration
        # closure first. Reapplying this migration must remain idempotent.
        assert conn.execute("SELECT to_regclass('project_checkout_custody') AS table_name").fetchone()['table_name'] == 'project_checkout_custody'
        assert conn.execute('SELECT count(*) AS n FROM project_checkout_custody').fetchone()['n'] == 0
        conn.execute(migration)
    database.run_transaction(initialize)
    try:
        lane = cases.lane.__wrapped__(monkeypatch)
        monkeypatch.setattr(custody.platform_link, 'platform_db', lambda: database)
        # Separate real connections race on the unique confirmation constraint.
        with ThreadPoolExecutor(max_workers=2) as pool:
            issued = list(pool.map(lambda _: cases.issue(lane), range(2)))
        assert issued[0] == issued[1]
        grant = cases.proof(lane)
        assert grant.fence_for(lane.tenant, custody.project.checkout_scope(cases.PROJECT, cases.DRAWING), lane.state) == 7
        rows = database.run_transaction(lambda conn: conn.execute('SELECT * FROM project_checkout_custody').fetchall())
        assert len(rows) == 1
        assert lane.token not in repr(rows)
        assert rows[0]['approved_args'] == cases.ARGS
        assert rows[0]['input_binding'] == cases.BINDING
        assert not {'capability', 'bearer', 'capability_digest'} & rows[0].keys()
        assert custody.revoke(lane.tenant, 'cid', 'session', issued[0]['delegation_id']) == {'revoked': True}
        with pytest.raises(custody.project.ProjectContextError, match='CHECKOUT_DENIED'):
            grant.fence_for(lane.tenant, custody.project.checkout_scope(cases.PROJECT, cases.DRAWING), lane.state)
        with pytest.raises(custody.CustodyError):
            cases.issue(lane)
    finally:
        database.run_transaction(lambda conn: conn.execute('DELETE FROM project_checkout_custody WHERE tenant_id=%s AND confirmation_id=%s', (cases.ORG, 'cid')))
