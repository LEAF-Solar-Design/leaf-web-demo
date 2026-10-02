#!/usr/bin/env bash
set -euo pipefail
readonly TRIVY_VERSION='0.75.0'
readonly TRIVY_IMAGE='public.ecr.aws/aquasecurity/trivy@sha256:9db099105405c648166e6b94155eb32f8da12673cf1f455207f7385cc9a77283'
[[ $# == 0 && ${LEAF_INSTANT_EXECUTION_ENABLED:-} == true ]] || exit 2
# The bootstrap already supplies an allowlist; also neutralize interpreter overrides.
for name in $(compgen -e); do
  case "$name" in PYTHON*|GIT_*|BASH_ENV|ENV|CDPATH|DOCKER_*|AWS_ENDPOINT_URL*|AWS_PROFILE|AWS_CONFIG_FILE|AWS_SHARED_CREDENTIALS_FILE|LD_*|BASH_FUNC_*) unset "$name" ;; esac
done
export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0
export AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1 AWS_MAX_ATTEMPTS=1 AWS_PAGER=''
for tool in python3.12 timeout git docker aws mktemp rm; do command -v "$tool" >/dev/null || exit 2; done
umask 077
scratch="$(timeout --kill-after=30s 5s mktemp -d /tmp/leaf-instant-producer.XXXXXX)"
export DOCKER_CONFIG="$scratch/docker-config"
cleanup() {
  status=$?
  trap - EXIT
  if [[ -f "$scratch/login-attempted" ]]; then
    timeout --kill-after=30s 15s docker logout 807034087062.dkr.ecr.us-east-1.amazonaws.com >/dev/null 2>&1 || true
  fi
  timeout --kill-after=30s 5s rm -rf -- "$scratch" || true
  exit "$status"
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
# Bound files even while a failing tool is producing them (512-byte units).
ulimit -f 32769
timeout --kill-after=30s 2150s python3.12 -I - "$scratch" "$TRIVY_VERSION" "$TRIVY_IMAGE" <<'PY'
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

scratch, version, scanner = sys.argv[1:]
work = Path(scratch)
registry = '807034087062.dkr.ecr.us-east-1.amazonaws.com'

def require(ok):
    if not ok:
        raise ValueError('instant producer contract refused')

def pairs(items):
    result = {}
    for key, value in items:
        require(key not in result)
        result[key] = value
    return result

def keys(value, expected):
    require(type(value) is dict and set(value) == set(expected.split()))

def hexval(value, length):
    require(type(value) is str and re.fullmatch('[0-9a-f]{%d}' % length, value))

def text(value):
    require(type(value) is str and 0 < len(value) <= 128 and not any(ord(c) < 32 for c in value))

def bounded(path, limit):
    require(not path.is_symlink() and stat.S_ISREG(path.stat().st_mode))
    with path.open('rb') as stream:
        value = stream.read(limit + 1)
    require(0 < len(value) <= limit)
    return value

def run(args, seconds, *, capture=False):
    path = work / 'command.log'
    with path.open('wb') as stream:
        proc = subprocess.run(['timeout', '--kill-after=30s', str(seconds) + 's', *args],
                              stdout=stream, stderr=subprocess.STDOUT, check=False)
    require(proc.returncode == 0)
    require(path.stat().st_size <= 16777216)
    return bounded(path, 1048576).decode('utf-8') if capture else None

def inspect(uri, revision):
    raw = run(['docker', 'image', 'inspect', '--format', '{{json .}}', uri], 15, capture=True)
    image = json.loads(raw, object_pairs_hook=pairs)
    require(image['Config']['Labels']['org.opencontainers.image.revision'] == revision)
    identity = image['Id']
    require(type(identity) is str and re.fullmatch('sha256:[0-9a-f]{64}', identity))
    return identity

try:
    encoded = os.environ['LEAF_INSTANT_EXECUTION_REQUEST_B64']
    require(0 < len(encoded) <= 32768)
    raw = base64.b64decode(encoded, validate=True)
    require(base64.b64encode(raw).decode() == encoded)
    request_path = Path(os.environ['LEAF_FORGE_REQUEST_FILE'])
    require(request_path.is_absolute())
    require(bounded(request_path, 24576) == raw)
    request = json.loads(raw, object_pairs_hook=pairs)
    require(json.dumps(request, sort_keys=True, separators=(',', ':'), allow_nan=False).encode() == raw)
    keys(request, 'schema transaction_id source_revision source_tree source_snapshot executor_package authority_sha256 reservation_id')
    require(request['schema'] == 'leaf.native-instant-execution.request.v1')
    require(type(request['transaction_id']) is str and re.fullmatch('d10-[0-9a-f]{16}', request['transaction_id']))
    require(request['reservation_id'] == request['transaction_id'] + '-instant-execution')
    revision, tree = request['source_revision'], request['source_tree']
    hexval(revision, 40)
    hexval(tree, 40)
    hexval(request['authority_sha256'], 64)
    snapshot = request['source_snapshot']
    keys(snapshot, 'repository commit tree bucket key version_id zip_sha256 bundle_sha256')
    keys(snapshot['repository'], 'id full_name')
    require(type(snapshot['repository']['id']) is int and snapshot['repository']['id'] > 0)
    require(snapshot['repository']['full_name'] == 'LEAF-Solar-Design/leaf-web-demo')
    require(snapshot['commit'] == revision and snapshot['tree'] == tree)
    hexval(snapshot['zip_sha256'], 64)
    hexval(snapshot['bundle_sha256'], 64)
    package = request['executor_package']
    keys(package, 'bucket key version_id sha256')
    hexval(package['sha256'], 64)
    for descriptor in (snapshot, package):
        for field in ('bucket', 'key', 'version_id'):
            text(descriptor[field])
        require(re.fullmatch('[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', descriptor['bucket']))
        require('\\' not in descriptor['key'] and ':' not in descriptor['key'])
        require(all(part not in ('', '.', '..') for part in descriptor['key'].split('/')))
        require(descriptor['version_id'] != 'null')
    source = Path(os.environ['LEAF_FORGE_SOURCE_DIR'])
    output = Path(os.environ['LEAF_FORGE_OUTPUT_DIR'])
    require(source.is_absolute() and not source.is_symlink() and source.is_dir())
    require(output.is_absolute() and not output.is_symlink() and output.is_dir())
    source, output = source.resolve(strict=True), output.resolve(strict=True)
    require(not output.is_relative_to(source) and not source.is_relative_to(output))
    require(stat.S_IMODE(output.stat().st_mode) == 0o700 and next(output.iterdir(), None) is None)
    os.chdir(source)
    require(run(['git', 'rev-parse', '--show-toplevel'], 10, capture=True).strip() == str(source))
    require(run(['git', 'rev-parse', 'HEAD'], 10, capture=True).strip() == revision)
    require(run(['git', 'rev-parse', 'HEAD^{tree}'], 10, capture=True).strip() == tree)
    run(['git', 'status', '--porcelain', '--untracked-files=all'], 10)
    require((work / 'command.log').stat().st_size == 0)
    dockerfile = source / 'deploy/Dockerfile.instant-execution'
    require(dockerfile.is_file() and not dockerfile.is_symlink() and dockerfile.resolve().is_relative_to(source))
    uri = registry + '/leaf-platform-instant-execution:sha-' + revision
    run(['docker', 'buildx', 'build', '--load', '--file', 'deploy/Dockerfile.instant-execution',
         '--build-arg', 'LEAF_SOURCE_SHA=' + revision, '--tag', uri, '.'], 1050)
    identity = inspect(uri, revision)
    run(['docker', 'pull', scanner], 150)
    run(['docker', 'run', '--rm', '--volume', '/var/run/docker.sock:/var/run/docker.sock',
         '--volume', scratch + ':/work', scanner, 'image', '--cache-dir', '/work/cache',
         '--db-repository', 'public.ecr.aws/aquasecurity/trivy-db:2',
         '--java-db-repository', 'public.ecr.aws/aquasecurity/trivy-java-db:1',
         '--exit-code', '1', '--ignore-unfixed', '--severity', 'HIGH,CRITICAL',
         '--format', 'json', '--output', '/work/scan-report.json', uri], 540)
    report = bounded(work / 'scan-report.json', 16777216)
    require(type(json.loads(report, object_pairs_hook=pairs)) is dict)
    require(inspect(uri, revision) == identity)
    (work / 'login-attempted').write_bytes(b'1')
    run(['bash', '--noprofile', '--norc', '-o', 'pipefail', '-c',
         'timeout --kill-after=30s 60s aws ecr get-login-password --region us-east-1 | '
         'timeout --kill-after=30s 60s docker login --username AWS --password-stdin ' + registry + ' >/dev/null 2>&1'], 65)
    pushed = run(['docker', 'push', uri], 150, capture=True)
    digests = re.findall(r'^sha-' + revision + r': digest: (sha256:[0-9a-f]{64}) size: [1-9][0-9]*\s*$', pushed, re.MULTILINE)
    require(len(digests) == 1)
    result = dict(schema='leaf.native-instant-execution.producer.v1', source_revision=revision,
                  source_tree=tree, image_uri=uri, image_digest=digests[0], local_image_id=identity,
                  scan=dict(status='passed', scanner='trivy', scanner_version=version,
                            severity='HIGH,CRITICAL', ignore_unfixed=True,
                            report_sha256=hashlib.sha256(report).hexdigest()))
    payload = json.dumps(result, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    require(len(payload) <= 1048576 and next(output.iterdir(), None) is None)
    staged = work / 'producer-result.json'
    staged.write_bytes(payload)
    # Move only the completed receipt; scratch and scan bytes never enter output.
    import shutil
    destination = output / 'producer-result.json'
    try:
        shutil.move(str(staged), str(destination))
    except Exception:
        # A cross-filesystem move can leave a partial copy on an I/O failure.
        destination.unlink(missing_ok=True)
        raise
except Exception:
    print('Instant execution producer refused publication or evidence', file=sys.stderr)
    sys.exit(1)
PY
