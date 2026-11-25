"""Single-attempt I01 decoder. Database access is limited to four functions."""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import hashlib
import os
import re
import signal
import threading
import time
from urllib.parse import urlsplit

from project_video_materials import VideoError, canonical

SCHEMA = 'leaf.video-decode-job.v1'
_HASH = re.compile(r'[0-9a-f]{64}')
_IDENTITY = {'tenant_id', 'organization_id', 'project_id', 'actor_binding_id', 'edit_id'}
_CONTEXT = {'schema', 'request_sha256', 'identity', 'material', 'transcripts', 'input_key', 'result_key'}


@dataclass(frozen=True, repr=False)
class DecoderConfig:
    database_url: str
    bucket: str
    worker_id: str
    source_sha: str

    def __post_init__(self):
        try:
            url = urlsplit(self.database_url)
            valid_url = (url.scheme in {'postgres', 'postgresql'} and url.hostname
                         and url.username and url.path not in {'', '/'} and not url.fragment)
            if url.port is not None and not 1 <= url.port <= 65535:
                valid_url = False
        except (TypeError, ValueError):
            valid_url = False
        if not valid_url or any(c.isspace() for c in self.database_url):
            raise ValueError('invalid decoder database configuration')
        if (not isinstance(self.bucket, str)
                or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', self.bucket)
                or '..' in self.bucket or '.-' in self.bucket or '-.' in self.bucket
                or re.fullmatch(r'[0-9.]+', self.bucket)):
            raise ValueError('invalid decoder bucket configuration')
        if not isinstance(self.worker_id, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', self.worker_id):
            raise ValueError('invalid decoder worker configuration')
        if not isinstance(self.source_sha, str) or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', self.source_sha):
            raise ValueError('invalid decoder source configuration')

    @classmethod
    def from_env(cls, environ=None):
        env = os.environ if environ is None else environ
        names = ('DATABASE_URL', 'LEAF_I01_DECODER_BUCKET', 'LEAF_I01_DECODER_WORKER_ID',
                 'LEAF_I01_DECODER_SOURCE_SHA')
        if any(not env.get(name) for name in names):
            raise ValueError('missing decoder configuration')
        return cls(*(env[name] for name in names))


class LostLease(Exception):
    pass


class LeaseGuard:
    def __init__(self, db, job_id, worker):
        self.db, self.job_id, self.worker = db, job_id, worker
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.last_renewal = time.monotonic()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def renew(self):
        with self.lock:
            self.recheck()
            try:
                if self.db.video_decode_heartbeat(self.job_id, self.worker, 60) is not True:
                    self.lost.set()
            except Exception:
                self.lost.set()
            self.recheck()
            self.last_renewal = time.monotonic()

    def recheck(self):
        if self.lost.is_set() or time.monotonic() - self.last_renewal >= 60:
            self.lost.set()
            raise LostLease()

    def _run(self):
        while not self.stop.wait(15):
            try:
                self.renew()
            except LostLease:
                return

    def start(self):
        self.renew()
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=11)


def _hash(value):
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and value.strip() == value and bool(value.strip())


def _validate(job):
    ctx = job['execution_context']
    if not isinstance(ctx, dict) or set(ctx) != _CONTEXT or ctx['schema'] != SCHEMA:
        raise ValueError()
    digest, identity, material = ctx['request_sha256'], ctx['identity'], ctx['material']
    if (not _hash(digest) or not isinstance(identity, dict) or set(identity) != _IDENTITY
            or not all(_text(value) for value in identity.values())
            or identity['tenant_id'] != job['request_tenant_id']
            or identity['project_id'] != str(job['project_id'])
            or type(job['attempt']) is not int or job['attempt'] != 1):
        raise ValueError()
    if (not isinstance(material, dict)
            or set(material) != {'material_id', 'sha256', 'byte_size', 'media_type'}
            or not _text(material['material_id']) or not _hash(material['sha256'])
            or type(material['byte_size']) is not int or not 0 < material['byte_size'] <= 64 * 1024 * 1024
            or material['media_type'] != 'video/quicktime' or not isinstance(ctx['transcripts'], list)
            or ctx['input_key'] != f'in/{digest}/1.mov' or ctx['result_key'] != f'out/{digest}/1.json'):
        raise ValueError()
    return ctx


def run_once(db, store, decode, config) -> str:
    job = db.video_decode_claim(config.worker_id, 60)
    if job is None:
        return 'idle'
    guard = LeaseGuard(db, job['job_id'], config.worker_id)

    def fail(code):
        guard.renew()
        try:
            outcome = db.video_decode_fail(job['job_id'], config.worker_id, job['attempt'], {'code': code})
        except Exception:
            return 'lost'
        guard.recheck()
        return 'failed' if outcome == 'failed' else 'lost'

    try:
        guard.start()
        try:
            ctx = _validate(job)
        except (KeyError, TypeError, ValueError):
            return fail('bad_request')
        try:
            response = store.get_object(Bucket=config.bucket, Key=ctx['input_key'])
            body = response['Body']
            try:
                raw = body.read(ctx['material']['byte_size'] + 1)
            finally:
                body.close()
            guard.recheck()
            if (type(raw) is not bytes or len(raw) != ctx['material']['byte_size']
                    or hashlib.sha256(raw).hexdigest() != ctx['material']['sha256']):
                return fail('video_changed')
            decoded = decode(raw, ctx['material'], identity=ctx['identity'], transcripts=ctx['transcripts'],
                             recheck=guard.recheck, deadline=time.monotonic() + 45)
            guard.recheck()
            images = [dict(frame_id=image['frame_id'],
                           sha256=hashlib.sha256(base64.b64decode(image['data'], validate=True)).hexdigest())
                      for image in decoded['images']]
            result = dict(request_sha256=ctx['request_sha256'], identity=ctx['identity'],
                          video_sha256=ctx['material']['sha256'], manifest=decoded['manifest'],
                          manifest_sha256=decoded['manifest_sha256'], images=images,
                          decoder=dict(source_sha=config.source_sha, worker_id=config.worker_id,
                                       attempt=job['attempt']), cleanup='settled', result_key=ctx['result_key'])
            payload = canonical({**result, 'images': [dict(**image, data=full['data'])
                                                     for image, full in zip(images, decoded['images'])]})
        except LostLease:
            return 'lost'
        except VideoError as exc:
            return fail(exc.code)
        except Exception:
            return fail('video_unavailable')
        # From here, any uncertainty leaves the running single-attempt job alone.
        try:
            guard.renew()
            store.put_object(Bucket=config.bucket, Key=ctx['result_key'], Body=payload,
                             ContentType='application/json', IfNoneMatch='*')
            guard.renew()
            outcome = db.video_decode_complete(job['job_id'], config.worker_id, job['attempt'], result)
            guard.recheck()
            return 'succeeded' if outcome == 'applied' else 'lost'
        except Exception:
            return 'lost'
    except LostLease:
        return 'lost'
    finally:
        guard.close()


_DECODER_SCHEMA = 'leaf_platform'  # Migration 0114 schema; the decoder role's search_path is pg_catalog only.


class DecoderDB:
    """Fresh autocommit connection per call, including the heartbeat thread."""
    def __init__(self, dsn):
        self.dsn = dsn

    def _call(self, function, args, *, row=False):
        import psycopg
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb
        with psycopg.connect(self.dsn, autocommit=True, connect_timeout=5,
                             row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                # RDS Proxy rejects startup options, so the 5 s statement timeout is set per session.
                cur.execute('SET statement_timeout = 5000')
                params = tuple(Jsonb(value) if isinstance(value, dict) else value for value in args)
                # Function names are internal constants, never request content.
                cur.execute(f'SELECT * FROM {_DECODER_SCHEMA}.{function}({", ".join(["%s"] * len(params))})', params)
                value = cur.fetchone()
                return value if row or value is None else next(iter(value.values()))

    def video_decode_claim(self, worker, lease):
        return self._call('video_decode_claim', (worker, lease), row=True)

    def video_decode_heartbeat(self, job, worker, lease):
        return self._call('video_decode_heartbeat', (job, worker, lease))

    def video_decode_complete(self, job, worker, attempt, result):
        return self._call('video_decode_complete', (job, worker, attempt, result))

    def video_decode_fail(self, job, worker, attempt, error):
        return self._call('video_decode_fail', (job, worker, attempt, error))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Leaf I01 decoder worker')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    try:
        config = DecoderConfig.from_env()
    except ValueError:
        parser.error('invalid or missing decoder configuration')
    import boto3
    from botocore.config import Config
    from project_video_materials import decode
    db = DecoderDB(config.database_url)
    store = boto3.client('s3', config=Config(connect_timeout=5, read_timeout=10,
                                            retries={'total_max_attempts': 1}))
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    while not stop.is_set():
        try:
            run_once(db, store, decode, config)
        except Exception:
            # Do not print exception content: provider/driver errors can contain secrets.
            raise SystemExit('decoder unavailable') from None
        if args.once:
            return
        stop.wait(1)


if __name__ == '__main__':
    main()
