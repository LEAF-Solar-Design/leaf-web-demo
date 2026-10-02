"""Offline native instant-image contract; provider executables are PATH fakes."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / '.codebuild/instant-execution-native-producer.sh'
PIN = 'public.ecr.aws/aquasecurity/trivy@sha256:9db099105405c648166e6b94155eb32f8da12673cf1f455207f7385cc9a77283'
REGISTRY = '807034087062.dkr.ecr.us-east-1.amazonaws.com'
POSIX = pytest.mark.skipif(os.name != 'posix', reason='Producer execution requires a POSIX host')


def test_static_scanner_and_shell_contract():
    raw = SCRIPT.read_bytes()
    assert b'\r' not in raw
    assert raw.splitlines()[0] == b'#!/usr/bin/env bash'
    source = raw.decode()
    for required in (PIN, "TRIVY_VERSION='0.75.0'", 'public.ecr.aws/aquasecurity/trivy-db:2',
                     'public.ecr.aws/aquasecurity/trivy-java-db:1', 'python3.12 -I', '--kill-after=30s'):
        assert required in source
    for forbidden in ('--push', '--cache-to', '--cache-from', 'github.com', 'ghcr.io',
                      'curl', 'wget', 'set -x', 'describe-images'):
        assert forbidden not in source


FAKE = r'''#!/usr/bin/env python3.12
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
name = Path(sys.argv[0]).name
root = Path(os.environ['FAKE_ROOT'])
fault = os.environ.get('FAKE_FAULT', '')
with (root / 'calls.jsonl').open('a') as stream:
    stream.write(json.dumps([name, *args]) + '\n')
if name == 'aws':
    assert args == ['ecr', 'get-login-password', '--region', 'us-east-1']
    if fault == 'aws': sys.exit(1)
    print('secret-password')
elif args[:2] == ['buildx', 'build']:
    if fault == 'build': sys.exit(1)
elif args[:2] == ['image', 'inspect']:
    counter = root / 'inspect-count'
    count = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(count))
    revision = args[-1].split(':sha-')[1]
    if fault == 'revision': revision = '0' * 40
    identity = 'b' * 64 if fault == 'changed-image' and count == 2 else 'a' * 64
    print(json.dumps({'Id': 'sha256:' + identity,
                      'Config': {'Labels': {'org.opencontainers.image.revision': revision}}}))
elif args[0] == 'pull':
    if fault == 'pull': sys.exit(1)
elif args[0] == 'run':
    volumes = [args[i + 1] for i, value in enumerate(args) if value == '--volume']
    work = Path(next(value[:-6] for value in volumes if value.endswith(':/work')))
    if fault == 'missing-report': sys.exit(0)
    report = work / 'scan-report.json'
    if fault == 'bad-report': report.write_text('[]')
    elif fault == 'malformed-report': report.write_text('{')
    elif fault == 'large-report': report.write_bytes(b' ' * 16777217)
    else: report.write_text('{"Results":[]}')
    if fault == 'vulnerability': sys.exit(1)
    if fault == 'scanner-error': sys.exit(2)
elif args[0] == 'login':
    assert sys.stdin.read().strip() == 'secret-password'
    if fault == 'login': sys.exit(1)
elif args[0] == 'push':
    if fault in ('push', 'immutable-tag'): sys.exit(1)
    if fault == 'bad-digest': print('tag: digest: nonsense size: 123')
    elif fault == 'wrong-push-tag': print('sha-wrong: digest: sha256:' + 'c' * 64 + ' size: 123')
    else: print(args[1].split(':')[1] + ': digest: sha256:' + 'c' * 64 + ' size: 123')
elif args[0] == 'logout':
    pass
else:
    raise AssertionError(args)
'''


def executable(path, source):
    path.write_text(source, encoding='utf-8')
    path.chmod(0o700)


def case(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'deploy').mkdir()
    (source / 'deploy/Dockerfile.instant-execution').write_text('FROM scratch\n')
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('AWS_', 'GIT_', 'DOCKER_', 'PYTHON', 'LEAF_'))}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null')
    def git(*args):
        return subprocess.run(['git', *args], cwd=source, env=env, check=True,
                              capture_output=True, text=True, timeout=10).stdout.strip()
    git('init', '-q')
    git('add', '.')
    git('-c', 'user.name=Offline', '-c', 'user.email=offline@example.invalid', 'commit', '-qm', 'fixture')
    revision, tree = git('rev-parse', 'HEAD'), git('rev-parse', 'HEAD^{tree}')
    output = tmp_path / 'output'
    output.mkdir(mode=0o700)
    tools = tmp_path / 'tools'
    tools.mkdir()
    executable(tools / 'docker', FAKE)
    executable(tools / 'aws', FAKE)
    # Keep setup Git real; drive Git through a logging PATH transport.
    real_git = shutil.which('git')
    executable(tools / 'git', '#!/usr/bin/env python3.12\n'
               'import json, os, subprocess, sys\n'
               'from pathlib import Path\n'
               "with (Path(os.environ['FAKE_ROOT']) / 'calls.jsonl').open('a') as stream:\n"
               "    stream.write(json.dumps(['git', *sys.argv[1:]]) + '\\n')\n"
               f'sys.exit(subprocess.run([{real_git!r}, *sys.argv[1:]], timeout=9).returncode)\n')
    real_timeout = shutil.which('timeout')
    executable(tools / 'timeout', '#!/usr/bin/env python3.12\n'
               'import os, sys\n'
               "if os.environ.get('FAKE_FAULT') == 'timeout' and sys.argv[3:5] == ['docker', 'run']:\n"
               '    sys.exit(124)\n'
               f'os.execv({real_timeout!r}, [{real_timeout!r}, *sys.argv[1:]])\n')
    request = dict(schema='leaf.native-instant-execution.request.v1', transaction_id='d10-' + '1' * 16,
                   source_revision=revision, source_tree=tree, authority_sha256='d' * 64,
                   reservation_id='d10-' + '1' * 16 + '-instant-execution',
                   source_snapshot=dict(repository=dict(id=123, full_name='LEAF-Solar-Design/leaf-web-demo'),
                                        commit=revision, tree=tree, bucket='offline-artifacts',
                                        key='snapshots/source.zip', version_id='version-1',
                                        zip_sha256='e' * 64, bundle_sha256='f' * 64),
                   executor_package=dict(bucket='offline-artifacts', key='packages/executor.zip',
                                         version_id='version-2', sha256='9' * 64))
    env.update(PATH=str(tools) + os.pathsep + env['PATH'], FAKE_ROOT=str(tmp_path),
               LEAF_INSTANT_EXECUTION_ENABLED='true', LEAF_FORGE_SOURCE_DIR=str(source),
               LEAF_FORGE_OUTPUT_DIR=str(output), LEAF_FORGE_REQUEST_FILE=str(tmp_path / 'request.json'))
    return source, output, request, env


def drive(request, env, raw=None, arguments=()):
    raw = raw if raw is not None else json.dumps(request, sort_keys=True, separators=(',', ':')).encode()
    Path(env['LEAF_FORGE_REQUEST_FILE']).write_bytes(raw)
    env.setdefault('LEAF_INSTANT_EXECUTION_REQUEST_B64', base64.b64encode(raw).decode())
    completed = subprocess.run(['bash', '--noprofile', '--norc', str(SCRIPT), *arguments],
                               env=env, cwd=env['LEAF_FORGE_SOURCE_DIR'], capture_output=True,
                               text=True, timeout=30)
    log = Path(env['FAKE_ROOT']) / 'calls.jsonl'
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    assert 'secret-password' not in completed.stdout + completed.stderr
    return completed, calls


def no_publication(output, calls):
    assert list(output.iterdir()) == []
    assert not any(call[0] == 'aws' or call[:2] in (['docker', 'login'], ['docker', 'push']) for call in calls)


@POSIX
def test_success_exact_build_scan_publish_and_evidence(tmp_path):
    source, output, request, env = case(tmp_path)
    completed, calls = drive(request, env)
    assert completed.returncode == 0, completed.stderr
    uri = REGISTRY + '/leaf-platform-instant-execution:sha-' + request['source_revision']
    docker = [call[1:] for call in calls if call[0] == 'docker']
    assert [args[0] for args in docker] == ['buildx', 'image', 'pull', 'run', 'image', 'login', 'push', 'logout']
    assert docker[0] == ['buildx', 'build', '--load', '--file', 'deploy/Dockerfile.instant-execution',
                         '--build-arg', 'LEAF_SOURCE_SHA=' + request['source_revision'], '--tag', uri, '.']
    assert docker[2] == ['pull', PIN]
    scan = docker[3]
    assert scan[-1] == uri and PIN in scan and '--rm' in scan
    for option, value in (('--exit-code', '1'), ('--severity', 'HIGH,CRITICAL'), ('--format', 'json'),
                          ('--db-repository', 'public.ecr.aws/aquasecurity/trivy-db:2'),
                          ('--java-db-repository', 'public.ecr.aws/aquasecurity/trivy-java-db:1')):
        assert scan[scan.index(option) + 1] == value
    assert '--ignore-unfixed' in scan and '--scanners' not in scan and '--vuln-type' not in scan
    assert docker[5] == ['login', '--username', 'AWS', '--password-stdin', REGISTRY]
    assert docker[6] == ['push', uri]
    assert next(i for i, call in enumerate(calls) if call[0] == 'aws') > calls.index(['docker', *scan])
    assert [path.name for path in output.iterdir()] == ['producer-result.json']
    raw = (output / 'producer-result.json').read_bytes()
    assert len(raw) <= 1048576
    assert json.loads(raw) == dict(schema='leaf.native-instant-execution.producer.v1',
        source_revision=request['source_revision'], source_tree=request['source_tree'], image_uri=uri,
        image_digest='sha256:' + 'c' * 64, local_image_id='sha256:' + 'a' * 64,
        scan=dict(status='passed', scanner='trivy', scanner_version='0.75.0', severity='HIGH,CRITICAL',
                  ignore_unfixed=True, report_sha256=hashlib.sha256(b'{"Results":[]}').hexdigest()))
    mount = next(scan[i + 1][:-6] for i, arg in enumerate(scan) if arg == '--volume' and scan[i + 1].endswith(':/work'))
    assert not Path(mount).exists()


@POSIX
@pytest.mark.parametrize('fault', ['schema', 'extra-key', 'duplicate-key', 'noncanonical', 'base64',
    'encoding-mismatch', 'sha', 'head', 'tree', 'repository', 'repository-id', 'snapshot-mismatch',
    'path', 'disabled', 'arguments', 'dirty', 'untracked', 'output-mode', 'output-entry', 'inside-source',
    'request-symlink', 'output-symlink', 'source-symlink', 'nested-key', 'missing-tool'])
def test_invalid_input_has_no_provider_calls(tmp_path, fault):
    source, output, request, env = case(tmp_path)
    raw, arguments = None, ()
    if fault == 'schema': request['schema'] = 'unsupported'
    if fault == 'extra-key': request['extra'] = True
    if fault == 'duplicate-key': raw = b'{"schema":"x","schema":"y"}'
    if fault == 'noncanonical': raw = json.dumps(request, indent=2).encode()
    if fault == 'base64': env['LEAF_INSTANT_EXECUTION_REQUEST_B64'] = '!'
    if fault == 'encoding-mismatch': env['LEAF_INSTANT_EXECUTION_REQUEST_B64'] = base64.b64encode(b'{}').decode()
    if fault == 'sha': request['source_revision'] = 'not-a-sha'
    if fault == 'head':
        request['source_revision'] = '0' * 40
        request['source_snapshot']['commit'] = '0' * 40
    if fault == 'tree':
        request['source_tree'] = '0' * 40
        request['source_snapshot']['tree'] = '0' * 40
    if fault == 'repository': request['source_snapshot']['repository']['full_name'] = 'attacker/repo'
    if fault == 'repository-id': request['source_snapshot']['repository']['id'] = True
    if fault == 'snapshot-mismatch': request['source_snapshot']['commit'] = '0' * 40
    if fault == 'path': request['executor_package']['key'] = '../escape.zip'
    if fault == 'disabled': env['LEAF_INSTANT_EXECUTION_ENABLED'] = 'false'
    if fault == 'arguments': arguments = ('unexpected',)
    if fault == 'dirty': (source / 'deploy/Dockerfile.instant-execution').write_text('changed')
    if fault == 'untracked': (source / 'untracked').write_text('changed')
    if fault == 'output-mode': output.chmod(0o755)
    if fault == 'output-entry': (output / 'foreign').write_text('existing')
    if fault == 'inside-source':
        inside = source / 'output'
        inside.mkdir(mode=0o700)
        env['LEAF_FORGE_OUTPUT_DIR'] = str(inside)
    if fault == 'request-symlink':
        target = tmp_path / 'request-target'
        target.write_text('{}')
        Path(env['LEAF_FORGE_REQUEST_FILE']).symlink_to(target)
    if fault == 'output-symlink':
        link = tmp_path / 'output-link'
        link.symlink_to(output, target_is_directory=True)
        env['LEAF_FORGE_OUTPUT_DIR'] = str(link)
    if fault == 'source-symlink':
        link = tmp_path / 'source-link'
        link.symlink_to(source, target_is_directory=True)
        env['LEAF_FORGE_SOURCE_DIR'] = str(link)
    if fault == 'nested-key': request['executor_package']['extra'] = 'unexpected'
    if fault == 'missing-tool':
        # A present but non-executable AWS stub must fail the tool prerequisite.
        # Use a PATH containing only controlled tools, with AWS omitted entirely.
        tools = tmp_path / 'tools'
        (tools / 'aws').unlink()
        for name in ('bash', 'python3.12', 'timeout', 'mktemp', 'rm'):
            path = tools / name
            if path.exists(): path.unlink()
            path.symlink_to(shutil.which(name))
        env['PATH'] = str(tools)
    completed, calls = drive(request, env, raw, arguments)
    assert completed.returncode != 0
    assert not any(call[0] in ('aws', 'docker') for call in calls)
    assert not (output / 'producer-result.json').exists()


@POSIX
@pytest.mark.parametrize('fault', ['build', 'revision', 'pull', 'vulnerability', 'scanner-error',
    'timeout', 'missing-report', 'bad-report', 'malformed-report', 'large-report', 'changed-image'])
def test_scan_or_image_failure_never_authenticates_or_pushes(tmp_path, fault):
    source, output, request, env = case(tmp_path)
    env['FAKE_FAULT'] = fault
    completed, calls = drive(request, env)
    assert completed.returncode != 0
    no_publication(output, calls)


@POSIX
@pytest.mark.parametrize('fault', ['aws', 'login', 'push', 'immutable-tag', 'bad-digest', 'wrong-push-tag'])
def test_publication_failure_emits_no_result(tmp_path, fault):
    source, output, request, env = case(tmp_path)
    env['FAKE_FAULT'] = fault
    completed, calls = drive(request, env)
    assert completed.returncode != 0
    assert list(output.iterdir()) == []
    assert ['docker', 'logout', REGISTRY] in calls
    assert sum(call[:2] == ['docker', 'push'] for call in calls) == (0 if fault in ('aws', 'login') else 1)


@POSIX
def test_git_and_python_environment_overrides_are_scrubbed(tmp_path):
    source, output, request, env = case(tmp_path)
    env.update(GIT_DIR='/nonexistent', GIT_WORK_TREE='/nonexistent', GIT_CONFIG_COUNT='1',
               GIT_CONFIG_KEY_0='core.worktree', GIT_CONFIG_VALUE_0='/nonexistent',
               PYTHONPATH='/nonexistent', PYTHONHOME='/nonexistent')
    completed, calls = drive(request, env)
    assert completed.returncode == 0, completed.stderr
    assert (output / 'producer-result.json').is_file()


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
