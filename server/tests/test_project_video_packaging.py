"""Canonical decoder supply declarations, plus optional real synthetic cage proof."""
from pathlib import Path
import hashlib
import os
import subprocess
import sys

import pytest

import project_video_materials as video
from test_postgres_container_wiring import _logical_lines

ROOT = Path(__file__).resolve().parents[2]


def test_canonical_app_image_supplies_the_fixed_decoder_tools():
    text = (ROOT / 'deploy/Dockerfile.app').read_text(encoding='utf-8')
    lines = _logical_lines(text)
    install = next(line for line in lines if 'apt-get install -y --no-install-recommends git ' in line)
    packages = install.split('apt-get install -y --no-install-recommends ', 1)[1].split(' && ', 1)[0].split()
    assert {'git', 'util-linux', 'ffmpeg', 'bubblewrap'} <= set(packages)
    # The build, rather than this static test, must actually find those executables.
    for binary in video.TOOLS:
        assert f'test -x {binary}' in install
    assert 'TRIXIE_DEBIAN_SECURITY_INRELEASE_SHA256' in install.split('apt-get install', 1)[0]
    assert 'TRIXIE_DEBIAN_UPDATES_INRELEASE_SHA256' in install.split('apt-get install', 1)[0]
    assert install.count('sha256sum -c -') >= 3
    assert [line for line in lines if line.startswith('USER ')][-1] == 'USER 10003:10003'


def test_real_namespace_decoder_with_synthetic_movie_when_runner_supplies_it(tmp_path, monkeypatch):
    if sys.platform != 'linux' or not video._available():
        uid = getattr(os, 'geteuid', lambda: None)()
        missing = [tool for tool in video.TOOLS if not Path(tool).is_file()]
        pytest.skip(f'No real containment proof: platform={sys.platform}, uid={uid}, missing_tools={missing}')
    # Generated local color frames are the only producer input, never original media.
    mkdtemp = video.tempfile.mkdtemp
    monkeypatch.setattr(video.tempfile, 'mkdtemp', lambda **kwargs: mkdtemp(dir=tmp_path, **kwargs))
    movie = tmp_path / 'synthetic.mov'
    result = subprocess.run([
        '/usr/bin/ffmpeg', '-hide_banner', '-nostdin', '-loglevel', 'error',
        '-f', 'lavfi', '-i', 'color=c=red:s=32x24:r=2:d=2',
        '-threads', '1', '-c:v', 'mpeg4', '-f', 'mov', str(movie),
    ], stdin=subprocess.DEVNULL, capture_output=True, timeout=15, shell=False,
        env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
    assert result.returncode == 0, 'Synthetic MOV producer failed; no decoder proof.'
    raw = movie.read_bytes()
    manifest = dict(material_id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', media_type='video/quicktime',
                    sha256=hashlib.sha256(raw).hexdigest(), byte_size=len(raw))
    import time
    value = video.decode(raw, manifest, identity=dict(
        tenant_id='11111111-1111-4111-8111-111111111111', organization_id='11111111-1111-4111-8111-111111111111',
        project_id='33333333-3333-4333-8333-333333333333', actor_binding_id='66666666-6666-4666-8666-666666666666',
        edit_id='55555555-5555-4555-8555-555555555555'),
        transcripts=[dict(material_id='99999999-9999-4999-8999-999999999999', sha256=hashlib.sha256(b'x').hexdigest(), size_bytes=1)],
        recheck=lambda: None, deadline=time.monotonic() + 45)
    assert value['manifest']['decoded_frame_count'] == 4
    assert value['manifest']['frames'][0]['timestamp_ms'] == 0
    assert value['manifest']['frames'][-1]['timestamp_ms'] > 0
    assert len(value['images']) == len(value['manifest']['frames'])
    assert not any(path.is_symlink() for path in tmp_path.iterdir())
    assert sorted(path.name for path in tmp_path.iterdir()) == ['synthetic.mov']
