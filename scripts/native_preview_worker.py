"""One authenticated F22 job. No credential acquisition or worker activation."""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import io
import json
import math
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
import zlib

ORIGIN = 'https://platform-staging.leafdesign.ai'
USER_AGENT = 'leaf-native-preview-worker/1'
MAX_PNG = 16 * 1024 * 1024
MAX_BODY = 4 * ((MAX_PNG + 2) // 3) + 17408
SAFE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
SHA = re.compile(r'[0-9a-f]{64}\Z')
ERROR_CODES = frozenset({
    'invalid-protocol', 'invalid-identity', 'invalid-json', 'source-integrity',
    'unsafe-archive', 'source-bounds', 'document-integrity', 'producer-conflict',
    'source-unavailable', 'stdin-required', 'invalid-auth', 'invalid-origin',
    'request-bounds', 'response-bounds', 'http-denied', 'http-unavailable',
    'authority-lost', 'heartbeat-drift', 'invalid-screenshot', 'invalid-native-facts',
    'native-failed', 'cleanup-unconfirmed', 'screenshot-integrity', 'screenshot-readback',
    'completion-unknown', 'windows-worker-required', 'invalid-granted-simulator',
    'invalid-ticket', 'mac-transport-failed', 'invalid-remote-clock',
    'screenshot-bounds', 'cleanup-release-unconfirmed',
    'journal-failed',
    'mac-permit-expired', 'mac-producer-failed', 'mac-extinction-unconfirmed',
})
AUTHORITY_FAILURES = frozenset({'none', 'heartbeat-request', 'heartbeat-contract',
                              'permit-refresh', 'deadline', 'ack-expired',
                              'allocator-lost', 'unexpected'})
EXECUTION_STAGES = frozenset({'source', 'mac-bootstrap', 'mac-permit-refresh',
                              'mac-producer', 'evidence', 'completion'})


class WorkerError(RuntimeError):
    """Only fixed safe diagnostic codes cross the CLI boundary."""


def error_code(error):
    if (type(error) is WorkerError and len(error.args) == 1
            and type(error.args[0]) is str and error.args[0] in ERROR_CODES):
        return error.args[0]
    return 'unexpected'


def require(condition, code='invalid-protocol'):
    if not condition:
        raise WorkerError(code)


def canonical_uuid(value):
    try:
        require(type(value) is str and str(uuid.UUID(value)) == value)
    except (ValueError, AttributeError):
        raise WorkerError('invalid-identity') from None
    return value


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def parse(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result)
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: require(False))
    except (ValueError, UnicodeError, RecursionError):
        raise WorkerError('invalid-json') from None


def producer():
    path = Path(__file__).resolve().parents[1] / 'platform/portfolio_ios_template.py'
    spec = importlib.util.spec_from_file_location('_native_preview_producer', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def binding(value):
    require(type(value) is dict and set(value) == {'org_id', 'project_id', 'actor_id', 'worker_id'})
    for key in ('org_id', 'project_id', 'actor_id'):
        canonical_uuid(value[key])
    require(type(value['worker_id']) is str and SAFE.fullmatch(value['worker_id']))
    return value.copy()


def validate_claim(value, scope, now):
    require(type(value) is dict and set(value) == {'job'})
    job = value['job']
    if job is None:
        return None
    require(type(job) is dict and set(job) == {'job_id', 'lease_owner', 'attempt', 'descriptor', 'deadline'})
    canonical_uuid(job['job_id'])
    canonical_uuid(job['lease_owner'])
    require(type(job['attempt']) is int and 1 <= job['attempt'] <= 2147483647)
    require(type(job['deadline']) in (int, float) and math.isfinite(job['deadline'])
            and now < job['deadline'] <= now + 3600)
    d = job['descriptor']
    if type(d) is dict and d.get('schema') == NATIVE_SCHEMA:
        try:
            native_descriptor(d, scope['org_id'], scope['project_id'])
        except (ValueError, TypeError, KeyError):
            raise WorkerError('invalid-protocol') from None
        return job
    require(type(d) is dict and set(d) == {'schema', 'target', 'org_id', 'project_id',
                                         'revision', 'document_sha256', 'archive_sha256', 'byte_size'})
    require(d['schema'] == 'leaf.project-preview-source.v1' and d['target'] == 'ios-simulator')
    require(all(d[k] == scope[k] for k in ('org_id', 'project_id')))
    require(type(d['revision']) is int and 1 <= d['revision'] <= 2147483647)
    require(all(type(d[k]) is str and SHA.fullmatch(d[k]) for k in ('document_sha256', 'archive_sha256')))
    require(type(d['byte_size']) is int and 0 < d['byte_size'] <= producer().MAX_ARCHIVE_BYTES)
    return job


def verify_source(raw, descriptor):
    """Treat even authenticated ZIP entries as hostile until exact regeneration."""
    if descriptor.get('schema') == NATIVE_SCHEMA:
        try:
            verify_native_archive(raw, descriptor)
            return raw
        except (ValueError, TypeError, KeyError, OSError, zipfile.BadZipFile, UnicodeError):
            raise WorkerError('source-integrity') from None
    template = producer()
    require(type(raw) is bytes and len(raw) == descriptor['byte_size']
            and len(raw) <= template.MAX_ARCHIVE_BYTES
            and hashlib.sha256(raw).hexdigest() == descriptor['archive_sha256'], 'source-integrity')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            require(1 <= len(entries) <= 250 and len({i.filename for i in entries}) == len(entries), 'unsafe-archive')
            require(sum(i.file_size for i in entries) <= template.MAX_ARCHIVE_BYTES, 'unsafe-archive')
            for entry in entries:
                path = PurePosixPath(entry.filename)
                require(not path.is_absolute() and '..' not in path.parts
                        and '\\' not in entry.filename and ':' not in entry.filename
                        and not entry.is_dir() and not entry.flag_bits & 1
                        and stat.S_IFMT(entry.external_attr >> 16) in (0, stat.S_IFREG), 'unsafe-archive')
            doc_entry = archive.getinfo('Portfolio/PortfolioContent/portfolio.json')
            require(doc_entry.file_size <= template.MAX_DOCUMENT_BYTES + 1, 'source-bounds')
            document = template.validate_document(parse(archive.read(doc_entry)))
            assets = {image['material_id']: archive.read('Portfolio/PortfolioContent/images/' + image['material_id'] + '.jpg')
                      for category in document['categories'] for image in category['images']}
            require(template.document_digest(document) == descriptor['document_sha256'], 'document-integrity')
            require(template.source_archive(document, descriptor['revision'], assets) == raw, 'producer-conflict')
    except WorkerError:
        raise
    except Exception:
        raise WorkerError('source-unavailable') from None
    return raw


def read_bearer(stream):
    require(not stream.isatty(), 'stdin-required')
    raw = stream.read(4098)
    if type(raw) is bytes:
        try:
            raw = raw.decode('ascii')
        except UnicodeError:
            raise WorkerError('invalid-auth') from None
    # A credential pipe may append one newline; no embedded whitespace/controls.
    token = raw[:-1] if raw.endswith('\n') else raw
    require(1 <= len(token) <= 4096 and all(33 <= ord(c) <= 126 for c in token), 'invalid-auth')
    return token


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Http:
    def __init__(self, scope, token, origin=ORIGIN, opener=None):
        require(origin == ORIGIN, 'invalid-origin')
        self.prefix = origin + '/api/projects/' + binding(scope)['project_id'] + '/native-previews/worker/'
        self._token = token
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def status(self, job_id: str, limit: int = 16384) -> bytes:
        canonical_uuid(job_id)
        require(type(limit) is int and 0 < limit <= 16384, 'response-bounds')
        url = self.prefix.removesuffix('worker/') + job_id
        request = urllib.request.Request(url, method='GET', headers={
            'Authorization': 'Bearer ' + self._token, 'Content-Type': 'application/json',
            'User-Agent': USER_AGENT})
        try:
            with self.opener.open(request, timeout=10) as response:
                require(response.status == 200 and response.geturl() == url, 'http-denied')
                raw = response.read(limit + 1)
                require(type(raw) is bytes and len(raw) <= limit, 'response-bounds')
                return raw
        except WorkerError:
            raise
        except Exception:
            raise WorkerError('http-unavailable') from None

    def request(self, method, suffix, body=None, limit=16384):
        data = None if body is None else encode(body)
        require(data is None or len(data) <= MAX_BODY, 'request-bounds')
        require(0 < limit <= producer().MAX_ARCHIVE_BYTES, 'response-bounds')
        request = urllib.request.Request(self.prefix + suffix, data=data, method=method,
            headers={'Authorization': 'Bearer ' + self._token, 'Content-Type': 'application/json',
                     'User-Agent': USER_AGENT})
        try:
            with self.opener.open(request, timeout=10) as response:
                require(response.status == 200 and response.geturl() == request.full_url, 'http-denied')
                raw = response.read(limit + 1)
                require(len(raw) <= limit, 'response-bounds')
                return raw
        except WorkerError:
            raise
        except Exception:
            raise WorkerError('http-unavailable') from None


class Authority:
    """Sticky cancellation, fixed deadline, fresh acknowledgments only."""
    def __init__(self, http, job, *, clock=time.time, monotonic=time.monotonic, journal=None):
        self.http, self.job, self.clock, self.monotonic = http, job, clock, monotonic
        self.deadline = job['deadline']
        self.end = monotonic() + self.deadline - clock()
        self.ack = float('-inf')
        self.cancelled = threading.Event()
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.refresh = None
        self.held = None
        self.thread = None
        self.authority_failure = 'none'
        self.execution_stage = 'source'
        self.journal = journal
        self.journal_failed = False

    def record(self, event):
        if self.journal is not None:
            try:
                self.journal(event)
            except BaseException:
                self.journal_failed = True
                self.fail('unexpected')
                raise WorkerError('journal-failed') from None

    def fail(self, cause):
        with self.lock:
            if self.authority_failure == 'none':
                self.authority_failure = cause if cause in AUTHORITY_FAILURES - {'none'} else 'unexpected'
            self.cancelled.set()

    def stage(self, stage):
        require(stage in EXECUTION_STAGES)
        # Only the foreground advances this; background refreshes never do.
        self.execution_stage = stage

    @property
    def identity(self):
        return {k: self.job[k] for k in ('lease_owner', 'attempt')}

    def check(self):
        require(not self.journal_failed, 'journal-failed')
        cause = None
        if self.cancelled.is_set():
            cause = 'unexpected'
        elif self.clock() >= self.deadline or self.monotonic() >= self.end:
            cause = 'deadline'
        elif self.monotonic() - self.ack >= 12:
            cause = 'ack-expired'
        elif self.held is not None and self.held.lost:
            cause = 'allocator-lost'
        if cause is not None:
            self.fail(cause)
            raise WorkerError('authority-lost')

    def beat(self):
        with self.lock:
            cause = 'unexpected'
            try:
                active = (not self.cancelled.is_set() and self.clock() < self.deadline
                          and self.monotonic() < self.end)
                if not active and not self.cancelled.is_set():
                    cause = 'deadline'
                require(active, 'authority-lost')
                sent = self.monotonic()
                cause = 'heartbeat-request'
                raw = self.http.request('POST', self.job['job_id'] + '/heartbeat', self.identity)
                cause = 'heartbeat-contract'
                reply = parse(raw)
                expected = {**self.identity, 'job_id': self.job['job_id'], 'deadline': self.deadline}
                require(type(reply) is dict and set(reply) == set(expected)
                        and all(type(reply[k]) is type(v) and reply[k] == v for k, v in expected.items()), 'heartbeat-drift')
                # Start validity at request send, not receipt; delayed replies cannot renew it.
                self.ack = sent
                cause = 'unexpected'
                self.check()
                if self.refresh is not None:
                    cause = 'permit-refresh'
                    self.refresh(min(self.ack + 10, self.end))
                cause = 'unexpected'
                self.check()
            except BaseException:
                self.fail(cause)
                raise

    def start(self):
        self.beat()
        def beats():
            while not self.stop.wait(max(0, self.ack + 2 - self.monotonic())):
                try:
                    self.beat()
                except BaseException:
                    return
        self.thread = threading.Thread(target=beats, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=11)

    def sleep(self, seconds):
        end = self.monotonic() + seconds
        while self.monotonic() < end:
            self.check()
            self.cancelled.wait(min(.1, end - self.monotonic()))
        self.check()


def validate_png(raw):
    require(type(raw) is bytes and 33 <= len(raw) <= MAX_PNG and raw[:16] == b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR', 'invalid-screenshot')
    width, height = struct.unpack('>II', raw[16:24])
    require(0 < width <= 16384 and 0 < height <= 16384 and width * height <= 67108864
            and zlib.crc32(raw[12:29]) & 0xffffffff == struct.unpack('>I', raw[29:33])[0], 'invalid-screenshot')
    return raw


def make_receipt(job, scope, facts, image):
    keys = {'simulator_udid', 'simulator_runtime', 'bundle_id', 'allocator_ticket',
            'build_exit_code', 'install_exit_code', 'launch_exit_code',
            'cleanup_complete', 'allocator_release_confirmed', 'screenshot_sha256'}
    native_source = job['descriptor'].get('schema') == NATIVE_SCHEMA
    if native_source:
        keys.add('build_artifact')
    require(type(facts) is dict and set(facts) == keys, 'invalid-native-facts')
    if native_source:
        try:
            validate_native_artifact(facts['build_artifact'], job['descriptor'])
        except (ValueError, TypeError, KeyError):
            raise WorkerError('invalid-native-facts') from None
        require(facts.get('bundle_id') == facts['build_artifact']['bundle_id'], 'invalid-native-facts')
    canonical_uuid(facts['simulator_udid'])
    for key in ('simulator_runtime', 'bundle_id', 'allocator_ticket'):
        require(type(facts[key]) is str and SAFE.fullmatch(facts[key]), 'invalid-native-facts')
    for key in ('build_exit_code', 'install_exit_code', 'launch_exit_code'):
        require(type(facts[key]) is int and facts[key] == 0, 'native-failed')
    require(facts['cleanup_complete'] is True and facts['allocator_release_confirmed'] is True, 'cleanup-unconfirmed')
    validate_png(image)
    require(facts['screenshot_sha256'] == hashlib.sha256(image).hexdigest(), 'screenshot-integrity')
    return {'schema': 'leaf.native-preview-worker-receipt.v1',
            **{k: job[k] for k in ('job_id', 'lease_owner', 'attempt')},
            'archive_sha256': job['descriptor']['archive_sha256'], 'worker_binding': scope,
            'target': 'ios-simulator', **facts}


def run_once(scope, http, native, output, *, clock=time.time, journal=None):
    scope = binding(scope)
    job = validate_claim(parse(http.request('POST', 'claim', {})), scope, clock())
    if job is None:
        return {'stage': 'empty', 'result': 'empty'}
    guard = Authority(http, job, clock=clock, journal=journal)
    settling = False
    failure_settlement = 'not-attempted-authority-lost'
    try:
        guard.record({'event': 'claim', 'job_id': job['job_id'], 'attempt': job['attempt'],
                      'source': native_identity(job['descriptor']),
                      'deadline': job['deadline']})
        guard.start()
        raw = http.request('GET', job['job_id'] + '/source?' + urllib.parse.urlencode(guard.identity),
                           limit=producer().MAX_ARCHIVE_BYTES)
        guard.check()
        verify_source(raw, job['descriptor'])
        guard.check()
        facts, image = native(raw, job, guard)
        guard.stage('evidence')
        guard.check()
        receipt = make_receipt(job, scope, facts, image)
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        artifact = output / (job['job_id'] + '-' + str(job['attempt']) + '.png')
        with artifact.open('xb') as stream:
            stream.write(image)
        require(artifact.read_bytes() == image, 'screenshot-readback')
        guard.stage('completion')
        guard.beat()
        guard.check()
        settling = True
        failure_settlement = 'request-failed'
        raw = http.request('POST', job['job_id'] + '/complete',
            {**guard.identity, 'outcome': 'complete', 'receipt': receipt,
             'screenshot_base64': base64.b64encode(image).decode('ascii')})
        failure_settlement = 'response-unconfirmed'
        reply = parse(raw)
        require(reply == {'job_id': job['job_id'], 'status': 'complete'}, 'completion-unknown')
        return {'stage': 'settled', 'result': 'complete', 'job_id': job['job_id'],
                'archive_sha256': job['descriptor']['archive_sha256'], 'screenshot_sha256': facts['screenshot_sha256']}
    except BaseException as error:
        failure_stage = guard.execution_stage
        code = error_code(error)
        if not settling:
            try:
                guard.beat()
                guard.check()
                failure_settlement = 'request-failed'
                raw = http.request('POST', job['job_id'] + '/complete', {**guard.identity, 'outcome': 'failed'})
                failure_settlement = 'response-unconfirmed'
                reply = parse(raw)
                if (type(reply) is dict and set(reply) == {'job_id', 'status'}
                        and type(reply['job_id']) is str and reply['job_id'] == job['job_id']
                        and type(reply['status']) is str and reply['status'] == 'failed'):
                    failure_settlement = 'accepted'
            except BaseException:
                pass
        result = {'stage': 'completion' if settling else 'execution',
                  'result': 'unknown' if settling else 'failed', 'job_id': job['job_id'],
                  'archive_sha256': job['descriptor']['archive_sha256']}
        try:
            report = getattr(native, 'report', {})
            local = {**result, 'execution_stage': failure_stage, 'error_code': code,
                     'authority_failure': guard.authority_failure,
                     'failure_settlement': failure_settlement,
                     **{k: report[k] for k in ('cleanup_complete', 'allocator_release_confirmed')
                                 if type(report.get(k)) is bool}}
            if job['descriptor'].get('schema') == NATIVE_SCHEMA and type(report.get('build_artifact')) is dict:
                validate_native_artifact(report['build_artifact'], job['descriptor'])
                local['build_artifact'] = report['build_artifact']
            Path(output).mkdir(parents=True, exist_ok=True)
            with (Path(output) / (job['job_id'] + '-' + str(job['attempt']) + '-failure.json')).open('x') as stream:
                json.dump(local, stream)
        except Exception:
            pass
        return result
    finally:
        guard.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('project-id', 'org-id', 'actor-id', 'worker-id', 'output'):
        parser.add_argument('--' + key, required=True)
    parser.add_argument('--origin', default=ORIGIN)
    args = parser.parse_args(argv)
    try:
        scope = binding({k: getattr(args, k) for k in ('project_id', 'org_id', 'actor_id', 'worker_id')})
        require(args.origin == ORIGIN, 'invalid-origin')
        token = read_bearer(sys.stdin)
        http = Http(scope, token, args.origin)
        from native_preview_mac import MacAdapter
        result = run_once(scope, http, MacAdapter(), args.output)
    except BaseException:
        result = {'stage': 'worker', 'result': 'unavailable'}
    print(json.dumps(result))
    return 0 if result['result'] in ('empty', 'complete') else 1




# Captured from nativeProjectSourceProfile.ts. Tests bind these bytes to that producer.
NATIVE_SCHEMA = 'leaf.project-repository-preview-source.v1'
NATIVE_PROFILE = 'ios-swiftui-xcodegen-v1'
NATIVE_LIMIT = 16 * 1024 * 1024
NATIVE_MANIFEST = '.leaf-native-sdk-manifest.json'
NATIVE_FIELDS = frozenset(('schema', 'target', 'source_profile', 'org_id', 'project_id',
    'actor_binding_id', 'repo_key', 'source_request_id', 'source_operation', 'source_version',
    'receipt_digest', 'source_commit', 'source_tree', 'material_refs', 'bundle_sha256',
    'archive_sha256', 'byte_size'))
NATIVE_APP_SOURCE = 'import SwiftUI\n\n@main\nstruct LeafProjectApp: App {\n    var body: some Scene {\n        WindowGroup {\n            ContentView()\n        }\n    }\n}\n'
NATIVE_PROJECT_SPEC = 'name: LeafProject\noptions:\n  deploymentTarget:\n    iOS: "17.0"\nsettings:\n  base:\n    SWIFT_VERSION: "5.0"\n    IPHONEOS_DEPLOYMENT_TARGET: "17.0"\n    SDKROOT: iphonesimulator\n    SUPPORTED_PLATFORMS: iphonesimulator\n    SUPPORTS_MACCATALYST: NO\n    CODE_SIGNING_ALLOWED: NO\n    CODE_SIGNING_REQUIRED: NO\ntargets:\n  LeafProject:\n    type: application\n    platform: iOS\n    sources:\n      - path: Sources\n      - path: Resources/Input\n        type: folder\n        buildPhase: resources\n        optional: true\n    settings:\n      base:\n        PRODUCT_BUNDLE_IDENTIFIER: ${bundleId}\n        PRODUCT_NAME: LeafProject\n        GENERATE_INFOPLIST_FILE: YES\n        INFOPLIST_KEY_UILaunchScreen_Generation: YES\n        TARGETED_DEVICE_FAMILY: "1,2"\n        MARKETING_VERSION: "1.0"\n        CURRENT_PROJECT_VERSION: "1"\n'


def _native_need(value):
    if not value:
        raise ValueError('Invalid Native source custody.')


def _native_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode()


def native_identity(descriptor):
    if descriptor.get('schema') == NATIVE_SCHEMA:
        return {k: descriptor[k] for k in ('schema', 'source_profile', 'repo_key',
            'source_request_id', 'source_version', 'receipt_digest', 'source_commit',
            'source_tree', 'archive_sha256')}
    return {k: descriptor[k] for k in ('revision', 'document_sha256', 'archive_sha256')}


def native_descriptor(value, org, project):
    from uuid import UUID
    _native_need(type(value) is dict and set(value) == NATIVE_FIELDS)
    d = dict(value)
    _native_need(d['schema'] == NATIVE_SCHEMA and d['target'] == 'ios-simulator'
        and d['source_profile'] == NATIVE_PROFILE and d['source_operation'] == 'create'
        and d['org_id'] == org and d['project_id'] == project)
    for key in ('org_id', 'project_id', 'actor_binding_id', 'repo_key', 'source_request_id'):
        _native_need(type(d[key]) is str and str(UUID(d[key])) == d[key] and UUID(d[key]).int != 0)
    for key in ('receipt_digest', 'bundle_sha256', 'archive_sha256', 'source_commit', 'source_tree'):
        size = 40 if key in ('source_commit', 'source_tree') else 64
        _native_need(type(d[key]) is str and re.fullmatch('[0-9a-f]{'+str(size)+'}', d[key]) is not None)
    _native_need(type(d['source_version']) is int and 1 <= d['source_version'] <= 2147483647
        and type(d['byte_size']) is int and 0 < d['byte_size'] <= NATIVE_LIMIT)
    refs = d['material_refs']
    _native_need(type(refs) is list and 1 <= len(refs) <= 8)
    ids, total = set(), 0
    for ref in refs:
        _native_need(type(ref) is dict and set(ref) == {'material_id', 'sha256', 'byte_size', 'path'})
        mid = ref['material_id']
        _native_need(type(mid) is str and str(UUID(mid)) == mid and UUID(mid).int != 0 and mid not in ids)
        ids.add(mid)
        _native_need(type(ref['sha256']) is str and re.fullmatch('[0-9a-f]{64}', ref['sha256']) is not None
            and type(ref['byte_size']) is int and 0 < ref['byte_size'] <= 1048576
            and ref['path'] in ('Resources/Input/'+mid+'.txt', 'Resources/Input/'+mid+'.json'))
        total += ref['byte_size']
    _native_need(total <= 4194304)
    return d


def _native_path(path):
    _native_need(type(path) is str and 0 < len(path.encode('utf-8')) <= 512
        and not path.startswith('/') and re.fullmatch(r'[A-Za-z0-9_./-]+', path) is not None)
    for part in path.split('/'):
        _native_need(part not in ('', '.', '..') and not part.endswith('.')
            and re.fullmatch(r'(?i:con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?', part) is None)
    return path


def native_tree(files):
    """Git object/tree hashing over the complete regular-file current tree."""
    tree = {}
    aliases = set()
    for path, (mode, raw) in files.items():
        _native_path(path)
        _native_need(path.casefold() not in aliases and mode in ('100644', '100755') and type(raw) is bytes)
        aliases.add(path.casefold())
        parts = path.split('/')
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            _native_need(type(node) is dict)
        _native_need(parts[-1] not in node)
        node[parts[-1]] = (mode, hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).digest())
    def digest(node):
        entries = []
        for name, item in node.items():
            directory = type(item) is dict
            mode, oid = ('40000', digest(item)) if directory else item
            entries.append(((name+('/' if directory else '')).encode(), mode.encode()+b' '+name.encode()+b'\0'+oid))
        raw = b''.join(value for _, value in sorted(entries))
        return hashlib.sha1(b'tree '+str(len(raw)).encode()+b'\0'+raw).digest()
    return digest(tree).hex()


def native_files_policy(files, descriptor):
    _native_need(0 < len(files) <= 1000 and sum(len(v[1]) for v in files.values()) <= NATIVE_LIMIT)
    _native_need(native_tree(files) == descriptor['source_tree'])
    bundle = 'private.leaf.project.p'+descriptor['project_id'].replace('-', '')
    expected = NATIVE_PROJECT_SPEC.replace('${bundleId}', bundle).encode()
    _native_need(files.get('project.yml') == ('100644', expected)
        and files.get('Sources/LeafProjectApp.swift') == ('100644', NATIVE_APP_SOURCE.encode()))
    content = files.get('Sources/ContentView.swift')
    _native_need(content is not None and content[0] == '100644' and bool(content[1].strip()))
    total = 0
    for path, (mode, raw) in files.items():
        _native_need(len(raw) <= NATIVE_LIMIT)
        if path.startswith('Sources/'):
            _native_need(mode == '100644' and path.endswith('.swift') and len(raw) <= 1048576)
            text = raw.decode('utf-8')
            _native_need(path == 'Sources/LeafProjectApp.swift' or re.search(r'@\s*main\b', text) is None)
        if path.startswith('Resources/Input/'):
            _native_need(mode == '100644' and len(raw) <= 1048576)
            total += len(raw)
    _native_need(total <= 4194304)
    for ref in descriptor['material_refs']:
        mode, raw = files.get(ref['path'], (None, None))
        _native_need(mode == '100644' and type(raw) is bytes and len(raw) == ref['byte_size']
            and hashlib.sha256(raw).hexdigest() == ref['sha256'])
        raw.decode('utf-8')


def native_archive(files, descriptor):
    native_files_policy(files, descriptor)
    _native_need(NATIVE_MANIFEST not in files)
    manifest = {'schema': 'leaf.native-sdk-tree.v1', 'source': native_identity(descriptor),
                'files': [{'path': p, 'mode': mode,
                    'blob': hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()}
                    for p, (mode, raw) in sorted(files.items())]}
    # Archive digest is an outer descriptor field, never circular manifest input.
    manifest['source'].pop('archive_sha256')
    members = dict(files, **{NATIVE_MANIFEST: ('100644', _native_json(manifest))})
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive:
        for path, (mode, raw) in sorted(members.items()):
            info = zipfile.ZipInfo(path, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | (0o755 if mode == '100755' else 0o644)) << 16
            archive.writestr(info, raw)
    raw = output.getvalue()
    _native_need(0 < len(raw) <= NATIVE_LIMIT)
    return raw


def verify_native_archive(raw, descriptor):
    d = native_descriptor(descriptor, descriptor['org_id'], descriptor['project_id'])
    _native_need(type(raw) is bytes and len(raw) == d['byte_size'] and len(raw) <= NATIVE_LIMIT
        and hashlib.sha256(raw).hexdigest() == d['archive_sha256'])
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
        _native_need(1 < len(infos) <= 1001 and len({i.filename for i in infos}) == len(infos)
            and sum(i.file_size for i in infos) <= NATIVE_LIMIT and not archive.comment)
        files = {}
        for i in infos:
            _native_path(i.filename)
            _native_need(not i.is_dir() and not i.flag_bits & 1 and not i.extra and not i.comment
                and i.compress_type == zipfile.ZIP_STORED and i.file_size <= NATIVE_LIMIT
                and stat.S_IFMT(i.external_attr >> 16) == stat.S_IFREG)
            mode = '100755' if i.external_attr >> 16 & 0o111 else '100644'
            if i.filename != NATIVE_MANIFEST:
                files[i.filename] = (mode, archive.read(i))
        native_files_policy(files, d)
        _native_need(native_archive(files, d) == raw)
    return files


def validate_native_artifact(value, descriptor):
    _native_need(type(value) is dict and set(value) == {'source', 'bundle_id', 'sdk',
        'app_tree_sha256', 'file_count', 'xcodegen_exit_code', 'xcode_version', 'xcodegen_version', 'sdk_version'})
    _native_need(value['source'] == native_identity(descriptor) and value['sdk'] == 'iphonesimulator'
        and value['bundle_id'] == 'private.leaf.project.p'+descriptor['project_id'].replace('-', '')
        and type(value['app_tree_sha256']) is str and re.fullmatch('[0-9a-f]{64}', value['app_tree_sha256']) is not None
        and type(value['file_count']) is int and 0 < value['file_count'] <= 10000
        and type(value['xcodegen_exit_code']) is int and value['xcodegen_exit_code'] == 0)
    for key in ('xcode_version', 'xcodegen_version', 'sdk_version'):
        text = value[key]
        _native_need(type(text) is str and 0 < len(text) <= 256
            and all(c in '\n\r\t' or 32 <= ord(c) < 127 for c in text))
    return value


def validate_native_identity(value):
    from uuid import UUID
    fields = {'schema', 'source_profile', 'repo_key', 'source_request_id', 'source_version',
              'receipt_digest', 'source_commit', 'source_tree', 'archive_sha256'}
    _native_need(type(value) is dict and set(value) == fields
        and value['schema'] == NATIVE_SCHEMA and value['source_profile'] == NATIVE_PROFILE
        and type(value['source_version']) is int and 1 <= value['source_version'] <= 2147483647)
    for key in ('repo_key', 'source_request_id'):
        _native_need(type(value[key]) is str and str(UUID(value[key])) == value[key] and UUID(value[key]).int != 0)
    for key in ('receipt_digest', 'source_commit', 'source_tree', 'archive_sha256'):
        size = 40 if key in ('source_commit', 'source_tree') else 64
        _native_need(type(value[key]) is str and re.fullmatch('[0-9a-f]{'+str(size)+'}', value[key]) is not None)
    return value


if __name__ == '__main__':
    raise SystemExit(main())
