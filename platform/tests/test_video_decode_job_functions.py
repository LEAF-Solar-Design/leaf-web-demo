"""Real PostgreSQL proof for the decoder's function-only authority."""
import copy
import os
from pathlib import Path
import secrets
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import pytest

from leaf_platform import db, store


pytestmark = pytest.mark.skipif(
    not (os.environ.get('DATABASE_URL') or (Path(__file__).parents[1] / '.env.local').exists()),
    reason='PostgreSQL integration test requires DATABASE_URL')
SIGNATURES = ('video_decode_claim(text,integer)', 'video_decode_heartbeat(uuid,text,integer)',
              'video_decode_complete(uuid,text,integer,jsonb)', 'video_decode_fail(uuid,text,integer,jsonb)')


class Jobs:
    def __init__(self, conn, org, project):
        self.conn, self.org, self.project = conn, org, project
        self.context = dict(schema='leaf.video-decode-job.v1', request_sha256='a' * 64,
                            identity=dict(tenant_id='decoder-tenant', organization_id='decoder-tenant',
                                          project_id=str(project.project_id), actor_binding_id='actor', edit_id='edit'),
                            material=dict(material_id='material', sha256='b' * 64, byte_size=10,
                                          media_type='video/quicktime'), transcripts=[],
                            input_key='in/' + 'a' * 64 + '/1.mov', result_key='out/' + 'a' * 64 + '/1.json')
        self.job_id = self.insert()

    def execute(self, statement, params=()):
        return self.conn.execute(statement, params)

    def insert(self, *, tool='video-decode', context=None, status='queued', attempt=0, max_attempts=1):
        job_id = uuid.uuid4()
        self.execute('INSERT INTO jobs (job_id, org_id, project_id, kind, tool_name, status, '
                     'request_tenant_id, execution_context, attempt, max_attempts) '
                     'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                     (job_id, self.org.org_id, self.project.project_id, 'run', tool, status,
                      'decoder-tenant', Jsonb(self.context if context is None else context), attempt, max_attempts))
        return job_id

    def claim(self, worker='decoder-test', lease=60):
        return self.execute('SELECT * FROM video_decode_claim(%s,%s)', (worker, lease)).fetchone()

    def call(self, name, payload=None, *, job_id=None, worker='decoder-test', attempt=1):
        assert name in {'complete', 'fail'}
        return self.execute(f'SELECT video_decode_{name}(%s,%s,%s,%s) AS outcome',
                            (self.job_id if job_id is None else job_id, worker, attempt, Jsonb(payload))).fetchone()['outcome']

    def result(self):
        return dict(request_sha256=self.context['request_sha256'], identity=copy.deepcopy(self.context['identity']),
                    manifest_sha256='c' * 64, video_sha256='b' * 64, result_key=self.context['result_key'])


@pytest.fixture
def jobs(make_org):
    org = make_org('Decoder functions')
    project = store.create_project(org.org_id, 'Decoder functions')
    store.set_project_authority_mode(org.org_id, project.project_id, 'postgres_canonical')
    with db.connection() as conn:
        # Decoder rows and temporary roles never survive the test, including failed tests.
        with conn.transaction(force_rollback=True):
            yield Jobs(conn, org, project)


def test_positive_claim_heartbeat_complete_and_replay(jobs):
    claimed = jobs.claim()
    assert claimed['job_id'] == jobs.job_id and claimed['attempt'] == 1
    assert claimed['execution_context'] == jobs.context
    assert jobs.execute('SELECT video_decode_heartbeat(%s,%s,%s) AS ok',
                        (jobs.job_id, 'decoder-test', 60)).fetchone()['ok'] is True
    result = jobs.result()
    assert jobs.call('complete', result) == 'applied'
    assert jobs.call('complete', result) == 'duplicate'
    changed = {**result, 'manifest_sha256': 'd' * 64}
    assert jobs.call('complete', changed) == 'conflict'
    row = jobs.execute('SELECT * FROM jobs WHERE job_id=%s', (jobs.job_id,)).fetchone()
    assert row['result'] == result and row['terminal_conflict'] == changed
    assert row['lease_owner'] is None and row['lease_expires_at'] is None
    assert row['finished_at'] is not None and row['status'] == 'succeeded'
    assert jobs.claim() is None


def test_hard_tool_and_schema_filters(jobs):
    jobs.execute('DELETE FROM jobs WHERE job_id=%s', (jobs.job_id,))
    wrong_tool = jobs.insert(tool='string-autofill-opt')
    wrong_schema = jobs.insert(context={**jobs.context, 'schema': 'unrelated'})
    assert jobs.claim() is None
    for job_id in (wrong_tool, wrong_schema):
        assert jobs.call('complete', jobs.result(), job_id=job_id) == 'missing'
        assert jobs.call('fail', {'code': 'video_changed'}, job_id=job_id) == 'missing'
        assert jobs.execute('SELECT video_decode_heartbeat(%s,%s,60) AS ok',
                            (job_id, 'decoder-test')).fetchone()['ok'] is False
        assert jobs.execute('SELECT status FROM jobs WHERE job_id=%s', (job_id,)).fetchone()['status'] == 'queued'


def test_expired_running_job_is_never_reclaimed(jobs):
    assert jobs.claim()['job_id'] == jobs.job_id
    jobs.execute("UPDATE jobs SET lease_expires_at=now()-interval '1 second' WHERE job_id=%s", (jobs.job_id,))
    assert jobs.claim('another-decoder') is None
    assert jobs.execute('SELECT status,attempt FROM jobs WHERE job_id=%s', (jobs.job_id,)).fetchone() == {
        'status': 'running', 'attempt': 1}


@pytest.mark.parametrize('condition', ['deleted', 'tenant', 'attempt', 'max_attempts'])
def test_claim_requires_fresh_single_attempt_live_row(jobs, condition):
    statements = {
        'deleted': 'UPDATE jobs SET deleted_at=now() WHERE job_id=%s',
        'tenant': 'UPDATE jobs SET request_tenant_id=NULL WHERE job_id=%s',
        'attempt': 'UPDATE jobs SET attempt=1 WHERE job_id=%s',
        'max_attempts': 'UPDATE jobs SET max_attempts=2 WHERE job_id=%s',
    }
    jobs.execute(statements[condition], (jobs.job_id,))
    assert jobs.claim() is None


@pytest.mark.parametrize('operation', ['complete', 'fail'])
@pytest.mark.parametrize('condition', ['worker', 'attempt', 'expired', 'null_worker', 'null_attempt'])
def test_owner_attempt_and_lease_are_required(jobs, operation, condition):
    jobs.claim()
    kwargs = {}
    if condition == 'expired':
        jobs.execute("UPDATE jobs SET lease_expires_at=now()-interval '1 second' WHERE job_id=%s", (jobs.job_id,))
    elif condition in {'worker', 'null_worker'}:
        kwargs['worker'] = 'other' if condition == 'worker' else None
    else:
        kwargs['attempt'] = 2 if condition == 'attempt' else None
    payload = jobs.result() if operation == 'complete' else {'code': 'video_changed'}
    assert jobs.call(operation, payload, **kwargs) == 'not_owner'
    assert jobs.execute('SELECT status FROM jobs WHERE job_id=%s', (jobs.job_id,)).fetchone()['status'] == 'running'
    if condition in {'worker', 'expired', 'null_worker'}:
        assert jobs.execute('SELECT video_decode_heartbeat(%s,%s,60) AS ok',
                            (jobs.job_id, kwargs.get('worker', 'decoder-test'))).fetchone()['ok'] is False


@pytest.mark.parametrize('field', ['tenant', 'request', 'manifest', 'video', 'key', 'null_identity', 'missing'])
def test_invalid_completion_cannot_mutate_job(jobs, field):
    jobs.claim()
    result = jobs.result()
    if field == 'tenant':
        result['identity']['tenant_id'] = 'another-tenant'
    elif field == 'request':
        result['request_sha256'] = 'd' * 64
    elif field == 'manifest':
        result['manifest_sha256'] = 'C' * 64
    elif field == 'video':
        result['video_sha256'] = 'd' * 64
    elif field == 'key':
        result['result_key'] = 'elsewhere'
    elif field == 'null_identity':
        result['identity'] = None
    else:
        del result['request_sha256']
    assert jobs.call('complete', result) == 'invalid'
    assert jobs.execute('SELECT result FROM jobs WHERE job_id=%s', (jobs.job_id,)).fetchone()['result'] is None


def test_failure_is_terminal_and_not_retried(jobs):
    jobs.claim()
    error = {'code': 'video_incomplete'}
    assert jobs.call('fail', error) == 'failed'
    assert jobs.call('fail', error) == 'duplicate'
    assert jobs.call('fail', {'code': 'video_changed'}) == 'conflict'
    assert jobs.claim() is None
    row = jobs.execute('SELECT status,attempt,lease_owner,error FROM jobs WHERE job_id=%s', (jobs.job_id,)).fetchone()
    assert row == dict(status='failed', attempt=1, lease_owner=None, error=error)


@pytest.mark.parametrize('error', [{}, {'code': ''}, {'code': ' '}, {'code': 'x' * 65}, {'code': 42}, None])
def test_failure_code_is_validated(jobs, error):
    jobs.claim()
    assert jobs.call('fail', error) == 'invalid'


@pytest.mark.parametrize('worker,lease', [('', 60), (' ', 60), (None, 60), ('worker', 14), ('worker', 301), ('worker', None)])
def test_claim_rejects_bad_worker_or_lease(jobs, worker, lease):
    with pytest.raises(psycopg.errors.RaiseException):
        with jobs.conn.transaction():
            jobs.claim(worker, lease)


def test_authority_and_live_project_required(jobs):
    jobs.execute("UPDATE project_authority_modes SET authority_mode='legacy_sqlite' WHERE project_id=%s",
                 (jobs.project.project_id,))
    assert jobs.claim() is None
    jobs.execute("UPDATE project_authority_modes SET authority_mode='postgres_canonical' WHERE project_id=%s",
                 (jobs.project.project_id,))
    jobs.execute('UPDATE projects SET deleted_at=now() WHERE project_id=%s', (jobs.project.project_id,))
    assert jobs.claim() is None


def test_function_security_and_idempotent_migration(jobs):
    migration = Path(__file__).parents[1] / 'migrations' / '0114_video_decode_job_functions.sql'
    # Raw migration text holds format() placeholders, so run it without query parameters.
    jobs.conn.execute(migration.read_text(encoding='utf-8'))
    for signature in SIGNATURES:
        # OID zero denotes PUBLIC, which is an ACL grantee rather than a pg_roles row.
        row = jobs.execute('SELECT pg_catalog.has_function_privilege(0::oid, %s::regprocedure, %s) AS public_execute, '
                           'p.prosecdef, p.proconfig FROM pg_catalog.pg_proc p WHERE p.oid=%s::regprocedure',
                           (signature, 'EXECUTE', signature)).fetchone()
        assert row['public_execute'] is False
        assert row['prosecdef'] is True
        assert 'search_path=pg_catalog, pg_temp' in row['proconfig']
    assert jobs.claim()['job_id'] == jobs.job_id


def test_execute_only_role_has_no_table_access(jobs):
    role = 'decoder_test_' + uuid.uuid4().hex
    try:
        with jobs.conn.transaction():
            jobs.execute(sql.SQL('CREATE ROLE {} NOLOGIN').format(sql.Identifier(role)))
    except psycopg.errors.InsufficientPrivilege:
        pytest.skip('test database does not permit CREATE ROLE')
    schema = jobs.execute('SELECT current_schema() AS schema').fetchone()['schema']
    jobs.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(schema), sql.Identifier(role)))
    for signature in SIGNATURES:
        jobs.execute(sql.SQL('GRANT EXECUTE ON FUNCTION {}.{} TO {}').format(
            sql.Identifier(schema), sql.SQL(signature), sql.Identifier(role)))
    second = jobs.insert()
    jobs.execute("UPDATE jobs SET created_at=now()+interval '1 second' WHERE job_id=%s", (second,))
    jobs.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with jobs.conn.transaction():
                jobs.execute('SELECT * FROM jobs').fetchall()
        claimed = jobs.claim()
        assert claimed['job_id'] == jobs.job_id
        assert jobs.execute('SELECT video_decode_heartbeat(%s,%s,60) AS ok',
                            (jobs.job_id, 'decoder-test')).fetchone()['ok'] is True
        assert jobs.call('complete', jobs.result()) == 'applied'
        assert jobs.claim()['job_id'] == second
        assert jobs.call('fail', {'code': 'video_changed'}, job_id=second) == 'failed'
    finally:
        jobs.execute('RESET ROLE')


@pytest.fixture
def committed_decoder():
    role = 'decoder_test_' + uuid.uuid4().hex
    password = secrets.token_urlsafe(24)
    with db.connection() as conn:
        previous_autocommit = conn.autocommit
        conn.autocommit = True
        org = project = job_id = None
        role_created = False
        schema = conn.execute('SELECT current_schema() AS schema').fetchone()['schema']
        try:
            try:
                conn.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(
                    sql.Identifier(role), sql.Literal(password)))
                role_created = True
            except psycopg.errors.InsufficientPrivilege:
                pytest.skip('test database does not permit CREATE ROLE')
            org = store.create_org('Decoder functions')
            project = store.create_project(org.org_id, 'Decoder functions')
            store.set_project_authority_mode(org.org_id, project.project_id, 'postgres_canonical')
            job_id = Jobs(conn, org, project).job_id
            conn.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(
                sql.Identifier(schema), sql.Identifier(role)))
            for signature in SIGNATURES:
                conn.execute(sql.SQL('GRANT EXECUTE ON FUNCTION {}.{} TO {}').format(
                    sql.Identifier(schema), sql.SQL(signature), sql.Identifier(role)))
            role_dsn = make_conninfo(host=conn.info.host, port=conn.info.port, dbname=conn.info.dbname,
                                     user=role, password=password)
            yield conn, role_dsn, job_id, schema
        finally:
            try:
                if job_id is not None:
                    conn.execute(sql.SQL('DELETE FROM {}.jobs WHERE job_id=%s').format(sql.Identifier(schema)),
                                 (job_id,))
                if project is not None:
                    conn.execute(sql.SQL('DELETE FROM {}.projects WHERE project_id=%s').format(sql.Identifier(schema)),
                                 (project.project_id,))
                if org is not None:
                    conn.execute(sql.SQL('DELETE FROM {}.orgs WHERE org_id=%s').format(sql.Identifier(schema)),
                                 (org.org_id,))
            finally:
                try:
                    if role_created:
                        for signature in SIGNATURES:
                            conn.execute(sql.SQL('REVOKE ALL ON FUNCTION {}.{} FROM {}').format(
                                sql.Identifier(schema), sql.SQL(signature), sql.Identifier(role)))
                        conn.execute(sql.SQL('REVOKE ALL ON SCHEMA {} FROM {}').format(
                            sql.Identifier(schema), sql.Identifier(role)))
                        conn.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
                finally:
                    conn.autocommit = previous_autocommit


def test_decoder_login_commits_claim_and_owner_reads_it_back(committed_decoder):
    owner, role_dsn, job_id, schema = committed_decoder
    with psycopg.connect(role_dsn, row_factory=dict_row) as conn:
        conn.execute('SET search_path = pg_catalog')
        claimed = conn.execute(sql.SQL('SELECT * FROM {}.video_decode_claim(%s,%s)').format(sql.Identifier(schema)),
                               ('decoder-test', 60)).fetchone()
        assert claimed['job_id'] == job_id
        conn.commit()
    row = owner.execute(sql.SQL('SELECT status,attempt,lease_owner FROM {}.jobs WHERE job_id=%s').format(
        sql.Identifier(schema)), (job_id,)).fetchone()
    assert row == dict(status='running', attempt=1, lease_owner='decoder-test')


def test_decoder_login_cannot_touch_tables_directly(committed_decoder):
    _, role_dsn, _, schema = committed_decoder
    with psycopg.connect(role_dsn) as conn:
        for statement in ('SELECT * FROM {}.jobs', 'UPDATE {}.jobs SET status = status',
                          'SELECT * FROM {}.project_sizing_exchanges',
                          'UPDATE {}.project_sizing_exchanges SET state = state'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql.SQL(statement).format(sql.Identifier(schema)))
            conn.rollback()


def test_sizing_check_is_definer_with_no_public_execute(committed_decoder):
    owner, role_dsn, _, schema = committed_decoder
    role = conninfo_to_dict(role_dsn)['user']
    migration = Path(__file__).parents[1] / 'migrations' / '0118_sizing_exchange_check_security_definer.sql'
    for rerun in (False, True):
        if rerun:
            owner.execute(migration.read_text(encoding='utf-8'))
        # OID zero denotes PUBLIC, which is an ACL grantee rather than a pg_roles row.
        row = owner.execute(
            "SELECT p.prosecdef, p.proconfig, "
            "pg_catalog.has_function_privilege(0::oid, p.oid, 'EXECUTE') AS public_execute, "
            "pg_catalog.has_function_privilege(%s, p.oid, 'EXECUTE') AS role_execute "
            "FROM pg_catalog.pg_proc p WHERE p.oid=pg_catalog.to_regprocedure("
            "pg_catalog.quote_ident(%s) || '.check_project_sizing_exchange_completion()')",
            (role, schema)).fetchone()
        assert row['prosecdef'] is True
        assert row['public_execute'] is False
        assert row['role_execute'] is False
        assert 'search_path=pg_catalog, pg_temp' in row['proconfig']


def test_untrusted_shadow_table_cannot_redirect_check(committed_decoder):
    owner, role_dsn, job_id, schema = committed_decoder
    with psycopg.connect(role_dsn, row_factory=dict_row) as conn:
        try:
            conn.execute('CREATE TEMP TABLE pg_temp.jobs (job_id uuid)')
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback()
            pytest.skip('test database does not permit decoder role to create a temp table')
        conn.execute('SET search_path = pg_temp, pg_catalog')
        claimed = conn.execute(sql.SQL('SELECT * FROM {}.video_decode_claim(%s,%s)').format(sql.Identifier(schema)),
                               ('decoder-test', 60)).fetchone()
        assert claimed['job_id'] == job_id
        conn.commit()
    row = owner.execute(sql.SQL('SELECT status,attempt,lease_owner FROM {}.jobs WHERE job_id=%s').format(
        sql.Identifier(schema)), (job_id,)).fetchone()
    assert row == dict(status='running', attempt=1, lease_owner='decoder-test')
