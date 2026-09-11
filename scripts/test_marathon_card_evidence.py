import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("marathon_card_evidence", Path(__file__).with_name("marathon_card_evidence.py"))
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


def receipt(tier="fixture-replay"):
    synthetic = tier == "fixture-replay"
    doc = dict(schema=evidence.SCHEMA, evidence_tier=tier, run_id="run-1", source_sha="a" * 40,
               tenant_binding="demo-tenant", source_artifacts=[dict(filename="state.json", sha256="b" * 64,
               kind="producer", synthetic=synthetic, reconstructed=False)], observed_records=[], screenshots=[],
               assertions=["Card matched the observed API state"])
    for index, state in enumerate(("running", "done")):
        oid = str(index)
        doc["observed_records"].append(dict(observation_id=oid, run_id="run-1", tenant_binding="demo-tenant",
            observed_at=1000 + index, synthetic=synthetic, reconstructed=False,
            capture_mode="live" if tier == "live-run" else "replay", source_artifacts=["state.json"],
            api_result={"builds": [{"id": "run-1", "lane": "fold", "state": state}]}))
        doc["screenshots"].append(dict(filename=oid + ".png", sha256="c" * 64, observation_id=oid,
            run_id="run-1", tenant_binding="demo-tenant", card_visible=True, card_state=state))
    return doc


@pytest.mark.parametrize("tier", evidence.TIERS)
def test_allowed_tiers(tier):
    assert evidence.validate_evidence(receipt(tier))["evidence_tier"] == tier


@pytest.mark.parametrize("change", [
    lambda d: d.update(evidence_tier="local-e2e"),
    lambda d: d.update(extra=True),
    lambda d: d["source_artifacts"][0].update(sha256="abcd"),
    lambda d: d["source_artifacts"][0].update(sha256="z" * 64),
    lambda d: d["observed_records"][0].update(run_id="other"),
    lambda d: d["observed_records"][0].update(tenant_binding="other"),
    lambda d: d["observed_records"][0]["api_result"]["builds"][0].update(id="other"),
    lambda d: d.update(screenshots=[]),
])
def test_invalid_receipts(change):
    doc = receipt()
    change(doc)
    with pytest.raises(ValueError):
        evidence.validate_evidence(doc)


@pytest.mark.parametrize("change", [
    lambda d: d["observed_records"][0].update(synthetic=True),
    lambda d: d["observed_records"][0].update(reconstructed=True),
    lambda d: d["source_artifacts"][0].update(kind="state"),
    lambda d: d["source_artifacts"][0].update(synthetic=True),
    lambda d: d["observed_records"][0].update(capture_mode="replay"),
    lambda d: d["observed_records"][1].update(observed_at=999),
])
def test_live_history_integrity(change):
    doc = receipt("live-run")
    change(doc)
    with pytest.raises(ValueError):
        evidence.validate_evidence(doc)


def test_live_requires_pair():
    doc = receipt("live-run")
    doc["observed_records"] = doc["observed_records"][1:]
    doc["screenshots"] = doc["screenshots"][1:]
    with pytest.raises(ValueError, match="nonterminal"):
        evidence.validate_evidence(doc)


@pytest.mark.parametrize("required,exit_code", [("fixture-replay", 0), ("live-run", 1)])
def test_cli_exact_tier(tmp_path, capsys, required, exit_code):
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt()), encoding="utf-8")
    assert evidence.main(["--check", str(path), "--require-tier", required]) == exit_code
    output = capsys.readouterr()
    assert len(output.out.splitlines()) == 1 and output.err == ""
    assert json.loads(output.out)["ok"] is (exit_code == 0)


def test_cli_bad_arguments(capsys):
    assert evidence.main([]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_duplicate_keys_refused(tmp_path):
    path = tmp_path / "receipt.json"
    path.write_text('{"schema": 1, "schema": 2}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        evidence.load_evidence(path)
