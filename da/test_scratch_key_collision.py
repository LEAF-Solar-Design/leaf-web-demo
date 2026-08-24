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
       honours dry_run, so with no `~/.aps/credentials.json` every extract-
       based test here dies `FileNotFoundError: APS creds missing`. That is
       precisely what CI hit while the same tests passed locally - a green
       local run hid 8 CI failures.
    2. With credentials present it goes FURTHER and mints a real OAuth token
       against https://developer.api.autodesk.com - a live external call from
       a unit test. With dummy credentials that surfaces as a 403.

    `_load_creds()` reads APS_CREDENTIALS_JSON before the file, and
    `auth_token()` is the single mint point, so stubbing both makes the module
    hermetic without touching client.py (which its importers treat as frozen).
    No token is minted and nothing leaves the process.
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
# The da/ DRIVERS must go through the helpers, not rebuild the buggy key shape
#
# PR #782 fixed the collision inside client.py, but da/arx_probe.py and
# da/write_spike.py never called those helpers: each still built
# `in/{int(time.time())}_{basename}` by hand, so the paid write and ARX legs
# kept the exact 1-second-granularity race the rest of the repo had left
# behind. Fixing a shared helper does nothing for a caller that bypasses it,
# so this guard is stated over the DRIVER SOURCE, not over client.py.
# --------------------------------------------------------------------------- #
_HAND_BUILT_KEY = re.compile(r"""["']?(?:in|out)/\{""")

DRIVERS = ("arx_probe.py", "write_spike.py")


@pytest.mark.parametrize("driver", DRIVERS)
def test_driver_does_not_hand_build_a_scratch_key(driver):
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), driver),
               encoding="utf-8").read()
    assert not _HAND_BUILT_KEY.search(src), (
        f"da/{driver} builds an OSS scratch key by hand. That shape has "
        "1-second resolution and no other entropy, so two runs in the same "
        "second collide and OSS last-write-wins hands one caller the other's "
        "bytes. Use client.ephemeral_input_key/ephemeral_output_key with "
        "client.run_nonce()."
    )


@pytest.mark.parametrize("driver", DRIVERS)
def test_driver_uses_the_uuid_derived_nonce_helpers(driver):
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), driver),
               encoding="utf-8").read()
    for symbol in ("run_nonce()", "ephemeral_input_key(", "ephemeral_output_key("):
        assert symbol in src, f"da/{driver} does not call client.{symbol}"
