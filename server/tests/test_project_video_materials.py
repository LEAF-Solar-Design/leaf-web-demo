"""Synthetic decoder contracts. No original media, provider or installed decoder is used."""
import copy
import hashlib
import io
import json
import os
import time
from types import SimpleNamespace

import pytest
from PIL import Image

import project_video_materials as video


@pytest.fixture
def synthetic(monkeypatch, tmp_path):
    pixels = io.BytesIO()
    Image.new('RGB', (16, 12), (255, 0, 0)).save(pixels, format='JPEG')
    jpeg = pixels.getvalue()
    raw = b'private synthetic movie bytes, not an original'
    material = dict(media_type='video/quicktime', material_id='movie-id',
                    byte_size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    state = SimpleNamespace(paths=[], jpeg=jpeg, probe=dict(streams=[dict(
        index=0, width=320, height=240, duration='5.0', nb_read_frames='150', start_time='0')]),
        stderr=b'', times=[0, 2000, 4000, 4960], damaged=False)
    monkeypatch.setattr(video, '_available', lambda: True)
    monkeypatch.setattr(video.tempfile, 'mkdtemp', lambda **kwargs: str(tmp_path / 'scratch'))
    # The decoder owns an exclusive private directory, just as mkdtemp would.
    (tmp_path / 'scratch').mkdir()

    def run(scratch, binary, args, name, deadline, recheck):
        state.paths.append((binary, args))
        recheck()
        out = scratch / 'output'
        if name == 'probe':
            (out / 'probe.stdout').write_bytes(json.dumps(state.probe).encode())
            (out / 'probe.stderr').write_bytes(state.stderr)
        else:
            lines = ['[Parsed_showinfo_1 @ x] config in time_base: 1/1000, frame_rate: 30/1']
            for index, stamp in enumerate(state.times):
                (out / f'frame-{index + 1:03d}.jpg').write_bytes(b'invalid' if state.damaged else jpeg)
                lines.append(f'[Parsed_showinfo_1 @ x] n: {index} pts: {stamp} pts_time:{stamp / 1000}')
            (out / 'decode.stderr').write_text('\n'.join(lines), encoding='ascii')
            (out / 'decode.stdout').write_bytes(b'')
    monkeypatch.setattr(video, '_run', run)
    state.call = lambda recheck=lambda: None: video.decode(raw, material,
        identity=dict(project_id='synthetic'), transcripts=[dict(material_id='text', sha256='a' * 64, size_bytes=3)],
        recheck=recheck, deadline=time.monotonic() + 100)
    state.raw, state.material, state.scratch = raw, material, tmp_path / 'scratch'
    return state


def test_exact_pixels_timestamps_full_decode_and_private_cleanup(synthetic):
    value = synthetic.call()
    manifest = value['manifest']
    assert manifest['decoded_frame_count'] == 150 and manifest['duration_ms'] == 5000
    assert [row['timestamp_ms'] for row in manifest['frames']] == [0, 2000, 4000, 4960]
    assert manifest['frames'][0]['sha256'] == hashlib.sha256(synthetic.jpeg).hexdigest()
    assert manifest['frames'][0]['width'] == 16 and manifest['frames'][0]['height'] == 12
    assert value['manifest_sha256'] == hashlib.sha256(video.canonical(manifest)).hexdigest()
    assert all('data' not in row for row in manifest['frames'])
    import base64
    assert base64.b64decode(value['images'][0]['data']) == synthetic.jpeg
    assert manifest['original']['sha256'] == synthetic.material['sha256']
    assert manifest['transcripts'][0]['material_id'] == 'text'
    assert not synthetic.scratch.exists()
    probe, decode = synthetic.paths
    assert '-count_frames' in probe[1]
    assert '-enable_drefs' in decode[1] and decode[1][decode[1].index('-enable_drefs') + 1] == '0'
    assert decode[1][decode[1].index('-protocol_whitelist') + 1] == 'file'
    assert 'eq(n\\,149)' in decode[1][decode[1].index('-vf') + 1]
    assert '-t' not in decode[1]  # Never manufacture a successful partial decode.


@pytest.mark.parametrize('mode', ['hash', 'size', 'media'])
def test_wrong_original_never_starts_a_decoder(synthetic, mode):
    if mode == 'hash':
        synthetic.material['sha256'] = 'f' * 64
    elif mode == 'size':
        synthetic.material['byte_size'] += 1
    else:
        synthetic.material['media_type'] = 'text/plain'
    with pytest.raises(video.VideoError, match='video_changed'):
        synthetic.call()
    assert not synthetic.paths


def test_missing_cage_is_unavailable_without_bare_fallback(synthetic, monkeypatch):
    monkeypatch.setattr(video, '_available', lambda: False)
    with pytest.raises(video.VideoError, match='video_unavailable'):
        synthetic.call()
    assert not synthetic.paths


@pytest.mark.parametrize('mode', ['duration', 'pixels', 'frames', 'probe-error', 'no-frames',
                                  'gap', 'missing-end', 'bad-jpeg', 'total-bytes', 'frame-bytes'])
def test_incomplete_or_over_budget_input_fails_and_cleans(synthetic, mode, monkeypatch):
    row = synthetic.probe['streams'][0]
    if mode == 'duration':
        row['duration'] = '181'
    elif mode == 'pixels':
        row.update(width=8000, height=8000)
    elif mode == 'frames':
        row['nb_read_frames'] = '0'
    elif mode == 'probe-error':
        synthetic.stderr = b'codec failed'
    elif mode == 'no-frames':
        synthetic.times = []
    elif mode == 'gap':
        synthetic.times = [0, 4960]
    elif mode == 'missing-end':
        synthetic.times = [0]
    elif mode == 'bad-jpeg':
        synthetic.damaged = True
    elif mode == 'total-bytes':
        monkeypatch.setattr(video, 'MAX_TOTAL_BYTES', len(synthetic.jpeg))
    elif mode == 'frame-bytes':
        monkeypatch.setattr(video, 'MAX_FRAME_BYTES', 100)
    with pytest.raises(video.VideoError):
        synthetic.call()
    assert not synthetic.scratch.exists()


def test_revoked_scope_and_cancellation_do_not_return_pixels(synthetic):
    checks = 0
    def recheck():
        nonlocal checks
        checks += 1
        if checks == 4:
            raise PermissionError('revoked')
    with pytest.raises(PermissionError):
        synthetic.call(recheck)
    assert not synthetic.scratch.exists()


def test_scratch_cleanup_failure_is_not_success(synthetic, monkeypatch):
    actual = video.shutil.rmtree
    def failure(*args):
        raise OSError('synthetic cleanup failure')
    monkeypatch.setattr(video.shutil, 'rmtree', failure)
    try:
        with pytest.raises(video.VideoError, match='video_cleanup_failed'):
            synthetic.call()
    finally:
        actual(synthetic.scratch)


def test_running_child_has_resource_namespace_bounds_and_is_reaped_on_cancel(monkeypatch, tmp_path):
    out = tmp_path / 'output'
    out.mkdir()
    calls = []
    child = SimpleNamespace(pid=123456, returncode=None, poll=lambda: None,
                            wait=lambda **kwargs: calls.append(('wait', kwargs)))
    def popen(argv, **kwargs):
        calls.append(('spawn', argv, kwargs))
        return child
    monkeypatch.setattr(video.subprocess, 'Popen', popen)
    monkeypatch.setattr(video.signal, 'SIGKILL', 9, raising=False)
    monkeypatch.setattr(video.os, 'killpg', lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()), raising=False)
    checks = 0
    def recheck():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise PermissionError('cancelled')
    with pytest.raises(PermissionError):
        video._run(tmp_path, '/usr/bin/ffmpeg', ['-f', 'mov', '/input.mov'], 'decode',
                   time.monotonic() + 100, recheck)
    _, argv, kwargs = calls[0]
    assert argv[0] == '/usr/bin/prlimit' and '--as=536870912' in argv and '--fsize=131072' in argv
    assert '--cpu=30' in argv and '--nofile=64' in argv and '--nproc=32' in argv
    assert '--unshare-all' in argv and '--cap-drop' in argv and '--clearenv' in argv
    assert '--ro-bind' in argv and '--die-with-parent' in argv
    assert kwargs['shell'] is False and kwargs['start_new_session'] is True
    assert kwargs['env'] == {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}
    assert calls[-1][0] == 'wait'


def test_sdk_fixture_is_a_real_synthetic_red_jpeg():
    import base64
    import re
    from pathlib import Path
    source = (Path(__file__).resolve().parents[2] / 'harness/test/projectVideoFixture.ts').read_text()
    encoded = re.search(r'Buffer.from\("([^"]+)", "base64"\)', source).group(1)
    with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
        image.load()
        assert image.size == (16, 12)
        red, green, blue = image.getpixel((0, 0))
        assert red > 240 and green < 15 and blue < 15
