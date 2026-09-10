"""Offline custody checks using the exact walk_worker index/bundle schema."""
import copy
import hashlib
import io
import json

import pytest

import campaign_walk_media as media


class S3:
    def __init__(self):
        self.objects, self.reads, self.signatures, self.streams = {}, [], [], []

    def publish(self, bucket, key, value):
        raw = media._canonical(value)
        reference = dict(bucket=bucket, key=key, version_id='frozen-version', sha256=hashlib.sha256(raw).hexdigest())
        self.objects[(bucket, key)] = raw
        return reference

    def get_object(self, **params):
        self.reads.append(params)
        raw = self.objects[(params['Bucket'], params['Key'])]
        stream = io.BytesIO(raw)
        self.streams.append(stream)
        return {'Body': stream, 'VersionId': 'frozen-version', 'ContentLength': len(raw)}

    def generate_presigned_url(self, operation, **kwargs):
        self.signatures.append((operation, kwargs))
        return 'https://example.invalid/transient-media'


def publish_media(s3, job, binding):
    """Mirror worker identity, bundle and index; media bytes stay client-side."""
    identity = {key: binding[key] for key in media.IDENTITY_FIELDS}
    identity.update(chromium_sha256='1'*64, chromium_version='130.0.0.0', lock_sha256='2'*64,
        executor_sha256='3'*64, playwright_version='1.50.0',
        binding_digest=hashlib.sha256(media._canonical(binding)).hexdigest(),
        executor_package_digest=binding['executor_digest'])
    base = job['media_prefix'] + binding['operation_id'] + '/' + binding['attempt_id'] + '/'
    artifact = {'path': 'screenshots/step-001.png', 'kind': 'screenshot', 'size': 7, 'sha256': '4'*64}
    bundle = {'version': 1, 'identity': identity, 'execution': 'succeeded', 'artifacts': [artifact], 'steps': []}
    bundle_ref = s3.publish(job['media_bucket'], base + 'bundle.json', bundle)
    index = {'version': 1, 'identity': identity, 'bundle_ref': bundle_ref,
        'artifacts': [{'path': artifact['path'], 'kind': artifact['kind'], 'size': artifact['size'],
            'ref': dict(bucket=job['media_bucket'], key=base+artifact['path'], version_id='artifact-version', sha256=artifact['sha256'])}],
        'verification': {'status': 'media_verified'}}
    reference = s3.publish(job['media_bucket'], base + 'index.json', index)
    return reference, index, bundle, base


@pytest.fixture
def case():
    s3 = S3()
    job = dict(media_bucket='trusted-media', media_prefix='walks/media/', source_bucket='trusted-source',
               source_prefix='walks/source/', profile_id='reviewed-smoke')
    profile = dict(version=1, profile_id=job['profile_id'], profile_revision=2, viewport={'width': 1280, 'height': 720})
    binding = dict(operation_id='a'*64, attempt_id='a'*48+'-00000001', org_id='org', project_id='project',
                   repository_id='repo', campaign_id='campaign', task_id='task', parent_attempt_id='parent',
                   parent_attempt_fence=1, executor_digest='b'*64)
    manifest = {key: binding[key] for key in ('campaign_id', 'task_id', 'parent_attempt_id', 'parent_attempt_fence')}
    manifest.update(version=1, profile_id=job['profile_id'], profile_revision=2,
                    profile_digest=hashlib.sha256(media._canonical(profile)).hexdigest())
    source = dict(version=1, profile=profile, manifest=manifest)
    source_digest = hashlib.sha256(media._canonical(source)).hexdigest()
    binding['source'] = s3.publish(job['source_bucket'], job['source_prefix']+source_digest+'.json', source)
    reference, index, bundle, base = publish_media(s3, job, binding)
    return s3, job, binding, reference, index, bundle, base


def freeze(case, *, bundle_changed=False):
    s3, job, _, _, index, bundle, base = case
    if bundle_changed:
        index['bundle_ref'] = s3.publish(job['media_bucket'], base+'bundle.json', bundle)
    return s3.publish(job['media_bucket'], base+'index.json', index)


def test_exact_worker_index_presigns_only_pinned_objects(case):
    s3, job, binding, reference, index, _, _ = case
    result = media.download(s3, job, binding, reference)
    assert set(result) == {'version', 'identity', 'viewport', 'bundle', 'artifacts', 'expires_in'}
    assert result['identity'] == index['identity'] and result['viewport'] == {'width': 1280, 'height': 720}
    assert result['expires_in'] == 300
    assert len(s3.reads) == 3 and len(s3.signatures) == 2
    refs = [index['bundle_ref'], index['artifacts'][0]['ref']]
    for (operation, params), ref in zip(s3.signatures, refs):
        assert operation == 'get_object'
        assert params == dict(Params={'Bucket': ref['bucket'], 'Key': ref['key'], 'VersionId': ref['version_id']},
                              ExpiresIn=300, HttpMethod='GET')
    assert all(stream.closed for stream in s3.streams)


@pytest.mark.parametrize('patch', [dict(bucket='untrusted'), dict(key='other/index.json'),
    dict(version_id='latest'), dict(version_id='null'), dict(version_id='other-version'), dict(sha256='f'*64)])
def test_untrusted_or_drifted_index_ref_rejected(case, patch):
    s3, job, binding, reference, *_ = case
    with pytest.raises(media.MediaUnavailable):
        media.download(s3, job, binding, {**reference, **patch})
    assert not s3.signatures


@pytest.mark.parametrize('field', list(media.IDENTITY_FIELDS) + ['binding_digest', 'executor_package_digest'])
def test_identity_cannot_be_rebound(case, field):
    s3, job, binding, _, index, *_ = case
    index['identity'][field] = 2 if field == 'parent_attempt_fence' else 'other'
    with pytest.raises(media.MediaUnavailable):
        media.download(s3, job, binding, freeze(case))
    assert not s3.signatures


@pytest.mark.parametrize('path', ['../secret', '/absolute', 'a//b', 'a/./b', 'a\\b', 'a:b',
                                'a/NUL.txt', 'COM1', 'bundle.json', 'index.json', 'a.'])
def test_unsafe_artifact_path_rejected_before_presigning(case, path):
    s3, job, binding, _, index, bundle, _ = case
    bundle['artifacts'][0]['path'] = path
    index['artifacts'][0]['path'] = path
    with pytest.raises(media.MediaUnavailable):
        media.download(s3, job, binding, freeze(case, bundle_changed=True))
    assert not s3.signatures


@pytest.mark.parametrize('patch', [dict(kind='video'), dict(size=8), dict(size=True),
    dict(ref={'bucket':'untrusted','key':'foreign','version_id':'v','sha256':'4'*64})])
def test_index_must_match_bundle(case, patch):
    s3, job, binding, _, index, *_ = case
    index['artifacts'][0].update(patch)
    with pytest.raises(media.MediaUnavailable):
        media.download(s3, job, binding, freeze(case))
    assert not s3.signatures


def test_artifact_hash_and_exact_base_required(case):
    s3, job, binding, _, index, *_ = case
    index['artifacts'][0]['ref']['sha256'] = '5'*64
    with pytest.raises(media.MediaUnavailable):
        media.download(s3, job, binding, freeze(case))
    index['artifacts'][0]['ref'].update(sha256='4'*64, key='walks/media/another-attempt/screen.png')
    with pytest.raises(media.MediaUnavailable):
        media.download(s3, job, binding, freeze(case))
    assert not s3.signatures


@pytest.mark.parametrize('mode', ['file', 'total', 'count'])
def test_artifact_limits(case, mode):
    s3, job, binding, _, index, bundle, base = case
    count = 257 if mode == 'count' else 3 if mode == 'total' else 1
    size = media.FILE_LIMIT+1 if mode == 'file' else media.FILE_LIMIT if mode == 'total' else 0
    bundle['artifacts'] = [dict(path=f'{n}.png', kind='screenshot', size=size, sha256='4'*64) for n in range(count)]
    index['artifacts'] = [dict(path=item['path'],kind=item['kind'],size=item['size'],
        ref=dict(bucket=job['media_bucket'],key=base+item['path'],version_id='v',sha256=item['sha256']))
        for item in bundle['artifacts']]
    with pytest.raises(media.MediaUnavailable):
        media.download(s3, job, binding, freeze(case, bundle_changed=True))
    assert not s3.signatures


def test_json_size_version_and_close(case):
    s3, job, binding, ref, *_ = case
    original = s3.get_object
    def oversized(**kwargs):
        obj = original(**kwargs)
        obj['ContentLength'] = media.JSON_LIMIT+1
        return obj
    s3.get_object = oversized
    with pytest.raises(media.MediaUnavailable):
        media.download(s3, job, binding, ref)
    assert s3.streams[-1].closed and not s3.signatures


def test_profile_digest_is_verified_from_frozen_source(case):
    s3, job, binding, _, index, bundle, base = case
    source_ref = binding['source']
    source = json.loads(s3.objects[(source_ref['bucket'],source_ref['key'])])
    source['profile']['viewport']['width'] = 1400
    digest = hashlib.sha256(media._canonical(source)).hexdigest()
    binding['source'] = s3.publish(job['source_bucket'], job['source_prefix']+digest+'.json', source)
    index['identity']['binding_digest'] = hashlib.sha256(media._canonical(binding)).hexdigest()
    bundle['identity'] = copy.deepcopy(index['identity'])
    with pytest.raises(media.MediaUnavailable):
        media.download(s3, job, binding, freeze(case,bundle_changed=True))
    assert not s3.signatures


def test_provider_failure_is_sanitized(case):
    s3, job, binding, ref, *_ = case
    def fail(*args, **kwargs):
        raise RuntimeError('provider signed URL must stay private')
    s3.generate_presigned_url = fail
    with pytest.raises(media.MediaUnavailable) as error:
        media.download(s3, job, binding, ref)
    assert str(error.value) == 'media_download_unavailable'
