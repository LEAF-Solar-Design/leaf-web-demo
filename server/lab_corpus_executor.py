"""Pinned, offline corpus producer for the app's existing durable job authority."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

SCHEMA = 'leaf.lab-corpus-job.v1'
OPERATION = 'internal.lab-corpus-evaluate'
RESULT_SCHEMA = 'leaf.lab-corpus-result.v1'
ARCHIVE_SHA256 = 'e3318b41811be1db14cc75a1a011c148e7fd7e077622fb09b9f68b6f189471b7'
ROOT = Path(__file__).resolve().parents[1]
SUPPLY = ROOT / 'engine/lab_corpus_evaluator.v1.json'
ARCHIVE = ROOT / 'engine/vendor/leafhft-9b9ced6f.tar.gz'
INSTALLED = ROOT / 'engine/lab-corpus-installed'
KERNEL_FILTER = Path('/usr/local/etc/leaf/seccomp-dwg2dxf.bpf')
MAX_RESULT_BYTES = 1024 * 1024
MAX_EVIDENCE_BYTES = MAX_RESULT_BYTES + 65536
POLICY_SCHEMA = 'leaf.lab-corpus-policy.v1'
# Sparse overrides preserve the selected parent's exact JSON numbers. Only
# fields implemented by the pinned offline suite are editable.
PARAMETERS_SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {
    'child_min_k': {'type': 'array', 'minItems': 1, 'maxItems': 16,
                    'items': {'type': 'integer', 'minimum': 0, 'maximum': 60}},
    'min_select_tickets': {'type': 'integer', 'minimum': 0, 'maximum': 6000000},
    'draws': {'type': 'integer', 'minimum': 1, 'maximum': 10000},
    'seed': {'type': 'integer', 'minimum': 0, 'maximum': 4294967295}}}
_OWNER = object()


class CorpusStopped(ValueError):
    """Raised only through the child's cleanup finally block."""


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _parameters(value):
    if not isinstance(value, dict) or set(value) - set(PARAMETERS_SCHEMA['properties']):
        raise ValueError('Invalid corpus policy parameters.')
    result = {}
    for name, item in value.items():
        schema = PARAMETERS_SCHEMA['properties'][name]
        items = item if name == 'child_min_k' else [item]
        if name == 'child_min_k':
            if not isinstance(item, list) or not 1 <= len(item) <= schema['maxItems']:
                raise ValueError('Invalid corpus policy parameters.')
            schema = schema['items']
        for number in items:
            if (type(number) not in (int, float) or not math.isfinite(number)
                    or not schema['minimum'] <= number <= schema['maximum'] or int(number) != number):
                raise ValueError('Invalid corpus policy parameters.')
        result[name] = [int(number) for number in items] if name == 'child_min_k' else int(item)
    return result


def compile_policy(authority, block_id, settings):
    """Compile only the admitted, immutable graph revision, never caller settings."""
    import lab_artifacts as ledger
    import lab_tool_manifests
    from lab_canonical import graph_digest
    from lab_evaluate import admit_graph, canonical_json
    artifact = ledger.get(authority['tenant_id'], authority['doc_id'], authority['graph_revision'])
    graph = artifact['graph']
    if artifact['sha256'] != authority['graph_sha256'] or graph_digest(graph) != authority['graph_sha256']:
        raise ValueError('Corpus graph lineage mismatch.')
    manifests = admit_graph(graph, lab_tool_manifests.registry_for(authority['tenant_id']))
    block = next((block for block in graph['blocks'] if block['id'] == block_id), None)
    if (block is None or manifests[block_id]['id'] != 'corpus-evaluator'
            or canonical_json(block['settings']) != canonical_json(settings)
            or block['settings']['project_id'] != authority['project_id']):
        raise ValueError('Corpus graph settings mismatch.')
    policy = {'schema': POLICY_SCHEMA, 'version': 1,
        'parent_suite_sha256': block['settings']['suite']['sha256'],
        'lineage': {key: authority[key] for key in ('tenant_id', 'project_id', 'doc_id',
                                                  'graph_revision', 'graph_sha256')},
        'block_id': block_id, 'parameters': _parameters(block['settings'].get('parameters', {}))}
    return dict(policy, digest=_hash(_json_bytes(policy)))


def validate_policy(policy):
    if (type(policy) is not dict or set(policy) != {'schema', 'version', 'parent_suite_sha256',
            'lineage', 'block_id', 'parameters', 'digest'}
            or policy['schema'] != POLICY_SCHEMA or type(policy['version']) is not int or policy['version'] != 1):
        raise ValueError('Invalid compiled corpus policy.')
    lineage = policy['lineage']
    if (type(lineage) is not dict or set(lineage) != {'tenant_id', 'project_id', 'doc_id',
            'graph_revision', 'graph_sha256'} or type(lineage['graph_revision']) is not int
            or lineage['graph_revision'] < 1 or any(type(lineage[key]) is not str
                or not 1 <= len(lineage[key]) <= 200 for key in ('tenant_id', 'project_id', 'doc_id'))
            or type(policy['block_id']) is not str or not 1 <= len(policy['block_id']) <= 64):
        raise ValueError('Invalid corpus policy lineage.')
    for digest in (policy['parent_suite_sha256'], lineage['graph_sha256'], policy['digest']):
        if type(digest) is not str or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('Invalid corpus policy digest.')
    if (_parameters(policy['parameters']) != policy['parameters']
            or _hash(_json_bytes({key: value for key, value in policy.items() if key != 'digest'})) != policy['digest']):
        raise ValueError('Corpus policy digest mismatch.')
    return json.loads(_json_bytes(policy))


def compiled_suite(raw, policy):
    """Materialize the registered suite with sparse edits, preserving its parent policy."""
    policy = validate_policy(policy)
    if _hash(raw) != policy['parent_suite_sha256']:
        raise ValueError('Corpus policy parent mismatch.')
    suite = json.loads(raw)
    suite.update(policy['parameters'])
    ks = suite.get('child_min_k')
    parent_k = suite.get('parent_policy', {}).get('min_k')
    if (not isinstance(ks, list) or not ks
            or any(type(k) is not int or k < 0 for k in ks)
            or (parent_k is not None and (type(parent_k) is not int or any(k >= parent_k for k in ks)))):
        raise ValueError('Corpus child policy must be below its parent min_k.')
    if len(ks) * sum(len(grid) for grid in suite.get('blocks', {}).values()) > 128:
        raise ValueError('Corpus policy candidate budget exceeded.')
    return _json_bytes(suite)


def submit_policy(context, *, idempotency_key):
    """Use the existing durable operation and custody checks with the compiled policy attached."""
    import jobs
    if (type(idempotency_key) is not str or not 1 <= len(idempotency_key) <= 200
            or any(ord(c) < 32 or ord(c) == 127 for c in idempotency_key)):
        raise ValueError('Corpus execution requires an idempotency key.')
    context = validate_context(context)
    require_executor()
    sources = read_sources(context)
    compiled_suite(sources['suite'], context['policy'])
    return jobs._submit_job(context['tenant_id'], {}, {}, '', False,
        context['org_id'], context['project_id'], idempotency_key=idempotency_key, lab_corpus=context)


def capability():
    manifest = json.loads(SUPPLY.read_bytes())
    if (manifest.get('schema') != 'leaf.lab-corpus-capability.v1'
            or manifest.get('archive_sha256') != ARCHIVE_SHA256
            or manifest.get('entry') != 'scripts/blocks_lab/suite.py'):
        raise ValueError('Corpus capability is unavailable.')
    return manifest


def install(destination=INSTALLED):
    """Image build producer: copy only digest-pinned Python modules, never data."""
    manifest = capability()
    if _hash(ARCHIVE.read_bytes()) != ARCHIVE_SHA256:
        raise ValueError('Corpus package identity mismatch.')
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(ARCHIVE, 'r:gz') as archive:
        for name, digest in manifest['files'].items():
            path = Path(name)
            if (path.is_absolute() or '..' in path.parts or path.suffix != '.py'
                    or path.parts[0] not in {'leafhft', 'scripts'}):
                raise ValueError('Invalid corpus package member.')
            member = archive.getmember(name)
            if not member.isfile():
                raise ValueError('Invalid corpus package member.')
            raw = archive.extractfile(member).read()
            if _hash(raw) != digest:
                raise ValueError('Corpus module identity mismatch.')
            target = destination / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
    return destination


def require_executor():
    manifest = capability()
    for name, digest in manifest['files'].items():
        path = INSTALLED / name
        if path.is_symlink() or _hash(path.read_bytes()) != digest:
            raise ValueError('Installed corpus module identity mismatch.')
    return manifest


def validate_context(value):
    fields = {'schema', 'tenant_id', 'org_id', 'project_id', 'actor_id',
              'capability_sha256', 'corpus', 'suite'}
    if (type(value) is not dict or set(value) not in (fields, fields | {'policy'})
            or value['schema'] != SCHEMA):
        raise ValueError('Invalid corpus context.')
    for key in ('tenant_id', 'org_id', 'project_id', 'actor_id'):
        if type(value[key]) is not str or str(uuid.UUID(value[key])) != value[key]:
            raise ValueError('Invalid corpus scope.')
    if value['tenant_id'] != value['org_id'] or value['capability_sha256'] != ARCHIVE_SHA256:
        raise ValueError('Corpus scope or capability mismatch.')
    for key in ('corpus', 'suite'):
        descriptor = value[key]
        if type(descriptor) is not dict or set(descriptor) != {'material_id', 'sha256', 'byte_size'}:
            raise ValueError('Invalid corpus material descriptor.')
        if (type(descriptor['material_id']) is not str
                or str(uuid.UUID(descriptor['material_id'])) != descriptor['material_id']
                or type(descriptor['sha256']) is not str
                or not re.fullmatch('[0-9a-f]{64}', descriptor['sha256'])
                or type(descriptor['byte_size']) is not int
                or not 0 < descriptor['byte_size'] <= (64 if key == 'corpus' else 8) * 1024 * 1024):
            raise ValueError('Invalid corpus material identity.')
    if 'policy' in value:
        policy = validate_policy(value['policy'])
        if (policy['parent_suite_sha256'] != value['suite']['sha256']
                or any(policy['lineage'][key] != value[key] for key in ('tenant_id', 'project_id'))):
            raise ValueError('Corpus policy scope mismatch.')
    return json.loads(json.dumps(value))


def record_context(execution, record):
    if type(execution) is not dict:
        raise ValueError('Invalid corpus execution.')
    if 'lab_corpus' not in execution and execution.get('operation') != OPERATION:
        if record.get('tool') == OPERATION:
            raise ValueError('Missing corpus execution.')
        return None
    if set(execution) != {'operation', 'lab_corpus'} or execution['operation'] != OPERATION:
        raise ValueError('Conflicting corpus execution.')
    context = validate_context(execution['lab_corpus'])
    if record.get('tool') != OPERATION or any(record.get(k) != context[k]
            for k in ('tenant_id', 'org_id', 'project_id')):
        raise ValueError('Durable corpus identity mismatch.')
    return context


def read_sources(context):
    """Resolve actual metadata and membership before reading private originals."""
    context = validate_context(context)
    import platform_link
    import project_reference_materials as originals
    platform_link._ensure_platform_package()
    from leaf_platform import project_reference_materials as metadata
    result = {}
    for key, media_type in (('corpus', 'application/gzip'), ('suite', 'application/json')):
        descriptor = context[key]
        material = metadata.get_material(context['org_id'], context['project_id'],
                                         context['actor_id'], descriptor['material_id'])
        if (any(material[k] != descriptor[k] for k in descriptor)
                or material['org_id'] != context['org_id']
                or material['project_id'] != context['project_id']
                or material['media_type'] != media_type):
            raise ValueError('Corpus material custody mismatch.')
        raw = originals.read_content(context['org_id'], context['project_id'], material)
        originals.validate(material['name'], media_type, raw)
        result[key] = raw
    return result


def guard(heartbeat, cancelled, deadline):
    if (not math.isfinite(deadline) or time.monotonic() >= deadline
            or cancelled() or not heartbeat() or cancelled()):
        raise ValueError('Corpus attempt lost its lease or execution budget.')


def _command(stage):
    argv = [sys.executable, '-I', str(Path(__file__).resolve()), '--child',
            str(INSTALLED.resolve()), str(stage.resolve())]
    if sys.platform == 'darwin':
        # Seatbelt is a kernel network denial; Python's audit hook further
        # restricts file reads, writes, process creation and native escape.
        return ['/usr/bin/sandbox-exec', '-p', '(version 1)(allow default)(deny network*)', *argv]
    if sys.platform == 'linux':
        if not KERNEL_FILTER.is_file():
            raise ValueError('Corpus kernel network isolation is unavailable.')
        status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines()
                      if ':' in line)
        # An unprivileged app cannot change its bounding set. Require that it
        # holds no usable/inheritable capabilities, then seal exec with NNP.
        # A privileged launcher is refused rather than silently retaining power.
        if os.geteuid() == 0 or any(int(status.get(key, '1').strip(), 16) != 0
                for key in ('CapEff', 'CapPrm', 'CapInh', 'CapAmb')):
            raise ValueError('Corpus execution requires an unprivileged app identity.')
        return ['/usr/bin/setpriv', '--no-new-privs',
                '--seccomp-filter', str(KERNEL_FILTER), *argv]
    raise ValueError('Corpus isolated runtime is unavailable.')


def _terminate(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


def run(job_id, worker_id, attempt, context, heartbeat, cancelled, deadline):
    context = validate_context(context)
    require_executor()
    guard(heartbeat, cancelled, deadline)
    sources = read_sources(context)
    guard(heartbeat, cancelled, deadline)
    with tempfile.TemporaryDirectory(prefix='leaf-lab-corpus-') as directory:
        stage = Path(directory)
        os.chmod(stage, 0o700)
        (stage / 'rows.jsonl.gz').write_bytes(sources.pop('corpus'))
        (stage / 'suite.json').write_bytes(sources.pop('suite'))
        evaluated_suite_sha256 = context['suite']['sha256']
        if 'policy' in context:
            evaluated_suite_sha256 = _hash(compiled_suite((stage / 'suite.json').read_bytes(), context['policy']))
            (stage / 'policy.json').write_bytes(_json_bytes(context['policy']))
        process = subprocess.Popen(_command(stage), cwd=stage, env={
            'PATH': '/usr/bin:/bin', 'HOME': str(stage), 'TMPDIR': str(stage),
            'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1',
            'PYTHONDONTWRITEBYTECODE': '1'}, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            checked_at = float('-inf')
            while process.poll() is None:
                now = time.monotonic()
                if not math.isfinite(deadline) or now >= deadline:
                    raise ValueError('Corpus attempt lost its lease or execution budget.')
                if now - checked_at >= 1:
                    guard(heartbeat, cancelled, deadline)
                    checked_at = time.monotonic()
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            guard(heartbeat, cancelled, deadline)
            output = stage / 'result.json'
            if process.returncode != 0 or not output.is_file() or output.stat().st_size > MAX_RESULT_BYTES:
                raise ValueError('Pinned corpus evaluator did not produce a bounded result.')
            raw = output.read_bytes()
            result = json.loads(raw)
            count = json.loads((stage / 'count.json').read_bytes())
            if (result.get('schema') != 'leafhft.block-suite-result.v1'
                    or result.get('suite_sha256') != evaluated_suite_sha256
                    or ('policy' in context and result.get('policy_digest') != context['policy']['digest'])
                    or type(count) is not int or not 0 < count <= 6_000_000):
                raise ValueError('Corpus result identity mismatch.')
            read_sources(context)
            guard(heartbeat, cancelled, deadline)
            return {'schema': RESULT_SCHEMA, 'job_id': job_id, 'worker_id': worker_id,
                    'attempt': attempt, 'context': context, 'row_count': count,
                    'evaluated_suite_sha256': evaluated_suite_sha256,
                    'rows_sha256': result['rows_sha256'], 'result_sha256': _hash(raw),
                    'result': result}
        except Exception as exc:
            raise CorpusStopped(str(exc)) from None
        finally:
            # Reap the entire private process group even after the group leader
            # exits. A result is never published while descendants can survive.
            _terminate(process)


def validate_result(context, job_id, worker_id, attempt, result):
    context = validate_context(context)
    expected_suite = (context['suite']['sha256'] if 'policy' not in context else
        _hash(compiled_suite(read_sources(context)['suite'], context['policy'])))
    if (type(result) is not dict or result.get('schema') != RESULT_SCHEMA
            or result.get('context') != context
            or result.get('job_id') != job_id or result.get('worker_id') != worker_id
            or type(result.get('attempt')) is not int or result['attempt'] != attempt
            or type(result.get('row_count')) is not int or not 0 < result['row_count'] <= 6_000_000
            or type(result.get('result')) is not dict
            or result.get('result', {}).get('schema') != 'leafhft.block-suite-result.v1'
            or not {'candidates', 'chosen', 'confirm_report', 'verdict', 'reasons'} <= set(result['result'])
            or result['result'].get('suite_sha256') != expected_suite
            or result.get('evaluated_suite_sha256', expected_suite) != expected_suite
            or result.get('rows_sha256') != result['result'].get('rows_sha256')
            or ('policy' in context and result['result'].get('policy_digest') != context['policy']['digest'])
            or len(json.dumps(result, allow_nan=False).encode()) > MAX_EVIDENCE_BYTES):
        raise ValueError('Corpus terminal identity mismatch.')


def _child(package, directory):
    """Private fixed entry, not a client-selectable command or general runner."""
    import resource
    package, stage = Path(package).resolve(), Path(directory).resolve()
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (600, 600))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_RESULT_BYTES, MAX_RESULT_BYTES))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    if sys.platform == 'linux':
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        memory_limit = min(4 * 1024**3, hard) if hard != resource.RLIM_INFINITY else 4 * 1024**3
        resource.setrlimit(resource.RLIMIT_AS, (memory_limit, hard))
    # Bootstrap the image's trusted numerical dependency while already inside
    # the kernel cage and scrubbed environment. NumPy initializes ctypes' own
    # Python API handle on Linux. Deny all further native loads before any
    # corpus/suite data or evaluator module is read.
    import numpy  # noqa: F401
    os.umask(0o077)
    sys.path.insert(0, str(package))
    roots = (package, stage, Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve())

    def audit(event, args):
        if (event.startswith(('socket.', 'subprocess.', 'os.exec', 'os.spawn', 'os.posix_spawn'))
                or event in {'os.fork', 'os.forkpty', 'ctypes.dlopen', 'ctypes.dlsym'}):
            raise PermissionError('Corpus execution operation refused.')
        if event == 'open' and not isinstance(args[0], int):
            path = Path(os.fsdecode(args[0])).resolve()
            mode, flags = args[1], args[2]
            write = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or bool(
                flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if (write and path not in {stage / 'result.json', stage / 'count.json'}) or (not write and not any(
                    path == root or root in path.parents for root in roots)):
                raise PermissionError('Corpus file access refused.')

    sys.addaudithook(audit)
    # Keep the pinned suite validation and evaluator. The source suite remains
    # intact; its compiled derivative has a separate identity in the report.
    from scripts.blocks_lab.suite import main, load_suite, run_suite
    policy_path = stage / 'policy.json'
    if policy_path.is_file():
        from scripts.block_verdict import _read_rows
        policy = validate_policy(json.loads(policy_path.read_bytes()))
        raw = compiled_suite((stage / 'suite.json').read_bytes(), policy)
        suite = load_suite(stage / 'suite.json')
        suite.clear()
        suite.update(json.loads(raw))
        suite.sha256 = _hash(raw)
        result = run_suite(_read_rows(stage / 'rows.jsonl.gz'), suite)
        result['policy_digest'] = policy['digest']
        (stage / 'result.json').write_bytes(_json_bytes(result) + b'\n')
        code = 0
    else:
        code = main(['--suite', str(stage / 'suite.json'), '--rows', str(stage / 'rows.jsonl.gz'),
                     '--out', str(stage / 'result.json')])
    if code == 0:
        import gzip
        with gzip.open(stage / 'rows.jsonl.gz', 'rb') as rows:
            count = sum(1 for _ in rows)
        (stage / 'count.json').write_text(json.dumps(count))
    return code


if __name__ == '__main__':
    if sys.argv[1:] == ['--install']:
        install()
    elif len(sys.argv) == 4 and sys.argv[1] == '--child':
        raise SystemExit(_child(sys.argv[2], sys.argv[3]))
    else:
        raise SystemExit(2)
