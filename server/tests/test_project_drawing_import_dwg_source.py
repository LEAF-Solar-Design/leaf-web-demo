"""C02 canonical original-DWG reopen, with real immutable store/cache readers."""
from contextlib import contextmanager
import hashlib
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import project_drawing_editor as editor
import dwg_convert
import store

DXF = b"0\nSECTION\n2\nENTITIES\n0\nLINE\n5\n2B\n8\nWalls\n10\n0\n20\n0\n11\n10\n21\n10\n0\nENDSEC\n0\nEOF\n"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def imported(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_DRAWING_STORE", "legacy")
    org, project_id, drawing_id, version_id = (uuid4() for _ in range(4))
    source_id = str(uuid4())
    original = Path(__file__).resolve().parents[2] / "data/rooftop_demo.dwg"
    raw = original.read_bytes()
    assert sha(raw) == "16390e082b6c73d835f21199e7777ae3a60680deae612f81d92048fd26544d21"
    backend = store.FilesystemBackend(str(tmp_path / "store"))
    store.ingest_drawing(backend, str(org), str(original), drawing_id=source_id)
    source_key = store.drawing_version_key(str(org), source_id, 1)
    # Same import provenance fields the ready-upload producer records. Only the
    # external converter is replaced; immutable store resolution/cache is real.
    source = {"kind": "account_upload", "tenant_id": str(org), "drawing_id": source_id,
              "version": 1, "upload_content_sha256": sha(raw),
              "stored_object": {"ref": source_key, "sha256": sha(raw), "bytes": len(raw)}}
    version = SimpleNamespace(version_id=version_id, oss_object=source_key,
        provenance={"schema": "leaf.drawing-import.v1", "source": source})
    binding = SimpleNamespace(organization_id=org, project_id=project_id,
        drawing_id=drawing_id, version=version)
    context = SimpleNamespace(binding=binding)
    monkeypatch.setattr(editor.project, "resolve_context", lambda *args, **kwargs: context)
    monkeypatch.setattr(editor, "_backend", lambda _org: backend)
    monkeypatch.setattr(editor.guest_uploads, "uploads_dir", lambda: tmp_path / "uploads")
    calls = []
    @contextmanager
    def convert(path):
        calls.append(Path(path).read_bytes())
        output = tmp_path / "converted.dxf"
        output.write_bytes(DXF)
        yield output
    monkeypatch.setattr(dwg_convert, "converted_dxf", convert)
    return SimpleNamespace(org=org, project=project_id, drawing=drawing_id,
        version=version_id, source=source, raw=raw, backend=backend, calls=calls)


def read(case):
    return editor.read_dxf(object(), case.project, case.drawing, case.version)


def test_original_dwg_import_reopens_exact_source_then_cache(imported):
    raw, digest = read(imported)
    assert raw == DXF and digest == sha(DXF)
    assert imported.calls == [imported.raw]
    assert read(imported) == (DXF, sha(DXF))
    assert imported.calls == [imported.raw]


@pytest.mark.parametrize("field", ["upload_digest", "stored_digest", "stored_ref", "stored_bytes", "version"])
def test_original_dwg_import_refuses_provenance_mismatch_before_conversion(imported, field):
    if field == "upload_digest": imported.source["upload_content_sha256"] = "0" * 64
    elif field == "stored_digest": imported.source["stored_object"]["sha256"] = "0" * 64
    elif field == "stored_ref": imported.source["stored_object"]["ref"] += "other"
    elif field == "stored_bytes": imported.source["stored_object"]["bytes"] += 1
    else: imported.source["version"] = "head"
    with pytest.raises(editor.EditorError) as error:
        read(imported)
    assert error.value.code == "PROJECT_EDITOR_SOURCE_INVALID"
    assert imported.calls == []


def test_imported_dxf_keeps_its_original_bytes(imported):
    payload = b'{"layers":[],"polylines":[]}'
    imported.backend.put(imported.source["stored_object"]["ref"], payload)
    imported.source["stored_object"].update(sha256=sha(payload), bytes=len(payload))
    imported.source["upload_content_sha256"] = sha(DXF)
    key = editor.upload_source_key(imported.org, imported.source["drawing_id"], sha(DXF))
    imported.backend.put(key, DXF)
    assert read(imported) == (DXF, sha(DXF))
    assert imported.calls == []


def test_imported_dwg_uses_pinned_version_even_after_new_head(imported, tmp_path):
    later = tmp_path / "later.dwg"
    later.write_bytes(imported.raw + b"later-version")
    store.put_drawing(imported.backend, str(imported.org), imported.source["drawing_id"],
                      str(later), 1)
    assert store.load_manifest(imported.backend, str(imported.org), imported.source["drawing_id"])["head"] == 2
    assert read(imported) == (DXF, sha(DXF))
    assert imported.calls == [imported.raw]
