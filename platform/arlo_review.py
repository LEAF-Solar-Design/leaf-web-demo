"""Project-scoped proposal decisions in the existing immutable history ledger.

A review selects a solver proposal. It never applies CAD or grants engineering
acceptance, and never edits the terminal solve result.
"""
from __future__ import annotations

from typing import Literal
import uuid

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from . import arlo_lab, project_lifecycle as lifecycle
from .db import run_transaction
from .models import canonical_hash
from .store import _insert_outbox

OPERATION = 'arlo.proposal.reviewed'


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    proposal_id: str = Field(min_length=1, max_length=160)
    result_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    decision: Literal['accept', 'reject']
    note: str = Field(default='', max_length=1000)


def _require(condition, message):
    if not condition:
        raise lifecycle.LifecycleConflict(message)


def _job(cur, org, project, job):
    cur.execute("SELECT * FROM jobs WHERE org_id = %(org)s AND project_id = %(project)s "
                "AND job_id = %(job)s AND tool_name = 'arlo-design' AND deleted_at IS NULL FOR UPDATE",
                {'org':org, 'project':project, 'job':job})
    row = cur.fetchone()
    if row is None:
        raise lifecycle.LifecycleUnavailable('ARLO job not found')
    _require(row['status'] == 'succeeded', 'Only completed ARLO jobs can be reviewed')
    cur.execute("SELECT version_id FROM drawing_versions WHERE org_id = %(org)s AND project_id = %(project)s "
                "AND version_id = %(version)s AND deleted_at IS NULL",
                {'org':org, 'project':project, 'version':row['input_version_id']})
    _require(cur.fetchone() is not None, 'ARLO input version is unavailable')
    envelope = row.get('result') or {}
    _require(isinstance(envelope,dict), 'Stored ARLO result is unavailable')
    result = envelope.get('solver_result') or {}
    body = envelope.get('solver_input') or {}
    _require(isinstance(result,dict) and isinstance(body,dict), 'Stored ARLO input or result is unavailable')
    _require(envelope.get('solver') == 'arlo-design' and result.get('contract') == 'arlo_design_result_v1'
             and result.get('status') == 'complete' and result.get('production_valid') is False,
             'Stored ARLO result is not a complete proposal result')
    for field, value in (('organization_id',org), ('project_id',project), ('input_version_id',row['input_version_id'])):
        _require(body.get(field) == str(value), 'Stored ARLO input identity differs from its job')
    _require(arlo_lab.digest(result) == envelope.get('result_sha256')
             and arlo_lab.digest(body) == envelope.get('input_sha256')
             and arlo_lab.digest(row['params']) == envelope.get('request_sha256'), 'Stored ARLO result bytes changed')
    proposals = result.get('proposals', [])
    _require(isinstance(proposals,list) and proposals and all(isinstance(p,dict) for p in proposals), 'ARLO proposals are unavailable')
    ids = [p.get('proposal_id') for p in proposals]
    _require(all(isinstance(i,str) and i for i in ids) and len(ids) == len(set(ids)), 'ARLO proposal identities are ambiguous')
    for proposal in proposals:
        source = proposal.get('source') or {}
        _require(all(source.get(key) == body[key] for key in ('organization_id','project_id','input_version_id'))
                 and source.get('request_hash') == result.get('request_hash')
                 and proposal.get('input_version_id') == str(row['input_version_id'])
                 and proposal.get('production_valid') is False and proposal.get('violations') == [],
                 'ARLO proposal does not bind a valid complete job result')
    return row, envelope, proposals


def _view(row):
    expected = canonical_hash('history-operation', {'operationType':OPERATION,'payload':row['payload']})
    _require(row['hash_value'] == expected.value, 'Stored ARLO review digest differs')
    return {'operation_id':str(row['operation_id']), 'payload':row['payload'],
            'hash_value':row['hash_value'], 'created_at':str(row['created_at'])}


def reviews(org_id, project_id, binding_id, job_id):
    def operation(conn):
        with conn.cursor() as cur:
            arlo_lab._scope(cur,org_id,project_id,binding_id,write=False)
            _, envelope, _ = _job(cur,org_id,project_id,job_id)
            cur.execute("SELECT * FROM history_operations WHERE org_id = %(org)s AND project_id = %(project)s "
                        "AND operation_type = %(type)s AND payload->>'jobId' = %(job)s ORDER BY created_at, operation_id",
                        {'org':org_id,'project':project_id,'type':OPERATION,'job':str(job_id)})
            return {'job_id':str(job_id),'result_sha256':envelope['result_sha256'],
                    'reviews':[_view(row) for row in cur.fetchall()]}
    return run_transaction(operation,isolation='serializable')


def review(org_id, project_id, binding_id, job_id, decision: ReviewDecision, idempotency_key: str):
    key = 'arlo-review:'+lifecycle._validate_idempotency_key(idempotency_key)
    def operation(conn):
        with conn.cursor() as cur:
            arlo_lab._scope(cur,org_id,project_id,binding_id,write=True)
            row, envelope, proposals = _job(cur,org_id,project_id,job_id)
            _require(envelope['result_sha256'] == decision.result_sha256, 'ARLO result changed since review opened')
            _require(any(p['proposal_id'] == decision.proposal_id for p in proposals), 'Proposal does not belong to this ARLO job')
            parent_id = envelope.get('history_operation_id')
            try:
                parent_id = uuid.UUID(str(parent_id))
            except ValueError:
                raise lifecycle.LifecycleConflict('Canonical solve history is unavailable') from None
            cur.execute("SELECT * FROM history_operations WHERE org_id = %(org)s AND project_id = %(project)s "
                        "AND operation_id = %(parent)s AND operation_type = 'solve.completed'",
                        {'org':org_id,'project':project_id,'parent':parent_id})
            parent = cur.fetchone()
            _require(parent is not None and parent['payload'].get('jobId') == str(job_id)
                     and parent['payload'].get('resultHash') == decision.result_sha256, 'Canonical solve history does not bind this result')
            payload = {'contract':'arlo_proposal_review_v1','jobId':str(job_id),'inputVersionId':str(row['input_version_id']),
                       'proposalId':decision.proposal_id,'resultHash':decision.result_sha256,'decision':decision.decision,
                       'note':decision.note,'actorBindingId':str(binding_id),'nativeApplied':False,'engineeringAccepted':False}
            hashed = canonical_hash('history-operation',{'operationType':OPERATION,'payload':payload})
            cur.execute("INSERT INTO history_operations (operation_id,org_id,project_id,operation_type,payload,idempotency_key,"
                        "hash_algorithm,hash_canonicalization,hash_domain,hash_value) VALUES "
                        "(%(id)s,%(org)s,%(project)s,%(type)s,%(payload)s,%(key)s,%(algorithm)s,%(canonicalization)s,%(domain)s,%(value)s) "
                        "ON CONFLICT (org_id,project_id,idempotency_key) DO NOTHING RETURNING *",
                        {'id':uuid.uuid4(),'org':org_id,'project':project_id,'type':OPERATION,'payload':Jsonb(payload),'key':key,**hashed.to_dict()})
            saved = cur.fetchone()
            if saved is None:
                cur.execute("SELECT * FROM history_operations WHERE org_id = %(org)s AND project_id = %(project)s AND idempotency_key = %(key)s",
                            {'org':org_id,'project':project_id,'key':key})
                saved = cur.fetchone()
                _require(saved is not None and saved['operation_type'] == OPERATION and saved['payload'] == payload
                         and saved['hash_value'] == hashed.value, 'Review idempotency key has different content')
                return {'review':_view(saved)}
            cur.execute("INSERT INTO history_edges (edge_id,org_id,project_id,parent_operation_id,child_operation_id) "
                        "VALUES (%(id)s,%(org)s,%(project)s,%(parent)s,%(child)s)",
                        {'id':uuid.uuid4(),'org':org_id,'project':project_id,'parent':parent_id,'child':saved['operation_id']})
            _insert_outbox(cur,org_id,project_id,'history_operation',saved['operation_id'],'history.operation.appended',
                           {'operationId':str(saved['operation_id']),'jobId':str(job_id)})
            return {'review':_view(saved)}
    return run_transaction(operation,isolation='serializable')
