"""Original bytes and real single-frame QuickTime/JPEG producer coverage."""
import hashlib
import gzip
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


def test_full_corpus_original_bytes_over_5000_rows_survive_storage(tmp_path, monkeypatch):
    import store
    import write_loop
    import json
    backend = store.FilesystemBackend(str(tmp_path))
    monkeypatch.setattr(write_loop, 'upload_backend_for_tenant', lambda tenant: backend)
    rows = b''.join(json.dumps({'row': index}).encode() + b'\n' for index in range(6001))
    raw = gzip.compress(rows, mtime=0)
    org, project = uuid.uuid4(), uuid.uuid4()
    material = references.prepare_and_store(org, project, 'BTC.JSONL.GZ', 'application/gzip', raw)
    assert material == references.prepare_and_store(org, project, 'BTC.JSONL.GZ', 'application/gzip', raw)
    assert material['sha256'] == hashlib.sha256(raw).hexdigest()
    assert material['media_type'] == 'application/gzip'
    downloaded = references.read_content(org, project, material)
    assert downloaded == raw
    assert gzip.decompress(downloaded) == rows
    assert len(gzip.decompress(downloaded).splitlines()) == 6001
    foreign = uuid.uuid4()
    with pytest.raises(references.ReferenceStoreUnavailable):
        references.read_content(org, foreign, material)


@pytest.mark.parametrize('payload', [
    b'', b'\n', b'[]\n', b'{"x":NaN}\n', b'{"x":Infinity}\n', b'{"x":1e999}\n',
    b'{"x":1,"x":2}\n', b'{"x":{"a":1,"a":2}}\n', b'{"x":"\xff"}\n', b'{\n',
    b'{"x":1}\n\n',
])
def test_corpus_rejects_invalid_json_rows(payload):
    with pytest.raises(references.InvalidReference):
        references.validate('x.jsonl.gz', 'application/gzip', gzip.compress(payload))


def test_corpus_gzip_envelope_crc_and_limits(monkeypatch):
    raw = gzip.compress(b'{"x":1}\n')
    for invalid in (raw[:-1], raw + b'junk', raw + raw,
                    raw[:-8] + bytes([raw[-8] ^ 1]) + raw[-7:], b'not gzip'):
        with pytest.raises(references.InvalidReference):
            references.validate('x.jsonl.gz', 'application/gzip', invalid)
    for name, mime in [('x.gz', 'application/gzip'), ('x.jsonl.gz', 'application/json'),
                       ('x.jsonl.gz', 'application/gzip; charset=utf-8')]:
        with pytest.raises(references.InvalidReference):
            references.validate(name, mime, raw)
    monkeypatch.setattr(references, 'CORPUS_MAX_BYTES', len(raw) - 1)
    with pytest.raises(references.InvalidReference):
        references.validate('x.jsonl.gz', 'application/gzip', raw)
    monkeypatch.setattr(references, 'CORPUS_MAX_BYTES', len(raw))
    assert references.validate('x.jsonl.gz', 'application/gzip', raw)
    monkeypatch.setattr(references, 'CORPUS_EXPANDED_MAX_BYTES', 7)
    with pytest.raises(references.InvalidReference):
        references.validate('x.jsonl.gz', 'application/gzip', raw)
    monkeypatch.setattr(references, 'CORPUS_EXPANDED_MAX_BYTES', 100)
    monkeypatch.setattr(references, 'CORPUS_LINE_MAX_BYTES', 6)
    with pytest.raises(references.InvalidReference):
        references.validate('x.jsonl.gz', 'application/gzip', raw)
    monkeypatch.setattr(references, 'CORPUS_LINE_MAX_BYTES', 7)
    assert references.validate('x.jsonl.gz', 'application/gzip', raw)
    monkeypatch.setattr(references, 'CORPUS_MAX_ROWS', 1)
    with pytest.raises(references.InvalidReference):
        references.validate('x.jsonl.gz', 'application/gzip', gzip.compress(b'{}\n{}\n'))


def test_corpus_chunk_and_line_boundaries_without_final_newline():
    import random
    import json
    randomizer = random.Random(17)
    rows = [json.dumps({'text': ''.join(randomizer.choices('abcdef0123456789', k=2048))}) for _ in range(70)]
    expanded = '\n'.join(rows).encode()
    raw = gzip.compress(expanded)
    assert len(raw) > 65536 and len(expanded) > 65536
    assert references.validate('chunked.jsonl.gz', 'application/gzip', raw)['sha256'] == hashlib.sha256(raw).hexdigest()


def test_corpus_postgres_upgrade_route_and_scope(tmp_path, monkeypatch):
    """Run only on an explicitly allocated private PostgreSQL proof producer."""
    import os
    from test_project_drawing_editor import canonical_ci_pg
    managed = os.environ.get('LEAF_E364B_PG_PARENT') and os.environ.get('DATABASE_URL')
    if not managed and not canonical_ci_pg():
        pytest.skip('requires managed or verified canonical disposable PostgreSQL producer')
    import platform_link
    db = platform_link.platform_db()
    db.apply_migration()
    from leaf_platform import api, project_lifecycle, store as metadata_store
    import store as blobs
    import write_loop
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    monkeypatch.setenv('LEAF_AUTH_LIVE', '0')
    backend = blobs.FilesystemBackend(str(tmp_path))
    monkeypatch.setattr(write_loop, 'upload_backend_for_tenant', lambda tenant: backend)
    org = metadata_store.create_org(name='Private corpus proof')
    actor = metadata_store.create_identity_binding(org.org_id, 'auth0', str(uuid.uuid4()), role='owner')
    project = project_lifecycle.create_blank_project(org.org_id, actor.binding_id,
        name='Original corpus', idempotency_key=str(uuid.uuid4()))['project']['project_id']
    app = FastAPI()
    app.include_router(api.router)
    headers = {'X-Org-Id': str(org.org_id), 'X-Actor-Binding-Id': str(actor.binding_id)}
    url = f'/api/projects/{project}/reference-files'
    raw = gzip.compress(b'{}\n' * 6001)
    migration = Path(__file__).resolve().parents[2] / 'platform/migrations/0099_lab_corpus_reference_materials.sql'
    with TestClient(app) as client:
        old = client.post(url + '?filename=notes.txt', content=b'original notes',
            headers={**headers, 'Content-Type': 'text/plain'})
        assert old.status_code == 201, old.text
        # Execute the additive SQL twice against real catalog constraints.
        for _ in range(2):
            with db.transaction() as conn:
                conn.execute(migration.read_text())
        db.assert_schema_current()
        response = client.post(url + '?filename=BTC.jsonl.gz', content=raw,
            headers={**headers, 'Content-Type': 'application/gzip'})
        assert response.status_code == 201, response.text
        material = response.json()['material']
        assert material['sha256'] == hashlib.sha256(raw).hexdigest()
        assert material['media_type'] == 'application/gzip' and material['byte_size'] == len(raw)
        assert material['preview_status'] == 'unsupported' and material['model_status'] == 'not_consumed'
        retry = client.post(url + '?filename=BTC.jsonl.gz', content=raw,
            headers={**headers, 'Content-Type': 'application/gzip'})
        assert retry.json()['material'] == material
        saved = client.get(url, headers=headers).json()['materials']
        assert {item['sha256'] for item in saved} == {material['sha256'], old.json()['material']['sha256']}
        downloaded = client.get(url + '/' + material['material_id'] + '/content', headers=headers)
        assert downloaded.content == raw and len(gzip.decompress(downloaded.content).splitlines()) == 6001
        stranger = metadata_store.create_identity_binding(org.org_id, 'auth0', str(uuid.uuid4()), role='owner')
        denied = {**headers, 'X-Actor-Binding-Id': str(stranger.binding_id)}
        assert client.get(url, headers=denied).status_code == 403
        assert client.post(url + '?filename=BTC.jsonl.gz', content=raw,
            headers={**denied, 'Content-Type': 'application/gzip'}).status_code == 403
        with db.transaction() as conn:
            conn.execute("UPDATE identity_bindings SET status='revoked', revoked_at=NOW() WHERE binding_id=%s", (actor.binding_id,))
        assert client.get(url + '/' + material['material_id'] + '/content', headers=headers).status_code == 403
    db.reset_pool()
