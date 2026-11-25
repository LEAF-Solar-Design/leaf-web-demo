import base64
import hashlib
import io
import json
import sys
from types import SimpleNamespace

import pytest

import video_decode_worker as worker
from project_video_materials import VideoError, canonical


RAW = b'private video'
FRAME = b'private frame'
CONFIG = worker.DecoderConfig('postgresql://decoder:credential@localhost/leaf',
                              'decoder-bucket', 'laptop-decoder', 'a' * 40)


def context():
    digest = 'b' * 64
    return dict(schema=worker.SCHEMA, request_sha256=digest,
                identity=dict(tenant_id='tenant', organization_id='tenant', project_id='project',
                              actor_binding_id='actor', edit_id='edit'),
                material=dict(material_id='material', sha256=hashlib.sha256(RAW).hexdigest(),
                              byte_size=len(RAW), media_type='video/quicktime'),
                transcripts=[], input_key=f'in/{digest}/1.mov', result_key=f'out/{digest}/1.json')


class DB:
    def __init__(self):
        self.job = dict(job_id='job', project_id='project', request_tenant_id='tenant',
                        attempt=1, execution_context=context())
        self.calls = []
        self.heartbeat = True
        self.complete_error = False

    def video_decode_claim(self, *args):
        self.calls.append(('claim', args))
        return self.job

    def video_decode_heartbeat(self, *args):
        self.calls.append(('heartbeat', args))
        if isinstance(self.heartbeat, Exception):
            raise self.heartbeat
        return self.heartbeat

    def video_decode_complete(self, *args):
        self.calls.append(('complete', args))
        if self.complete_error:
            raise RuntimeError('uncertain')
        return 'applied'

    def video_decode_fail(self, *args):
        self.calls.append(('fail', args))
        return 'failed'


class Store:
    def __init__(self):
        self.gets, self.puts = [], []
        self.raw = RAW
        self.put_error = False

    def get_object(self, **kwargs):
        self.gets.append(kwargs)
        return {'Body': io.BytesIO(self.raw)}

    def put_object(self, **kwargs):
        self.puts.append(kwargs)
        if self.put_error:
            raise RuntimeError('uncertain')


def decode(raw, material, *, identity, transcripts, recheck, deadline):
    assert raw == RAW and material == context()['material']
    assert identity == context()['identity'] and transcripts == []
    assert deadline > worker.time.monotonic()
    recheck()
    manifest = {'schema': 'test.manifest'}
    return dict(manifest=manifest, manifest_sha256=hashlib.sha256(canonical(manifest)).hexdigest(),
                images=[dict(frame_id='frame-001', data=base64.b64encode(FRAME).decode())])


def terminal(db, name):
    return [args for call, args in db.calls if call == name]


def test_positive_path_and_private_output(capsys, caplog):
    db, store = DB(), Store()
    assert worker.run_once(db, store, decode, CONFIG) == 'succeeded'
    assert db.calls[0] == ('claim', (CONFIG.worker_id, 60))
    assert store.gets == [dict(Bucket=CONFIG.bucket, Key=context()['input_key'])]
    put, = store.puts
    assert put['Key'] == context()['result_key'] and put['Bucket'] == CONFIG.bucket
    assert put['IfNoneMatch'] == '*' and put['ContentType'] == 'application/json'
    saved = terminal(db, 'complete')[0][3]
    assert saved == dict(request_sha256=context()['request_sha256'], identity=context()['identity'],
                        video_sha256=hashlib.sha256(RAW).hexdigest(), manifest={'schema': 'test.manifest'},
                        manifest_sha256=hashlib.sha256(canonical({'schema': 'test.manifest'})).hexdigest(),
                        images=[dict(frame_id='frame-001', sha256=hashlib.sha256(FRAME).hexdigest())],
                        decoder=dict(source_sha=CONFIG.source_sha, worker_id=CONFIG.worker_id, attempt=1),
                        cleanup='settled', result_key=context()['result_key'])
    full = json.loads(put['Body'])
    assert base64.b64decode(full['images'][0]['data']) == FRAME
    assert {k: v for k, v in full.items() if k != 'images'} == {k: v for k, v in saved.items() if k != 'images'}
    assert not terminal(db, 'fail')
    output = capsys.readouterr()
    logs = output.out + output.err + caplog.text
    for private in (context()['input_key'], context()['result_key'], 'credential',
                    base64.b64encode(FRAME).decode(), CONFIG.database_url):
        assert private not in logs


@pytest.mark.parametrize('change', ['schema', 'tenant', 'project', 'input_key', 'result_key',
                                    'identity_extra', 'byte_size', 'hash', 'transcripts', 'missing'])
def test_bad_requests_never_download(change):
    db, store = DB(), Store()
    ctx = db.job['execution_context']
    if change in {'tenant', 'project'}:
        ctx['identity'][change + '_id'] = 'other'
    elif change == 'identity_extra':
        ctx['identity']['extra'] = 'unexpected'
    elif change == 'byte_size':
        ctx['material']['byte_size'] = True
    elif change == 'hash':
        ctx['request_sha256'] = 'B' * 64
    elif change == 'missing':
        del ctx['material']
    else:
        ctx[change] = None
    assert worker.run_once(db, store, decode, CONFIG) == 'failed'
    assert terminal(db, 'fail')[0][3] == {'code': 'bad_request'}
    assert not store.gets and not store.puts and not terminal(db, 'complete')


def test_idle():
    db, store = DB(), Store()
    db.job = None
    assert worker.run_once(db, store, decode, CONFIG) == 'idle'
    assert not store.gets and len(db.calls) == 1


def test_video_changed():
    db, store = DB(), Store()
    store.raw = b'x' * len(RAW)
    assert worker.run_once(db, store, decode, CONFIG) == 'failed'
    assert terminal(db, 'fail')[0][3] == {'code': 'video_changed'}
    assert not store.puts


def test_video_error_code():
    db, store = DB(), Store()

    def refused(*args, **kwargs):
        raise VideoError('video_cleanup_failed')

    assert worker.run_once(db, store, refused, CONFIG) == 'failed'
    assert terminal(db, 'fail')[0][3] == {'code': 'video_cleanup_failed'}


@pytest.mark.parametrize('heartbeat', [False, RuntimeError('credential must stay private')])
def test_lost_mid_decode(heartbeat):
    db, store = DB(), Store()

    def interrupted(*args, **kwargs):
        db.heartbeat = heartbeat
        # Exercise the actual guard renewal used by its background thread.
        kwargs['recheck'].__self__.renew()
        pytest.fail('lost custody did not interrupt decode')

    assert worker.run_once(db, store, interrupted, CONFIG) == 'lost'
    assert not terminal(db, 'fail') and not terminal(db, 'complete') and not store.puts


@pytest.mark.parametrize('stage', ['put', 'complete'])
def test_uncertain_result_never_retries(stage):
    db, store = DB(), Store()
    store.put_error = stage == 'put'
    db.complete_error = stage == 'complete'
    assert worker.run_once(db, store, decode, CONFIG) == 'lost'
    assert len(store.puts) == 1
    assert len(terminal(db, 'complete')) == (stage == 'complete')
    assert not terminal(db, 'fail')
    assert len(terminal(db, 'claim')) == 1


def test_loss_after_put_prevents_complete():
    db, store = DB(), Store()
    put = store.put_object

    def lose(**kwargs):
        put(**kwargs)
        db.heartbeat = False

    store.put_object = lose
    assert worker.run_once(db, store, decode, CONFIG) == 'lost'
    assert len(store.puts) == 1 and not terminal(db, 'complete') and not terminal(db, 'fail')


def test_guard_renews_every_fifteen_seconds_and_fails_closed():
    db = DB()
    guard = worker.LeaseGuard(db, 'job', CONFIG.worker_id)
    waits = []

    class Stop:
        def wait(self, interval):
            waits.append(interval)
            return False

    guard.stop = Stop()
    db.heartbeat = RuntimeError('unavailable')
    guard._run()
    assert waits == [15]
    assert terminal(db, 'heartbeat') == [('job', CONFIG.worker_id, 60)]
    with pytest.raises(worker.LostLease):
        guard.recheck()


@pytest.mark.parametrize('field,value', [('database_url', ''), ('database_url', 'https://host/db'),
                                       ('bucket', 'bad/key'), ('worker_id', ' '), ('source_sha', 'bad')])
def test_config_rejects_malformed(field, value):
    config = dict(database_url=CONFIG.database_url, bucket=CONFIG.bucket,
                  worker_id=CONFIG.worker_id, source_sha=CONFIG.source_sha)
    config[field] = value
    with pytest.raises(ValueError):
        worker.DecoderConfig(**config)


def test_environment_config():
    env = dict(DATABASE_URL=CONFIG.database_url, LEAF_I01_DECODER_BUCKET=CONFIG.bucket,
               LEAF_I01_DECODER_WORKER_ID=CONFIG.worker_id, LEAF_I01_DECODER_SOURCE_SHA=CONFIG.source_sha)
    assert worker.DecoderConfig.from_env(env) == CONFIG
    for name in env:
        missing = dict(env)
        del missing[name]
        with pytest.raises(ValueError):
            worker.DecoderConfig.from_env(missing)


@pytest.mark.parametrize('tool', ['string-autofill-opt', 'arlo-design'])
def test_normal_worker_main_dispatch_unchanged(monkeypatch, tool):
    import canonical_worker
    assert tool in canonical_worker.ADAPTERS
    calls = []
    monkeypatch.setattr(sys, 'argv', ['canonical_worker.py', '--tool', tool, '--once'])
    monkeypatch.setattr(canonical_worker.platform_link, '_load_platform', lambda: (
        None, SimpleNamespace(assert_schema_current=lambda: calls.append('schema')), None))
    monkeypatch.setattr(canonical_worker.signal, 'signal', lambda *_: None)
    monkeypatch.setattr(canonical_worker, 'serve', lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(worker, 'main', lambda *_: pytest.fail('normal worker delegated to decoder'))
    canonical_worker.main()
    assert calls[0] == 'schema'
    assert calls[1]['tool_name'] == tool and calls[1]['once'] is True


def test_decoder_dispatch_before_schema(monkeypatch):
    import canonical_worker
    calls = []
    monkeypatch.setattr(sys, 'argv', ['canonical_worker.py', '--tool', 'video-decode', '--once'])
    monkeypatch.setattr(canonical_worker.platform_link, '_load_platform',
                        lambda: pytest.fail('decoder used application schema admission'))
    monkeypatch.setattr(worker, 'main', lambda argv: calls.append(argv))
    canonical_worker.main()
    assert calls == [['--once']]


def test_decoder_db_sets_timeout_after_connect_without_startup_options(monkeypatch):
    from types import ModuleType

    connect_kwargs = {}
    executed = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=None):
            executed.append((sql, params))

        def fetchone(self):
            return {'video_decode_heartbeat': True}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def cursor(self):
            return Cursor()

    def connect(dsn, **kwargs):
        connect_kwargs.update(kwargs)
        return Connection()

    psycopg = ModuleType('psycopg')
    psycopg.connect = connect
    psycopg.rows = ModuleType('psycopg.rows')
    psycopg.rows.dict_row = object()
    psycopg.types = ModuleType('psycopg.types')
    psycopg.types.json = ModuleType('psycopg.types.json')
    psycopg.types.json.Jsonb = lambda value: value
    for name, module in (('psycopg', psycopg), ('psycopg.rows', psycopg.rows),
                         ('psycopg.types', psycopg.types), ('psycopg.types.json', psycopg.types.json)):
        monkeypatch.setitem(sys.modules, name, module)

    result = worker.DecoderDB('postgresql://u:p@h/d').video_decode_heartbeat('job', 'w', 60)

    assert result is True
    assert 'options' not in connect_kwargs
    assert connect_kwargs['connect_timeout'] == 5
    assert connect_kwargs['autocommit'] is True
    assert executed[0][0] == 'SET statement_timeout = 5000'
    assert executed[1][0].startswith('SELECT * FROM video_decode_heartbeat(')
