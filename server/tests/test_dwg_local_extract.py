"""
The APS-free DWG read lane (dwg2dxf -> dxf_intake) and the engine toggle's
server half: sandboxed conversion, structured fail-closed rejections, engine
routing (per-upload field + LEAF_GUEST_DWG_EXTRACT default), the upfront
availability gate, policy advertisement, and the byte-identical APS path.

The subprocess plumbing is exercised through a STUB converter that honors the
real dwg2dxf argv contract ([bin, -y, -o, OUT, SRC]) so every mode runs on any
host; the one test that needs the REAL binary (converting the repo's real
data/rooftop_demo.dwg) skips with an allowlisted reason where dwg2dxf is not
installed and runs wherever it is (the app container ships it).

Run:  cd server && python -m pytest tests/test_dwg_local_extract.py -q
"""
from __future__ import annotations

import io
import json
import os
import shutil
import stat
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import dwg_convert
import dxf_intake
import guest_uploads
import write_loop

# Passes the route's cheap AC1 magic sniff; hostile to any real converter.
MALFORMED_DWG = b"AC1032" + b"\x00" * 64 + b"this is not a drawing"

# What the stub "converts" every source to — coordinates nothing else in the
# repo shares (rooftop demo lives around 14000-15500).
STUB_DXF = (
    "0\nSECTION\n2\nENTITIES\n"
    "0\nLWPOLYLINE\n5\nCAFE\n8\nConverted\n70\n1\n"
    "10\n771.5\n20\n882.25\n10\n993.0\n20\n104.75\n10\n555.125\n20\n616.5\n"
    "0\nENDSEC\n0\nEOF\n"
)
STUB_COORD = 771.5
ROOFTOP_COORD = 14323.816  # first polyline x of data/rooftop_demo.intake.json

ROOFTOP_DWG = Path(__file__).resolve().parents[2] / "data" / "rooftop_demo.dwg"
_REAL_DWG2DXF = shutil.which("dwg2dxf")


def _stub_converter(tmp_path: Path, mode: str = "ok") -> Path:
    """A platform-appropriate fake dwg2dxf honoring the real argv contract,
    so what's under test is the actual subprocess plumbing."""
    stub_py = tmp_path / f"stub_{mode}.py"
    stub_py.write_text(textwrap.dedent(f"""
        import sys, time
        mode = {mode!r}
        out = sys.argv[sys.argv.index("-o") + 1]
        src = sys.argv[-1]
        if mode == "fail":
            print("ERROR: Invalid DWG")
            sys.exit(1)
        if mode == "empty":
            sys.exit(0)
        if mode == "sleep":
            time.sleep(30)
            sys.exit(0)
        open(src, "rb").read()  # the staged source must exist and be readable
        with open(out, "w") as fh:
            fh.write({STUB_DXF!r})
        sys.exit(0)
    """), encoding="utf-8")
    if os.name == "nt":
        wrapper = tmp_path / f"stub_{mode}.bat"
        wrapper.write_text(f'@echo off\r\n"{sys.executable}" "{stub_py}" %*\r\n',
                           encoding="utf-8")
    else:
        wrapper = tmp_path / f"stub_{mode}.sh"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{stub_py}" "$@"\n',
                           encoding="utf-8")
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return wrapper


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_GUEST_STORE_DIR", str(tmp_path / "guest"))
    monkeypatch.setenv("LEAF_UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "store"))
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
    monkeypatch.delenv("LEAF_GUEST_DWG_EXTRACT", raising=False)
    monkeypatch.delenv("LEAF_DWG2DXF_BIN", raising=False)
    guest_uploads._reset_rate_state()
    # Deterministic tests: run extraction inline instead of a thread.
    monkeypatch.setattr(
        guest_uploads, "start_extraction_thread",
        lambda tenant_id, drawing_id, ext: guest_uploads.run_extraction(
            tenant_id, drawing_id, ext))
    return TestClient(app_module.app)


def _upload(client, data=MALFORMED_DWG, name="site.dwg", engine=None,
            headers=None):
    form = {"engine": engine} if engine else None
    return client.post("/api/drawings/upload",
                       files={"file": (name, io.BytesIO(data))},
                       data=form, headers=headers or {})


def _status(client, receipt):
    return client.get(
        f"/api/drawings/{receipt['drawing_id']}/upload-status",
        headers={"X-Tenant-Id": receipt["tenant_id"]}).json()


# --------------------------------------------------------------------------- #
# dwg_convert unit coverage (stub converter, real subprocess)
# --------------------------------------------------------------------------- #
def test_convert_success_parses_and_cleans_scratch(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_DWG2DXF_BIN", str(_stub_converter(tmp_path)))
    src = tmp_path / "u.dwg"
    src.write_bytes(MALFORMED_DWG)
    with dwg_convert.converted_dxf(src) as dxf_path:
        scratch = dxf_path.parent
        intake = dxf_intake.parse_dxf_file(dxf_path, source_name="u.dwg")
    assert intake["polylines"][0]["pts"][0][0] == STUB_COORD
    assert not scratch.exists(), "the conversion scratch dir must be deleted"


def test_convert_failure_is_a_structured_rejection(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_DWG2DXF_BIN",
                       str(_stub_converter(tmp_path, "fail")))
    src = tmp_path / "u.dwg"
    src.write_bytes(MALFORMED_DWG)
    with pytest.raises(dwg_convert.ConvertError) as err:
        with dwg_convert.converted_dxf(src):
            raise AssertionError("a failed conversion must never yield")
    assert err.value.error_code == "BAD_PARAMS"
    assert err.value.retryable is False


def test_convert_empty_output_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_DWG2DXF_BIN",
                       str(_stub_converter(tmp_path, "empty")))
    src = tmp_path / "u.dwg"
    src.write_bytes(MALFORMED_DWG)
    with pytest.raises(dwg_convert.ConvertError) as err:
        with dwg_convert.converted_dxf(src):
            pass
    assert err.value.error_code == "BAD_PARAMS"


def test_convert_timeout_kills_the_child_and_reports(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_DWG2DXF_BIN",
                       str(_stub_converter(tmp_path, "sleep")))
    monkeypatch.setenv("LEAF_DWG_CONVERT_TIMEOUT_S", "1")
    src = tmp_path / "u.dwg"
    src.write_bytes(MALFORMED_DWG)
    with pytest.raises(dwg_convert.ConvertError) as err:
        with dwg_convert.converted_dxf(src):
            pass
    assert err.value.error_code == "TIMEOUT"
    assert err.value.retryable is False


def test_convert_output_size_cap(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_DWG2DXF_BIN", str(_stub_converter(tmp_path)))
    monkeypatch.setenv("LEAF_DWG_CONVERT_MAX_OUTPUT_BYTES", "16")
    src = tmp_path / "u.dwg"
    src.write_bytes(MALFORMED_DWG)
    with pytest.raises(dwg_convert.ConvertError) as err:
        with dwg_convert.converted_dxf(src):
            pass
    assert err.value.error_code == "BAD_PARAMS"
    assert "output cap" in err.value.message


def test_convert_unavailable_raises_internal(monkeypatch, tmp_path):
    monkeypatch.setattr(dwg_convert, "dwg2dxf_bin", lambda: None)
    src = tmp_path / "u.dwg"
    src.write_bytes(MALFORMED_DWG)
    with pytest.raises(dwg_convert.ConvertError) as err:
        with dwg_convert.converted_dxf(src):
            pass
    assert err.value.error_code == "INTERNAL"


@pytest.mark.skipif(_REAL_DWG2DXF is None,
                    reason="dwg2dxf binary not installed on this host")
def test_real_dwg2dxf_converts_the_repo_fixture(monkeypatch):
    """REAL producer topology: GNU dwg2dxf converting the repo's real DWG.

    Runs wherever the binary exists (the app container ships it; see
    deploy/Dockerfile.app); allowlisted skip elsewhere."""
    monkeypatch.delenv("LEAF_DWG2DXF_BIN", raising=False)
    with dwg_convert.converted_dxf(ROOFTOP_DWG) as dxf_path:
        intake = dxf_intake.parse_dxf_file(dxf_path,
                                           source_name="rooftop_demo.dwg")
    assert intake["layers"], "the real drawing must yield real layer names"
    assert intake["polylines"], "the real drawing must yield real polylines"


# --------------------------------------------------------------------------- #
# engine resolution
# --------------------------------------------------------------------------- #
def test_engine_default_auto_tracks_converter_availability(monkeypatch, tmp_path):
    monkeypatch.delenv("LEAF_GUEST_DWG_EXTRACT", raising=False)
    monkeypatch.setenv("LEAF_DWG2DXF_BIN", str(_stub_converter(tmp_path)))
    assert guest_uploads.dwg_extract_mode() == "local"
    monkeypatch.setattr(dwg_convert, "dwg2dxf_bin", lambda: None)
    assert guest_uploads.dwg_extract_mode() == "aps"
    # An explicit value wins regardless of availability; a typo resolves auto.
    monkeypatch.setenv("LEAF_GUEST_DWG_EXTRACT", "aps")
    assert guest_uploads.dwg_extract_mode() == "aps"
    monkeypatch.setenv("LEAF_GUEST_DWG_EXTRACT", "lokal")
    assert guest_uploads.dwg_extract_mode() == "aps"  # auto: no converter


# --------------------------------------------------------------------------- #
# end-to-end through the route (stub converter)
# --------------------------------------------------------------------------- #
def test_dwg_local_upload_serves_converted_geometry(client, monkeypatch, tmp_path):
    """Acceptance 1's shape: a .dwg upload on the Local engine lands the SAME
    intake surface a native DXF upload lands — status ready, geometry from the
    conversion of their bytes, never the cached rooftop demo."""
    monkeypatch.setenv("LEAF_DWG2DXF_BIN", str(_stub_converter(tmp_path)))
    r = _upload(client, engine="local")
    assert r.status_code == 202, r.text
    receipt = r.json()
    assert receipt["status"] == "extracting"
    assert _status(client, receipt)["status"] == "ready"

    tenant, did = receipt["tenant_id"], receipt["drawing_id"]
    marker = guest_uploads.read_marker(
        write_loop.upload_backend_for_tenant(tenant), tenant, did)
    assert marker["extract_engine"] == "local"

    i = client.get(f"/api/drawings/{did}/intake",
                   headers={"X-Tenant-Id": tenant})
    assert i.status_code == 200
    intake = i.json()["intake"]
    coords = [c for p in intake["polylines"] for pt in p["pts"] for c in pt]
    assert STUB_COORD in coords, "the converted geometry must be served"
    assert ROOFTOP_COORD not in coords, "the demo intake must NEVER leak in"

    # The v1 version blob still holds the RAW DWG bytes (what a later live
    # write signs and sends to APS as HostDwg) — conversion feeds the intake
    # cache only, exactly like the broker path.
    import store
    backend = write_loop.upload_backend_for_tenant(tenant)
    manifest = json.loads(
        backend.get(store.manifest_key(tenant, did)).decode("utf-8"))
    assert manifest["head"] == 1
    assert backend.get(
        store.drawing_version_key(tenant, did, 1)) == MALFORMED_DWG


def test_dwg_local_malformed_fails_closed_process_healthy(
        client, monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_DWG2DXF_BIN",
                       str(_stub_converter(tmp_path, "fail")))
    r = _upload(client, engine="local")
    assert r.status_code == 202
    view = _status(client, r.json())
    assert view["status"] == "failed"
    assert view["error"]["error_code"] == "BAD_PARAMS"
    assert view["error"]["retryable"] is False

    # Fail CLOSED means the drawing never became readable...
    tenant, did = r.json()["tenant_id"], r.json()["drawing_id"]
    i = client.get(f"/api/drawings/{did}/intake",
                   headers={"X-Tenant-Id": tenant})
    assert i.status_code == 404

    # ...and the process stays healthy: the next upload works end to end.
    monkeypatch.setenv("LEAF_DWG2DXF_BIN", str(_stub_converter(tmp_path)))
    r2 = _upload(client, data=MALFORMED_DWG + b"2", engine="local")
    assert r2.status_code == 202
    assert _status(client, r2.json())["status"] == "ready"


def test_dwg_aps_engine_is_byte_identical_at_aps_live_0(client, monkeypatch, tmp_path):
    """Acceptance 2: the APS side of the toggle behaves exactly like today —
    even when the local converter is present and would have worked."""
    monkeypatch.setenv("LEAF_DWG2DXF_BIN", str(_stub_converter(tmp_path)))
    r = _upload(client, engine="aps")
    assert r.status_code == 202
    view = _status(client, r.json())
    assert view["status"] == "failed"
    assert view["error"] == {
        "error_code": "APS_UNAVAILABLE",
        "message": "DWG extraction requires the live APS path; "
                   "upload a DXF to try the local demo",
        "retryable": False,
    }


def test_dwg_default_engine_used_when_field_absent(client, monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_DWG2DXF_BIN", str(_stub_converter(tmp_path)))
    r = _upload(client)  # no engine field: auto resolves local (stub present)
    assert r.status_code == 202
    assert _status(client, r.json())["status"] == "ready"
    tenant, did = r.json()["tenant_id"], r.json()["drawing_id"]
    marker = guest_uploads.read_marker(
        write_loop.upload_backend_for_tenant(tenant), tenant, did)
    assert marker["extract_engine"] == "local"


def test_same_bytes_other_engine_is_a_new_drawing_not_a_dedupe_hit(
        client, monkeypatch, tmp_path):
    """sol-critic #552 round-1 RED: a content-dedupe hit must never silently
    override the visible toggle. Same bytes + same engine recover the same
    receipt; same bytes on the OTHER engine are a DIFFERENT drawing whose own
    engine really runs."""
    monkeypatch.setenv("LEAF_DWG2DXF_BIN", str(_stub_converter(tmp_path)))
    first = _upload(client, engine="local").json()
    tenant = first["tenant_id"]
    assert _status(client, first)["status"] == "ready"

    # Same bytes + same engine: the SAME drawing (idempotent recovery intact).
    again = _upload(client, engine="local",
                    headers={"X-Tenant-Id": tenant}).json()
    assert again["drawing_id"] == first["drawing_id"]

    # Same bytes + APS engine: a NEW drawing that really runs the APS branch
    # (honest APS_UNAVAILABLE at APS_LIVE=0) while the local drawing stays
    # ready and untouched.
    other = _upload(client, engine="aps",
                    headers={"X-Tenant-Id": tenant}).json()
    assert other["drawing_id"] != first["drawing_id"]
    aps_view = _status(client, other)
    assert aps_view["status"] == "failed"
    assert aps_view["error"]["error_code"] == "APS_UNAVAILABLE"
    assert _status(client, first)["status"] == "ready"

    marker = guest_uploads.read_marker(
        write_loop.upload_backend_for_tenant(tenant), tenant,
        other["drawing_id"])
    assert marker["extract_engine"] == "aps"


def test_engine_field_garbage_is_400(client):
    r = _upload(client, engine="cloud")
    assert r.status_code == 400
    assert r.json()["error"]["error_code"] == "BAD_PARAMS"
    assert "engine" in r.json()["error"]["message"]


def test_local_engine_unavailable_is_upfront_503_and_burns_no_quota(
        client, monkeypatch):
    monkeypatch.setattr(dwg_convert, "dwg2dxf_bin", lambda: None)
    r = _upload(client, engine="local")
    assert r.status_code == 503
    assert "not available" in r.json()["error"]["message"]
    with guest_uploads._RATE_LOCK:
        assert guest_uploads._RATE_STATE["total"] == 0


def test_dxf_uploads_ignore_the_dwg_engine_field(client, monkeypatch):
    """The toggle governs DWG only; a .dxf upload with the field set still
    parses locally and records no engine."""
    monkeypatch.setattr(dwg_convert, "dwg2dxf_bin", lambda: None)
    dxf = ("0\nSECTION\n2\nENTITIES\n"
           "0\nLWPOLYLINE\n5\nAB\n8\nL1\n70\n0\n"
           "10\n1.5\n20\n2.5\n10\n3.5\n20\n4.5\n"
           "0\nENDSEC\n0\nEOF\n").encode("utf-8")
    r = _upload(client, data=dxf, name="mine.dxf", engine="local")
    assert r.status_code == 202
    receipt = r.json()
    assert _status(client, receipt)["status"] == "ready"
    marker = guest_uploads.read_marker(
        write_loop.upload_backend_for_tenant(receipt["tenant_id"]),
        receipt["tenant_id"], receipt["drawing_id"])
    assert marker["extract_engine"] is None


def test_policy_advertises_the_engine_toggle(client, monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_DWG2DXF_BIN", str(_stub_converter(tmp_path)))
    body = client.get("/api/site/guest-upload-policy").json()
    assert body["dwg_engines"] == ["local", "aps"]
    assert body["dwg_engine_default"] == "local"
    assert body["dwg_local_ok"] is True
    monkeypatch.setattr(dwg_convert, "dwg2dxf_bin", lambda: None)
    body = client.get("/api/site/guest-upload-policy").json()
    assert body["dwg_engine_default"] == "aps"
    assert body["dwg_local_ok"] is False
