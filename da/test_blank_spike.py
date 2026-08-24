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


# --------------------------------------------------------------------------- #
# activity drift comparison (shared by the spike and the broker producer)
# --------------------------------------------------------------------------- #
def test_activity_body_matches_ignores_cosmetic_drift():
    spec = blank_lisp.blank_activity_spec("LeafBlankCreate", "Autodesk.AutoCAD+26_0")
    same = dict(spec, description="reworded", id="ignored-by-the-version-endpoint")
    assert blank_lisp.activity_body_matches(same, spec) is True


def test_activity_body_matches_catches_every_execution_relevant_change():
    spec = blank_lisp.blank_activity_spec("LeafBlankCreate", "Autodesk.AutoCAD+26_0")
    assert blank_lisp.activity_body_matches(dict(spec), spec) is True
    for field, drifted in (
        ("engine", "Autodesk.AutoCAD+24_3"),
        ("commandLine", [r'$(engine.path)\accoreconsole.exe /s "$(settings[script].path)"']),
        ("parameters", {"Result": {"verb": "put", "required": True,
                                   "localName": "output.dwg"}}),
    ):
        assert blank_lisp.activity_body_matches(dict(spec, **{field: drifted}), spec) is False
        # A field the live body simply lacks is drift too, not a pass.
        assert blank_lisp.activity_body_matches(
            {k: v for k, v in spec.items() if k != field}, spec) is False


def test_activity_body_matches_fails_closed_on_a_non_dict():
    spec = blank_lisp.blank_activity_spec("LeafBlankCreate", "Autodesk.AutoCAD+26_0")
    for junk in (None, "", [], 0):
        assert blank_lisp.activity_body_matches(junk, spec) is False
        assert blank_lisp.activity_body_matches(spec, junk) is False


def test_the_activex_body_that_shipped_is_detected_as_drift():
    """The concrete case this guard exists for: the pre-consolidation broker body
    baked its script into `settings` and had no Script parameter."""
    spec = blank_lisp.blank_activity_spec("LeafBlankDwgFeasibility",
                                          "Autodesk.AutoCAD+26_0",
                                          out_localname="blank.dwg")
    shipped = {
        "engine": "Autodesk.AutoCAD+26_0",
        "commandLine": [r'$(engine.path)\accoreconsole.exe /s "$(settings[script].path)"'],
        "parameters": {"Result": {"verb": "put", "required": True,
                                  "localName": "blank.dwg"}},
    }
    assert blank_lisp.activity_body_matches(shipped, spec) is False


def test_apis_injected_parameter_defaults_are_not_drift():
    """The version-churn trap: APS echoing back defaults must NOT read as drift.

    If it did, the caller would publish a new activity VERSION and repoint the
    alias on EVERY run. APS versions cannot be deleted, so that is unbounded
    permanent growth on a shared account, not a cosmetic bug.
    """
    spec = blank_lisp.blank_activity_spec("LeafBlankCreate", "Autodesk.AutoCAD+26_0")
    rendered = {
        "id": "LeafBlankCreate",
        "version": 3,
        "description": "whatever APS stored",
        "engine": spec["engine"],
        "commandLine": spec["commandLine"],
        "parameters": {
            name: {**param, "zip": False, "ondemand": False, "description": ""}
            for name, param in spec["parameters"].items()
        },
    }
    assert blank_lisp.activity_body_matches(rendered, spec) is True


def test_a_changed_parameter_field_is_still_drift_despite_the_tolerance():
    spec = blank_lisp.blank_activity_spec("LeafBlankCreate", "Autodesk.AutoCAD+26_0")
    for name, key, bad in (
        ("Script", "localName", "other.scr"),
        ("Script", "verb", "put"),
        ("Result", "localName", "elsewhere.dwg"),
        ("Result", "required", False),
    ):
        drifted = {**spec, "parameters": {
            pname: ({**param, key: bad} if pname == name else dict(param))
            for pname, param in spec["parameters"].items()}}
        assert blank_lisp.activity_body_matches(drifted, spec) is False, (name, key)


def test_added_or_removed_parameters_are_drift():
    """The name SET is exact, so a stale HostDwg or a missing Script is caught."""
    spec = blank_lisp.blank_activity_spec("LeafBlankCreate", "Autodesk.AutoCAD+26_0")
    stale = {**spec, "parameters": {**spec["parameters"],
             "HostDwg": {"verb": "get", "required": True, "localName": "host.dwg"}}}
    assert blank_lisp.activity_body_matches(stale, spec) is False
    missing = {**spec, "parameters": {
        k: v for k, v in spec["parameters"].items() if k != "Script"}}
    assert blank_lisp.activity_body_matches(missing, spec) is False


def test_a_settings_only_change_is_invisible_and_that_is_why_script_is_an_argument():
    """Documents the LIMITATION found by a real-APS readback: the live version
    body carries no `settings`, so a baked-in script cannot be drift-checked."""
    spec = blank_lisp.blank_activity_spec("LeafBlankCreate", "Autodesk.AutoCAD+26_0")
    assert "settings" not in spec, "the recipe must never be baked into settings"
    # Even if a caller DID bake one in, the comparison cannot see it.
    assert blank_lisp.activity_body_matches(
        {**spec, "settings": {"script": {"value": "(totally different)"}}}, spec) is True


# --------------------------------------------------------------------------- #
# ensure_blank_activity - a 409 must not be trusted as "already correct"
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP " + str(self.status_code))


class _FakeRequests:
    """Records every call; each verb is scripted with a response queue so a
    test can assert both the outcome and exactly which endpoints were hit -
    the whole point being to prove a matching 409 does NOT publish a version
    and a drifted 409 DOES.
    """

    def __init__(self, get=(), post=(), patch=()):
        self.calls = []
        self._get = list(get)
        self._post = list(post)
        self._patch = list(patch)

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        return self._get.pop(0)

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw.get("data")))
        return self._post.pop(0)

    def patch(self, url, **kw):
        self.calls.append(("PATCH", url, kw.get("data")))
        return self._patch.pop(0)


def _fake_da_client():
    ns = types.SimpleNamespace()
    ns.DA = "https://developer.api.autodesk.com/da/us-east/v3"
    ns.ALIAS = "prod"
    ns.ENGINE = "Autodesk.AutoCAD+26_0"
    ns._HTTP_TIMEOUT = 60
    ns._auth_headers = lambda: {"Authorization": "Bearer fake"}
    return ns


def _install_fake_requests(monkeypatch, fake):
    monkeypatch.setitem(sys.modules, "requests", fake)


def test_aliased_matching_version_reuses_the_live_version_when_it_matches(monkeypatch):
    client = _fake_da_client()
    spec = blank_lisp.blank_activity_spec(blank_spike.BLANK_ACTIVITY, client.ENGINE)
    fake = _FakeRequests(
        get=[_FakeResponse(200, {"id": client.ALIAS, "version": 1}),
             _FakeResponse(200, spec)],
    )
    headers = {"Authorization": "Bearer fake"}
    version = blank_spike._aliased_matching_version(client, fake, spec, headers)
    assert version == 1
    assert not fake._post, "a matching live version must never publish a new one"


def test_aliased_matching_version_publishes_a_new_version_on_drift(monkeypatch):
    client = _fake_da_client()
    spec = blank_lisp.blank_activity_spec(blank_spike.BLANK_ACTIVITY, client.ENGINE)
    drifted_live = {**spec, "commandLine": [
        r'$(engine.path)\accoreconsole.exe /s "$(settings[script].path)"']}
    fake = _FakeRequests(
        get=[_FakeResponse(200, {"id": client.ALIAS, "version": 1}),
             _FakeResponse(200, drifted_live)],
        post=[_FakeResponse(200, {"version": 2})],
    )
    headers = {"Authorization": "Bearer fake"}
    version = blank_spike._aliased_matching_version(client, fake, spec, headers)
    assert version == 2, "drift must publish and return the NEW version, not reuse the stale one"
    post_calls = [c for c in fake.calls if c[0] == "POST"]
    assert len(post_calls) == 1
    assert post_calls[0][1].endswith("/activities/" + blank_spike.BLANK_ACTIVITY + "/versions")
    published_body = json.loads(post_calls[0][2])
    assert "id" not in published_body, "the version body must not carry the id path segment"
    assert published_body["commandLine"] == spec["commandLine"]


def test_aliased_matching_version_treats_a_missing_alias_as_no_match(monkeypatch):
    """A 404 on the alias (never provisioned) must fall through to publish,
    exactly like drift - it must never raise or be mistaken for a match."""
    client = _fake_da_client()
    spec = blank_lisp.blank_activity_spec(blank_spike.BLANK_ACTIVITY, client.ENGINE)
    fake = _FakeRequests(
        get=[_FakeResponse(404, {})],
        post=[_FakeResponse(200, {"version": 1})],
    )
    headers = {"Authorization": "Bearer fake"}
    version = blank_spike._aliased_matching_version(client, fake, spec, headers)
    assert version == 1
    assert len([c for c in fake.calls if c[0] == "GET"]) == 1, \
        "a 404 alias has no version to read back - must not attempt a second GET"


def test_aliased_matching_version_rejects_an_unusable_published_version(monkeypatch):
    client = _fake_da_client()
    spec = blank_lisp.blank_activity_spec(blank_spike.BLANK_ACTIVITY, client.ENGINE)
    fake = _FakeRequests(
        get=[_FakeResponse(404, {})],
        post=[_FakeResponse(200, {"version": None})],
    )
    headers = {"Authorization": "Bearer fake"}
    with pytest.raises(RuntimeError, match="unusable version"):
        blank_spike._aliased_matching_version(client, fake, spec, headers)


def test_ensure_blank_activity_409_with_matching_live_version_reuses_it(monkeypatch):
    """The 409-and-correct path: no version publish, no alias PATCH needed."""
    client = _fake_da_client()
    spec = blank_lisp.blank_activity_spec(blank_spike.BLANK_ACTIVITY, client.ENGINE)
    fake = _FakeRequests(
        get=[_FakeResponse(200, {"id": client.ALIAS, "version": 1}),
             _FakeResponse(200, spec)],
        post=[_FakeResponse(409, {}),   # POST /activities -> already exists
              _FakeResponse(200, {})],  # POST .../aliases -> alias set at v1
    )
    _install_fake_requests(monkeypatch, fake)
    result = blank_spike.ensure_blank_activity(client, dry_run=False)
    assert result["created"] is False
    assert result["version"] == 1
    assert not fake._patch, "no drift means no alias repoint"
    version_posts = [c for c in fake.calls
                     if c[0] == "POST" and c[1].endswith("/versions")]
    assert version_posts == [], "a matching version must never be republished"


def test_ensure_blank_activity_409_with_drift_publishes_and_repoints_the_alias(monkeypatch):
    """THE defect this fix closes: a 409 whose live body no longer matches the
    spec must publish a new version and move the alias onto it, not report
    success on the strength of the 409 alone."""
    client = _fake_da_client()
    spec = blank_lisp.blank_activity_spec(blank_spike.BLANK_ACTIVITY, client.ENGINE)
    stale_live = {**spec, "parameters": {
        k: v for k, v in spec["parameters"].items() if k != "Script"}}
    fake = _FakeRequests(
        get=[_FakeResponse(200, {"id": client.ALIAS, "version": 1}),
             _FakeResponse(200, stale_live)],
        post=[_FakeResponse(409, {}),           # POST /activities -> already exists
              _FakeResponse(200, {"version": 2}),  # POST .../versions -> new version
              _FakeResponse(409, {})],           # POST .../aliases -> alias already exists
        patch=[_FakeResponse(200, {})],          # PATCH .../aliases/<alias> -> repointed
    )
    _install_fake_requests(monkeypatch, fake)
    result = blank_spike.ensure_blank_activity(client, dry_run=False)
    assert result["created"] is False
    assert result["version"] == 2
    patch_calls = [c for c in fake.calls if c[0] == "PATCH"]
    assert len(patch_calls) == 1
    assert patch_calls[0][1].endswith("/aliases/" + client.ALIAS)
    assert json.loads(patch_calls[0][2]) == {"version": 2}


def test_ensure_blank_activity_first_ever_create_still_uses_version_one(monkeypatch):
    """Regression: when the Activity does not exist yet (no 409 at all), the
    version stays a plain 1 - untouched by the new drift-repair path."""
    client = _fake_da_client()
    fake = _FakeRequests(
        post=[_FakeResponse(201, {}),   # POST /activities -> created
              _FakeResponse(200, {})],  # POST .../aliases -> alias set at v1
    )
    _install_fake_requests(monkeypatch, fake)
    result = blank_spike.ensure_blank_activity(client, dry_run=False)
    assert result["created"] is True
    assert result["version"] == 1
    assert not fake._get, "a first-ever create has nothing to read back"
