"""Actual pinned evaluator and existing durable PostgreSQL producer topology."""
import copy
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lab_corpus_executor as corpus


@pytest.fixture(scope='module')
def private_pg():
    """Reuse canonical CI PG, otherwise produce the actual private managed PG16."""
    from test_project_drawing_editor import canonical_ci_pg
    import platform_link
    db = platform_link.platform_db()
    authority = None
    run_root = None
    aliases = ('DATABASE_URL', 'OVERLAY_PG_URL', 'ANNOTATION_PG_URL')
    prior = {key: os.environ.get(key) for key in aliases}
    try:
        if not canonical_ci_pg() and not (os.environ.get('LEAF_E364B_PG_PARENT') and prior['DATABASE_URL']):
            if any(prior.values()):
                raise ValueError('Ambient PostgreSQL configuration refused.')
            producer = Path(__file__).resolve().parents[2] / 'scripts/proof_project_pg.py'
            spec = importlib.util.spec_from_file_location('_lab_corpus_private_pg', producer)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            run_root = Path(tempfile.mkdtemp(prefix='leaf-lab-corpus-pg-', dir='/tmp')).resolve()
            os.chmod(run_root, 0o700)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            authority = module.NativePgAuthority.start('lab-corpus-' + uuid.uuid4().hex[:16],
                run_root=run_root, port=port,
                environ=dict(os.environ, LEAF_PG_BIN='/opt/homebrew/opt/postgresql@16/bin'))
            for key in aliases:
                os.environ[key] = authority.dsn()
        db.reset_pool()
        db.apply_migration()
        yield db
    finally:
        db.reset_pool()
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if authority is not None:
            stopped = authority.stop()
            if stopped and run_root.exists():
                run_root.rmdir()
            assert stopped and not run_root.exists(), 'Private PostgreSQL cleanup failed.'
            print('L03_PRODUCER_PRIVATE_PG cleanup=true directory_absent=true major=16')
        elif run_root is not None and run_root.exists() and not any(run_root.iterdir()):
            run_root.rmdir()


@pytest.fixture(scope='module')
def kernel_filter(tmp_path_factory):
    if sys.platform != 'linux':
        return corpus.KERNEL_FILTER
    # Use the identical source producer as Dockerfile.app, in a private test
    # directory. Never change the worker's packages, config or runtime paths.
    source = Path(__file__).resolve().parents[2] / 'deploy/gen_seccomp_filter.c'
    assert hashlib.sha256(source.read_bytes()).hexdigest() == '2953b0e75f8fcfe9dbb1cb3af681285275da8f5d58f4c2998631529ecf7b0cd6'
    directory = tmp_path_factory.mktemp('canonical-corpus-cage')
    compiler = shutil.which('cc')
    assert compiler, 'Canonical corpus cage producer needs a C compiler and libseccomp development files.'
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(directory)}
    built = subprocess.run([compiler, '-O2', str(source), '-lseccomp', '-o', str(directory / 'generator')],
        capture_output=True, text=True, timeout=30, env=env)
    if built.returncode != 0:
        env = ephemeral_seccomp_toolchain(directory, env, compiler)
        built = subprocess.run([compiler, '-O2', str(source), '-lseccomp', '-o', str(directory / 'generator')],
            capture_output=True, text=True, timeout=30, env=env)
    assert built.returncode == 0, 'Canonical corpus cage generator failed: ' + built.stderr[:2048]
    output = directory / 'seccomp-dwg2dxf.bpf'
    generated = subprocess.run([str(directory / 'generator'), str(output)],
        capture_output=True, text=True, timeout=5, env=env)
    assert generated.returncode == 0 and output.stat().st_size > 0
    print('L03_LINUX_CAGE producer_sha256=2953b0e75f8fcfe9dbb1cb3af681285275da8f5d58f4c2998631529ecf7b0cd6 filter_sha256=' + hashlib.sha256(output.read_bytes()).hexdigest())
    return output


def ephemeral_seccomp_toolchain(directory, env, compiler):
    """Extract authenticated APT supply privately; never install host packages."""
    def command(argv, *, cwd=directory):
        result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                                text=True, timeout=90)
        if result.returncode != 0:
            # Repository diagnostics can contain operator-owned source URLs.
            # Emit only the fixed stage, never raw download output or config.
            raise AssertionError('Ephemeral canonical seccomp supply unavailable: '
                                 + argv[0] + ' exit ' + str(result.returncode))
        return result.stdout.strip()

    def fields(text):
        return dict(line.split(': ', 1) for line in text.splitlines() if ': ' in line and not line.startswith(' '))

    arch = command(['dpkg', '--print-architecture'])
    expected = {}
    for name in ('libseccomp-dev', 'libseccomp2'):
        policy = command(['apt-cache', 'policy', name])
        candidate = next((line.split(':', 1)[1].strip() for line in policy.splitlines()
                          if line.strip().startswith('Candidate:')), '')
        assert candidate and candidate != '(none)', 'No authenticated seccomp package candidate.'
        records = [fields(part) for part in command(['apt-cache', 'show', name + '=' + candidate]).split('\n\n')]
        metadata = next((row for row in records if row.get('Package') == name
                         and row.get('Version') == candidate and row.get('Architecture') == arch), None)
        assert metadata and len(metadata.get('SHA256', '')) == 64, 'Missing trusted seccomp package digest.'
        expected[name] = metadata
        command(['apt-get', 'download', name + '=' + candidate])
    packages = list(directory.glob('*.deb'))
    assert len(packages) == 2, 'Unexpected ephemeral seccomp supply set.'
    prefix = directory / 'toolchain'
    prefix.mkdir()
    observed = set()
    for package in packages:
        assert package.stat().st_size <= 32 * 1024 * 1024, 'Ephemeral package exceeds its bound.'
        row = fields(command(['dpkg-deb', '--field', str(package), 'Package', 'Version', 'Architecture']))
        metadata = expected.get(row.get('Package'))
        assert metadata and row['Package'] not in observed, 'Unexpected ephemeral package identity.'
        digest = hashlib.sha256(package.read_bytes()).hexdigest()
        assert (row['Version'] == metadata['Version'] and row['Architecture'] == arch
                and digest == metadata['SHA256']), 'Ephemeral package identity mismatch.'
        observed.add(row['Package'])
        command(['dpkg-deb', '--extract', str(package), str(prefix)])
        print('L03_EPHEMERAL_SUPPLY package=' + row['Package'] + ' version=' + row['Version']
              + ' architecture=' + arch + ' sha256=' + digest)
    assert observed == set(expected)
    multiarch = command([compiler, '-print-multiarch'])
    assert multiarch and '/' not in multiarch and '..' not in multiarch
    library = str(prefix / 'usr/lib' / multiarch)
    return dict(env, CPATH=str(prefix / 'usr/include'), LIBRARY_PATH=library, LD_LIBRARY_PATH=library)


@pytest.fixture
def installed(tmp_path, monkeypatch, kernel_filter):
    monkeypatch.setattr(corpus, 'KERNEL_FILTER', kernel_filter)
    path = corpus.install(tmp_path / 'package')
    monkeypatch.setattr(corpus, 'INSTALLED', path)
    corpus.require_executor()
    return path


def synthetic_inputs():
    rows = [{'market_id': f'market-{i // 1000}', 'close_ms': (i // 1000 + 1) * 900000,
             'boundary': 'gt', 'symbol': 'SYNTHETIC', 'k': 10, 'pre_window_s': 0,
             'strike': 100, 'sum_k': 1000, 'last': 100, 'outcome': i % 2,
             'final_avg': 101 if i % 2 else 99,
             'venue_yes_bid_c': 40, 'venue_yes_ask_c': 60, 'venue_ask_size': 5,
             'venue_no_ask_size': 5, 'mad': 1, 'phase': 'k10'} for i in range(6001)]
    raw = gzip.compress(b'\n'.join(json.dumps(row).encode() for row in rows) + b'\n')
    suite = json.dumps({'schema': 'leafhft.block-suite.v1', 'symbol': 'SYNTHETIC',
        'parent_policy': {'min_k': 2}, 'child_min_k': [1],
        'blocks': {'null.identity': [{}]}, 'min_select_tickets': 1_000_000,
        'draws': 1, 'seed': 1}).encode()
    return rows, raw, suite


@pytest.fixture
def lane(private_pg, installed, tmp_path, monkeypatch):
    import jobs
    import platform_link
    import write_loop
    import store as blobs
    platform_link._ensure_platform_package()
    from leaf_platform import store, project_lifecycle, project_reference_materials
    import project_reference_materials as originals
    monkeypatch.setenv('LEAF_JOBS_STORE', 'postgres')
    org = store.create_org('Synthetic offline evaluator proof')
    actor = store.create_identity_binding(org.org_id, 'auth0', str(uuid.uuid4()), role='owner')
    project = project_lifecycle.create_blank_project(org.org_id, actor.binding_id,
        name='Synthetic corpus', idempotency_key=str(uuid.uuid4()))['project']['project_id']
    backend = blobs.FilesystemBackend(str(tmp_path / 'blobs'))
    monkeypatch.setattr(write_loop, 'upload_backend_for_tenant', lambda tenant: backend)
    # Synthetic source inputs, never an original corpus or stored evaluator answer.
    rows, raw, suite = synthetic_inputs()
    materials = {}
    for key, name, mime, data in [('corpus', 'synthetic.jsonl.gz', 'application/gzip', raw),
                                   ('suite', 'suite.json', 'application/json', suite)]:
        prepared = originals.prepare_and_store(org.org_id, project, name, mime, data)
        metadata = project_reference_materials.commit(org.org_id, project, actor.binding_id, prepared)
        materials[key] = {k: metadata[k] for k in ('material_id', 'sha256', 'byte_size')}
    context = corpus.validate_context({'schema': corpus.SCHEMA, 'tenant_id': str(org.org_id),
        'org_id': str(org.org_id), 'project_id': str(project), 'actor_id': str(actor.binding_id),
        'capability_sha256': corpus.ARCHIVE_SHA256, **materials})
    scheduled = []
    class Queue:
        def submit(self, *args, **kwargs):
            scheduled.append((args, kwargs))
    monkeypatch.setattr(jobs, 'ensure_started', lambda: None)
    monkeypatch.setitem(jobs._executors, jobs.LANE_SLOW, Queue())
    return context, jobs, scheduled, rows, backend


def submit(lane, key=None):
    context, jobs, *_ = lane
    return jobs.submit_lab_corpus(context['tenant_id'], context['org_id'], context['project_id'],
        context['actor_id'], context['corpus'], context['suite'],
        capability_sha256=context['capability_sha256'], idempotency_key=key or str(uuid.uuid4()))


def test_actual_pinned_cli_through_durable_job_and_exact_6001_rows(lane, monkeypatch):
    context, jobs, scheduled, rows, _ = lane
    errors = []
    real_run = corpus.run
    def observed(*args, **kwargs):
        try:
            return real_run(*args, **kwargs)
        except Exception as exc:
            errors.append(type(exc).__name__ + ': ' + str(exc))
            raise
    monkeypatch.setattr(corpus, 'run', observed)
    child_errors = tempfile.TemporaryFile(mode='w+b')
    real_popen = corpus.subprocess.Popen
    def start(*args, **kwargs):
        kwargs['stderr'] = child_errors
        return real_popen(*args, **kwargs)
    monkeypatch.setattr(corpus.subprocess, 'Popen', start)
    key = str(uuid.uuid4())
    job_id = submit(lane, key)
    assert submit(lane, key) == job_id and len(scheduled) == 1
    durable = jobs._pg_store.durable_context(job_id)
    assert durable['execution'] == {'operation': corpus.OPERATION, 'lab_corpus': context}
    assert durable['params'] == {} and jobs.get_job(job_id)['dwg'] == ''
    jobs._run_lab_corpus(job_id)
    record = jobs.get_job(job_id)
    child_errors.seek(0)
    diagnostic = child_errors.read(8192).decode(errors='replace')
    child_errors.close()
    if record['status'] != 'complete':
        pytest.fail('\n'.join(errors) + '\n' + diagnostic)
    result = record['result']['result']
    assert result['row_count'] == 6001
    canonical = json.dumps(rows, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    assert result['rows_sha256'] == hashlib.sha256(canonical).hexdigest()
    assert result['context'] == context and result['result']['schema'] == 'leafhft.block-suite-result.v1'
    assert result['result']['suite_sha256'] == context['suite']['sha256']
    assert jobs._redispatch_record(job_id) is False


@pytest.mark.parametrize('mismatch', ['tenant', 'project', 'actor', 'corpus', 'suite', 'capability'])
def test_custody_refusal_before_job_or_process(lane, mismatch):
    context, jobs, scheduled, *_ = lane
    original = copy.deepcopy(context)
    if mismatch == 'tenant':
        context['tenant_id'] = str(uuid.uuid4())
    elif mismatch in {'project', 'actor'}:
        context[mismatch + '_id'] = str(uuid.uuid4())
    elif mismatch in {'corpus', 'suite'}:
        context[mismatch]['sha256'] = '0' * 64
    else:
        context['capability_sha256'] = '0' * 64
    with pytest.raises(Exception):
        submit(lane)
    assert not scheduled
    context.clear()
    context.update(original)


def test_restart_dispatch_reuses_durable_corpus_identity(lane):
    context, jobs, scheduled, *_ = lane
    job_id = submit(lane)
    scheduled.clear()
    assert jobs._redispatch_record(job_id)
    assert scheduled[0][0] == (jobs._run_lab_corpus, job_id)
    worker = str(uuid.uuid4())
    attempt = jobs.claim_lease(job_id, worker)
    assert attempt == 1
    with pytest.raises(ValueError, match='owning attempt'):
        jobs.complete_callback(job_id, 'complete', result_env={'ok': True, 'result': {}},
            worker_id=worker, provenance={'attempt': attempt, 'execution_path': 'local'})
    assert jobs.get_job(job_id)['status'] == 'running'


@pytest.mark.parametrize('reason', ['cancel', 'deadline', 'lease'])
def test_running_child_is_killed_reaped_and_stage_removed(lane, monkeypatch, reason):
    context, *_ = lane
    real_popen = corpus.subprocess.Popen
    children = []
    stages = []
    # Start a real long child to discriminate the actual kill/reap lifecycle.
    def start(argv, **kwargs):
        stages.append(Path(kwargs['cwd']))
        child = real_popen([sys.executable, '-I', '-c', 'import time; time.sleep(60)'], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(corpus.subprocess, 'Popen', start)
    started = time.monotonic()
    with pytest.raises(ValueError, match='lease or execution budget'):
        corpus.run(str(uuid.uuid4()), 'worker', 1, context,
            lambda: reason != 'lease' or not children,
            lambda: reason == 'cancel' and bool(children),
            started + (0.3 if reason == 'deadline' else 30))
    assert children and children[0].poll() is not None
    with pytest.raises(ProcessLookupError):
        os.kill(children[0].pid, 0)
    assert all(not p.exists() for p in stages)


def test_changed_installed_module_is_refused(installed):
    (installed / 'scripts/blocks_lab/suite.py').write_text('raise RuntimeError("substitution")')
    with pytest.raises(ValueError, match='module identity'):
        corpus.require_executor()


def test_unregistered_or_generic_operation_is_refused(lane):
    context, jobs, *_ = lane
    with pytest.raises(ValueError, match='internal admission'):
        jobs.submit_job(context['tenant_id'], {'name': corpus.OPERATION}, {}, '', False)
    bad = {'operation': corpus.OPERATION, 'lab_corpus': context, 'native_preview': {}}
    with pytest.raises(ValueError, match='Conflicting'):
        corpus.record_context(bad, {'tool': corpus.OPERATION})


def test_kernel_cage_real_cli_and_network_refusal(installed, tmp_path, monkeypatch):
    """Linux producer prerequisite; PG custody is independently proven above."""
    _, raw, suite = synthetic_inputs()
    org = str(uuid.uuid4())
    descriptor = lambda data: {'material_id': str(uuid.uuid4()),
        'sha256': hashlib.sha256(data).hexdigest(), 'byte_size': len(data)}
    context = {'schema': corpus.SCHEMA, 'tenant_id': org, 'org_id': org,
        'project_id': str(uuid.uuid4()), 'actor_id': str(uuid.uuid4()),
        'corpus': descriptor(raw), 'suite': descriptor(suite),
        'capability_sha256': corpus.ARCHIVE_SHA256}
    monkeypatch.setattr(corpus, 'read_sources', lambda scope: {'corpus': raw, 'suite': suite})
    real_popen = corpus.subprocess.Popen
    with tempfile.TemporaryFile(mode='w+b') as child_errors, monkeypatch.context() as patch:
        def start(*args, **kwargs):
            kwargs['stderr'] = child_errors
            return real_popen(*args, **kwargs)
        patch.setattr(corpus.subprocess, 'Popen', start)
        try:
            result = corpus.run(str(uuid.uuid4()), 'linux-positive', 1, context,
                                lambda: True, lambda: False, time.monotonic() + 30)
        except Exception as exc:
            child_errors.seek(0)
            pytest.fail(type(exc).__name__ + ': ' + str(exc) + '\n'
                        + child_errors.read(8192).decode(errors='replace'))
    assert result['row_count'] == 6001 and result['context'] == context
    command = corpus._command(tmp_path)
    command = command[:command.index(sys.executable)] + [sys.executable, '-I', '-c',
        'import socket\ntry: socket.socket()\nexcept PermissionError: raise SystemExit(0)\nraise SystemExit(9)']
    probe = subprocess.run(command, capture_output=True, timeout=5,
        env={'PATH': '/usr/bin:/bin', 'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1'})
    assert probe.returncode == 0, 'Kernel cage did not refuse socket creation.'
