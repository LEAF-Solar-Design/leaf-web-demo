"""Synthetic decoder contracts. No original media, provider or installed decoder is used."""
import copy
import hashlib
import io
import json
import os
import time
from types import SimpleNamespace
from pathlib import Path

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
        if name == 'sandbox':
            return
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
    sandbox, probe, decode = synthetic.paths
    assert sandbox == ('/usr/bin/ffprobe', ['-version'])
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
    library_mounts = ['--dir', '/etc/alternatives', '--symlink',
                      '/usr/lib/x86_64-linux-gnu/blas/libblas.so.3',
                      '/etc/alternatives/libblas.so.3-x86_64-linux-gnu']
    monkeypatch.setattr(video, '_library_symlinks', lambda: library_mounts)
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
    start = argv.index('/etc/alternatives') - 1
    assert argv[start:start + len(library_mounts)] == library_mounts
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


@pytest.mark.parametrize('stderr,reason', [
    (b'bwrap: Creating new namespace failed: Operation not permitted\nPRIVATE-input', 'sandbox_denied'),
    (b'bwrap: execvp /private/program: No such file or directory', 'exec_unavailable'),
    (b'prlimit: failed to set a limit: Operation not permitted', 'resource_limit_denied'),
    (b'ffprobe: PRIVATE-media-data', 'process_failed'),
    (b'PRIVATE-media-data: bwrap: Operation not permitted', 'process_failed'),
])
def test_nonzero_child_retains_only_safe_diagnostic_after_reaping(monkeypatch, tmp_path, stderr, reason):
    (tmp_path / 'output').mkdir()
    state = {'reaped': False}
    child = SimpleNamespace(pid=123456, returncode=1, poll=lambda: 1)
    def popen(argv, **kwargs):
        state['argv'] = argv
        state['stderr'] = kwargs['stderr']
        kwargs['stderr'].write(stderr)
        kwargs['stderr'].flush()
        return child
    def finish(proc):
        assert proc is child
        state['reaped'] = True
    monkeypatch.setattr(video.subprocess, 'Popen', popen)
    monkeypatch.setattr(video, '_finish', finish)
    diagnose = video._process_diagnostic
    def after_reap(*args):
        assert state['reaped'] is True
        return diagnose(*args)
    monkeypatch.setattr(video, '_process_diagnostic', after_reap)
    with pytest.raises(video.VideoError, match='^video_incomplete$') as caught:
        video._run(tmp_path, '/usr/bin/ffprobe', ['/input.mov'], 'probe', time.monotonic() + 10, lambda: None)
    assert state['reaped'] is True
    assert caught.value.decoder_diagnostic == dict(
        operation='probe', returncode=1, reason=reason, stderr_state='nonempty',
        stderr_prefix=('bwrap' if stderr.startswith(b'bwrap:') else
                       'prlimit' if stderr.startswith(b'prlimit:') else
                       'ffprobe' if stderr.startswith(b'ffprobe:') else 'other'))
    assert 'PRIVATE' not in str(caught.value.__dict__)
    assert '--unshare-all' in state['argv'] and '--as=536870912' in state['argv']
    assert state['stderr'].closed


def test_diagnostic_read_is_bounded_and_happens_after_reap():
    class Stderr:
        def seek(self, offset):
            assert offset == 0
        def read(self, size):
            assert size == 8192
            return b'bwrap: Permission denied PRIVATE'
    assert video._process_diagnostic('untrusted-name', True, Stderr()) == dict(
        operation='unknown', returncode=None, reason='sandbox_denied',
        stderr_state='nonempty', stderr_prefix='bwrap')


def test_diagnostic_io_error_does_not_replace_original_failure():
    class Stderr:
        def seek(self, offset):
            raise OSError('PRIVATE path')
    assert video._process_diagnostic('decode', -9, Stderr()) == dict(
        operation='decode', returncode=-9, reason='unknown',
        stderr_state='unreadable', stderr_prefix='unknown')


def test_reap_failure_has_precedence_over_process_diagnostic(monkeypatch, tmp_path):
    (tmp_path / 'output').mkdir()
    child = SimpleNamespace(pid=123456, returncode=1, poll=lambda: 1)
    monkeypatch.setattr(video.subprocess, 'Popen', lambda *args, **kwargs: child)
    def failed_reap(proc):
        raise RuntimeError('PRIVATE reap detail')
    monkeypatch.setattr(video, '_finish', failed_reap)
    with pytest.raises(video.VideoError, match='^video_cleanup_failed$') as caught:
        video._run(tmp_path, '/usr/bin/ffprobe', [], 'probe', time.monotonic() + 10, lambda: None)
    assert caught.value.decoder_diagnostic is None


@pytest.mark.parametrize('raw,state,prefix', [
    (b'', 'empty', 'none'),
    (b'bwrap: unrecognized PRIVATE failure', 'nonempty', 'bwrap'),
    (b'ffprobe: PRIVATE failure', 'nonempty', 'ffprobe'),
    (b'PRIVATE-media-data', 'nonempty', 'other'),
    (b'prlimit: PRIVATE failure', 'nonempty', 'prlimit'),
    (None, 'unreadable', 'unknown'),
    (b'\n' + b'PRIVATE' * 2000, 'nonempty', 'other'),
], ids=['empty', 'bwrap', 'ffprobe', 'other', 'prlimit', 'unreadable', 'blank-first-line'])
def test_diagnostic_preserves_closed_stderr_shape_without_content(raw, state, prefix):
    import io
    class Stderr:
        def seek(self, offset):
            assert offset == 0
            if raw is None:
                raise OSError('PRIVATE')
        def read(self, size):
            assert size == 8192
            return io.BytesIO(raw).read(size)
    diagnostic = video._process_diagnostic('probe', 1, Stderr())
    assert diagnostic['stderr_state'] == state
    assert diagnostic['stderr_prefix'] == prefix
    assert diagnostic['reason'] == ('unknown' if raw is None else 'process_failed')
    assert set(diagnostic) == {'operation', 'returncode', 'reason', 'stderr_state', 'stderr_prefix'}
    assert 'PRIVATE' not in str(diagnostic)


def test_sandbox_self_check_uses_supplied_executable_and_shared_budget(monkeypatch, tmp_path):
    calls = []
    recheck = lambda: None
    monkeypatch.setattr(video, '_run', lambda *args: calls.append(args))
    video._require_sandbox(tmp_path, 123, recheck)
    assert calls == [(tmp_path, '/usr/bin/ffprobe', ['-version'], 'sandbox', 123, recheck)]
    producer = Path(__file__).resolve().parents[2] / 'deploy/Dockerfile.app'
    assert 'test -x /usr/bin/ffprobe' in producer.read_text(encoding='utf-8')


@pytest.mark.parametrize('code,expected', [
    ('video_incomplete', 'video_unavailable'),
    ('video_timeout', 'video_timeout'),
    ('video_cleanup_failed', 'video_cleanup_failed'),
    ('video_unavailable', 'video_unavailable'),
])
def test_sandbox_self_check_preserves_safe_diagnostic_and_failure_precedence(monkeypatch, code, expected):
    error = video.VideoError(code)
    error.decoder_diagnostic = dict(operation='sandbox', returncode=1, reason='process_failed',
                                   stderr_state='nonempty', stderr_prefix='bwrap')
    def failure(*args):
        raise error
    monkeypatch.setattr(video, '_run', failure)
    with pytest.raises(video.VideoError) as caught:
        video._require_sandbox(None, 123, lambda: None)
    assert caught.value.code == expected
    assert caught.value.decoder_diagnostic is error.decoder_diagnostic
    if code != 'video_incomplete':
        assert caught.value is error


def test_sandbox_self_check_preserves_authority_failure(monkeypatch):
    error = PermissionError('revoked')
    def failure(*args):
        raise error
    monkeypatch.setattr(video, '_run', failure)
    with pytest.raises(PermissionError) as caught:
        video._require_sandbox(None, 123, lambda: None)
    assert caught.value is error


def test_sandbox_failure_never_starts_media_probe_and_cleans(synthetic, monkeypatch):
    calls = []
    def failure(scratch, binary, args, name, deadline, recheck):
        calls.append((binary, args, name))
        raise video.VideoError('video_incomplete')
    monkeypatch.setattr(video, '_run', failure)
    with pytest.raises(video.VideoError, match='^video_unavailable$'):
        synthetic.call()
    assert calls == [('/usr/bin/ffprobe', ['-version'], 'sandbox')]
    assert not synthetic.scratch.exists()


def test_media_probe_failure_after_sandbox_stays_incomplete(synthetic, monkeypatch):
    names = []
    def failure(scratch, binary, args, name, deadline, recheck):
        names.append(name)
        if name == 'probe':
            raise video.VideoError('video_incomplete')
    monkeypatch.setattr(video, '_run', failure)
    with pytest.raises(video.VideoError, match='^video_incomplete$'):
        synthetic.call()
    assert names == ['sandbox', 'probe']
    assert not synthetic.scratch.exists()


def test_sandbox_diagnostic_operation_is_closed():
    result = video._process_diagnostic('sandbox', 1, io.BytesIO(b'bwrap: PRIVATE'))
    assert result['operation'] == 'sandbox'
    assert 'PRIVATE' not in str(result)


@pytest.fixture
def library_paths(monkeypatch):
    links = {}
    targets = {}
    monkeypatch.setattr(Path, 'is_symlink', lambda path: path in links)
    monkeypatch.setattr(Path, 'exists', lambda path: False)
    monkeypatch.setattr(Path, 'readlink', lambda path: links[path])
    monkeypatch.setattr(Path, 'resolve', lambda path, **kwargs: targets[path])
    monkeypatch.setattr(Path, 'is_file', lambda path: True)
    return links, targets


@pytest.mark.parametrize('libraries', [('libblas',), ('liblapack',), ('libblas', 'liblapack')])
def test_only_supported_library_links_are_recreated(library_paths, libraries):
    links, targets = library_paths
    expected = ['--dir', '/etc/alternatives']
    for library, directory in (('libblas', 'blas'), ('liblapack', 'lapack')):
        if library not in libraries:
            continue
        link = Path(f'/etc/alternatives/{library}.so.3-x86_64-linux-gnu')
        target = Path(f'/usr/lib/x86_64-linux-gnu/{directory}/{library}.so.3')
        links[link] = target
        targets[target] = target.with_name(library + '.so.3.12.1')
        expected.extend(['--symlink', str(target), str(link)])
    assert video._library_symlinks() == expected


def test_direct_library_runtime_adds_no_etc_exposure(library_paths):
    assert video._library_symlinks() == []


@pytest.mark.parametrize('mode', ['unexpected-link', 'escape', 'missing', 'cycle', 'directory', 'regular-entry'])
def test_invalid_library_dependency_fails_closed(library_paths, monkeypatch, mode):
    links, targets = library_paths
    link = Path('/etc/alternatives/libblas.so.3-x86_64-linux-gnu')
    target = Path('/usr/lib/x86_64-linux-gnu/blas/libblas.so.3')
    links[link] = target
    targets[target] = target.with_name('libblas.so.3.12.1')
    if mode == 'unexpected-link':
        links[link] = Path('/etc/private-library')
    elif mode == 'escape':
        targets[target] = Path('/outside/libblas.so.3')
    elif mode in ('missing', 'cycle'):
        error = FileNotFoundError() if mode == 'missing' else RuntimeError('cycle')
        def failure(*args, **kwargs):
            raise error
        monkeypatch.setattr(Path, 'resolve', failure)
    elif mode == 'directory':
        monkeypatch.setattr(Path, 'is_file', lambda path: False)
    else:
        links.clear()
        monkeypatch.setattr(Path, 'exists', lambda path: path == link)
    with pytest.raises(video.VideoError, match='^video_unavailable$'):
        video._library_symlinks()
