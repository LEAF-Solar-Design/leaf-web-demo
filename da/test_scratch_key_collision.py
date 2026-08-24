#!/usr/bin/env python3
"""Regression tests for the APS scratch-key collision.

THE DEFECT, as it behaved before this suite existed:

    input_key = f"{_ephemeral_prefix(tenant_id)}in/{int(time.time())}_{dwg_name}"

with `_ephemeral_prefix(None) == ""`. That key has ONE-SECOND resolution and no
other entropy, so two extractions of the same basename starting in the same
wall-clock second produce the SAME OSS object key. OSS PUTs are last-write-wins,
so the loser downloads the winner's bytes: a PAID WorkItem that returns
coherent-but-unrelated geometry instead of failing. That is the 2026-07-25
"returned unrelated rooftop geometry" staging-acceptance failure, and it is why
LEAF_GUEST_DXF_EXTRACT was pinned to 'local'.

`APS_MAX_CONCURRENCY=1` never protected against it: that gates WorkItem
admission only, while upload_object/download_object run outside it.

Every test here is offline - dry_run bodies only, no credentials, no network,
no dollars. The clock is FROZEN in the collision tests, because a test that
relies on real wall-clock time to separate two keys is testing the clock, not
the fix.
"""
from __future__ import annotations

import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import client  # noqa: E402

DWG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                   "data", "rooftop_demo.dwg")
FROZEN_TS = 1787529600


@pytest.fixture(autouse=True)
def hermetic_aps(monkeypatch):
    """These tests derive KEY SHAPES: no credentials, no network, ever.

    Two separate leaks made this module non-hermetic, and BOTH were invisible
    on a developer box that happens to hold real APS credentials:

    1. `client.extract(..., dry_run=True)` calls `_load_creds()` BEFORE it
       honours dry_run (`activity_qualified` -> `nickname` -> `auth_token` is
       the first statement), so with no `~/.aps/credentials.json` every
       extract-based test here dies `FileNotFoundError: APS creds missing`.
       That is exactly what CI hits while the same tests pass locally: a green
       local run hiding 8 red CI ones.
    2. With credentials present it goes FURTHER and makes LIVE calls to
       https://developer.api.autodesk.com — `auth_token()` mints a real OAuth
       token and `nickname()` GETs /da/us-east/v3/forgeapps/me. `dry_run`
       means "no dollars", NOT "no network", so these unit tests were reaching
       the public internet on every run.

    `_load_creds()` reads APS_CREDENTIALS_JSON BEFORE the file, and
    `auth_token()`/`nickname()` are the only mint/lookup points, so stubbing
    the three makes the module hermetic without touching da/client.py — its
    importers treat that module as frozen. No token is minted and nothing
    leaves the process.
    """
    monkeypatch.setenv("APS_CREDENTIALS_JSON", json.dumps(
        {"client_id": "test-client-id", "client_secret": "test-client-secret"}))
    monkeypatch.setattr(client, "auth_token", lambda: "test-token-never-sent")
    monkeypatch.setattr(client, "nickname", lambda: "test-nickname")


@pytest.fixture
def frozen_clock(monkeypatch):
    """Pin time.time so ONLY the nonce can separate two runs' keys."""
    monkeypatch.setattr(client.time, "time", lambda: float(FROZEN_TS))
    return FROZEN_TS


# --------------------------------------------------------------------------- #
# The nonce itself
# --------------------------------------------------------------------------- #
def test_run_nonce_is_unique_across_many_calls():
    assert len({client.run_nonce() for _ in range(500)}) == 500


def test_run_nonce_does_not_depend_on_the_clock(frozen_clock):
    """A clock cannot fix a clock-resolution collision. If the nonce were
    time-derived, freezing the clock would collapse it."""
    assert len({client.run_nonce() for _ in range(200)}) == 200


def test_run_nonce_is_key_safe():
    for _ in range(50):
        n = client.run_nonce()
        assert n.isalnum() and n.islower() or n.isdigit(), n
        assert "/" not in n and "_" not in n and n


# --------------------------------------------------------------------------- #
# FROZEN CONTRACT: the no-nonce helper shape must not move
# --------------------------------------------------------------------------- #
def test_helper_without_nonce_is_byte_for_byte_legacy():
    """da/test_multitenant.py pins this too; duplicated here so the guarantee
    is visible from the file that introduces the nonce."""
    ts = 1784319563
    assert client.ephemeral_input_key("rooftop_demo.dwg", None, ts) == \
        f"in/{ts}_rooftop_demo.dwg"
    assert client.ephemeral_output_key("count_by_layer", None, ts) == \
        f"out/{ts}_count_by_layer.result.json"


def test_helper_without_nonce_still_tenant_scopes():
    ts = 1784319563
    assert client.ephemeral_input_key("a.dwg", "acme", ts) == \
        f"t/acme/in/{ts}_a.dwg"


# --------------------------------------------------------------------------- #
# The nonce changes the key without breaking its shape
# --------------------------------------------------------------------------- #
def test_nonce_changes_the_key_but_keeps_prefix_and_suffix():
    ts = 1784319563
    legacy = client.ephemeral_input_key("rooftop_demo.dwg", None, ts)
    nonced = client.ephemeral_input_key("rooftop_demo.dwg", None, ts, nonce="abc123")
    assert nonced != legacy
    # Shape guarantees other code and tests depend on:
    assert nonced.startswith("in/")
    assert nonced.endswith("_rooftop_demo.dwg")
    assert str(ts) in nonced


def test_two_nonces_never_collide_on_a_frozen_clock():
    ts = 1784319563
    keys = {client.ephemeral_input_key("same.dwg", None, ts, nonce=client.run_nonce())
            for _ in range(200)}
    assert len(keys) == 200


def test_output_key_nonce_behaves_the_same():
    ts = 1784319563
    a = client.ephemeral_output_key("op", None, ts, nonce="n1")
    b = client.ephemeral_output_key("op", None, ts, nonce="n2")
    assert a != b
    assert a.startswith("out/") and a.endswith("op.result.json")


# --------------------------------------------------------------------------- #
# THE ACTUAL DEFECT: two bare extractions in the same second
# --------------------------------------------------------------------------- #
def test_bare_extract_twice_in_one_second_does_not_collide(frozen_clock):
    """The exact reported scenario: `da.extract(<path>)` with no tenant and no
    drawing id - what server/broker.py (x2) and server/write_loop.py call.
    Before the nonce these two keys were IDENTICAL and OSS was last-write-wins."""
    a = client.extract(DWG, dry_run=True)
    b = client.extract(DWG, dry_run=True)
    assert a["input_object"] != b["input_object"], \
        "two bare extractions in the same second still share an input key"
    assert a["output_object"] != b["output_object"], \
        "two bare extractions in the same second still share an output key"


def test_bare_extract_keeps_its_legacy_key_shape(frozen_clock):
    """Collision-proof must not mean shape-changed: da/test_store.py's
    test_legacy_extract_dry_run_unchanged asserts both of these."""
    res = client.extract(DWG, dry_run=True)
    assert res["input_object"].startswith("in/")
    assert res["input_object"].endswith("_rooftop_demo.dwg")
    assert res["output_object"].startswith("out/")
    assert res["output_object"].endswith(".families.txt")


def test_tenant_scoped_extract_also_does_not_collide(frozen_clock):
    """A tenant prefix narrows the blast radius to one tenant; it never removed
    the collision, because the rest of the key is identical."""
    a = client.extract(DWG, dry_run=True, tenant_id="acme")
    b = client.extract(DWG, dry_run=True, tenant_id="acme")
    assert a["input_object"].startswith("t/acme/in/")
    assert a["input_object"] != b["input_object"]


def test_extract_shares_one_nonce_between_its_input_and_output(frozen_clock):
    """Both keys of a single run should carry the SAME nonce, so an operator can
    grep one run's objects together in OSS."""
    res = client.extract(DWG, dry_run=True)
    in_mid = res["input_object"][len("in/"):].split("_")[1]
    out_mid = res["output_object"][len("out/"):].split("_")[1]
    assert in_mid == out_mid, (res["input_object"], res["output_object"])


# --------------------------------------------------------------------------- #
# run_tool has the same exposure and the same fix
# --------------------------------------------------------------------------- #
def _tool():
    return {"name": "count-by-layer", "engine_op": "count_by_layer",
            "version": "1.0.0"}


def test_bare_run_tool_twice_in_one_second_does_not_collide(frozen_clock):
    a = client.run_tool(DWG, _tool(), {}, dry_run=True)
    b = client.run_tool(DWG, _tool(), {}, dry_run=True)
    assert a["input_object"] != b["input_object"]
    assert a["output_object"] != b["output_object"]


def test_bare_run_tool_keeps_its_legacy_key_shape(frozen_clock):
    res = client.run_tool(DWG, _tool(), {"foo": 1}, dry_run=True)
    assert res["input_object"].startswith("in/")
    assert res["output_object"].endswith("count_by_layer.result.json")


# --------------------------------------------------------------------------- #
# The version-aware branch must stay untouched - it never had the bug
# --------------------------------------------------------------------------- #
def test_version_aware_extract_still_references_the_store_key(frozen_clock):
    """The nonce must not leak into the persistent store path. A version key is
    deterministic by design - that is what makes undo work."""
    res = client.extract(DWG, dry_run=True, tenant_id="acme",
                         drawing_id="0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b",
                         version="head")
    assert "/v/0000000" in res["input_object"]
    assert not res["input_object"].startswith("in/")
    assert "in/" not in res["input_object"]


def test_version_aware_extract_is_stable_across_runs(frozen_clock):
    """Two reads of the same stored version resolve the SAME input key."""
    kw = dict(dry_run=True, tenant_id="acme",
              drawing_id="0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b", version=1)
    a = client.extract(DWG, **kw)
    b = client.extract(DWG, **kw)
    assert a["input_object"] == b["input_object"]
    # ...while their scratch OUTPUT keys still differ, since those are per-run.
    assert a["output_object"] != b["output_object"]


# --------------------------------------------------------------------------- #
# The DRIVERS (da/arx_probe.py, da/write_spike.py)
#
# client.py was fixed, but both LIVE drivers kept hand-building
# `in/{ts}_{base}` / `out/{ts}_{base}` themselves, bypassing the helpers and
# keeping the ONE-SECOND collision alive on the exact code paths that submit
# PAID WorkItems. Their `_extract_with_status()` and their write/ARX path are
# network-bound end to end, so the enforceable guard is a SOURCE guard: no
# hand-built scratch key may reappear, and the helpers must be the ones used.
#
# A source guard is only worth its bytes if the pattern it greps is proven to
# match the thing it bans, so the regex is negative-controlled first.
# --------------------------------------------------------------------------- #
DRIVERS = ("arx_probe.py", "write_spike.py")

# An f-string literal that opens a scratch key with `in/{` or `out/{` — i.e.
# a key whose entropy is interpolated inline instead of coming from client.py.
_HAND_BUILT_KEY = re.compile(r'f?"(?:in|out)/\{')

# The exact shape both drivers used to carry, kept verbatim as the negative
# control for the regex above.
_KNOWN_BAD_SOURCE = '    input_key = f"in/{ts}_{dwg_name}"\n'


def _driver_source(name: str) -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def test_hand_built_key_pattern_matches_the_known_bad_shape():
    """Negative control: a ban whose pattern was never shown to fire is
    decoration. This is the literal line both drivers shipped."""
    assert _HAND_BUILT_KEY.search(_KNOWN_BAD_SOURCE)
    assert _HAND_BUILT_KEY.search('        out_key = f"out/{ts}_output.dwg"\n')
    # ...and it must not fire on the fixed form.
    assert not _HAND_BUILT_KEY.search(
        "    input_key = client.ephemeral_input_key(dwg_name, nonce=nonce)\n")


@pytest.mark.parametrize("name", DRIVERS)
def test_driver_builds_no_scratch_key_by_hand(name):
    src = _driver_source(name)
    hits = _HAND_BUILT_KEY.findall(src)
    assert not hits, (
        f"da/{name} hand-builds {len(hits)} scratch key(s) instead of calling "
        "client.ephemeral_input_key/ephemeral_output_key. A key built from "
        "int(time.time()) plus a basename collides at one-second resolution, "
        "and the loser of that collision downloads the winner's bytes from a "
        "PAID WorkItem."
    )


@pytest.mark.parametrize("name", DRIVERS)
def test_driver_uses_the_nonce_helpers(name):
    """Absence of the bad shape is not presence of the fix: a driver that
    simply stopped submitting would also pass the ban above."""
    src = _driver_source(name)
    for helper in ("client.run_nonce()", "client.ephemeral_input_key(",
                   "client.ephemeral_output_key("):
        assert helper in src, f"da/{name} never calls {helper}"


@pytest.mark.parametrize("name", DRIVERS)
def test_driver_nonce_is_not_time_derived(name):
    """One nonce per run, from client.run_nonce() (uuid4-derived). A driver
    that re-derived its own entropy from the clock would reproduce the defect
    while still passing the two checks above."""
    src = _driver_source(name)
    assert not re.search(r"nonce\s*=\s*.*time\.", src), (
        f"da/{name} derives a nonce from the clock; a clock cannot fix a "
        "clock-resolution collision."
    )
