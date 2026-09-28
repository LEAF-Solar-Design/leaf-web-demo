"""TCM-10: Leaf-only declarations and their publisher attribution."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from cost_meter import internal, publisher  # noqa: E402

PERIOD = "2026-09"


def _config(**changes):
    entry = {"resource_id": "aws:codebuild", "share": {"leaf|ci": "1"},
             "basis": "Only Leaf CI uses this resource.", "status": "MEASURED"}
    entry.update(changes)
    return {"version": 1, "entries": [entry]}


def _script():
    from cost_meter import publish_main
    return publish_main


def test_seed_loads_and_emits_the_usage_contract():
    assert internal.DEFAULT_CONFIG_PATH == SERVER_DIR / "cost_meter" / "data" / "cost-internal-resources.yaml"
    config = internal.load_internal_config()
    assert config.entries[0].basis == (
        "every CodeBuild project in the account is Leaf CI (leaf-ci-*) or a Leaf GitHub Actions runner (leaf-gha-runner-*)")
    observations = internal.internal_usage_observations(PERIOD, config)
    assert observations == [{
        "kind": "usage", "resource_id": "aws:codebuild", "period": PERIOD,
        "unit": "share", "total_usage": "1", "usages": {"leaf|ci": "1"},
        "status": "MEASURED", "coverage": "complete", "source": internal.SOURCE,
    }]
    assert json.loads(json.dumps(observations)) == observations


def test_internal_config_env_override_and_explicit_path_precedence(tmp_path, monkeypatch):
    override = tmp_path / "internal.yaml"
    override.write_text(json.dumps(_config(resource_id="vendor:override")), encoding="utf-8")
    monkeypatch.setenv("LEAF_COST_INTERNAL_CONFIG", str(override))
    assert internal.load_internal_config().entries[0].resource_id == "vendor:override"
    assert internal.load_internal_config(internal.DEFAULT_CONFIG_PATH).entries[0].resource_id == "aws:codebuild"


@pytest.mark.parametrize("share", [
    {}, {"tenant_a|": "1"}, {"leaf|unattributed": "1"}, {"leaf|": "1"},
    {"leaf|ci": 1}, {"leaf|ci": 1.0}, {"leaf|ci": True}, {"leaf|ci": None},
    {"leaf|ci": "NaN"}, {"leaf|ci": "Infinity"}, {"leaf|ci": "nonsense"},
    {"leaf|ci": "-1"}, {"leaf|ci": "0"}, {"leaf|ci": "2"},
    {"leaf|ci": "0.9"}, {"leaf|ci": "0.5", "leaf|fleet": "0.6"},
    {"leaf|ci": "0.9999999999999", "leaf|fleet": "0.0000000000001"},
])
def test_invalid_shares_refuse(share):
    with pytest.raises(internal.InternalConfigError):
        internal.internal_usage_observations(PERIOD, _config(share=share))


@pytest.mark.parametrize("changes", [
    {"extra": "unknown"}, {"resource_id": "aws:Amazon CodeBuild"},
    {"resource_id": "aws:code_build"}, {"resource_id": "codebuild"},
    {"basis": " "}, {"basis": None}, {"status": "confirmed"}, {"status": []},
])
def test_invalid_entries_refuse(changes):
    with pytest.raises(internal.InternalConfigError):
        internal.validate_internal_config(_config(**changes))


def test_unknown_top_keys_missing_keys_and_duplicate_ids_refuse():
    config = _config()
    with pytest.raises(internal.InternalConfigError):
        internal.validate_internal_config(dict(config, extra=True))
    del config["entries"][0]["basis"]
    with pytest.raises(internal.InternalConfigError):
        internal.validate_internal_config(config)
    config = _config()
    config["entries"] *= 2
    with pytest.raises(internal.InternalConfigError, match="duplicate resource_id"):
        internal.validate_internal_config(config)


@pytest.mark.parametrize("text", [
    "version: 1\nversion: 1\nentries: []\n",
    "version: 1\nentries:\n- resource_id: aws:codebuild\n  share: {leaf|ci: '1', leaf|ci: '1'}\n  basis: CI only\n  status: MEASURED\n",
    "entries: [",
])
def test_duplicate_yaml_keys_and_bad_yaml_refuse(text):
    with pytest.raises(internal.InternalConfigError):
        internal.parse_internal_config(text)


def test_split_estimated_entries_and_period_validation():
    config = _config(share={"leaf|development": "0.25", "leaf|ci": "0.5", "leaf|fleet": "0.25"},
                     status="ESTIMATED")
    config["entries"].append(dict(config["entries"][0], resource_id="vendor:tools"))
    observations = internal.internal_usage_observations(PERIOD, config)
    assert len(observations) == 2
    assert all(o["status"] == "ESTIMATED" and o["coverage"] == "complete" for o in observations)
    assert observations[0]["usages"] == config["entries"][0]["share"]
    with pytest.raises(internal.InternalConfigError):
        internal.internal_usage_observations("2026-13", config)


def test_codebuild_cost_joins_to_leaf_ci():
    usage = internal.internal_usage_observations(PERIOD, internal.load_internal_config())
    cost = {"kind": "cost", "resource_id": "aws:codebuild", "period": PERIOD,
            "gross_cost_usd": "6670", "credits_usd": "0", "coverage": "complete",
            "source_batch_id": "ce:2026-09", "source": "aws-cost-explorer"}
    ((rp, shares),) = publisher.build_period(PERIOD, [cost, *usage])
    assert rp.gross_cost_usd == Decimal("6670")
    assert [(s.participant_id, s.dimension, s.usage_share, s.status) for s in shares] == [
        ("leaf", "ci", Decimal("1.0"), "MEASURED")]


@pytest.mark.parametrize("from_file", [False, True])
def test_real_measured_usage_keeps_priority(tmp_path, from_file):
    script = _script()
    collect = script.DEFAULT_COLLECTORS["internal-resources"]
    now = datetime(2026, 9, 28, tzinfo=timezone.utc)
    real = dict(collect(PERIOD, now)[0], source="real-meter", unit="build-minute",
                total_usage="10", usages={"leaf|development": "10"})
    collectors = {"internal-resources": collect}
    paths = []
    if from_file:
        path = tmp_path / "usage.jsonl"
        path.write_text(json.dumps(real) + "\n", encoding="utf-8")
        paths.append(str(path))
    else:
        collectors["real-meter"] = lambda p, n: [real]
    observations, used, missing = script._gather(PERIOD, now, collectors, paths, lambda line: None)
    assert "internal-resources" in used and missing == []
    ((rp, shares),) = publisher.build_period(PERIOD, observations)
    assert rp.unit == "build-minute"
    assert [(s.participant_id, s.dimension, s.usage_share) for s in shares] == [
        ("leaf", "development", Decimal("1"))]
