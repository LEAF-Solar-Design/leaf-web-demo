"""Versioned chart preferences on the existing canonical project history graph."""
from __future__ import annotations
from copy import deepcopy
import re
from uuid import UUID, uuid4
from psycopg.types.json import Jsonb
from . import db, store, project_lifecycle as lifecycle, project_web_page
from .models import canonical_hash, canonical_json_bytes, verify_canonical_hash

OP = 'browser.chart.state'
DESCRIPTOR = {'source_request_id','version','receipt_digest','source_commit','source_tree',
              'job_id','artifact_id','manifest_sha256'}


def _uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError('Invalid chart identity.')
    return value


def descriptor_copy(value):
    if type(value) is not dict or set(value) != DESCRIPTOR:
        raise ValueError('Invalid chart identity.')
    result = deepcopy(value)
    for key in ('source_request_id','job_id','artifact_id'):
        _uuid(result[key])
    for key, length in [('receipt_digest',64),('source_commit',40),('source_tree',40),('manifest_sha256',64)]:
        if not isinstance(result[key], str) or re.fullmatch('[0-9a-f]{%s}' % length, result[key]) is None:
            raise ValueError('Invalid chart identity.')
    if type(result['version']) is not int or not 1 <= result['version'] <= 2147483647:
        raise ValueError('Invalid chart identity.')
    return result


def state_copy(value):
    keys = {'version','range','scale','reference','averaging','timeAxis','series','groups'}
    if (type(value) is not dict or set(value) != keys or type(value['version']) is not int or value['version'] != 1
            or value['range'] not in ('all','2026') or value['scale'] not in ('Log','Linear')
            or value['reference'] not in ('2023-01-01','2026-02-01')
            or value['averaging'] not in (28,56,84,'Recorded') or value['timeAxis'] not in ('expanded2026','even')):
        raise ValueError('Invalid chart state.')
    result = deepcopy(value)
    for key in ('series','groups'):
        items = result[key]
        if (type(items) is not list or len(items) > 256 or
                any(not isinstance(v,str) or not v or len(v.encode('utf-16-le')) // 2 > 200 for v in items)
                or len(set(items)) != len(items)):
            raise ValueError('Invalid chart selection.')
    if len(canonical_json_bytes(result)) > 16384:
        raise ValueError('Chart state is too large.')
    return result


def _branch(descriptor):
    return 'browser-chart-state/' + descriptor['source_request_id'] + '/' + descriptor['job_id']


def _head(cur, org, project, branch):
    cur.execute('SELECT h.*,b.sequence AS revision FROM branch_refs b JOIN history_operations h '
                'ON h.org_id=b.org_id AND h.project_id=b.project_id AND h.operation_id=b.operation_id '
                'WHERE b.org_id=%s AND b.project_id=%s AND b.ref_name=%s ORDER BY b.sequence DESC LIMIT 1',
                (org, project, branch))
    return cur.fetchone()


def _projection(row, descriptor):
    if row is None:
        return None
    payload = row['payload']
    metadata = {k:row['hash_'+k] for k in ('algorithm','canonicalization','domain','value')}
    if (type(row['revision']) is not int or row['revision']<1 or row['operation_type'] != OP or not verify_canonical_hash('history-operation',
            {'operationType':OP,'payload':payload}, metadata)
            or payload.get('descriptor') != descriptor):
        raise lifecycle.LifecycleConflict('Saved chart identity could not be verified.')
    state = state_copy(payload['state'])
    return {'revision':row['revision'],'descriptor':descriptor,'state':state,
            'digest':canonical_hash('browser-chart-state',{'descriptor':descriptor,'state':state}).value}


def read(org, project, actor, descriptor):
    descriptor = descriptor_copy(descriptor)
    store._require_postgres_authority(org, project)
    def operation(conn):
        with conn.cursor() as cur:
            project_web_page._authorize(cur, org, project, actor)
            return _projection(_head(cur,org,project,_branch(descriptor)),descriptor)
    return db.run_transaction(operation)


def save(org, project, actor, descriptor, state, *, expected_revision, save_id):
    descriptor, state = descriptor_copy(descriptor), state_copy(state)
    _uuid(save_id)
    if type(expected_revision) is not int or not 0 <= expected_revision < 2147483647:
        raise ValueError('Invalid chart revision.')
    payload = {'descriptor':descriptor,'state':state,'expected_revision':expected_revision}
    digest = canonical_hash('history-operation',{'operationType':OP,'payload':payload})
    key, branch = 'browser-chart-state:'+save_id, _branch(descriptor)
    store._require_postgres_authority(org, project)
    def operation(conn):
        with conn.cursor() as cur:
            # Same existing live-project lock and role checks as page saves.
            project_web_page._authorize(cur,org,project,actor,write=True)
            cur.execute('SELECT h.*,b.sequence AS revision FROM history_operations h LEFT JOIN branch_refs b '
                        'ON h.org_id=b.org_id AND h.project_id=b.project_id AND h.operation_id=b.operation_id AND b.ref_name=%s '
                        'WHERE h.org_id=%s AND h.project_id=%s AND h.idempotency_key=%s',
                        (branch,org,project,key))
            prior = cur.fetchone()
            if prior is not None:
                if prior['payload'] != payload:
                    raise lifecycle.LifecycleConflict('Chart save identity was already used.')
                return _projection(prior,descriptor)
            head = _head(cur,org,project,branch)
            current = _projection(head,descriptor)
            revision = current['revision'] if current else 0
            if revision != expected_revision:
                raise lifecycle.LifecycleConflict('Chart changed elsewhere. Reload before saving.')
            operation_id = uuid4()
            cur.execute('INSERT INTO history_operations (operation_id,org_id,project_id,operation_type,payload,idempotency_key,'
                        'hash_algorithm,hash_canonicalization,hash_domain,hash_value) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                        (operation_id,org,project,OP,Jsonb(payload),key,digest.algorithm,digest.canonicalization,digest.domain,digest.value))
            if head:
                cur.execute('INSERT INTO history_edges (edge_id,org_id,project_id,parent_operation_id,child_operation_id) '
                            'VALUES (%s,%s,%s,%s,%s)',(uuid4(),org,project,head['operation_id'],operation_id))
            cur.execute('INSERT INTO branch_refs (ref_id,org_id,project_id,ref_name,sequence,operation_id) VALUES (%s,%s,%s,%s,%s,%s)',
                        (uuid4(),org,project,branch,revision+1,operation_id))
            store._insert_outbox(cur,org,project,'history_operation',operation_id,'history.operation.appended',{'operationId':str(operation_id)})
            return _projection(_head(cur,org,project,branch),descriptor)
    return db.run_transaction(operation,isolation='serializable')
