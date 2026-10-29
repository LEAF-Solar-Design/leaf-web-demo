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
    def __init__(self, http, job, *, clock=time.time, monotonic=time.monotonic):
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
            while not self.stop.wait(2):
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
    require(type(facts) is dict and set(facts) == keys, 'invalid-native-facts')
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


def run_once(scope, http, native, output, *, clock=time.time):
    scope = binding(scope)
    job = validate_claim(parse(http.request('POST', 'claim', {})), scope, clock())
    if job is None:
        return {'stage': 'empty', 'result': 'empty'}
    guard = Authority(http, job, clock=clock)
    settling = False
    failure_settlement = 'not-attempted-authority-lost'
    try:
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


if __name__ == '__main__':
    raise SystemExit(main())
