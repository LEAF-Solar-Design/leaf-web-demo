"""Transient authenticated media locators bound to frozen worker evidence.

This verifies object custody and metadata. It does not accept a browser journey
or decode its media. Presigned URLs never enter the campaign ledger.
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit

JSON_LIMIT = 128 * 1024
FILE_LIMIT = 128 * 1024**2
TOTAL_LIMIT = 256 * 1024**2
IDENTITY_FIELDS = ('operation_id', 'attempt_id', 'org_id', 'project_id', 'repository_id',
                   'campaign_id', 'task_id', 'parent_attempt_id', 'parent_attempt_fence')
RUNTIME_FIELDS = {'chromium_sha256', 'chromium_version', 'lock_sha256', 'executor_sha256', 'playwright_version'}
KINDS = {'screenshot', 'video', 'steps', 'timings', 'replay'}


class MediaUnavailable(ValueError):
    code = 'media_download_unavailable'

    def __init__(self):
        super().__init__(self.code)


def _require(condition):
    if not condition:
        raise MediaUnavailable()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _sha(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _integer(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def safe_prefix(value):
    return (isinstance(value, str) and 1 < len(value) <= 512 and value.endswith('/')
            and not value.startswith('/') and ':' not in value and '\\' not in value
            and not re.search(r'[\x00-\x1f\x7f]', value)
            and all(part not in {'', '.', '..'} for part in value[:-1].split('/')))


def _path(value):
    _require(isinstance(value, str) and 0 < len(value) <= 512
             and not re.search(r'[\\:\x00-\x1f\x7f]', value) and not value.startswith('/'))
    for part in value.split('/'):
        _require(part not in {'', '.', '..'} and not part.endswith(('.', ' '))
                 and re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', part) is None)
    _require(value.casefold() not in {'bundle.json', 'index.json'})
    return value


def _ref(value, bucket, key):
    _require(isinstance(value, dict) and set(value) == {'bucket', 'key', 'version_id', 'sha256'})
    _require(all(isinstance(item, str) and 0 < len(item) <= 1024 and item == item.strip()
                 and '\x00' not in item for item in value.values()))
    _require(value['bucket'] == bucket and value['key'] == key and _sha(value['sha256'])
             and value['version_id'].lower() not in {'null', 'latest'})
    return dict(value)


def _pairs(items):
    result = {}
    for key, value in items:
        _require(key not in result)
        result[key] = value
    return result


def _get_json(s3, reference):
    obj = s3.get_object(Bucket=reference['bucket'], Key=reference['key'], VersionId=reference['version_id'])
    stream = obj['Body']
    try:
        _require(obj.get('VersionId') == reference['version_id']
                 and _integer(obj.get('ContentLength'), 1, JSON_LIMIT))
        content = bytearray()
        while True:
            chunk = stream.read(min(65536, JSON_LIMIT + 1 - len(content)))
            _require(isinstance(chunk, bytes))
            if not chunk:
                break
            content.extend(chunk)
            _require(len(content) <= JSON_LIMIT)
        _require(len(content) == obj['ContentLength']
                 and hashlib.sha256(content).hexdigest() == reference['sha256'])
        value = json.loads(content, object_pairs_hook=_pairs)
        _require(isinstance(value, dict))
        _canonical(value)
        return value
    finally:
        stream.close()


def _viewport(s3, job, binding):
    source_ref = binding['source']
    _require(isinstance(source_ref, dict) and _sha(source_ref.get('sha256'))
             and safe_prefix(job['source_prefix']))
    reference = _ref(source_ref, job['source_bucket'], job['source_prefix'] + source_ref['sha256'] + '.json')
    source = _get_json(s3, reference)
    _require(set(source) == {'version', 'manifest', 'profile'} and type(source['version']) is int and source['version'] == 1)
    manifest, profile = source['manifest'], source['profile']
    _require(isinstance(manifest, dict) and isinstance(profile, dict)
             and type(manifest.get('version')) is int and manifest['version'] == 1
             and type(profile.get('version')) is int and profile['version'] == 1
             and manifest.get('profile_digest') == hashlib.sha256(_canonical(profile)).hexdigest()
             and profile.get('profile_id') == manifest.get('profile_id') == job['profile_id']
             and _integer(profile.get('profile_revision'), 1, 2**53-1)
             and type(manifest.get('profile_revision')) is int
             and profile['profile_revision'] == manifest['profile_revision']
             and all(manifest.get(key) == binding[key] for key in
                     ('campaign_id', 'task_id', 'parent_attempt_id', 'parent_attempt_fence'))
             and type(manifest.get('parent_attempt_fence')) is int)
    viewport = profile.get('viewport')
    _require(isinstance(viewport, dict) and set(viewport) == {'width', 'height'}
             and _integer(viewport['width'], 640, 1920) and _integer(viewport['height'], 480, 1440))
    return dict(viewport)


def _download(s3, job, binding, media_ref):
    _require(safe_prefix(job['media_prefix']) and _sha(binding.get('operation_id'))
             and isinstance(binding.get('attempt_id'), str)
             and re.fullmatch(re.escape(binding['operation_id'][:48]) + r'-[0-9]{8}', binding['attempt_id'])
             and _sha(binding.get('executor_digest')))
    base = job['media_prefix'] + binding['operation_id'] + '/' + binding['attempt_id'] + '/'
    reference = _ref(media_ref, job['media_bucket'], base + 'index.json')
    index = _get_json(s3, reference)
    _require(set(index) == {'version', 'identity', 'bundle_ref', 'artifacts', 'verification'}
             and type(index['version']) is int and index['version'] == 1)
    identity = index['identity']
    _require(isinstance(identity, dict) and set(identity) == set(IDENTITY_FIELDS) | RUNTIME_FIELDS
             | {'binding_digest', 'executor_package_digest'})
    _require(all(identity[key] == binding[key] for key in IDENTITY_FIELDS)
             and type(identity['parent_attempt_fence']) is int
             and identity['binding_digest'] == hashlib.sha256(_canonical(binding)).hexdigest()
             and identity['executor_package_digest'] == binding['executor_digest']
             and all(_sha(identity[key]) for key in ('chromium_sha256', 'lock_sha256', 'executor_sha256'))
             and all(isinstance(identity[key], str) and re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,3}', identity[key])
                     for key in ('chromium_version', 'playwright_version')))
    viewport = _viewport(s3, job, binding)
    bundle_ref = _ref(index['bundle_ref'], job['media_bucket'], base + 'bundle.json')
    bundle = _get_json(s3, bundle_ref)
    _require(set(bundle) == {'version', 'identity', 'execution', 'artifacts', 'steps'}
             and type(bundle['version']) is int and bundle['version'] == 1
             and bundle['identity'] == identity and bundle['execution'] in {'succeeded', 'failed'}
             and isinstance(bundle['steps'], list))
    entries, originals = index['artifacts'], bundle['artifacts']
    _require(isinstance(entries, list) and isinstance(originals, list) and len(entries) == len(originals) <= 256)
    expected, seen, total = {}, set(), 0
    for item in originals:
        _require(isinstance(item, dict) and set(item) == {'path', 'kind', 'size', 'sha256'})
        path = _path(item['path'])
        _require(path.casefold() not in seen and isinstance(item['kind'], str) and item['kind'] in KINDS
                 and _integer(item['size'], 0, FILE_LIMIT) and _sha(item['sha256']))
        seen.add(path.casefold())
        expected[path] = item
        total += item['size']
    _require(total <= TOTAL_LIMIT)
    artifacts = []
    for item in entries:
        _require(isinstance(item, dict) and set(item) == {'path', 'kind', 'size', 'ref'})
        path = _path(item['path'])
        original = expected.pop(path, None)
        _require(original is not None and item['kind'] == original['kind']
                 and type(item['size']) is int and item['size'] == original['size'])
        ref = _ref(item['ref'], job['media_bucket'], base + path)
        _require(ref['sha256'] == original['sha256'])
        artifacts.append({'path': path, 'kind': item['kind'], 'size': item['size'], 'ref': ref})
    _require(not expected)

    def sign(ref):
        url = s3.generate_presigned_url('get_object', Params={'Bucket': ref['bucket'], 'Key': ref['key'],
            'VersionId': ref['version_id']}, ExpiresIn=300, HttpMethod='GET')
        _require(isinstance(url, str) and len(url) <= 8192)
        parsed = urlsplit(url)
        _require(parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password)
        return url

    result = {'version': 1, 'identity': identity, 'viewport': viewport,
              'bundle': {'ref': bundle_ref, 'url': sign(bundle_ref)},
              'artifacts': [{**item, 'url': sign(item['ref'])} for item in artifacts], 'expires_in': 300}
    _require(len(_canonical(result)) <= 1024 * 1024)
    return result


def download(s3, job, binding, media_ref):
    """Return ephemeral locators, or one sanitized failure without provider text."""
    try:
        return _download(s3, job, binding, media_ref)
    except Exception:
        raise MediaUnavailable() from None
