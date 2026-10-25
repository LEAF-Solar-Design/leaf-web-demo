"""CAS mirror and generation-fenced git authority for Lab documents.

Mirror failures never fail a ledger write. Git authority stages under the tenant
lock, commits outside the transaction, then completes under a lease fence.
Publication is independently retryable through the durable outbox.
No document content or author identity is sent to the alert path.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.request
import uuid

import lab_artifacts as ledger
import emf_metrics

LOG = logging.getLogger(__name__)
MAX_REPLAY_REVISIONS = 2000
REF = "refs/heads/lab/main"
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_git_tenants (
 tenant_id TEXT PRIMARY KEY,
 mode TEXT NOT NULL DEFAULT 'ledger' CHECK (mode IN ('ledger','replaying','mirror')),
 frozen BOOLEAN NOT NULL DEFAULT FALSE, remote_sha TEXT, local_sha TEXT,
 receipt TEXT NOT NULL DEFAULT '{}', notified TEXT NOT NULL DEFAULT '[]',
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS lab_git_mirror (
 tenant_id TEXT NOT NULL, doc_id TEXT NOT NULL, revision INTEGER NOT NULL,
 "commit" TEXT NOT NULL, sha256 TEXT NOT NULL,
 PRIMARY KEY (tenant_id, doc_id, revision)
);
"""


class MirrorError(RuntimeError):
    def __init__(self, code, observed=None, capability=None):
        self.code = code
        self.observed = observed
        self.capability = capability
        super().__init__(code)


def _allowed(tenant):
    ids = [value.strip() for value in os.environ.get("LEAF_LAB_GIT_TENANTS", "").split(",") if value.strip()]
    return len(ids) <= 100 and tenant in ids


def _post(tenant, endpoint, body):
    base = os.environ.get("LEAF_AUTHOR_HARNESS_URL", "").strip().rstrip("/")
    secret = os.environ.get("LEAF_HARNESS_SECRET", "")
    if not base or not secret:
        raise MirrorError("unknown")
    try:
        request = urllib.request.Request(base + "/lab/" + endpoint,
            data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "x-tenant-id": tenant,
                     "X-Harness-Secret": secret})
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                raise MirrorError("unknown")
            result = json.loads(response.read())
            capability = getattr(response, 'headers', {}).get('x-leaf-lab-capability')
        if not isinstance(result, dict) or (endpoint in {'inspect', 'commit', 'publish'} and result.get("ref") != REF):
            raise MirrorError("unknown")
        result['_capability'] = capability
        return result
    except urllib.error.HTTPError as error:
        # Never forward harness text, which may contain sensitive diagnostics.
        codes = {("commit", 403): "lab_repository_refused",
                 ("inspect", 403): "lab_repository_refused",
                 ("publish", 403): "lab_publish_refused",
                 ("publish", 422): "lab_publish_invalid"}
        observed = None
        code = codes.get((endpoint, error.code), "unknown")
        try:
            detail = json.loads(error.read())['error']
            observed = detail.get('observed_sha')
            if error.code == 409 and detail.get('code') in {
                    'lab_ref_conflict', 'lab_generation_conflict', 'lab_publish_conflict',
                    'lab_history_nonlinear', 'lab_history_too_long', 'lab_ops_ambiguous',
                    'lab_history_not_ancestor'}:
                code = detail['code']
        except Exception:
            pass
        raise MirrorError(code, observed, (error.headers or {}).get('x-leaf-lab-capability', '')) from None
    except MirrorError:
        raise
    except Exception:
        raise MirrorError("unknown") from None


def _inspect(tenant):
    result = _post(tenant, "inspect", {"limit": 200})
    head = result.get("head")
    if ((head is not None and (not isinstance(head, str) or not _SHA.fullmatch(head)))
            or not isinstance(result.get("documents"), list)
            or not isinstance(result.get("commits"), list)):
        raise MirrorError("unknown")
    return result


def _generation_post(tenant, endpoint, body, target=None):
    """Mirror writers discover a fence before every mutation, including retries."""
    for attempt in range(2):
        discovered = _post(tenant, 'generation', {})
        generation = discovered.get('generation')
        if ('generation' not in discovered or (generation is not None and
                (not isinstance(generation, str) or not re.fullmatch('[1-9][0-9]{0,18}', generation)))):
            raise MirrorError('unknown')
        request = dict(body)
        if generation is not None:
            request['generation'] = str(generation)
            if endpoint == 'publish':
                request['target_sha'] = target
        try:
            return _post(tenant, endpoint, request)
        except MirrorError as error:
            if error.code != 'lab_generation_conflict' or attempt:
                raise


def _git_post(tenant, endpoint, body):
    try:
        result = _post(tenant, endpoint, body)
    except MirrorError as error:
        if error.capability is not None and error.capability != 'lab-generation-v1':
            raise MirrorError('lab_harness_capability_missing') from None
        raise
    if result.get('_capability') != 'lab-generation-v1':
        raise MirrorError('lab_harness_capability_missing')
    return result


def _blob(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode('ascii') + b'\0' + data).hexdigest()


def stage_write(sql, tenant, doc_id, data, author, current, parent, kind):
    import lab_git_authority as store

    active = store.authority_get_sql(sql, tenant)
    if not active or active['mode'] != 'git':
        raise ledger.GitUnavailable('lab_store_migrating')
    live = sql.execute("SELECT op_id FROM lab_git_pending WHERE tenant_id=? AND doc_id=? AND state<>'abandoned'",
                       (tenant, doc_id)).fetchone()
    if live:
        raise ledger.GitConflict('lab_artifact_conflict')
    reserved = store.reservations_sql(sql, tenant)
    counts = sql.execute('SELECT COUNT(*) AS n FROM lab_documents WHERE tenant_id=?', (tenant,)).fetchone()['n']
    revisions = sql.execute('SELECT COUNT(*) AS n FROM lab_revisions WHERE tenant_id=?', (tenant,)).fetchone()['n']
    used = sql.execute('SELECT COALESCE(SUM(byte_count),0) AS n FROM lab_blobs WHERE tenant_id=?', (tenant,)).fetchone()['n']
    digest = hashlib.sha256(data).hexdigest()
    exists = sql.execute('SELECT sha256 FROM lab_blobs WHERE tenant_id=? AND sha256=?', (tenant, digest)).fetchone()
    added = 0 if exists else len(data)
    if ((current is None and counts + reserved['new_documents'] >= ledger.MAX_TENANT_DOCS)
            or revisions + reserved['revisions'] >= ledger.MAX_TENANT_REVISIONS):
        raise ledger.TooLarge('tenant_revision_cap')
    if used + reserved['bytes'] + added > ledger.MAX_TENANT_BYTES:
        raise ledger.TooLarge('tenant_byte_cap')
    base = None if current is None else _blob(ledger._revision(sql, tenant, doc_id, current)['canonical_text'].encode('utf-8'))
    local = _state(sql, tenant)['local_sha']
    if not isinstance(local, str) or not _SHA.fullmatch(local):
        raise ledger.GitUnavailable('lab_store_unavailable')
    # Persist the writer context with the lease so recovery retains the original
    # generation and attribution without a second, independently committed row.
    owner = json.dumps({'generation': str(active['generation']), 'author': author})
    row = store.stage_sql(sql, tenant, uuid.uuid4().hex, doc_id, (current or 0) + 1,
        parent, kind, data.decode('utf-8'), digest, base_blob=base, expected_parents=[local],
        lease_owner=owner, lease_s=60, reserved_bytes=added, new_document=current is None)
    return {**row, '_git_staged': True, '_generation': str(active['generation']), '_author': author}


def _pending_context(row, active):
    try:
        context = json.loads(row['lease_owner'])
        return context['generation'], context['author']
    except (ValueError, TypeError, KeyError):
        return str(active['generation']), 'git-recovery'


def _pending_message(row, author):
    import lab_git_authority as store

    created = store._date(row['created_at']).timestamp()
    return _message({**row, 'created_at': created, 'author': author,
                     'published_revision': None}) + '\nLab-Op: ' + row['op_id']


def _changes(tenant, since, head):
    """Validate a complete parent chain across every page, never a prefix."""
    commits, after, expected = [], None, head
    seen = set()
    while True:
        body = {'since': since, 'head': head, 'limit': 200}
        if after is not None:
            body['after'] = after
        page = _git_post(tenant, 'changes', body)
        entries = page.get('commits')
        if not isinstance(entries, list) or 'next' not in page:
            raise MirrorError('lab_history_not_ancestor')
        for entry in entries:
            if not isinstance(entry, dict):
                raise MirrorError('lab_history_nonlinear')
            sha = entry.get('commit')
            if (not isinstance(sha, str) or not _SHA.fullmatch(sha) or sha != expected or sha in seen
                    or not isinstance(entry.get('changed_paths'), list)
                    or not isinstance(entry.get('trailers'), dict)):
                raise MirrorError('lab_history_nonlinear')
            seen.add(sha)
            expected = entry.get('parent')
            commits.append(entry)
        if len(commits) > 10000:
            raise MirrorError('lab_history_too_long')
        after = page['next']
        if after is None:
            if expected != since:
                raise MirrorError('lab_history_not_ancestor')
            return commits
        if not entries or after != entries[-1]['commit']:
            raise MirrorError('lab_history_not_ancestor')


def _git_inspect(tenant):
    result = _git_post(tenant, 'inspect', {'limit': 200})
    if (not isinstance(result.get('head'), str) or not _SHA.fullmatch(result['head'])
            or not isinstance(result.get('documents'), list)
            or any(not isinstance(doc, dict) or not isinstance(doc.get('path'), str)
                   or not isinstance(doc.get('blob'), str) or not _SHA.fullmatch(doc['blob'])
                   for doc in result['documents'])):
        raise MirrorError('unknown')
    return result


def _rebase(tenant, row, inspection):
    import lab_git_authority as store

    head = inspection['head']
    history = _changes(tenant, row['expected_parents'][-1], head)
    blobs = [doc.get('blob') for doc in inspection['documents'] if doc.get('path') == _path(row)]
    if (len(blobs) > 1 or (blobs[0] if blobs else None) != row['base_blob']
            or not store.charge_rebase(tenant, row['op_id'], row['fence'], head)):
        raise MirrorError('lab_ref_conflict')
    return {**row, 'expected_parents': row['expected_parents'] + [head],
            'rebase_count': row['rebase_count'] + 1,
            '_history': history + row.get('_history', [])}


def _quarantine_sql(sql, tenant, row, reason):
    import lab_git_authority as store

    pending = store.get_sql(sql, tenant, row['op_id'])
    active = store.authority_get_sql(sql, tenant)
    if (not pending or pending['fence'] != row['fence'] or pending['state'] == 'abandoned'
            or not active or active['mode'] != 'git'
            or str(_pending_context(row, active)[0]) != str(active['generation'])):
        return False
    if store._get(sql, 'lab_git_quarantine', tenant, 'op_id', row['op_id']) is None:
        store._insert(sql, 'lab_git_quarantine', dict(tenant_id=tenant, op_id=row['op_id'],
            doc_id=row['doc_id'], revision=row['revision'], canonical_text=row['canonical_text'],
            sha256=row['sha256'], reason=reason, detected_at=store._now().isoformat()))
    sql.execute("UPDATE lab_git_pending SET state='abandoned' WHERE tenant_id=? AND op_id=? AND fence=?",
                (tenant, row['op_id'], row['fence']))
    store.enqueue_sql(sql, tenant, 'notify', row['op_id'], {'event': 'lab_replay_divergence'})
    return True


def _complete_pending(tenant, row, generation, commit, parent, author='git-recovery', *, history=()):
    import lab_git_authority as store

    proven = {entry['commit']: entry for entry in history}

    class CompletionConflict(Exception):
        pass

    def operation(sql):
        active = store.authority_get_sql(sql, tenant)
        pending = store.get_sql(sql, tenant, row['op_id'])
        if (not active or str(active['generation']) != str(generation) or active['mode'] != 'git'
                or not pending or pending['fence'] != row['fence'] or pending['state'] == 'abandoned'):
            return None
        current = ledger._head(sql, tenant, row['doc_id'])
        if (current or 0) + 1 != row['revision']:
            _quarantine_sql(sql, tenant, row, 'revision_divergence')
            return None
        result = ledger._append(sql, tenant, row['doc_id'], row['canonical_text'].encode('utf-8'),
                                author, current, row['parent_revision'], row['kind'])
        store.record_sql(sql, tenant, commit, row['op_id'], row['doc_id'], row['revision'], row['kind'], parent)
        local = _state(sql, tenant)['local_sha']
        moved = False
        def is_ancestor(older, newer):
            seen = set()
            while newer != older:
                if newer in seen:
                    return False
                seen.add(newer)
                known = store._get(sql, 'lab_git_commits', tenant, 'commit', newer) or proven.get(newer)
                if not known:
                    return False
                newer = known['parent']
            return True

        # Rebase/recovery history is proved by _changes. Bring the local cursor
        # through that proof before applying the completed commit's parent CAS.
        if local != parent and is_ancestor(local, parent):
            if sql.execute('UPDATE lab_git_tenants SET local_sha=? WHERE tenant_id=? AND local_sha=?',
                           (parent, tenant, local)).rowcount != 1:
                raise CompletionConflict()
            local = parent
        if local == parent:
            if sql.execute('UPDATE lab_git_tenants SET local_sha=?,updated_at=CURRENT_TIMESTAMP '
                           'WHERE tenant_id=? AND local_sha=?', (commit, tenant, parent)).rowcount != 1:
                raise CompletionConflict()
            local, moved = commit, True
        else:
            # An earlier completion can arrive after its recorded descendants.
            # It may fill a ledger gap, but must never rewind the local cursor.
            if not is_ancestor(commit, local):
                raise CompletionConflict()
        verified = active['verified_head']
        for _ in range(1000):
            successors = sql.execute('SELECT "commit" FROM lab_git_commits WHERE tenant_id=? '
                'AND (parent=? OR (parent IS NULL AND CAST(? AS TEXT) IS NULL)) LIMIT 2',
                (tenant, verified, verified)).fetchall()
            if not successors:
                break
            if len(successors) != 1:
                raise CompletionConflict()
            successor = successors[0]['commit']
            if sql.execute('UPDATE lab_git_authority SET verified_head=?,updated_at=? '
                           'WHERE tenant_id=? AND generation=? AND '
                           '(verified_head=? OR (verified_head IS NULL AND CAST(? AS TEXT) IS NULL))',
                           (successor, store._now().isoformat(), tenant, active['generation'],
                            verified, verified)).rowcount != 1:
                raise CompletionConflict()
            verified = successor
        if not store.complete_conditional_sql(sql, tenant, row['op_id'], row['fence']):
            raise CompletionConflict()
        if moved:
            store.enqueue_sql(sql, tenant, 'publish', local, {'target_sha': local, 'generation': str(generation)})
        return result
    try:
        return store._run(tenant, operation, write=True)
    except CompletionConflict:
        return None


def commit_pending(tenant, row, generation, author='git-recovery', *, uncertain=False):
    import lab_git_authority as store

    while True:
        def fence_valid(sql):
            active = store.authority_get_sql(sql, tenant)
            pending = store.get_sql(sql, tenant, row['op_id'])
            if not active or active['mode'] != 'git' or str(active['generation']) != str(generation):
                raise ledger.GitUnavailable('lab_store_migrating')
            if (not pending or pending['fence'] != row['fence'] or pending['state'] == 'abandoned'
                    or pending['expected_parents'] != row['expected_parents']):
                raise ledger.GitConflict('lab_artifact_conflict')
            sql.execute("UPDATE lab_git_pending SET state='committing' WHERE tenant_id=? AND op_id=? AND fence=?",
                        (tenant, row['op_id'], row['fence']))
        store._run(tenant, fence_valid, write=True)
        try:
            result = _git_post(tenant, 'commit', {'expected_sha': row['expected_parents'][-1],
                'generation': str(generation), 'documents': [{'path': _path(row), 'content': row['canonical_text']}],
                'message': _pending_message(row, author)})
            commit = result.get('commit')
            if not isinstance(commit, str) or not _SHA.fullmatch(commit):
                raise MirrorError('unknown')
            completed = _complete_pending(tenant, row, generation, commit, row['expected_parents'][-1], author,
                                          history=row.get('_history', ()))
            if completed is None:
                raise ledger.GitConflict('lab_artifact_conflict')
            return completed
        except MirrorError as error:
            if error.code == 'lab_ref_conflict':
                try:
                    row = _rebase(tenant, row, _git_inspect(tenant))
                    continue
                except MirrorError as refused:
                    if refused.code == 'lab_harness_capability_missing':
                        store.mark_ambiguous(tenant, row['op_id'], row['fence'])
                        raise ledger.GitUnavailable(refused.code) from None
                    if uncertain:
                        store.mark_ambiguous(tenant, row['op_id'], row['fence'])
                        raise ledger.GitUnavailable('lab_store_unavailable') from None
                    else:
                        store.abandon(tenant, row['op_id'], row['fence'])
                    raise ledger.GitConflict('lab_artifact_conflict') from None
            store.mark_ambiguous(tenant, row['op_id'], row['fence'])
            code = ('lab_store_migrating' if error.code == 'lab_generation_conflict' else
                    'lab_harness_capability_missing' if error.code == 'lab_harness_capability_missing' else
                    'lab_store_unavailable')
            raise ledger.GitUnavailable(code) from None


def _recover_row(tenant, row):
    import lab_git_authority as store

    active = store.authority_get(tenant)
    if not active or active['mode'] != 'git':
        raise ledger.GitUnavailable('lab_store_migrating')
    generation, author = _pending_context(row, active)
    if str(active['generation']) != str(generation):
        store.mark_ambiguous(tenant, row['op_id'], row['fence'])
        raise ledger.GitUnavailable('lab_store_migrating')
    row = store.take_over_expired(tenant, row['op_id'], row['lease_owner'], 60)
    if row is None:
        return None
    try:
        inspected = _git_inspect(tenant)
        lookup = _git_post(tenant, 'ops', {'op_ids': [row['op_id']], 'head': inspected['head']})
        if not isinstance(lookup.get('ops'), dict) or row['op_id'] not in lookup['ops']:
            raise MirrorError('unknown')
        match = lookup['ops'][row['op_id']]
        history = _changes(tenant, row['expected_parents'][0], inspected['head'])
        for entry in history:
            trailers = entry['trailers']
            if _path(row) not in entry['changed_paths'] and trailers.get('Lab-Doc') != row['doc_id']:
                continue
            if trailers.get('Lab-Op') == row['op_id']:
                continue
            known = store.commit_get(tenant, entry['commit'])
            if known and known['doc_id'] == row['doc_id'] and str(known['revision']) == trailers.get('Lab-Revision'):
                continue
            store._run(tenant, lambda sql: _quarantine_sql(sql, tenant, row, 'replay_divergence'), write=True)
            raise ledger.GitConflict('lab_artifact_conflict')
        if match is not None:
            if (not isinstance(match, dict) or match.get('doc_id') != row['doc_id']
                    or match.get('revision') != row['revision'] or match.get('sha256') != row['sha256']):
                raise MirrorError('lab_ops_ambiguous')
            entry = next((item for item in history if item['commit'] == match.get('commit')), None)
            if entry is None or any(entry['trailers'].get(key) != value for key, value in {
                    'Lab-Op': row['op_id'], 'Lab-Doc': row['doc_id'],
                    'Lab-Revision': str(row['revision']), 'Lab-Sha256': row['sha256']}.items()):
                raise MirrorError('lab_ops_ambiguous')
            return _complete_pending(tenant, row, generation, entry['commit'], entry['parent'], author, history=history)
        if inspected['head'] not in row['expected_parents']:
            row = _rebase(tenant, row, inspected)
        return commit_pending(tenant, row, generation, author, uncertain=True)
    except MirrorError as error:
        store.mark_ambiguous(tenant, row['op_id'], row['fence'])
        raise ledger.GitUnavailable('lab_harness_capability_missing' if error.code == 'lab_harness_capability_missing'
                                    else 'lab_store_unavailable') from None


def recover_document(tenant, doc_id):
    import lab_git_authority as store

    rows = store._run(tenant, lambda sql: sql.execute("SELECT op_id FROM lab_git_pending "
        "WHERE tenant_id=? AND doc_id=? AND state<>'abandoned'", (tenant, doc_id)).fetchall())
    for item in rows:
        row = store.get(tenant, item['op_id'])
        if row is None:
            continue
        if store._date(row['lease_expires_at']) > store._now():
            raise ledger.GitConflict('lab_artifact_conflict')
        _recover_row(tenant, row)


def recover_pending(tenant, limit=10):
    import lab_git_authority as store

    if type(limit) is not int or limit < 0:
        raise ValueError('lab_git_invalid_limit')
    rows = store._run(tenant, lambda sql: sql.execute("SELECT op_id FROM lab_git_pending "
        "WHERE tenant_id=? AND state<>'abandoned' AND lease_expires_at<=? ORDER BY created_at,op_id LIMIT ?",
        (tenant, store._now().isoformat(), limit)).fetchall())
    results = []
    for item in rows:
        row = store.get(tenant, item['op_id'])
        if row:
            try:
                results.append(_recover_row(tenant, row))
            except (ledger.GitConflict, ledger.GitUnavailable):
                continue
    return results


def process_outbox(tenant, limit=20):
    import lab_git_authority as store

    completed = 0
    for row in store.claim_due(tenant, limit):
        try:
            if row['kind'] == 'publish':
                state = mirror_status(tenant)
                body = {'expected_remote_sha': state['remote_sha'], 'target_sha': row['payload']['target_sha'],
                        'generation': str(row['payload']['generation'])}
                try:
                    result = _git_post(tenant, 'publish', body)
                except MirrorError as error:
                    if (error.code != 'lab_publish_conflict' or not isinstance(error.observed, str)
                            or not _SHA.fullmatch(error.observed)):
                        raise
                    # A lost response or another worker may already have moved
                    # the remote. The harness proves the target cannot regress it.
                    result = _git_post(tenant, 'publish', {**body, 'expected_remote_sha': error.observed})
                published = result.get('published')
                if not isinstance(published, str) or not _SHA.fullmatch(published):
                    raise MirrorError('unknown')
                ledger._run(tenant, lambda sql: _update(sql, tenant, remote_sha=published), write=True)
            else:
                _notify(tenant, row['payload']['event'], row['identity'])
            store.mark_done(tenant, row['outbox_id'])
            completed += 1
        except Exception:
            store.defer(tenant, row['outbox_id'], min(3600, 2 ** min(row['attempts'], 12)))
            if row['kind'] == 'publish':
                store.enqueue(tenant, 'notify', row['identity'], {'event': 'lab_commit_unknown'})
    return completed


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _state(sql, tenant):
    sql.execute("INSERT INTO lab_git_tenants (tenant_id) VALUES (?) ON CONFLICT DO NOTHING", (tenant,))
    row = dict(sql.execute("SELECT * FROM lab_git_tenants WHERE tenant_id=?", (tenant,)).fetchone())
    row["receipt"] = _json(row["receipt"])
    row["notified"] = _json(row["notified"])
    return row


def _update(sql, tenant, **values):
    assignments, params = [], []
    for key, value in values.items():
        if key in {"receipt", "notified"}:
            value = json.dumps(value)
            assignments.append(key + ("=CAST(? AS jsonb)" if sql.postgres else "=?"))
        else:
            assignments.append(key + "=?")
        params.append(value)
    sql.execute("UPDATE lab_git_tenants SET " + ",".join(assignments)
                + ",updated_at=CURRENT_TIMESTAMP WHERE tenant_id=?", (*params, tenant))


def mirror_status(tenant):
    return ledger._run(tenant, lambda sql: _state(sql, tenant), write=True)


def check_frozen(sql, tenant):
    row = sql.execute("SELECT frozen FROM lab_git_tenants WHERE tenant_id=?", (tenant,)).fetchone()
    if row and row["frozen"]:
        raise ledger.StoreMigrating("lab_store_migrating")


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _path(row):
    return "lab/" + ("widgets" if row["kind"] == "widget" else "graphs") + "/" + row["doc_id"] + ".json"


def _message(row):
    created = datetime.fromtimestamp(row["created_at"], timezone.utc).isoformat().replace("+00:00", "Z")
    trailers = ["Lab-Doc: " + row["doc_id"], "Lab-Revision: " + str(row["revision"]),
                "Lab-Kind: " + row["kind"], "Lab-Sha256: " + row["sha256"],
                "Lab-Created-At: " + created,
                "Lab-Author-Hash: " + hashlib.sha256(row["author"].encode("utf-8")).hexdigest()]
    if row["published_revision"] == row["revision"]:
        trailers.append("Lab-Published: true")
    return "Lab ledger mirror\n\n" + "\n".join(trailers)


def _rows(sql, tenant, watermark=None):
    rows = sql.execute(
        "SELECT r.*,d.kind,d.head,d.published_revision FROM lab_revisions r"
        " JOIN lab_documents d ON d.tenant_id=r.tenant_id AND d.doc_id=r.doc_id"
        " WHERE r.tenant_id=? ORDER BY r.created_at,r.doc_id,r.revision", (tenant,)).fetchall()
    result = []
    for row in rows:
        if watermark is not None and row["revision"] > watermark.get(row["doc_id"], 0):
            continue
        result.append(dict(row))
    return result


def _equal(sql, tenant, inspection):
    heads = sql.execute("SELECT doc_id,head FROM lab_documents WHERE tenant_id=?", (tenant,)).fetchall()
    expected = {}
    for head in heads:
        row = ledger._revision(sql, tenant, head["doc_id"], head["head"])
        expected[_path(row)] = row["sha256"]
    documents = inspection["documents"]
    return (len(documents) == len(expected)
            and {doc.get("path"): doc.get("sha256") for doc in documents} == expected)


def _record(sql, tenant, row, commit):
    sql.execute('INSERT INTO lab_git_mirror (tenant_id,doc_id,revision,"commit",sha256)'
                " VALUES (?,?,?,?,?) ON CONFLICT (tenant_id,doc_id,revision)"
                ' DO UPDATE SET "commit"=excluded."commit",sha256=excluded.sha256',
                (tenant, row["doc_id"], row["revision"], commit, row["sha256"]))


def _known_revisions(sql, tenant, commits):
    for commit in commits:
        pairs = re.findall(r"^(Lab-[A-Za-z0-9-]+): (.*)$", commit["message"], re.MULTILINE)
        trailers = dict(pairs)
        if "Lab-Revision" not in trailers:
            continue
        if any(sum(key == name for key, _ in pairs) != 1 for name in ("Lab-Revision", "Lab-Doc")):
            return False
        try:
            row = ledger._revision(sql, tenant, trailers.get("Lab-Doc"), int(trailers["Lab-Revision"]))
        except (ValueError, ledger.NotFound):
            return False
        if trailers.get("Lab-Sha256") != row["sha256"]:
            return False
    return True


def _commit(sql, tenant, state, row):
    canonical_text = ledger._revision(sql, tenant, row["doc_id"], row["revision"])["canonical_text"]
    try:
        result = _generation_post(tenant, "commit", {"expected_sha": state["local_sha"],
            "documents": [{"path": _path(row), "content": canonical_text}],
            "message": _message(row)})
    except MirrorError as error:
        if error.code != "lab_ref_conflict":
            raise
        # Recover a lost commit response only from an exact parent, trailer
        # message and content digest match. Never overwrite an unrelated head.
        inspected = _inspect(tenant)
        commits = inspected["commits"]
        if (not commits or commits[0].get("parent") != state["local_sha"]
                or commits[0].get("message") != _message(row)
                or commits[0].get("commit") != inspected["head"]
                or not any(doc.get("path") == _path(row) and doc.get("sha256") == row["sha256"]
                           for doc in inspected["documents"])):
            raise
        result = {"commit": inspected["head"]}
    commit = result.get("commit")
    if not isinstance(commit, str) or not _SHA.fullmatch(commit):
        raise MirrorError("unknown")
    _record(sql, tenant, row, commit)
    receipt = state["receipt"]
    receipt = {**receipt, "heads": {**receipt.get("heads", {}), row["doc_id"]: row["revision"]}}
    # A commit SHA binds its entire ancestry. Extend this proof only through
    # our CAS parent, so flip-back can verify histories longer than inspect's
    # 200-commit window without claiming anything about unseen foreign history.
    if "verified_head" in receipt and receipt["verified_head"] == state["local_sha"]:
        receipt = {**receipt, "verified_head": commit}
    if row["published_revision"] == row["revision"]:
        receipt = {**receipt, "published": {**receipt.get("published", {}), row["doc_id"]: row["revision"]}}
    _update(sql, tenant, local_sha=commit, receipt=receipt)
    return commit


def _publish(sql, tenant, state):
    if state["local_sha"] == state["remote_sha"]:
        return
    try:
        result = _generation_post(tenant, "publish", {"expected_remote_sha": state["remote_sha"]}, state['local_sha'])
    except MirrorError as error:
        # A previous unknown publication may already have advanced the remote.
        # This later CAS response is independent evidence of its current SHA.
        if error.code != "lab_publish_conflict" or error.observed != state["local_sha"]:
            raise
        result = {"published": error.observed}
    if result.get("published") != state["local_sha"]:
        raise MirrorError("unknown")
    _update(sql, tenant, remote_sha=state["local_sha"])


def _notify(tenant, event, identity):
    key = json.dumps([tenant, event, identity], separators=(",", ":"))
    def reserve(sql):
        state = _state(sql, tenant)
        if key in state["notified"]:
            return False
        _update(sql, tenant, notified=state["notified"] + [key])
        return True
    if not ledger._run(tenant, reserve, write=True):
        return
    # Read back durable state before publishing either alert signal.
    if key not in mirror_status(tenant)["notified"]:
        return
    getattr(emf_metrics, "emit_" + event)()
    LOG.warning(event, extra={"tenant": tenant, "event": event, "mirror_key": key})


def enable_mirror(tenant, actor):
    if not _allowed(tenant):
        raise MirrorError("lab_mirror_ineligible")
    inspection = None
    def begin(sql):
        nonlocal inspection
        inspection = _inspect(tenant)
        state = _state(sql, tenant)
        receipt = state["receipt"]
        if receipt.get("status") == "in_progress":
            if inspection["head"] != receipt["last_commit"]:
                return "divergence"
            _update(sql, tenant, mode="replaying", frozen=True)
            return receipt
        if state["mode"] != "ledger":
            raise MirrorError("lab_mirror_mode_refused")
        count = sql.execute("SELECT COUNT(*) AS n FROM lab_revisions WHERE tenant_id=?", (tenant,)).fetchone()["n"]
        if count > MAX_REPLAY_REVISIONS:
            raise MirrorError("lab_replay_too_large")
        rows = _rows(sql, tenant)
        watermark = {row["doc_id"]: row["head"] for row in rows}
        receipt = {"schema": "leaf.lab-replay.v1", "tenant": tenant,
                   "watermark": watermark, "ordinal": 0,
                   "base_head": inspection["head"],
                   "last_commit": inspection["head"], "started_at": _now(),
                   "status": "in_progress"}
        commits = inspection["commits"]
        complete = ((inspection["head"] is None and not commits)
                    or (commits and commits[-1].get("parent") is None))
        prior = state["receipt"]
        proven = "verified_head" in prior and prior["verified_head"] == inspection["head"]
        if (complete or proven) and _known_revisions(sql, tenant, commits):
            receipt["verified_head"] = inspection["head"]
        _update(sql, tenant, mode="replaying", frozen=True,
                local_sha=inspection["head"], receipt=receipt)
        return receipt
    receipt = ledger._run(tenant, begin, write=True)
    try:
        if receipt == "divergence":
            raise MirrorError("lab_replay_divergence")
        while True:
            def step(sql):
                state = _state(sql, tenant)
                current = state["receipt"]
                rows = _rows(sql, tenant, current["watermark"])
                ordinal = current["ordinal"]
                if ordinal >= len(rows):
                    return False
                commit = _commit(sql, tenant, state, rows[ordinal])
                current = _state(sql, tenant)["receipt"]
                _update(sql, tenant, receipt={**current, "ordinal": ordinal + 1, "last_commit": commit})
                return True
            if not ledger._run(tenant, step, write=True):
                break
        def finish(sql):
            state = _state(sql, tenant)
            _publish(sql, tenant, state)
            inspected = _inspect(tenant)
            if inspected["head"] != state["local_sha"] or not _equal(sql, tenant, inspected):
                raise MirrorError("lab_replay_divergence")
            _update(sql, tenant, mode="mirror", frozen=False,
                    receipt={**state["receipt"], "status": "complete"})
        ledger._run(tenant, finish, write=True)
    except Exception as error:
        divergent = isinstance(error, MirrorError) and error.code in {
            "lab_replay_divergence", "lab_ref_conflict", "lab_publish_conflict"}
        def fail(sql):
            state = _state(sql, tenant)
            values = {"mode": "ledger", "frozen": False}
            if divergent:
                values["receipt"] = {**state["receipt"], "status": "divergent"}
            _update(sql, tenant, **values)
        ledger._run(tenant, fail, write=True)
        if divergent:
            _notify(tenant, "lab_replay_divergence", inspection["head"])
        raise
    return mirror_status(tenant)


def catch_up(tenant):
    identity = None
    try:
        while True:
            def step(sql):
                state = _state(sql, tenant)
                if state["mode"] != "mirror" or not _allowed(tenant):
                    return None
                for row in _rows(sql, tenant):
                    mirrored = sql.execute("SELECT revision FROM lab_git_mirror"
                        " WHERE tenant_id=? AND doc_id=? AND revision=?",
                        (tenant, row["doc_id"], row["revision"])).fetchone()
                    # A publication is metadata on an existing revision. Commit
                    # it before newer pending revisions so it cannot overwrite
                    # their document head with the older published content.
                    unpublished_pointer = (row["published_revision"] == row["revision"]
                        and state["receipt"].get("published", {}).get(row["doc_id"]) != row["revision"])
                    # An older publish pointer may have been mirrored after
                    # this head was already committed by another worker. Restore
                    # the head bytes before publishing the resulting git tree.
                    stale_head = (row["revision"] == row["head"]
                        and state["receipt"].get("heads", {}).get(row["doc_id"]) != row["revision"])
                    if mirrored is None or unpublished_pointer or stale_head:
                        _commit(sql, tenant, state, row)
                        return True
                return False
            pending = ledger._run(tenant, step, write=True)
            if pending is None:
                return
            if not pending:
                break
        def finish(sql):
            state = _state(sql, tenant)
            _publish(sql, tenant, state)
            _update(sql, tenant, receipt={**state["receipt"], "lagging": False})
        ledger._run(tenant, finish, write=True)
    except Exception:
        def lag(sql):
            state = _state(sql, tenant)
            _update(sql, tenant, receipt={**state["receipt"], "lagging": True})
            return state["local_sha"]
        identity = ledger._run(tenant, lag, write=True)
        _notify(tenant, "lab_commit_unknown", identity)


def after_write(tenant):
    # Keep this guard cheap and do not consult the harness for ledger tenants.
    try:
        import lab_git_authority as authority

        if authority.authority_get(tenant) is not None:
            return
        if _allowed(tenant) and mirror_status(tenant)["mode"] == "mirror":
            catch_up(tenant)
    except Exception:
        # The ledger transaction has committed. Even an alert/storage failure
        # must not change the result of the original ledger operation.
        pass


def flip_back(tenant, actor):
    def operation(sql):
        state = _state(sql, tenant)
        inspected = _inspect(tenant)
        if not _equal(sql, tenant, inspected):
            return inspected["head"]
        commits = inspected["commits"]
        receipt = state["receipt"]
        proven = "verified_head" in receipt and receipt["verified_head"] == inspected["head"]
        # Unseen ancestry is acceptable only when it was proved before this
        # window and extended exclusively by our CAS commits.
        if commits and commits[-1].get("parent") is not None and not proven:
            return inspected["head"]
        if not proven:
            if "base_head" not in receipt:
                return inspected["head"]
            for commit in commits:
                if commit["commit"] == receipt["base_head"]:
                    break
                row = sql.execute('SELECT doc_id,revision FROM lab_git_mirror'
                    ' WHERE tenant_id=? AND "commit"=?',
                    (tenant, commit["commit"])).fetchone()
                pairs = re.findall(r"^(Lab-[A-Za-z0-9-]+): (.*)$", commit["message"], re.MULTILINE)
                if row is None or any(sum(key == name for key, _ in pairs) != 1
                                      for name in ("Lab-Doc", "Lab-Revision")):
                    return inspected["head"]
                trailers = dict(pairs)
                try:
                    matches = (trailers["Lab-Doc"] == row["doc_id"]
                               and int(trailers["Lab-Revision"]) == row["revision"])
                except ValueError:
                    return inspected["head"]
                if not matches:
                    return inspected["head"]
        if not _known_revisions(sql, tenant, commits):
            return inspected["head"]
        _update(sql, tenant, mode="ledger", frozen=False)
        return False
    try:
        refused = ledger._run(tenant, operation, write=True)
    except Exception:
        # A failed inspection cannot prove that flip-back is safe.
        refused = mirror_status(tenant)["local_sha"]
    if refused is not False:
        _notify(tenant, "lab_replay_divergence", refused)
        raise MirrorError("lab_flip_back_refused")
    return mirror_status(tenant)
