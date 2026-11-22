"""X04 encoded source bytes survive intake and JSON replay without file access."""
import hashlib
import json
import pytest
from dxf_intake import parse_dxf_bytes, DxfParseError
from test_x04_original_bytes_replay import RAW, guard_source
from test_x04_minsert_arrays import TOOL, selected
import tool_loader


@pytest.mark.parametrize("bom,crlf", [(False, False), (True, False), (False, True), (True, True)])
def test_utf8_bom_crlf_and_nonascii_references_preserve_original_bytes(tmp_path, monkeypatch, bom, crlf):
    tool_loader.run_tool_dynamic(TOOL, {}, {}, aps_live=False)
    attempts = guard_source(monkeypatch, tmp_path / "source.dxf")
    name = "café-屋根.shx"
    encoded = RAW.replace(b"notes.shx", name.encode("utf-8"))
    if crlf:
        encoded = encoded.replace(b"\n", b"\r\n")
    if bom:
        encoded = b"\xef\xbb\xbf" + encoded
    raw = bytearray(encoded)
    original_hash = hashlib.sha256(raw).hexdigest()
    intake = parse_dxf_bytes(raw)
    assert bytes(raw) == encoded and hashlib.sha256(raw).hexdigest() == original_hash
    assert intake["dependency_report"]["dependencies"][0]["name"] == name
    restored = json.loads(json.dumps(intake, allow_nan=False))
    outcome = selected(restored)
    assert outcome["ok"] and outcome["result"]["count"] == 7
    assert outcome["overlay"]["highlight_handles"] == ["A", "B"]
    assert bytes(raw) == encoded and hashlib.sha256(raw).hexdigest() == original_hash and attempts == []


@pytest.mark.parametrize("encoded,message", [
    (b"AutoCAD Binary DXF\r\n\x1a\x00", "binary DXF"),
    ("0\nSECTION\n2\nENTITIES\n".encode("utf-16"), "no DXF group-code pairs"),
    (b"\xff\xfe\xfd", "no DXF group-code pairs"),
])
def test_unsupported_encoding_or_structure_fails_explicitly_without_reopen(tmp_path, monkeypatch, encoded, message):
    attempts = guard_source(monkeypatch, tmp_path / "source.dxf")
    raw = bytearray(encoded)
    digest = hashlib.sha256(raw).hexdigest()
    with pytest.raises(DxfParseError, match=message):
        parse_dxf_bytes(raw)
    assert bytes(raw) == encoded and hashlib.sha256(raw).hexdigest() == digest and attempts == []
    # The existing reader uses UTF-8 replacement decoding. These negative inputs
    # assert its explicit unsupported-format/structure errors, not strict UTF-8 rejection.
