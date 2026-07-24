"""Offline tests for the DXF extract Activity + extension-based routing.

No network, no APS. These lock the contract that the live guest-upload bug
turned on: DXF must reach an Activity whose HostDwg localName keeps the `.dxf`
extension and whose script ends in the save-safe QUIT.
"""

import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import client  # noqa: E402
import lisp  # noqa: E402


def test_extract_activity_for_routes_by_extension():
    assert client.extract_activity_for("/staged/guest--u-1.dwg") == client.EXTRACT_ACTIVITY
    assert client.extract_activity_for("/staged/guest--u-1.dxf") == client.EXTRACT_DXF_ACTIVITY
    # user-supplied filename, so case must not matter
    assert client.extract_activity_for("/staged/guest--u-1.DXF") == client.EXTRACT_DXF_ACTIVITY
    assert client.extract_activity_for("/staged/guest--u-1.DwG") == client.EXTRACT_ACTIVITY
    # unknown/absent extension falls back to the DWG Activity (never DXF)
    assert client.extract_activity_for("/staged/noext") == client.EXTRACT_ACTIVITY


def test_dxf_activity_spec_keeps_dxf_extension_and_save_safe_quit():
    spec = client.extract_dxf_activity_spec()
    assert spec["id"] == client.EXTRACT_DXF_ACTIVITY
    # the whole point: HostDwg localName carries .dxf, not .dwg
    assert spec["parameters"]["HostDwg"]["localName"] == "input.dxf"
    assert spec["parameters"]["HostDwg"]["localName"].endswith(".dxf")
    script = spec["settings"]["script"]["value"]
    # save-safe quit (marks the doc saved so QUIT asks nothing), NOT the raw quit
    assert "vla-put-Saved" in script
    assert '(command "_.QUIT" "_Y")' not in script
    # same fidelity as the DWG extract — all family probes present
    for marker in ("LAYER|", "PL|", "IN|", "F3|", "BD|", "GEO|", "IMG"):
        assert marker in script


def test_dwg_activity_spec_is_byte_identical_to_pre_change():
    """The live LeafExtract+prod Activity must NOT change. Its script is the
    frozen DWG-proven one; only DXF gets the new ending."""
    dwg = client.extract_activity_spec()
    assert dwg["parameters"]["HostDwg"]["localName"] == "input.dwg"
    assert dwg["settings"]["script"]["value"].rstrip().endswith('(command "_.QUIT" "_Y")')
    # default build_scr() is exactly what the DWG Activity ships
    assert dwg["settings"]["script"]["value"] == lisp.build_scr()


def test_dwg_and_dxf_scripts_differ_only_in_the_quit_line():
    """Same extraction body, different ending — the fidelity guarantee."""
    dwg = lisp.build_scr()
    dxf = lisp.build_scr(quit_form=lisp.QUIT_SAVED)
    dwg_body = dwg.rsplit(lisp.QUIT_DEFAULT, 1)[0]
    dxf_body = dxf.rsplit(lisp.QUIT_SAVED, 1)[0]
    assert dwg_body == dxf_body
    assert hashlib.sha256(dwg_body.encode()).hexdigest() == \
        hashlib.sha256(dxf_body.encode()).hexdigest()


def test_dxf_and_dwg_activities_have_distinct_ids():
    assert client.EXTRACT_ACTIVITY != client.EXTRACT_DXF_ACTIVITY
    assert client.extract_activity_spec()["id"] != client.extract_dxf_activity_spec()["id"]
