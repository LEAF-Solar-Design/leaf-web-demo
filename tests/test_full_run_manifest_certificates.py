"""Multiple supervisor certificates and archive reference closure."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("capture_manifest", ROOT / "scripts/ci/full_run_manifest.py")
MANIFEST = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MANIFEST)


def capture_fixture(tmp_path):
    catalog = {"schema": "leaf.ci.test-catalog.v1", "kind": "web",
               "runner_catalog_sha256": "e" * 64, "suites": [{"id": "sample", "test_ids": []}]}
    catalog["catalog_sha256"] = MANIFEST.hashlib.sha256(MANIFEST.canonical(catalog)).hexdigest()
    inputs = dict(repo="leaf-web-demo", run_id="fixture-build:uuid", source_sha="a" * 40,
                  source_tree="b" * 40, capture_sha="c" * 40, image="fixture",
                  execution_mode="full", full_run_complete=True, test_id_reporting_complete=True)
    bindings = {key: inputs[key] for key in MANIFEST.BINDING_FIELDS if key != "catalog_sha256"}
    bindings["catalog_sha256"] = catalog["catalog_sha256"]
    selection = tmp_path / "selection"
    output = tmp_path / "process-capture/out"
    readsets = tmp_path / "gate-logs/readsets"
    selection.mkdir()
    readsets.mkdir(parents=True)
    (readsets / "startup.json").write_text('{"schema":"python-diagnostic"}', encoding="utf-8")
    output.mkdir(parents=True)
    for name in ("context.json", "capture-epoch.json"):
        (output.parent / name).write_text("{}", encoding="utf-8")
    certificates = []
    for attempt in (1, 2):
        directory = output / "sample" / str(attempt)
        (directory / "reports").mkdir(parents=True)
        shards = directory / "readsets/sample" / str(attempt)
        shards.mkdir(parents=True)
        certificate = dict(bindings, schema="leaf.ci.process-tree.v1", capture_group="base",
                           capture_epoch="d" * 64, capture_epoch_manifest={"parser_version": "fixture"},
                           attempt=attempt)
        certificates.append(certificate)
        (directory / "reports/process-tree-base.json").write_bytes(MANIFEST.canonical(certificate))
        (directory / "reports/trace-receipt-base.json").write_text(
            json.dumps({"capture_errors": [], "capture_complete": True}), encoding="utf-8")
        outcomes = directory / "attempts-tree.jsonl"
        outcomes.write_text('{"outcome":"passed"}\n', encoding="utf-8")
        shard = dict(bindings, schema="leaf.ci.readset.v1", suite_id="sample", worker="tree",
                     attempt=attempt, trace_kind="linux-process-tree", capture_epoch="d" * 64,
                     process_tree_ref="reports/process-tree-base.json", union_ref="reports/process-tree-base.json",
                     process_tree_sha256=MANIFEST.hashlib.sha256(MANIFEST.canonical(certificate)).hexdigest(),
                     outcomes_ref=str(outcomes))
        (shards / "tree-process.json").write_bytes(MANIFEST.canonical(shard))
        (directory / "capture-suites.json").write_text(json.dumps([
            {"suite_id": "sample", "attempt": attempt, "worker": "tree",
             "test_ids": ["sample::test_ok"], "outcomes_ref": str(outcomes)}]), encoding="utf-8")
    MANIFEST.stage_process_capture(output, readsets, selection)
    shards = [json.loads(path.read_text()) for path in readsets.rglob("*.json")]
    manifest = MANIFEST.build_manifest(inputs, catalog, shards, certificates=certificates)
    (selection / "full-run.json").write_bytes(MANIFEST.canonical(manifest))
    members = {path.relative_to(selection).as_posix(): path
               for path in selection.rglob("*") if path.is_file()}
    members.update({path.relative_to(readsets.parent).as_posix(): path
                    for path in readsets.rglob("*") if path.is_file()})
    return inputs, catalog, certificates, manifest, members, readsets, selection


def test_multiple_certificates_are_bound_and_flattened(tmp_path):
    _, _, certificates, manifest, members, readsets, selection = capture_fixture(tmp_path)
    assert len(list((selection / "reports").glob("process-tree-*.json"))) == 2
    assert len(list((selection / "reports").glob("trace-receipt-*.json"))) == 2
    assert len(manifest["process_tree_sha256s"]) == len(certificates) == 2
    assert manifest["capture_epoch"] == "d" * 64
    assert manifest["workers_by_suite"] == {"sample": ["tree"]}
    assert manifest["full_run_complete"] is True
    assert (readsets.parent / "diagnostics/startup.json").is_file()
    assert not (readsets / "startup.json").exists()
    assert not any(name.startswith("diagnostics/") for name in members)
    MANIFEST.validate_package(members)
    for path in readsets.rglob("*.json"):
        shard = json.loads(path.read_text())
        assert shard["outcomes_ref"] in members
        assert shard["process_tree_ref"] in members


def test_root_capture_suites_matches_fetcher_contract(tmp_path):
    *_, members, _, selection = capture_fixture(tmp_path)
    rows = json.loads((selection / "capture-suites.json").read_text())
    # fetch_readsets.read_evidence requires a list with suite_id and attempt;
    # it derives {schema: leaf.ci.capture-suites.v1, rows: len(rows), sha256: ...}.
    assert isinstance(rows, list)
    assert len(rows) == 2
    assert [row["attempt"] for row in rows] == [1, 2]
    assert all(row["suite_id"] == "sample" for row in rows)
    assert all(row["outcomes_ref"] in members for row in rows)
    assert all(row["test_ids"] == ["sample::test_ok"] for row in rows)
    assert len(list((selection / "capture").glob("*/*/capture-suites.json"))) == 2


@pytest.mark.parametrize("missing", ["certificate", "outcome", "epoch"])
def test_package_requires_references_in_actual_member_set(tmp_path, missing):
    *_, members, readsets, _ = capture_fixture(tmp_path)
    shard = json.loads(next(readsets.rglob("*.json")).read_text())
    ref = (shard["process_tree_ref"] if missing == "certificate" else
           shard["outcomes_ref"] if missing == "outcome" else "capture/capture-epoch.json")
    # Still on disk: validating a directory instead of the packed members would miss this.
    assert members.pop(ref).is_file()
    with pytest.raises(ValueError, match="missing_"):
        MANIFEST.validate_package(members)


def test_package_checks_certificate_digest(tmp_path):
    *_, members, _, _ = capture_fixture(tmp_path)
    path = next(path for name, path in members.items() if name.startswith("reports/process-tree-"))
    certificate = json.loads(path.read_text())
    certificate["changed"] = True
    path.write_bytes(MANIFEST.canonical(certificate))
    with pytest.raises(ValueError, match="certificate"):
        MANIFEST.validate_package(members)


def test_mixed_or_foreign_certificates_fail_closed(tmp_path):
    inputs, catalog, certificates, *_ = capture_fixture(tmp_path)
    mixed = copy.deepcopy(certificates)
    mixed[1]["capture_epoch"] = "e" * 64
    manifest = MANIFEST.build_manifest(inputs, catalog, certificates=mixed)
    assert manifest["full_run_complete"] is False
    assert manifest["completeness_reasons"] == ["mixed_capture_epoch"]
    foreign = copy.deepcopy(certificates)
    foreign[1]["run_id"] = "other-run"
    with pytest.raises(ValueError, match="process_tree_binding_mismatch"):
        MANIFEST.build_manifest(inputs, catalog, certificates=foreign)


def test_cli_loads_every_certificate(tmp_path, monkeypatch):
    inputs, catalog, _, _, members, readsets, selection = capture_fixture(tmp_path)
    import io
    monkeypatch.setattr(MANIFEST.sys, "stdin", io.StringIO(json.dumps(inputs)))
    catalog_path = selection / "catalog.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    destination = selection / "cli-full-run.json"
    assert MANIFEST.main(["--catalog", str(catalog_path), "--readsets", str(readsets),
                          "--reports", str(selection / "reports"), "--output", str(destination)]) == 0
    document = json.loads(destination.read_text())
    assert len(document["process_tree_sha256s"]) == 2
    assert document["process_tree_sha256s"] == json.loads(members["full-run.json"].read_text())["process_tree_sha256s"]
