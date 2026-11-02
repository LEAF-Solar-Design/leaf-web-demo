"""Original bytes and real single-frame QuickTime/JPEG producer coverage."""
import hashlib
import io
from pathlib import Path
import struct
import sys
import uuid

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'da'))
import project_reference_materials as references


def atom(kind, payload):
    return struct.pack('>I4s', 8 + len(payload), kind) + payload


def movie():
    """Mux a Pillow-produced JPEG as one real 16x8, one-second MOV frame.

    QuickTime producer topology: ftyp/mdat/moov, video track, self-contained
    data reference, JPEG sample description, timing/chunk/size/offset tables.
    No codec, metadata, or sample payload is replaced with dummy text.
    """
    output = io.BytesIO()
    Image.new('RGB', (16, 8), 'red').save(output, format='JPEG')
    jpeg = output.getvalue()
    ftyp = atom(b'ftyp', b'qt  ' + struct.pack('>I', 0) + b'qt  ')
    matrix = struct.pack('>9I', 65536, 0, 0, 0, 65536, 0, 0, 0, 1073741824)
    mvhd = atom(b'mvhd', struct.pack('>6I', 0, 0, 0, 1, 1, 65536) + struct.pack('>H', 256)
                + bytes(10) + matrix + bytes(24) + struct.pack('>I', 2))
    tkhd = atom(b'tkhd', struct.pack('>6I', 3, 0, 0, 1, 0, 1) + bytes(16)
                + matrix + struct.pack('>II', 16 << 16, 8 << 16))
    mdhd = atom(b'mdhd', struct.pack('>5I2H', 0, 0, 0, 1, 1, 0, 0))
    hdlr = atom(b'hdlr', bytes(4) + b'mhlrvide' + bytes(12) + b'\x00')
    sample = (bytes(6) + struct.pack('>HHH4sIIHHIIIH', 1, 0, 0, b'jpeg', 0, 0,
              16, 8, 72 << 16, 72 << 16, 0, 1) + bytes(32) + struct.pack('>Hh', 24, -1))
    stsd = atom(b'stsd', struct.pack('>II', 0, 1) + atom(b'jpeg', sample))
    stts = atom(b'stts', struct.pack('>4I', 0, 1, 1, 1))
    stsc = atom(b'stsc', struct.pack('>5I', 0, 1, 1, 1, 1))
    stsz = atom(b'stsz', struct.pack('>4I', 0, 0, 1, len(jpeg)))
    stco = atom(b'stco', struct.pack('>3I', 0, 1, len(ftyp) + 8))
    dref = atom(b'dref', struct.pack('>II', 0, 1) + atom(b'url ', struct.pack('>I', 1)))
    minf = atom(b'minf', atom(b'vmhd', struct.pack('>I4H', 1, 0, 0, 0, 0))
                + atom(b'dinf', dref) + atom(b'stbl', stsd + stts + stsc + stsz + stco))
    moov = atom(b'moov', mvhd + atom(b'trak', tkhd + atom(b'mdia', mdhd + hdlr + minf)))
    return ftyp + atom(b'mdat', jpeg) + moov


@pytest.mark.parametrize('name,mime,raw', [
    ('lesson.TXT', 'text/plain; charset=utf-8', 'Teach café\r\n'.encode()),
    ('lesson.json', 'application/json', b'{"lessons":[{"title":"One"}],"stages":273}'),
    ('camera.MOV', 'video/quicktime', movie()),
])
def test_supported_original_bytes_are_immutable_and_project_scoped(tmp_path, monkeypatch, name, mime, raw):
    import store
    import write_loop
    backend = store.FilesystemBackend(str(tmp_path))
    monkeypatch.setattr(write_loop, 'upload_backend_for_tenant', lambda tenant: backend)
    org, project = uuid.uuid4(), uuid.uuid4()
    first = references.prepare_and_store(org, project, name, mime, raw)
    again = references.prepare_and_store(org, project, name, mime, raw)
    assert first == again
    assert first['sha256'] == hashlib.sha256(raw).hexdigest()
    assert first['byte_size'] == len(raw) and first['name'] == name
    assert not {'width', 'height', 'preview_sha256'} & first.keys()
    key = references.object_key(org, project, first['sha256'])
    assert references.read_content(org, project, first) == raw
    assert references.object_key(org, uuid.uuid4(), first['sha256']) != key
    assert references.object_key(uuid.uuid4(), project, first['sha256']) != key
    with pytest.raises(store.ImmutableConflict):
        backend.put_if_absent_or_verify(key, b'replacement')
    assert backend.get(key) == raw


@pytest.mark.parametrize('name,mime,raw', [
    ('x.txt', 'text/plain', b''), ('x.txt', 'text/plain', b'\xff'),
    ('x.txt', 'text/plain', b'hello\x00world'),
    ('x.json', 'application/json', b'{'), ('x.json', 'application/json', b'{"x":NaN}'),
    ('x.json', 'application/json', b'{"x":1,"x":2}'),
    ('x.json', 'text/plain', b'{}'), ('x.txt', 'image/png', b'text'),
    ('x.json', '', b'{}'), ('x.json', 'application/octet-stream', b'{}'),
    ('../x.txt', 'text/plain', b'text'), ('x\n.txt', 'text/plain', b'text'),
    ('x.zip', 'application/zip', b'PK'), ('x.mov', 'video/mp4', movie()),
    ('x.mov', 'video/quicktime', b'not a movie'),
    ('x.mov', 'video/quicktime', atom(b'ftyp', b'qt  \x00\x00\x00\x00qt  ')),
    ('x.mov', 'video/quicktime', movie()[:-1]),
    ('x.mov', 'video/quicktime', movie().replace(b'vide', b'soun')),
    ('x.mov', 'video/quicktime', movie().replace(b'qt  ', b'isom')),
])
def test_invalid_format_mime_body_or_filename_is_rejected(name, mime, raw):
    with pytest.raises(references.InvalidReference):
        references.validate(name, mime, raw)


def test_actual_type_boundaries_and_teacher_video_size():
    for name, mime, raw in [('x.txt', 'text/plain', b'a' * references.TEXT_MAX_BYTES),
                            ('x.json', 'application/json', b'"' + b'a' * (references.TEXT_MAX_BYTES - 2) + b'"')]:
        assert references.validate(name, mime, raw)['byte_size'] == references.TEXT_MAX_BYTES
        with pytest.raises(references.InvalidReference):
            references.validate(name, mime, raw + b' ')
    raw = movie()
    for size in [55337053, references.VIDEO_MAX_BYTES]:
        padded = raw + atom(b'free', bytes(size - len(raw) - 8))
        assert references.validate('lesson.mov', 'video/quicktime', padded)['byte_size'] == size
    with pytest.raises(references.InvalidReference):
        references.validate('lesson.mov', 'video/quicktime', padded + b' ')


def test_ops_sized_json_preserves_original_bytes(tmp_path, monkeypatch):
    import store
    import write_loop
    backend = store.FilesystemBackend(str(tmp_path))
    monkeypatch.setattr(write_loop, 'upload_backend_for_tenant', lambda tenant: backend)
    org, project = uuid.uuid4(), uuid.uuid4()
    # A valid object at the real scoped Ops input's byte size, without copying private rows.
    prefix, suffix = b'{"records":["', b'"]}'
    raw = prefix + b'x' * (5224278 - len(prefix) - len(suffix)) + suffix
    material = references.prepare_and_store(org, project, 'project-input.json', 'application/json', raw)
    assert material['byte_size'] == 5224278
    assert material['sha256'] == hashlib.sha256(raw).hexdigest()
    assert references.read_content(org, project, material) == raw


def test_failed_storage_retry_and_corruption_refusal(tmp_path, monkeypatch):
    import store
    import write_loop
    backend = store.FilesystemBackend(str(tmp_path))
    org, project = uuid.uuid4(), uuid.uuid4()
    class Offline:
        def put_if_absent_or_verify(self, key, raw):
            raise OSError('offline')
    monkeypatch.setattr(write_loop, 'upload_backend_for_tenant', lambda tenant: Offline())
    with pytest.raises(references.ReferenceStoreUnavailable, match='Original storage failed'):
        references.prepare_and_store(org, project, 'lesson.txt', 'text/plain', b'original')
    monkeypatch.setattr(write_loop, 'upload_backend_for_tenant', lambda tenant: backend)
    material = references.prepare_and_store(org, project, 'lesson.txt', 'text/plain', b'original')
    assert references.read_content(org, project, material) == b'original'
    class Corrupt:
        def get(self, key):
            return b'corrupt!'
    monkeypatch.setattr(write_loop, 'upload_backend_for_tenant', lambda tenant: Corrupt())
    with pytest.raises(references.ReferenceStoreUnavailable, match='integrity'):
        references.read_content(org, project, material)
    monkeypatch.setattr(write_loop, 'upload_backend_for_tenant', lambda tenant: backend)
    with pytest.raises(references.ReferenceStoreUnavailable):
        references.read_content(org, project, {**material, 'byte_size': 1})
    assert backend.get(references.object_key(org, project, material['sha256'])) == b'original'
