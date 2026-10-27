import hashlib
import io
from pathlib import Path
import struct
import subprocess
import sys
import uuid
import zlib

import pytest
from PIL import Image
from pillow_heif import register_heif_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import project_material_images as images


def photo(fmt='PNG', size=(32, 16), **options):
    output = io.BytesIO()
    Image.new('RGB', size, 'red').save(output, format=fmt, **options)
    return output.getvalue()


def test_real_png_and_heif_produce_bounded_clean_jpeg():
    register_heif_opener()
    for fmt, expected in [('PNG', 'image/png'), ('HEIF', 'image/heif')]:
        info, preview = images.decode_image(photo(fmt, (2200, 20)))
        assert info == {'media_type': expected, 'width': 2200, 'height': 20}
        with Image.open(io.BytesIO(preview)) as decoded:
            assert decoded.format == 'JPEG'
            assert max(decoded.size) == 2048
            assert not decoded.getexif()
            assert 'icc_profile' not in decoded.info


def test_jpeg_orientation_is_applied_without_copying_exif():
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = 'private camera description'
    raw = photo('JPEG', (30, 10), exif=exif)
    info, preview = images.decode_image(raw)
    assert (info['width'], info['height']) == (10, 30)
    with Image.open(io.BytesIO(preview)) as result:
        assert result.size == (10, 30)
        assert not result.getexif()
    assert b'private camera description' not in preview


@pytest.mark.parametrize('raw', [b'not a photo', b'<svg/>', b'', photo('JPEG')[:-20], photo('PNG')[:-20], photo('GIF')])
def test_invalid_type_and_truncated_images_fail(raw):
    with pytest.raises(images.InvalidImage):
        images.decode_image(raw)


def test_animation_and_oversized_dimensions_fail():
    output = io.BytesIO()
    Image.new('RGB', (4, 4), 'red').save(output, format='PNG', save_all=True,
                                     append_images=[Image.new('RGB', (4, 4), 'blue')], duration=20)
    with pytest.raises(images.InvalidImage):
        images.decode_image(output.getvalue())
    register_heif_opener()
    multiple = io.BytesIO()
    Image.new('RGB', (4, 4), 'red').save(multiple, format='HEIF', save_all=True,
                                      append_images=[Image.new('RGB', (4, 4), 'blue')])
    with pytest.raises(images.InvalidImage):
        images.decode_image(multiple.getvalue())
    raw = photo()
    header = struct.pack('>II', 10000, 5000) + raw[24:29]
    oversized = raw[:16] + header + struct.pack('>I', zlib.crc32(b'IHDR' + header)) + raw[33:]
    with pytest.raises(images.InvalidImage):
        images.decode_image(oversized)


def test_decoder_has_no_provider_environment_and_has_timeout(monkeypatch):
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'synthetic-credential-must-not-pass')
    real_run = images.subprocess.run
    def inspect(*args, **kwargs):
        assert 'AWS_SECRET_ACCESS_KEY' not in kwargs['env']
        assert 'PATH' not in kwargs['env']
        assert kwargs['close_fds'] is True
        assert kwargs['timeout'] == 15
        assert args[0][1] == '-I'
        return real_run(*args, **kwargs)
    monkeypatch.setattr(images.subprocess, 'run', inspect)
    images.decode_image(photo())
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('decoder', 15)
    monkeypatch.setattr(images.subprocess, 'run', timeout)
    with pytest.raises(images.InvalidImage, match='time limit'):
        images.decode_image(photo())


def test_originals_are_immutable_and_keys_are_project_scoped(tmp_path, monkeypatch):
    import write_loop
    import store
    backend = store.FilesystemBackend(str(tmp_path))
    monkeypatch.setattr(write_loop, 'upload_backend_for_tenant', lambda tenant: backend)
    org, project = uuid.uuid4(), uuid.uuid4()
    raw = photo()
    first = images.prepare_and_store(org, project, 'photo.png', raw)
    second = images.prepare_and_store(org, project, 'renamed.png', raw)
    assert first['sha256'] == second['sha256'] == hashlib.sha256(raw).hexdigest()
    key = images.object_key(org, project, first['sha256'], 'original')
    assert backend.get(key) == raw
    with pytest.raises(store.ImmutableConflict):
        backend.put_if_absent_or_verify(key, b'different')
    assert images.object_key(org, uuid.uuid4(), first['sha256'], 'original') != key
    assert images.read_content(org, project, first, 'original') == raw


def test_failed_preview_store_never_returns_ready_metadata(tmp_path, monkeypatch):
    import write_loop
    class FailedStore:
        def put_if_absent_or_verify(self, key, raw):
            if '/preview/' in key:
                raise OSError('unavailable')
    monkeypatch.setattr(write_loop, 'upload_backend_for_tenant', lambda tenant: FailedStore())
    with pytest.raises(images.MaterialStoreUnavailable):
        images.prepare_and_store(uuid.uuid4(), uuid.uuid4(), 'photo.png', photo())
