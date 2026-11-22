"""Native adapter proof with inert private originals and synthetic decoder output."""
import copy
import hashlib
import uuid

import pytest
import browser_project_authoring as authoring
import test_browser_project_authoring as existing

lane = existing.lane


def prepare(lane, monkeypatch):
    movie = lane.add(b'synthetic video bytes', 'video/quicktime', str(uuid.uuid4()))
    lane.request['material_refs'].append(movie)
    lane.decoded = []
    visual = dict(manifest=dict(schema='synthetic-private-frame-manifest'), manifest_sha256='e' * 64, images=[])
    def decode(raw, row, **kwargs):
        lane.decoded.append((raw, copy.deepcopy(row), kwargs))
        kwargs['recheck']()
        return copy.deepcopy(visual)
    monkeypatch.setattr(authoring.videos, 'decode', decode)
    return movie, visual


def call(lane):
    return authoring.author(lane.tenant, existing.PROJECT, lane.request,
                           cancelled=lambda: lane.cancelled, source_profile=authoring.NATIVE_PROFILE)


def test_native_selected_originals_and_remaining_budget_reach_harness(lane, monkeypatch):
    movie, visual = prepare(lane, monkeypatch)
    original_post = authoring.requests.post
    seen = []
    def post(url, *, data, headers, timeout, allow_redirects):
        import json
        body = json.loads(data)
        seen.append(body)
        assert body['video'] == visual and 0 < body['budget_ms'] <= 240000
        assert 0 < sum(timeout) <= 240 and allow_redirects is False
        assert len(body['material']) == 1 and body['material'][0]['sha256'] == lane.request['material_refs'][0]['sha256']
        assert movie['material_id'] not in [item['path'] for item in body['material']]
        # Reuse the existing synthetic receipt producer, whose legacy timeout assertion stays intact.
        return original_post(url, data=data, headers=headers, timeout=(10, 240), allow_redirects=False)
    monkeypatch.setattr(authoring.requests, 'post', post)
    assert call(lane)['receipt']['changed_paths'] == ['Sources/ContentView.swift']
    raw, row, kwargs = lane.decoded[0]
    assert hashlib.sha256(raw).hexdigest() == movie['sha256'] == row['sha256']
    assert kwargs['identity']['actor_binding_id'] == existing.ACTOR
    assert kwargs['transcripts'] == [dict(material_id=existing.MATERIAL,
        sha256=lane.request['material_refs'][0]['sha256'], size_bytes=len(existing.RAW))]
    assert seen[0]['material'][0]['product_b64'] == existing.base64.b64encode(existing.RAW).decode()


def test_pre_dispatch_validation_never_decodes_or_authors(lane, monkeypatch):
    prepare(lane, monkeypatch)
    authoring.validate_native_materials(lane.tenant, existing.PROJECT, lane.request)
    assert not lane.decoded and not lane.posts and not lane.initializations


@pytest.mark.parametrize('mode', ['decoder', 'revoked', 'cancelled', 'stale', 'foreign', 'no-transcript'])
def test_unusable_video_or_scope_prevents_source_and_dispatch(lane, monkeypatch, mode):
    movie, visual = prepare(lane, monkeypatch)
    if mode == 'decoder':
        def decode(*args, **kwargs):
            raise authoring.videos.VideoError('video_unavailable')
        monkeypatch.setattr(authoring.videos, 'decode', decode)
    elif mode in ('revoked', 'cancelled'):
        def decode(*args, **kwargs):
            setattr(lane, 'revoked' if mode == 'revoked' else 'cancelled', True)
            return visual
        monkeypatch.setattr(authoring.videos, 'decode', decode)
    elif mode == 'stale':
        lane.rows[movie['material_id']]['sha256'] = '0' * 64
    elif mode == 'foreign':
        lane.rows[movie['material_id']]['actor_binding_id'] = str(uuid.uuid4())
    else:
        lane.request['material_refs'] = [movie]
    with pytest.raises(authoring.AuthoringError):
        call(lane)
    assert not lane.posts and not lane.initializations


def test_decode_does_not_reset_the_existing_overall_deadline(lane, monkeypatch):
    from types import SimpleNamespace
    prepare(lane, monkeypatch)
    times = iter([0, 241])
    monkeypatch.setattr(authoring, 'time', SimpleNamespace(monotonic=lambda: next(times)))
    with pytest.raises(authoring.AuthoringError, match='decode budget'):
        call(lane)
    assert not lane.posts and not lane.initializations
