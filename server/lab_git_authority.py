"""Inert git authority substrate. All mutations share the ledger tenant lock."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3
import uuid

import lab_artifacts as ledger

GIT_AUTHORITY_READY = False

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_git_generations (
 tenant_id TEXT PRIMARY KEY, generation BIGINT NOT NULL CHECK (generation > 0));
CREATE TRIGGER IF NOT EXISTS lab_git_generations_no_delete BEFORE DELETE ON lab_git_generations
 BEGIN SELECT RAISE(ABORT,'lab_git_generations_retained'); END;
CREATE TABLE IF NOT EXISTS lab_git_authority (
 tenant_id TEXT PRIMARY KEY, mode TEXT NOT NULL CHECK (mode IN ('git','draining')),
 generation BIGINT NOT NULL, base_head TEXT, verified_head TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lab_git_pending (
 op_id TEXT PRIMARY KEY CHECK (length(op_id)=32 AND op_id NOT GLOB '*[^0-9a-f]*'),
 tenant_id TEXT NOT NULL, doc_id TEXT NOT NULL, revision BIGINT NOT NULL, parent_revision BIGINT,
 kind TEXT NOT NULL CHECK (kind IN ('graph','widget')), canonical_text TEXT NOT NULL,
 sha256 TEXT NOT NULL, base_blob TEXT, expected_parents TEXT NOT NULL
 CHECK (json_type(expected_parents)='array' AND json_array_length(expected_parents)<=4),
 rebase_count INTEGER NOT NULL DEFAULT 0 CHECK (rebase_count BETWEEN 0 AND 3),
 fence INTEGER NOT NULL DEFAULT 1, lease_owner TEXT NOT NULL, lease_expires_at TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'staged' CHECK (state IN ('staged','committing','ambiguous','abandoned')),
 reserved_bytes BIGINT NOT NULL CHECK (reserved_bytes>=0), new_document BOOLEAN NOT NULL,
 created_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS lab_git_pending_live_idx ON lab_git_pending (tenant_id,doc_id) WHERE state <> 'abandoned';
CREATE TRIGGER IF NOT EXISTS lab_git_pending_parents_insert BEFORE INSERT ON lab_git_pending
 WHEN EXISTS (SELECT 1 FROM json_each(NEW.expected_parents)
 WHERE type <> 'text' OR length(value)<>40 OR value GLOB '*[^0-9a-f]*')
 BEGIN SELECT RAISE(ABORT,'lab_git_invalid_parents'); END;
CREATE TRIGGER IF NOT EXISTS lab_git_pending_parents_update BEFORE UPDATE OF expected_parents ON lab_git_pending
 WHEN EXISTS (SELECT 1 FROM json_each(NEW.expected_parents)
 WHERE type <> 'text' OR length(value)<>40 OR value GLOB '*[^0-9a-f]*')
 BEGIN SELECT RAISE(ABORT,'lab_git_invalid_parents'); END;
CREATE TABLE IF NOT EXISTS lab_git_commits (
 tenant_id TEXT NOT NULL, "commit" TEXT NOT NULL CHECK (length("commit")=40 AND "commit" NOT GLOB '*[^0-9a-f]*'),
 op_id TEXT NOT NULL, doc_id TEXT NOT NULL, revision BIGINT NOT NULL,
 kind TEXT NOT NULL CHECK (kind IN ('graph','widget')), parent TEXT, recorded_at TEXT NOT NULL,
 PRIMARY KEY (tenant_id,"commit"));
CREATE TRIGGER IF NOT EXISTS lab_git_commits_no_update BEFORE UPDATE ON lab_git_commits
 BEGIN SELECT RAISE(ABORT,'lab_git_commits_append_only'); END;
CREATE TRIGGER IF NOT EXISTS lab_git_commits_no_delete BEFORE DELETE ON lab_git_commits
 BEGIN SELECT RAISE(ABORT,'lab_git_commits_append_only'); END;
CREATE TABLE IF NOT EXISTS lab_git_outbox (
 outbox_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('publish','notify')),
 identity TEXT NOT NULL, sequence BIGINT NOT NULL, payload TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt_at TEXT NOT NULL, done_at TEXT, UNIQUE (tenant_id,kind,identity));
CREATE TABLE IF NOT EXISTS lab_git_quarantine (
 tenant_id TEXT NOT NULL, op_id TEXT NOT NULL, doc_id TEXT NOT NULL, revision BIGINT NOT NULL,
 canonical_text TEXT NOT NULL, sha256 TEXT NOT NULL, reason TEXT NOT NULL, detected_at TEXT NOT NULL,
 PRIMARY KEY (tenant_id,op_id));
CREATE TABLE IF NOT EXISTS lab_git_transitions (
 tenant_id TEXT NOT NULL, transition_id TEXT NOT NULL CHECK (length(transition_id)=32 AND transition_id NOT GLOB '*[^0-9a-f]*'),
 kind TEXT NOT NULL CHECK (kind IN ('cutover','rollback')), from_generation BIGINT NOT NULL,
 to_generation BIGINT NOT NULL, barrier_head TEXT, owner TEXT NOT NULL, lease_expires_at TEXT NOT NULL,
 state TEXT NOT NULL CHECK (state IN ('planned','fenced','resolved','drained','proven','done','reverted')),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (tenant_id,transition_id));
CREATE UNIQUE INDEX IF NOT EXISTS lab_git_transition_live_idx ON lab_git_transitions (tenant_id) WHERE state NOT IN ('done','reverted');
CREATE TABLE IF NOT EXISTS lab_git_exit_token (
 singleton INTEGER PRIMARY KEY CHECK (singleton=1), tenant_id TEXT NOT NULL, transition_id TEXT NOT NULL);
DROP TRIGGER IF EXISTS lab_git_tenants_guard_update;
CREATE TRIGGER lab_git_tenants_guard_update BEFORE UPDATE ON lab_git_tenants
 WHEN EXISTS (SELECT 1 FROM lab_git_authority WHERE tenant_id=OLD.tenant_id)
 AND (NEW.tenant_id IS NOT OLD.tenant_id OR NEW.frozen IS NOT 1 OR NEW.mode IS NOT 'mirror')
 BEGIN
 SELECT RAISE(ABORT,'lab_git_exit_refused')
 WHERE NEW.tenant_id IS NOT OLD.tenant_id OR NOT EXISTS (
 SELECT 1 FROM lab_git_exit_token e JOIN lab_git_transitions t
 ON t.tenant_id=e.tenant_id AND t.transition_id=e.transition_id
 WHERE e.tenant_id=OLD.tenant_id AND t.state NOT IN ('done','reverted'));
 DELETE FROM lab_git_exit_token WHERE tenant_id=OLD.tenant_id;
 END;
DROP TRIGGER IF EXISTS lab_git_tenants_guard_delete;
CREATE TRIGGER lab_git_tenants_guard_delete BEFORE DELETE ON lab_git_tenants
 WHEN EXISTS (SELECT 1 FROM lab_git_authority WHERE tenant_id=OLD.tenant_id)
 BEGIN
 SELECT RAISE(ABORT,'lab_git_exit_refused')
 WHERE NOT EXISTS (SELECT 1 FROM lab_git_exit_token e JOIN lab_git_transitions t
 ON t.tenant_id=e.tenant_id AND t.transition_id=e.transition_id
 WHERE e.tenant_id=OLD.tenant_id AND t.state NOT IN ('done','reverted'));
 DELETE FROM lab_git_exit_token WHERE tenant_id=OLD.tenant_id;
 END;
"""


def _now():
    return datetime.now(timezone.utc)


def _date(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def _run(tenant, operation, *, write=False):
    def transaction(sql):
        if not sql.postgres:
            # executescript would commit the ledger transaction: execute complete
            # statements individually, including trigger bodies.
            statement = ''
            for line in _SQLITE_SCHEMA.splitlines(True):
                statement += line
                if sqlite3.complete_statement(statement):
                    sql.execute(statement)
                    statement = ''
            sql.execute('DELETE FROM lab_git_exit_token')
        result = operation(sql)
        if not sql.postgres:
            sql.execute('DELETE FROM lab_git_exit_token')
        return result
    # SQLite readiness DDL and token cleanup also mutate, even on reads.
    return ledger._run(tenant, transaction, write=True)


def _get(sql, table, tenant, key=None, value=None):
    row = sql.execute('SELECT * FROM ' + table + ' WHERE tenant_id=?' +
                      (' AND "' + key + '"=?' if key else ''),
                      (tenant, value) if key else (tenant,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    for field in ('expected_parents', 'payload'):
        if field in result and isinstance(result[field], str):
            result[field] = json.loads(result[field])
    return result


def _insert(sql, table, values):
    fields = list(values)
    placeholders = [('CAST(? AS jsonb)' if sql.postgres and k in ('payload', 'expected_parents') else '?') for k in fields]
    sql.execute('INSERT INTO ' + table + ' (' + ','.join('"' + k + '"' for k in fields) +
                ') VALUES (' + ','.join(placeholders) + ')', tuple(values.values()))


def next_generation(tenant):
    def operation(sql):
        sql.execute('INSERT INTO lab_git_generations (tenant_id,generation) VALUES (?,1) '
                    'ON CONFLICT (tenant_id) DO UPDATE SET generation=lab_git_generations.generation+1', (tenant,))
        return _get(sql, 'lab_git_generations', tenant)['generation']
    return _run(tenant, operation, write=True)


def authority_get(tenant):
    return _run(tenant, lambda sql: _get(sql, 'lab_git_authority', tenant))


def authority_insert(tenant, generation, base_head=None, verified_head=None, mode='git'):
    def operation(sql):
        allocated = _get(sql, 'lab_git_generations', tenant)
        if not allocated or allocated['generation'] != generation:
            raise ValueError('lab_git_generation_not_allocated')
        _insert(sql, 'lab_git_authority', dict(tenant_id=tenant, mode=mode, generation=generation,
                base_head=base_head, verified_head=verified_head, updated_at=_now().isoformat()))
        return _get(sql, 'lab_git_authority', tenant)
    return _run(tenant, operation, write=True)


def authority_set_mode(tenant, mode):
    return _run(tenant, lambda sql: sql.execute('UPDATE lab_git_authority SET mode=?,updated_at=? WHERE tenant_id=?',
                (mode, _now().isoformat(), tenant)).rowcount == 1, write=True)


@contextmanager
def authorize_exit(sql, tenant, transition_id):
    """Authorize an exit in the caller's transaction; SQLite consumes one use."""
    if sql.postgres:
        if sql.conn.info.transaction_status.name != 'INTRANS':
            raise ValueError('lab_git_exit_requires_transaction')
        if sql.execute('SHOW transaction_read_only').fetchone()['transaction_read_only'] != 'off':
            raise ValueError('lab_git_exit_requires_transaction')
    elif not sql.conn.in_transaction:
        raise ValueError('lab_git_exit_requires_transaction')
    row = _get(sql, 'lab_git_transitions', tenant, 'transition_id', transition_id)
    if row is None or row['state'] in ('done', 'reverted'):
        raise ValueError('lab_git_exit_refused')
    if sql.postgres:
        sql.execute("SELECT set_config('leaf.lab_git_writer', ?, true)", (tenant,))
        sql.execute("SELECT set_config('leaf.lab_git_transition', ?, true)", (transition_id,))
        yield
    else:
        sql.execute('INSERT INTO lab_git_exit_token VALUES (1,?,?) ON CONFLICT (singleton) '
                    'DO UPDATE SET tenant_id=excluded.tenant_id,transition_id=excluded.transition_id', (tenant, transition_id))
        try:
            yield
        finally:
            sql.execute('DELETE FROM lab_git_exit_token WHERE tenant_id=? AND transition_id=?', (tenant, transition_id))


def authority_delete(tenant, transition_id):
    def operation(sql):
        with authorize_exit(sql, tenant, transition_id):
            return sql.execute('DELETE FROM lab_git_authority WHERE tenant_id=?', (tenant,)).rowcount == 1
    return _run(tenant, operation, write=True)


def stage(tenant, op_id, doc_id, revision, parent_revision, kind, canonical_text, sha256,
          base_blob=None, expected_parents=(), lease_owner='writer', lease_s=30,
          reserved_bytes=None, new_document=False):
    parents = list(expected_parents)
    if len(parents) > 4 or any(not isinstance(p, str) or not re.fullmatch('[0-9a-f]{40}', p) for p in parents):
        raise ValueError('lab_git_invalid_parents')
    expires = _expiry(lease_s)
    def operation(sql):
        _insert(sql, 'lab_git_pending', dict(op_id=op_id, tenant_id=tenant, doc_id=doc_id, revision=revision,
            parent_revision=parent_revision, kind=kind, canonical_text=canonical_text, sha256=sha256,
            base_blob=base_blob, expected_parents=json.dumps(parents), rebase_count=0, fence=1,
            lease_owner=lease_owner, lease_expires_at=expires, state='staged',
            reserved_bytes=len(canonical_text.encode()) if reserved_bytes is None else reserved_bytes,
            new_document=new_document, created_at=_now().isoformat()))
        return _get(sql, 'lab_git_pending', tenant, 'op_id', op_id)
    return _run(tenant, operation, write=True)


def get(tenant, op_id):
    return _run(tenant, lambda sql: _get(sql, 'lab_git_pending', tenant, 'op_id', op_id))


def complete_conditional(tenant, op_id, fence):
    return _run(tenant, lambda sql: sql.execute('DELETE FROM lab_git_pending WHERE tenant_id=? AND op_id=? AND fence=?',
                (tenant, op_id, fence)).rowcount == 1, write=True)


def _expiry(lease_s):
    if not isinstance(lease_s, (int, float)) or not 0 < lease_s < float('inf'):
        raise ValueError('lab_git_invalid_lease')
    return (_now() + timedelta(seconds=lease_s)).isoformat()


def take_over_expired(tenant, op_id, owner, lease_s):
    expires = _expiry(lease_s)
    def operation(sql):
        changed = sql.execute("UPDATE lab_git_pending SET lease_owner=?,lease_expires_at=?,fence=fence+1 "
            "WHERE tenant_id=? AND op_id=? AND lease_expires_at<? AND state<>'abandoned'",
            (owner, expires, tenant, op_id, _now().isoformat())).rowcount
        return _get(sql, 'lab_git_pending', tenant, 'op_id', op_id) if changed else None
    return _run(tenant, operation, write=True)


def charge_rebase(tenant, op_id, fence, new_parent):
    if not isinstance(new_parent, str) or not re.fullmatch('[0-9a-f]{40}', new_parent):
        raise ValueError('lab_git_invalid_parent')
    def operation(sql):
        row = _get(sql, 'lab_git_pending', tenant, 'op_id', op_id)
        if not row or row['fence'] != fence or row['state'] == 'abandoned' or row['rebase_count'] >= 3:
            return False
        parents = row['expected_parents'] + [new_parent]
        if len(parents) > 4:
            return False
        cast = 'CAST(? AS jsonb)' if sql.postgres else '?'
        sql.execute('UPDATE lab_git_pending SET rebase_count=rebase_count+1,expected_parents=' + cast +
                    ' WHERE tenant_id=? AND op_id=? AND fence=?', (json.dumps(parents), tenant, op_id, fence))
        return True
    return _run(tenant, operation, write=True)


def _pending_state(tenant, op_id, fence, state):
    return _run(tenant, lambda sql: sql.execute("UPDATE lab_git_pending SET state=? WHERE tenant_id=? AND op_id=? "
        "AND fence=? AND state<>'abandoned'", (state, tenant, op_id, fence)).rowcount == 1, write=True)


def mark_ambiguous(tenant, op_id, fence):
    return _pending_state(tenant, op_id, fence, 'ambiguous')


def abandon(tenant, op_id, fence):
    return _pending_state(tenant, op_id, fence, 'abandoned')


def reservations(tenant):
    def operation(sql):
        row = sql.execute("SELECT COALESCE(SUM(reserved_bytes),0) AS bytes,COUNT(*) AS revisions,"
            "COALESCE(SUM(CASE WHEN new_document THEN 1 ELSE 0 END),0) AS new_documents "
            "FROM lab_git_pending WHERE tenant_id=? AND state<>'abandoned'", (tenant,)).fetchone()
        return dict(row)
    return _run(tenant, operation)


def record(tenant, commit, op_id, doc_id, revision, kind, parent=None):
    return _run(tenant, lambda sql: _insert(sql, 'lab_git_commits', dict(tenant_id=tenant,
        commit=commit, op_id=op_id, doc_id=doc_id, revision=revision, kind=kind, parent=parent,
        recorded_at=_now().isoformat())), write=True)


def commit_get(tenant, commit):
    return _run(tenant, lambda sql: _get(sql, 'lab_git_commits', tenant, 'commit', commit))


def enqueue(tenant, kind, identity, payload):
    def operation(sql):
        row = sql.execute('SELECT outbox_id FROM lab_git_outbox WHERE tenant_id=? AND kind=? AND identity=?',
                          (tenant, kind, identity)).fetchone()
        if row:
            return _get(sql, 'lab_git_outbox', tenant, 'outbox_id', row['outbox_id'])
        seq = sql.execute('SELECT COALESCE(MAX(sequence),0)+1 AS n FROM lab_git_outbox WHERE tenant_id=?', (tenant,)).fetchone()['n']
        outbox_id = uuid.uuid4().hex
        _insert(sql, 'lab_git_outbox', dict(outbox_id=outbox_id, tenant_id=tenant, kind=kind,
            identity=identity, sequence=seq, payload=json.dumps(payload, allow_nan=False), attempts=0,
            next_attempt_at=_now().isoformat(), done_at=None))
        return _get(sql, 'lab_git_outbox', tenant, 'outbox_id', outbox_id)
    return _run(tenant, operation, write=True)


def claim_due(tenant, limit=100):
    """Reserve due entries for 30 seconds under their tenant's ledger lock."""
    if type(limit) is not int or limit < 0:
        raise ValueError('lab_git_invalid_limit')
    def operation(sql):
        rows = sql.execute('SELECT outbox_id FROM lab_git_outbox WHERE tenant_id=? AND done_at IS NULL '
            'AND next_attempt_at<=? ORDER BY sequence LIMIT ?', (tenant, _now().isoformat(), limit)).fetchall()
        result = []
        for row in rows:
            sql.execute('UPDATE lab_git_outbox SET attempts=attempts+1,next_attempt_at=? WHERE tenant_id=? AND outbox_id=?',
                        ((_now() + timedelta(seconds=30)).isoformat(), tenant, row['outbox_id']))
            result.append(_get(sql, 'lab_git_outbox', tenant, 'outbox_id', row['outbox_id']))
        return result
    return _run(tenant, operation, write=True)


def mark_done(tenant, outbox_id):
    return _run(tenant, lambda sql: sql.execute('UPDATE lab_git_outbox SET done_at=? WHERE tenant_id=? '
        'AND outbox_id=? AND done_at IS NULL', (_now().isoformat(), tenant, outbox_id)).rowcount == 1, write=True)


def defer(tenant, outbox_id, backoff):
    due = _expiry(backoff)
    return _run(tenant, lambda sql: sql.execute('UPDATE lab_git_outbox SET next_attempt_at=? WHERE tenant_id=? '
        'AND outbox_id=? AND done_at IS NULL', (due, tenant, outbox_id)).rowcount == 1, write=True)


def quarantine_add(tenant, op_id, doc_id, revision, canonical_text, sha256, reason):
    return _run(tenant, lambda sql: _insert(sql, 'lab_git_quarantine', dict(tenant_id=tenant, op_id=op_id,
        doc_id=doc_id, revision=revision, canonical_text=canonical_text, sha256=sha256, reason=reason,
        detected_at=_now().isoformat())), write=True)


def begin(tenant, transition_id, kind, from_generation, to_generation, owner, lease_s=30, barrier_head=None):
    expires = _expiry(lease_s)
    def operation(sql):
        now = _now().isoformat()
        _insert(sql, 'lab_git_transitions', dict(tenant_id=tenant, transition_id=transition_id, kind=kind,
            from_generation=from_generation, to_generation=to_generation, owner=owner, lease_expires_at=expires,
            barrier_head=barrier_head, state='planned', created_at=now, updated_at=now))
        return _get(sql, 'lab_git_transitions', tenant, 'transition_id', transition_id)
    return _run(tenant, operation, write=True)


def advance(tenant, transition_id, owner, from_state, to_state, **fields):
    if set(fields) - {'barrier_head', 'lease_expires_at'}:
        raise ValueError('lab_git_invalid_transition_fields')
    def operation(sql):
        values = dict(fields, state=to_state, updated_at=_now().isoformat())
        return sql.execute('UPDATE lab_git_transitions SET ' + ','.join(k + '=?' for k in values) +
            " WHERE tenant_id=? AND transition_id=? AND owner=? AND state=? AND state NOT IN ('done','reverted')",
            (*values.values(), tenant, transition_id, owner, from_state)).rowcount == 1
    return _run(tenant, operation, write=True)


def transition_take_over_expired(tenant, transition_id, owner, lease_s):
    expires = _expiry(lease_s)
    return _run(tenant, lambda sql: sql.execute('UPDATE lab_git_transitions SET owner=?,lease_expires_at=?,updated_at=? '
        "WHERE tenant_id=? AND transition_id=? AND lease_expires_at<? AND state NOT IN ('done','reverted')",
        (owner, expires, _now().isoformat(), tenant, transition_id, _now().isoformat())).rowcount == 1, write=True)


def get_active(tenant):
    def operation(sql):
        row = sql.execute("SELECT * FROM lab_git_transitions WHERE tenant_id=? AND state NOT IN ('done','reverted')", (tenant,)).fetchone()
        return dict(row) if row else None
    return _run(tenant, operation)
