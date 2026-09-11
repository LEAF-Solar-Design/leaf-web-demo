"""Source-pinned operator authoring. Run with python -I; never submits a job.

JSON manifest fields are closed. Operator attribution is separate from the
persisted project principal. Funding must be published by the trusted operator.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import uuid

LIMIT = 131072

class Refused(ValueError):
    pass


def require(ok):
    if not ok:
        raise Refused('authoring input or persisted binding conflict')


def closed(value, fields):
    require(isinstance(value, dict) and set(value) == set(fields.split()))
    return value


def text(value, maximum=1024):
    require(isinstance(value, str) and 0 < len(value) <= maximum
            and value == value.strip() and '\x00' not in value)
    return value


def identifier(value):
    text(value, 36)
    require(str(uuid.UUID(value)) == value)
    return value


def digest(value):
    return hashlib.sha256(raw(value)).hexdigest()


def raw(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def pairs(items):
    result = {}
    for key, value in items:
        require(key not in result)
        result[key] = value
    return result


def decode(data):
    require(isinstance(data, bytes) and len(data) <= LIMIT)
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(Refused('invalid JSON')))


def load(path):
    with Path(path).open('rb') as stream:
        return decode(stream.read(LIMIT + 1))


def reference(value):
    closed(value, 'bucket key version_id sha256')
    for item in value.values():
        text(item)
    require(value['version_id'].lower() not in ('latest', 'null'))
    require(re.fullmatch('[0-9a-f]{64}', value['sha256']) is not None)
    require(not value['key'].startswith('/') and all(
        p not in ('', '.', '..') for p in value['key'].split('/')))
    return value


def manifest(value):
    closed(value, 'version org_id project_id tenant_id principal_binding_id operator_arn authority_ref campaign machine_id task')
    require(type(value['version']) is int and value['version'] == 1)
    for key in ('org_id', 'project_id', 'tenant_id', 'principal_binding_id'):
        identifier(value[key])
    require(re.fullmatch(r'arn:aws:(?:iam|sts)::[0-9]{12}:(?:role|assumed-role)/[A-Za-z0-9+=,.@_/-]+', text(value['operator_arn'])) is not None)
    reference(value['authority_ref'])
    text(value['machine_id'], 200)
    c = closed(value['campaign'], 'title prompt idempotency_key')
    for key, maximum in [('title', 200), ('prompt', 32768), ('idempotency_key', 128)]:
        text(c[key], maximum)
    t = closed(value['task'], 'task_key title spec source_sha owned_paths verify_command declared_artifacts idempotency_key')
    for key, maximum in [('task_key', 128), ('title', 200), ('spec', 16384), ('verify_command', 4096), ('idempotency_key', 128)]:
        text(t[key], maximum)
    require(re.fullmatch('[A-Za-z0-9._-]+', t['task_key']) is not None)
    require(isinstance(t['source_sha'], str) and re.fullmatch('[0-9a-f]{40}', t['source_sha']) is not None)
    for key, maximum in [('owned_paths', 64), ('declared_artifacts', 32)]:
        require(isinstance(t[key], list) and len(t[key]) <= maximum)
        for item in t[key]:
            text(item)
        require(len(set(t[key])) == len(t[key]))
    require(len(raw(value)) <= LIMIT)
    return value


def prepared(value):
    closed(value, 'campaign_id enrollment_id task_id prepared_input_digest')
    for key in ('campaign_id', 'enrollment_id', 'task_id'):
        identifier(value[key])
    require(isinstance(value['prepared_input_digest'], str) and re.fullmatch('[0-9a-f]{64}', value['prepared_input_digest']) is not None)
    return value


def job_for(installation, m):
    # Production validates the entire installation using its actual adapter.
    require(installation.get('enabled') is True)
    jobs = [j for j in installation['jobs'] if
            (j['org_id'], j['project_id']) == (m['org_id'], m['project_id'])]
    require(len(jobs) == 1)
    job = jobs[0]
    require(job['commit_sha'] == m['task']['source_sha'])
    require(type(job['reservation_microusd']) is int and job['reservation_microusd'] > 0)
    return job


def input_digest(m, installation):
    return digest({'manifest': m, 'installation': installation})


def task_fields(m):
    return dict(m['task'], capability='terraform.native', stages=['implementation'],
                kind='task', depends_on=[])


def run(stage, m, installation, backend, *, identities=None, funding_ref=None, reader=None):
    m = manifest(m)
    job = job_for(installation, m)
    fingerprint = input_digest(m, installation)
    if stage == 'activate':
        identities = prepared(identities)
        require(identities['prepared_input_digest'] == fingerprint)
        ref = reference(funding_ref)
        require(ref['bucket'] == job['acceptance_bucket'] and ref['key'].startswith(job['acceptance_prefix']))
        data = reader(ref)
        require(isinstance(data, bytes) and len(data) <= LIMIT and hashlib.sha256(data).hexdigest() == ref['sha256'])
        grant = decode(data)
        expected = dict(version=1, org_id=m['org_id'], project_id=m['project_id'],
            tenant_id=m['tenant_id'], principal_binding_id=m['principal_binding_id'],
            operator_arn=m['operator_arn'], authority_ref=m['authority_ref'],
            **identities, source_sha=job['commit_sha'], job_name=job['job_name'],
            repository_id=job['repository_id'], environment=job['environment'],
            limit_microusd=job['reservation_microusd'], max_active=1)
        closed(grant, ' '.join([*expected, 'allocation_id']))
        text(grant['allocation_id'], 128)
        require(type(grant['version']) is int and type(grant['limit_microusd']) is int
                and type(grant['max_active']) is int)
        require(all(grant[k] == v for k, v in expected.items()))
    else:
        require(stage in ('inspect', 'prepare') and identities is None and funding_ref is None)
    # This guard remains open across the canonical methods' own transactions.
    with backend.guard(m, read_only=stage == 'inspect'):
        current = backend.inspect(m)
        if stage == 'inspect':
            return current
        if stage == 'prepare':
            campaign = backend.campaign(m)
            enrollment = backend.enrollment(m, campaign)
            task = backend.task(m, campaign)
            return dict(campaign_id=campaign, enrollment_id=enrollment, task_id=task,
                        prepared_input_digest=fingerprint)
        require(all(current.get(k) == identities[k] for k in ('campaign_id', 'enrollment_id', 'task_id')))
        backend.allocate(m, identities['campaign_id'], grant['allocation_id'],
                         job['reservation_microusd'], ref)
        backend.enable(m, identities['campaign_id'], identities['enrollment_id'])
        return dict(identities, allocation_id=grant['allocation_id'], state='enabled',
                    operator_arn=m['operator_arn'], principal_binding_id=m['principal_binding_id'])


class Stores:
    def __init__(self, package):
        self.db = importlib.import_module(package + '.db')
        self.c = importlib.import_module(package + '.campaigns')
        self.e = importlib.import_module(package + '.campaign_enrollment')
        self.t = importlib.import_module(package + '.campaign_execution')
        self.a = importlib.import_module(package + '.campaign_developer_execution')

    @contextmanager
    def guard(self, m, read_only=False):
        with self.db.connection() as conn:
            with conn.cursor() as cur:
                if read_only:
                    cur.execute('SET TRANSACTION READ ONLY')
                cur.execute("SET LOCAL statement_timeout='15s'")
                cur.execute("SET LOCAL lock_timeout='5s'")
                cur.execute('SELECT m.binding_id FROM project_member_bindings m '
                    'JOIN identity_bindings b ON b.platform_tenant_id=m.org_id AND b.binding_id=m.binding_id '
                    'JOIN projects p ON p.org_id=m.org_id AND p.project_id=m.project_id '
                    "WHERE m.org_id=%s AND m.project_id=%s AND m.binding_id=%s "
                    "AND m.status='active' AND b.status='active' AND m.role IN ('owner','editor') "
                    "AND p.deleted_at IS NULL AND p.status='active' "
                    + ('' if read_only else 'FOR SHARE OF m,b,p'),
                    (m['org_id'], m['project_id'], m['principal_binding_id']))
                require(cur.fetchone() is not None)
                require(m['tenant_id'] == m['org_id'])
                require(m['machine_id'] in self.e.allowed_machines())
                self.cur = cur
                try:
                    yield
                finally:
                    self.cur = None

    def inspect(self, m):
        cur = self.cur
        scope = (m['org_id'], m['project_id'])
        cur.execute('SELECT * FROM campaigns WHERE org_id=%s AND project_id=%s AND idempotency_key=%s',
                    (*scope, m['campaign']['idempotency_key']))
        c = cur.fetchone()
        result = {'principal_binding_id': m['principal_binding_id']}
        if c is None:
            return result
        require(all(str(c[k]) == v for k, v in dict(tenant_id=m['tenant_id'], principal_id=m['principal_binding_id'], **m['campaign']).items()))
        require(c['status'] in ('accepted', 'running'))
        cid = str(c['campaign_id'])
        result['campaign_id'] = cid
        cur.execute("SELECT * FROM campaign_host_enrollments WHERE org_id=%s AND project_id=%s AND campaign_id=%s AND machine_id=%s AND capability='campaign.host-enrollment'", (*scope, cid, m['machine_id']))
        e = cur.fetchone()
        if e:
            import os
            require(e['service_subject'] == os.environ.get('LEAF_CAMPAIGN_WORKER_SUBJECT') and e['state'] != 'revoked')
            result['enrollment_id'] = str(e['enrollment_id'])
        cur.execute('SELECT * FROM campaign_tasks WHERE org_id=%s AND project_id=%s AND campaign_id=%s AND task_key=%s', (*scope, cid, m['task']['task_key']))
        t = cur.fetchone()
        if t:
            require(all(t[k] == v for k, v in task_fields(m).items() if k != 'depends_on'))
            require(t['parent_task_id'] is None)
            cur.execute('SELECT 1 FROM campaign_task_dependencies WHERE org_id=%s AND project_id=%s AND campaign_id=%s AND task_id=%s', (*scope, cid, t['task_id']))
            require(cur.fetchone() is None)
            result['task_id'] = str(t['task_id'])
        cur.execute('SELECT allocation_id FROM campaign_developer_allocations WHERE org_id=%s AND project_id=%s AND campaign_id=%s', (*scope, cid))
        a = cur.fetchone()
        if a:
            result['allocation_id'] = a['allocation_id']
        return result

    def campaign(self, m):
        row = self.c.submit_campaign(m['org_id'], m['project_id'], m['tenant_id'], m['principal_binding_id'], **m['campaign'])
        return str(row['campaign_id'])

    def enrollment(self, m, cid):
        row = self.e.request_enrollment(m['org_id'], m['project_id'], cid, m['principal_binding_id'], machine_id=m['machine_id'], capability='campaign.host-enrollment')
        return str(row['enrollment_id'])

    def task(self, m, cid):
        return str(self.t.submit_task(m['org_id'], m['project_id'], cid, **task_fields(m))['task_id'])

    def allocate(self, m, cid, aid, amount, ref):
        self.a.record_allocation(m['org_id'], m['project_id'], cid, allocation_id=aid,
                                limit_microusd=amount, max_active=1, evidence_ref=ref)

    def enable(self, m, cid, eid):
        self.e.enable_enrollment(m['org_id'], m['project_id'], cid, eid, m['principal_binding_id'])


def runtime():
    require(sys.flags.isolated == 1)
    import platform  # Resolve stdlib before adding the server import directory.
    require(hasattr(platform, 'python_implementation'))
    root = Path(__file__).resolve().parents[1]
    name = 'native_author_platform'
    spec = importlib.util.spec_from_file_location(name, root/'platform/__init__.py', submodule_search_locations=[str(root/'platform')])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    sys.path.insert(0, str(root/'server'))
    from campaign_native_developer_bridge import _validate_installation
    return Stores(name), _validate_installation


def object_reader(ref):
    import boto3
    from botocore.config import Config
    client = boto3.client('s3', config=Config(connect_timeout=5, read_timeout=10, retries={'total_max_attempts': 1}))
    response = client.get_object(Bucket=ref['bucket'], Key=ref['key'], VersionId=ref['version_id'])
    stream = response['Body']
    try:
        require(response.get('VersionId') == ref['version_id'])
        return stream.read(LIMIT + 1)
    finally:
        stream.close()


def main(argv=None, *, dependencies=runtime, reader=object_reader):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['inspect', 'prepare', 'activate'])
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--installation', required=True)
    parser.add_argument('--prepared')
    parser.add_argument('--funding-ref')
    args = parser.parse_args(argv)
    try:
        require((args.stage == 'activate') == bool(args.prepared and args.funding_ref))
        require(args.stage == 'activate' or not (args.prepared or args.funding_ref))
        m = manifest(load(args.manifest))
        installation = load(args.installation)
        backend, validate = dependencies()
        installation = validate(installation)
        result = run(args.stage, m, installation, backend,
            identities=load(args.prepared) if args.prepared else None,
            funding_ref=load(args.funding_ref) if args.funding_ref else None, reader=reader)
        print(json.dumps({'ok': True, 'result': result}, sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({'ok': False, 'error': 'authoring_refused_or_outcome_unknown',
                          'recovery': 'inspect and resume the same manifest and identities'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
