#!/usr/bin/env python3
"""Offline tests for the T3-02 blank-DWG creation spike.

Every test here is PURE: no APS credentials, no network, no dollars. The parts
that must be right before a paid run - the .scr the engine executes, the
Activity contract, the byte validation, and the provenance assertion that stops
a wrong-drawing result passing as success - are all exercised here.
"""
from __future__ import annotations

import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import blank_lisp  # noqa: E402
import blank_spike  # noqa: E402


# --------------------------------------------------------------------------- #
# Marker layer: the provenance token
# --------------------------------------------------------------------------- #
def test_new_marker_layer_is_unique_and_valid():
    seen = {blank_lisp.new_marker_layer() for _ in range(200)}
    assert len(seen) == 200, "marker collision: entropy is not per-run"
    for m in seen:
        assert blank_lisp.validate_marker(m) == m
        assert m.startswith(blank_lisp.MARKER_PREFIX)


@pytest.mark.parametrize("bad", [
    "",
    "leaf_blank_lower",                 # lowercase
    'LEAF" (command "_.ERASE")',        # would terminate the LISP string
    "LEAF-BLANK-1",                     # hyphen
    "LEAF BLANK",                       # space
    "LEAF_BLANK_" + "A" * 300,          # over length
    None,
    123,
])
def test_validate_marker_rejects_unsafe(bad):
    with pytest.raises((ValueError, TypeError)):
        blank_lisp.validate_marker(bad)


def test_build_blank_scr_refuses_injected_marker():
    with pytest.raises(ValueError):
        blank_lisp.build_blank_scr('X" "Y')


# --------------------------------------------------------------------------- #
# The .scr the engine actually runs
# --------------------------------------------------------------------------- #
def test_blank_scr_shape():
    m = "LEAF_BLANK_ABCDEF123456"
    scr = blank_lisp.build_blank_scr(m)
    lines = [ln for ln in scr.split("\n") if ln]
    assert len(lines) == 6, lines
    # No stray escape may reach the engine: a real newline inside a LISP string
    # would split the command and hang the WorkItem on a prompt.
    assert chr(92) not in scr
    assert lines[0] == '(setvar "CMDECHO" 0)'
    assert lines[1] == '(command "_.-LAYER" "_Make" "' + m + '" "")'
    assert lines[-1] == '(command "_.QUIT" "_Y")'


def test_blank_scr_saveas_is_explicit_and_targets_result_localname():
    scr = blank_lisp.build_blank_scr("LEAF_BLANK_ABCDEF123456")
    # Explicit format, never "" - the empty answer inherits the engine image's
    # default and makes the output format a silent contract with the engine.
    assert '(command "_.SAVEAS" "' + blank_lisp.SAVEAS_FORMAT + '" "' \
           + blank_lisp.OUT_LOCALNAME + '")' in scr
    assert '(command "_.SAVEAS" "" ' not in scr


def test_blank_scr_dirties_the_database_before_saveas():
    """The layer MAKE must precede SAVEAS: on a pristine drawing SAVEAS can stop
    on a prompt, which hangs the WorkItem to its timeout."""
    scr = blank_lisp.build_blank_scr("LEAF_BLANK_ABCDEF123456")
    assert scr.index("_Make") < scr.index("_.SAVEAS")


# --------------------------------------------------------------------------- #
# The Activity contract - this is what makes it CREATE, not upload
# --------------------------------------------------------------------------- #
def test_activity_spec_has_no_input_drawing():
    spec = blank_lisp.blank_activity_spec("LeafBlankCreate", "Autodesk.AutoCAD+26_0")
    blob = json.dumps(spec)
    assert "HostDwg" not in blob, "a blank CREATE must not take an input drawing"
    assert "input.dwg" not in blob
    assert set(spec["parameters"]) == {"Script", "Result"}
    assert spec["parameters"]["Result"]["verb"] == "put"
    assert spec["parameters"]["Script"]["verb"] == "get"


def test_activity_command_line_has_no_input_switch():
    spec = blank_lisp.blank_activity_spec("LeafBlankCreate", "Autodesk.AutoCAD+26_0")
    cmd = spec["commandLine"][0]
    assert " /i " not in cmd, "no /i: the engine opens its own acad.dwt"
    assert "/s " in cmd
    assert "$(args[Script].path)" in cmd, "the script arrives per-run, not baked in"


def test_activity_spec_is_marker_independent():
    """One Activity version must serve every run. If the marker were baked into
    the Activity body, every create would need a new Activity VERSION."""
    a = blank_lisp.blank_activity_spec("LeafBlankCreate", "Autodesk.AutoCAD+26_0")
    b = blank_lisp.blank_activity_spec("LeafBlankCreate", "Autodesk.AutoCAD+26_0")
    assert a == b
    assert "settings" not in a


# --------------------------------------------------------------------------- #
# Byte validation - never register rubbish as drawing version 1
# --------------------------------------------------------------------------- #
def test_validate_dwg_bytes_accepts_a_real_signature():
    data = b"AC1032" + b"\x00" * blank_lisp.MIN_PLAUSIBLE_DWG_BYTES
    assert blank_spike.validate_dwg_bytes(data) == "AC1032"


def _pad(prefix, as_str=False):
    """Build an oversized payload at call time. Kept out of the parametrize ids:
    a 9KB inline value lands in PYTEST_CURRENT_TEST and overflows the 32767-char
    Windows environment-variable limit."""
    if as_str:
        return prefix + "0" * 9000
    return prefix + b"\x00" * 9000


@pytest.mark.parametrize("case,exc", [
    ("empty", ValueError),        # empty PUT
    ("truncated", ValueError),    # signature present but far under the floor
    ("html", ValueError),         # an error page, not a drawing
    ("dxf", ValueError),          # right size, wrong format
    ("str", TypeError),           # str, not bytes
])
def test_validate_dwg_bytes_fails_closed(case, exc):
    data = {
        "empty": b"",
        "truncated": b"AC1032",
        "html": _pad(b"<html>error</html>"),
        "dxf": _pad(b"DXF   "),
        "str": _pad("AC1032", as_str=True),
    }[case]
    with pytest.raises(exc):
        blank_spike.validate_dwg_bytes(data)


# --------------------------------------------------------------------------- #
# Provenance - the check that makes a 200 meaningless on its own
# --------------------------------------------------------------------------- #
def test_assert_provenance_passes_when_the_marker_round_trips():
    marker = "LEAF_BLANK_ABCDEF123456"
    intake = {"layers": ["0", marker]}
    assert marker in blank_spike.assert_provenance(intake, marker)


def test_assert_provenance_reads_dict_shaped_layers():
    marker = "LEAF_BLANK_ABCDEF123456"
    intake = {"layers": [{"name": "0"}, {"name": marker}]}
    assert marker in blank_spike.assert_provenance(intake, marker)


def test_assert_provenance_reads_nested_families_shape():
    marker = "LEAF_BLANK_ABCDEF123456"
    intake = {"families": {"layers": ["0", marker]}}
    assert marker in blank_spike.assert_provenance(intake, marker)


@pytest.mark.parametrize("intake", [
    {"layers": ["0", "LEAF_BLANK_OTHERRUN0000"]},   # ANOTHER run's drawing
    {"layers": ["0"]},                              # some unrelated drawing
    {"layers": []},
    {},
    None,
])
def test_assert_provenance_fails_on_foreign_bytes(intake):
    """This is the scratch-key-collision guard. A run that reads back another
    drawing's bytes must FAIL, never report a confident wrong PASS."""
    with pytest.raises(RuntimeError, match="PROVENANCE FAIL"):
        blank_spike.assert_provenance(intake, "LEAF_BLANK_ABCDEF123456")


# --------------------------------------------------------------------------- #
# Cost cap - fails closed BEFORE a billable submit
# --------------------------------------------------------------------------- #
def test_cap_guard_blocks_on_workitem_count(monkeypatch):
    monkeypatch.setattr(blank_spike, "_LEDGER",
                        [{"usd_est": 0.001}] * blank_spike.CAP_WORKITEMS)
    with pytest.raises(RuntimeError, match="LANE CAP"):
        blank_spike._cap_guard("create")


def test_cap_guard_blocks_on_dollars(monkeypatch):
    monkeypatch.setattr(blank_spike, "_LEDGER",
                        [{"usd_est": blank_spike.CAP_USD + 1.0}])
    with pytest.raises(RuntimeError, match="LANE CAP"):
        blank_spike._cap_guard("read")


def test_cap_guard_allows_the_authorized_two(monkeypatch):
    monkeypatch.setattr(blank_spike, "_LEDGER", [])
    blank_spike._cap_guard("create")
    monkeypatch.setattr(blank_spike, "_LEDGER", [{"usd_est": 0.01}])
    blank_spike._cap_guard("read")


# --------------------------------------------------------------------------- #
# metered_submit - exact cost for a leg that runs inside a frozen helper
# --------------------------------------------------------------------------- #
def _fake_client(statuses):
    """A MODULE-shaped stand-in for da/client.

    Deliberately a namespace holding plain functions, not a class with methods:
    the real `client.extract` resolves the module-global name `submit_workitem`
    at call time, and that is exactly the indirection metered_submit relies on.
    A bound method would also make identity assertions meaningless, since every
    attribute access rebinds.
    """
    ns = types.SimpleNamespace()
    ns.ENGINE = "Autodesk.AutoCAD+26_0"
    pending = list(statuses)

    def submit_workitem(*a, **k):
        return pending.pop(0)

    def _engine_seconds(status):
        return status.get("engine_seconds")

    def extract(*a, **k):
        ns.submit_workitem("activity", {})
        return {"layers": ["0"]}

    ns.submit_workitem = submit_workitem
    ns._engine_seconds = _engine_seconds
    ns.extract = extract
    return ns


def test_metered_submit_costs_a_leg_inside_a_frozen_helper(monkeypatch):
    monkeypatch.setattr(blank_spike, "_LEDGER", [])
    c = _fake_client([{"id": "w1", "status": "success", "engine_seconds": 3.6}])
    with blank_spike.metered_submit(c, "read") as m:
        c.extract("x.dwg")
    assert len(m.blocks) == 1
    assert m.blocks[0]["id"] == "w1"
    assert m.blocks[0]["label"] == "read"
    # 3.6 engine-seconds at $10/hr = $0.01 exactly
    assert m.blocks[0]["usd_est"] == round(3.6 / 3600.0 * blank_spike.USD_PER_HR, 4)
    assert len(blank_spike._LEDGER) == 1


def test_metered_submit_restores_the_original(monkeypatch):
    monkeypatch.setattr(blank_spike, "_LEDGER", [])
    c = _fake_client([{"id": "w1", "status": "success", "engine_seconds": 1.0}])
    original = c.submit_workitem
    with blank_spike.metered_submit(c, "read"):
        assert c.submit_workitem is not original
        c.extract("x.dwg")
    assert c.submit_workitem is original


def test_metered_submit_restores_even_when_the_call_raises(monkeypatch):
    monkeypatch.setattr(blank_spike, "_LEDGER", [])
    c = _fake_client([])
    original = c.submit_workitem
    with pytest.raises(IndexError):
        with blank_spike.metered_submit(c, "read"):
            c.extract("x.dwg")
    assert c.submit_workitem is original


def test_metered_submit_ignores_dry_run_bodies(monkeypatch):
    monkeypatch.setattr(blank_spike, "_LEDGER", [])
    c = _fake_client([{"_dry_run": True, "activityId": "a"}])
    with blank_spike.metered_submit(c, "read") as m:
        c.extract("x.dwg")
    assert m.blocks == []
    assert blank_spike._LEDGER == [], "a dry-run body is not billable"


def test_metered_submit_passes_the_status_through_untouched(monkeypatch):
    monkeypatch.setattr(blank_spike, "_LEDGER", [])
    status = {"id": "w1", "status": "success", "engine_seconds": 2.0}
    c = _fake_client([status])
    with blank_spike.metered_submit(c, "read"):
        got = c.submit_workitem("activity", {})
    assert got is status


# --------------------------------------------------------------------------- #
# Presigned-credential redaction - the receipt is committed to git
# --------------------------------------------------------------------------- #
def test_redact_report_url_drops_the_presigned_credential():
    url = ("https://dasprod-store.s3.us-east-1.amazonaws.com/workItem/OWNER/ID/report"
           "?X-Amz-Security-Token=SECRET&X-Amz-Signature=DEADBEEF&X-Amz-Expires=3600")
    out = blank_spike.redact_report_url(url)
    assert "X-Amz-Security-Token" not in out
    assert "X-Amz-Signature" not in out
    assert "SECRET" not in out
    # the object path still identifies the report exactly
    assert out.startswith("https://dasprod-store.s3.us-east-1.amazonaws.com/"
                          "workItem/OWNER/ID/report")


@pytest.mark.parametrize("url", [None, "", 123, {}])
def test_redact_report_url_passes_through_non_urls(url):
    assert blank_spike.redact_report_url(url) == url


def test_redact_report_url_leaves_a_bare_url_alone():
    url = "https://example.invalid/report"
    assert blank_spike.redact_report_url(url) == url


def test_record_redacts_before_it_reaches_the_ledger(monkeypatch):
    monkeypatch.setattr(blank_spike, "_LEDGER", [])
    client = _fake_client([])
    block = blank_spike._record(client, "create", {
        "id": "w1", "status": "success", "engine_seconds": 1.0,
        "reportUrl": "https://x.invalid/r?X-Amz-Security-Token=SECRET",
    })
    assert "SECRET" not in json.dumps(block)
    assert "SECRET" not in json.dumps(blank_spike._LEDGER)


# --------------------------------------------------------------------------- #
# Receipt immutability stamp
# --------------------------------------------------------------------------- #
def test_stamp_receipt_is_deterministic_and_excludes_itself():
    a = blank_spike.stamp_receipt({"spike": "x", "pass": True})
    b = blank_spike.stamp_receipt({"pass": True, "spike": "x"})
    assert a["sha256"] == b["sha256"], "digest must not depend on key order"
    # Re-stamping an already-stamped receipt reproduces the same digest, which
    # is what lets a reader verify one.
    again = blank_spike.stamp_receipt(dict(a))
    assert again["sha256"] == a["sha256"]


def test_stamp_receipt_changes_when_a_field_changes():
    a = blank_spike.stamp_receipt({"spike": "x", "pass": True})
    b = blank_spike.stamp_receipt({"spike": "x", "pass": False})
    assert a["sha256"] != b["sha256"]


def test_write_receipt_round_trips(tmp_path):
    path = str(tmp_path / "r.json")
    blank_spike.write_receipt({"spike": "x", "pass": True}, path)
    with open(path, encoding="utf-8", newline="") as fh:
        raw = fh.read()
    assert "\r\n" not in raw, "CRLF churns the whole file in git"
    loaded = json.loads(raw)
    digest = loaded.pop("sha256")
    assert blank_spike.stamp_receipt(loaded)["sha256"] == digest


# --------------------------------------------------------------------------- #
# The generated repo reference stays in sync with the builder
# --------------------------------------------------------------------------- #
def test_scr_reference_matches_the_builder(tmp_path):
    path = str(tmp_path / "blank_create.scr")
    blank_spike.write_scr_reference(path)
    with open(path, encoding="utf-8") as fh:
        body = fh.read()
    assert blank_lisp.build_blank_scr("LEAF_BLANK_XXXXXXXXXXXX") in body

    committed = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "engine", "blank_create.scr")
    if os.path.exists(committed):
        with open(committed, encoding="utf-8") as fh:
            assert blank_lisp.build_blank_scr("LEAF_BLANK_XXXXXXXXXXXX") in fh.read(), \
                "engine/blank_create.scr is stale: regenerate with " \
                "`python da/blank_spike.py --write-scr-reference`"


# --------------------------------------------------------------------------- #
# witness entity (the broker's read oracle counts entities, not layer names)
# --------------------------------------------------------------------------- #
def test_default_recipe_carries_no_witness_entity():
    """The spike's own recipe is unchanged: client.extract reads the layer TABLE."""
    scr = blank_lisp.build_blank_scr("LEAF_BLANK_ABCDEF123456")
    assert "_.POINT" not in scr


def test_witness_draws_one_point_on_the_marker_layer_before_saveas():
    """Measured 2026-08-24 on real accoreconsole 2026: count_by_layer.lsp reports
    counts={} for a bare marker layer and counts={marker: 1} with this point."""
    marker = "LEAF_BLANK_ABCDEF123456"
    scr = blank_lisp.build_blank_scr(marker, witness=True)
    assert f'(command "_.POINT" "{blank_lisp.WITNESS_POINT}")' in scr
    # MAKE sets the marker layer current, so the point must land after it and
    # before the drawing is written.
    assert scr.index('"_Make"') < scr.index('"_.POINT"') < scr.index("SAVEAS")
    assert scr.count('"_.POINT"') == 1


def test_witness_is_the_only_difference_from_the_proven_recipe():
    marker = "LEAF_BLANK_ABCDEF123456"
    plain = blank_lisp.build_blank_scr(marker).splitlines()
    witness = blank_lisp.build_blank_scr(marker, witness=True).splitlines()
    assert [line for line in witness if line not in plain] == [
        f'(command "_.POINT" "{blank_lisp.WITNESS_POINT}")',
        '(progn (princ "LEAF-BLANK-WITNESS") (princ))',
    ]
