"""Dependency-free proof for the gold-set replay core (platform/replay.py).

No Postgres, no third-party imports: the module under test is loaded straight
from its file path (same pattern as test_ledger_static.py / test_hashing_static.py).
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest


_PKG = pathlib.Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("leaf_platform_replay", _PKG / "replay.py")
assert _SPEC and _SPEC.loader
replay = importlib.util.module_from_spec(_SPEC)
sys.modules["leaf_platform_replay"] = replay
_SPEC.loader.exec_module(replay)


PAYLOADS = {
    "solve-a": {"tool": "autofill-string-targets", "points": [[0.0001, 1.5]], "n": 3},
    "solve-b": {"tool": "string-autofill-opt", "elev_m": 12.30004, "ok": True},
}


def test_mint_then_replay_all_match():
    gold = replay.mint_gold_set(PAYLOADS)
    report = replay.replay_gold_set(gold)
    assert report.total == 2
    assert report.matched == 2
    assert report.diverged == ()
    assert report.skipped == ()
    assert report.ok


def test_tampered_payload_diverges_with_both_digests():
    gold = replay.mint_gold_set(PAYLOADS)
    gold[0]["payload"]["n"] = 4  # tamper after minting
    report = replay.replay_gold_set(gold)
    assert report.matched == 1
    assert len(report.diverged) == 1
    d = report.diverged[0]
    assert d.record_id == gold[0]["id"]
    assert d.expected_digest == gold[0]["expected_digest"]
    assert d.actual_digest == replay.stable_hash(gold[0]["payload"])
    assert d.actual_digest != d.expected_digest
    assert not report.ok


def test_tampered_expected_digest_diverges():
    gold = replay.mint_gold_set(PAYLOADS)
    gold[1]["expected_digest"] = "0" * 64
    report = replay.replay_gold_set(gold)
    assert len(report.diverged) == 1
    assert report.diverged[0].expected_digest == "0" * 64


def test_empty_gold_set_is_visible_vacuous_pass():
    report = replay.replay_gold_set([])
    assert report.total == 0
    assert report.matched == 0
    assert report.ok  # passes, but total=0 is visible in the report


def test_malformed_records_skip_visibly_never_crash():
    gold = replay.mint_gold_set(PAYLOADS)
    gold.append({"id": "no-digest", "payload": {"x": 1}})            # missing digest
    gold.append({"id": "bad-payload", "payload": object(),           # unhashable
                 "expected_digest": "0" * 64})
    gold.append("not-a-dict")                                        # wrong shape
    report = replay.replay_gold_set(gold)
    assert report.matched == 2
    assert set(report.skipped) == {"no-digest", "bad-payload", "index:4"}
    assert not report.ok  # skips are visible failures of the gate, not silence


def test_report_is_json_serializable():
    gold = replay.mint_gold_set(PAYLOADS)
    gold[0]["expected_digest"] = "f" * 64
    report = replay.replay_gold_set(gold)
    round_tripped = json.loads(json.dumps(report.to_dict()))
    assert round_tripped["total"] == 2
    assert round_tripped["ok"] is False
    assert round_tripped["diverged"][0]["expected_digest"] == "f" * 64


def test_save_load_round_trip(tmp_path):
    gold = replay.mint_gold_set(PAYLOADS)
    path = tmp_path / "gold.json"
    replay.save_gold_set(gold, path)
    loaded = replay.load_gold_set(path)
    report = replay.replay_gold_set(loaded)
    assert report.ok and report.total == 2


def test_load_rejects_non_list(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    with pytest.raises(ValueError):
        replay.load_gold_set(path)
