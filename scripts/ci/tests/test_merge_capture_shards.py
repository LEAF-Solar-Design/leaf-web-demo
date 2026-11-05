"""Fixture proofs for the extracted-archive merger; no captures are executed."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import quote

import pytest

import full_run_manifest
import merge_capture_shards as merger
import select_tests


SCRIPT = Path(__file__).resolve().parents[1] / "merge_capture_shards.py"
SUITES = ["alpha", "beta/suite", "unobserved"]


def write_json(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(merger.canonical(document) + b"\n")


def read_json(path):
    return json.loads(path.read_bytes())


def snapshot(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*")) if path.is_file()}


def make_shard(tmp_path, name, sid, *, change=None, complete=False):
    root = tmp_path / name
    root.mkdir()
    change = change or {}
    catalog = {"schema": "leaf.ci.test-catalog.v1", "kind": "web",
               "runner_blob_sha": change.get("runner", "d" * 40),
               "suites": [{"id": suite, "test_ids": []} for suite in SUITES]}
    if "catalog_sha256" in change:
        catalog["fixture_catalog_revision"] = change["catalog_sha256"]
    catalog["catalog_sha256"] = select_tests.catalog_info(catalog)[1]
    inputs = {"repo": "leaf-web-demo", "run_id": "build:" + name,
              "source_sha": change.get("source_sha", "a" * 40),
              "source_tree": change.get("source_tree", "b" * 40),
              "capture_sha": change.get("capture_sha", "c" * 40),
              "image": change.get("image_digest", "sha256:" + "e" * 64),
              "execution_mode": "selected", "full_run_complete": False,
              "test_id_reporting_complete": False}
    epoch = {"schema": "leaf.ci.capture-epoch.v1", "external_root_table": {},
             "external_root_digests": {}, "image_manifest_digest": inputs["image"],
             "interpreter_identity": {"version": change.get("python_version", sys.version)},
             "toolchain_fingerprint": full_run_manifest.toolchain_fingerprint(inputs["image"], inputs["capture_sha"]),
             "parser_version": change.get("capture_epoch", "fixture-parser")}
    capture_epoch = select_tests.normalized_epoch(epoch)
    binding = {key: inputs[key] for key in merger.BINDINGS if key in inputs}
    binding["catalog_sha256"] = catalog["catalog_sha256"]
    certificate = dict(binding, schema="leaf.ci.process-tree.v1", capture_epoch=capture_epoch,
                       capture_epoch_manifest=epoch, complete=complete, decoder_complete=complete,
                       trace_loss=False, reasons=[] if complete else ["fixture_incomplete"])
    encoded = quote(sid, safe="").replace(".", "%2E")
    directory = "readsets/" + encoded + "/1/"
    certificate_ref = "reports/process-tree-" + encoded + ".json"
    outcome_ref = directory + "attempts.jsonl"
    readset = dict(binding, schema="leaf.ci.readset.v1", suite_id=sid, worker="gw0", attempt=1,
                   trace_kind="linux-process-tree", python_only=False,
                   capture_epoch=capture_epoch, capture_epoch_manifest=epoch,
                   outcomes_ref=outcome_ref, process_tree_ref=certificate_ref, union_ref=certificate_ref,
                   process_tree_sha256=merger.digest(certificate), reads=[],
                   capture_complete=complete, children_complete=complete,
                   generated_inputs_resolved=complete, shared_state_closure_resolved=complete,
                   external_inputs_resolved=complete, decoder_complete=complete, trace_loss=False,
                   incomplete_reasons=[] if complete else ["fixture_incomplete"])
    manifest = full_run_manifest.build_manifest(inputs, catalog, [readset], certificates=[certificate])
    # Exercise producer metadata and accepted-subset semantics together.
    manifest["accepted_suite_ids"] = [sid]
    write_json(root / "catalog.json", catalog)
    write_json(root / "full-run.json", manifest)
    write_json(root / "decision.json", {"execution_mode": "selected", "executed_suite_ids": [sid]})
    write_json(root / "capture/capture-epoch.json", epoch)
    write_json(root / "capture/context.json", binding)
    write_json(root / certificate_ref, certificate)
    write_json(root / (directory + "worker.json"), readset)
    write_json(root / (directory + "worker.misses.json"), [])
    (root / outcome_ref).write_bytes(b'{"status":"PASS","test_id":"fixture::test"}\n')
    write_json(root / "readsets-rejected/foreign.json", {"suite_id": "foreign"})
    return root


def invoke(out, *shards):
    return subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "--out", str(out),
                           *(str(shard) for shard in shards)], capture_output=True, text=True)


def assert_refusal(tmp_path, shards, reason):
    before = [snapshot(shard) for shard in shards]
    out = tmp_path / "merged"
    result = invoke(out, *shards)
    assert result.returncode != 0
    assert result.stderr == ""
    assert result.stdout.splitlines() == [json.dumps({"merged": False, "reason": reason}, sort_keys=True)]
    assert not out.exists()
    assert not list(tmp_path.glob(".merged.*"))
    assert [snapshot(shard) for shard in shards] == before


def readset_path(root):
    return next(path for path in root.glob("readsets/*/*/*.json") if not path.name.endswith(".misses.json"))


def test_disjoint_union_is_exact_deterministic_and_preserves_originals(tmp_path):
    first = make_shard(tmp_path, "one", "alpha")
    second = make_shard(tmp_path, "two", "beta/suite")
    originals = [snapshot(first), snapshot(second)]
    # Whitespace is deliberately noncanonical: original manifests stay verbatim.
    original = read_json(first / "full-run.json")
    (first / "full-run.json").write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")
    originals[0] = snapshot(first)
    out, reversed_out = tmp_path / "merged", tmp_path / "reversed"
    for target, shards in ((out, [first, second]), (reversed_out, [second, first])):
        result = invoke(target, *shards)
        assert result.returncode == 0, result.stdout + result.stderr
    assert snapshot(out) == snapshot(reversed_out)
    assert [snapshot(first), snapshot(second)] == originals
    expected = {name: raw for files in originals for name, raw in files.items() if name.startswith("readsets/")}
    actual = {name: raw for name, raw in snapshot(out).items() if name.startswith("readsets/")}
    assert actual == expected
    for files in originals:
        for name, raw in files.items():
            if name.startswith("reports/"):
                assert (out / name).read_bytes() == raw
    assert (out / "catalog.json").read_bytes() == (first / "catalog.json").read_bytes()
    assert not (out / "full-run.json").exists()
    assert not (out / "readsets-rejected").exists()
    manifest = read_json(out / "merged-manifest.json")
    assert manifest["run_ids"] == ["build:one", "build:two"]
    assert manifest["accepted_suite_ids"] == ["alpha", "beta/suite"]
    assert manifest["suite_ids"] == SUITES
    assert manifest["uncovered"] == ["unobserved"]
    assert manifest["unproven"] == ["alpha", "beta/suite"]
    assert manifest["mandatory_suite_ids"] == SUITES
    assert manifest["complete"] is False
    assert manifest["full_run_complete"] is False
    assert manifest["test_id_reporting_complete"] is False
    for index, root in enumerate((first, second)):
        record = manifest["shards"][index]
        raw = (root / "full-run.json").read_bytes()
        assert (out / record["manifest"]).read_bytes() == raw
        assert record["manifest_sha256"] == hashlib.sha256(raw).hexdigest()
    # Reuse the actual selector's capture-epoch check, including its root digests.
    mapping = {"capture_epoch": manifest["capture_epoch"], "external_root_digests": {}}
    select_tests.verify_capture_epoch(mapping, out / "capture/capture-epoch.json")


def test_complete_capture_coverage_still_is_not_a_full_run(tmp_path):
    shards = [make_shard(tmp_path, str(index), sid, complete=True) for index, sid in enumerate(SUITES)]
    out = tmp_path / "merged"
    result = invoke(out, *shards)
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = read_json(out / "merged-manifest.json")
    assert manifest["uncovered"] == manifest["unproven"] == []
    assert manifest["mandatory_suite_ids"] == SUITES
    assert manifest["complete_scope"] == "capture_coverage"
    assert manifest["complete"] is True
    assert manifest["full_run_complete"] is False
    assert not (out / "full-run.json").exists()


def test_contradictory_capture_flags_do_not_invent_completeness(tmp_path):
    shards = [make_shard(tmp_path, str(index), sid, complete=True) for index, sid in enumerate(SUITES)]
    path = readset_path(shards[0])
    readset = read_json(path)
    readset["trace_loss"] = True
    write_json(path, readset)
    out = tmp_path / "merged"
    result = invoke(out, *shards)
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = read_json(out / "merged-manifest.json")
    assert manifest["uncovered"] == []
    assert manifest["unproven"] == ["alpha"]
    assert manifest["complete"] is False
    assert manifest["mandatory_suite_ids"] == SUITES


@pytest.mark.parametrize("field,value", [
    ("source_sha", "1" * 40),
    ("source_tree", "2" * 40),
    ("runner", "3" * 40),
    ("catalog_sha256", "changed-catalog"),
    ("image_digest", "sha256:" + "4" * 64),
    ("capture_epoch", "changed-parser"),
    ("python_version", "different-python"),
    ("capture_sha", "5" * 40),
])
def test_lineage_mismatch(tmp_path, field, value):
    shards = [make_shard(tmp_path, "one", "alpha"),
              make_shard(tmp_path, "two", "beta/suite", change={field: value})]
    assert_refusal(tmp_path, shards, "lineage_mismatch:" + field)


def test_overlap(tmp_path):
    shards = [make_shard(tmp_path, "one", "alpha"), make_shard(tmp_path, "two", "alpha")]
    assert_refusal(tmp_path, shards, "overlap")


def test_missing_readsets(tmp_path):
    shard = make_shard(tmp_path, "one", "alpha")
    readset_path(shard).unlink()
    assert_refusal(tmp_path, [shard], "missing_readsets")


def test_missing_referenced_outcomes(tmp_path):
    shard = make_shard(tmp_path, "one", "alpha")
    doc = read_json(readset_path(shard))
    (shard / doc["outcomes_ref"]).unlink()
    assert_refusal(tmp_path, [shard], "missing_readsets")


def test_malformed_outcomes(tmp_path):
    shard = make_shard(tmp_path, "one", "alpha")
    doc = read_json(readset_path(shard))
    (shard / doc["outcomes_ref"]).write_bytes(b'{"status":\n')
    assert_refusal(tmp_path, [shard], "malformed")


def test_missing_observed_worker(tmp_path):
    shard = make_shard(tmp_path, "one", "alpha")
    path = shard / "full-run.json"
    manifest = read_json(path)
    manifest["workers_by_suite"]["alpha"].append("gw1")
    write_json(path, manifest)
    assert_refusal(tmp_path, [shard], "missing_readsets")


@pytest.mark.parametrize("reference", ["/absolute/outcomes.jsonl", "../outside.jsonl",
                                        "readsets/alpha/../outside.jsonl", "C:/outside.jsonl",
                                        "..\\outside.jsonl"])
def test_path_escape(tmp_path, reference):
    shard = make_shard(tmp_path, "one", "alpha")
    path = readset_path(shard)
    doc = read_json(path)
    doc["outcomes_ref"] = reference
    write_json(path, doc)
    assert_refusal(tmp_path, [shard], "path_escape")


@pytest.mark.parametrize("reference", ["/absolute/member", "../outside"])
def test_manifest_member_path_escape(tmp_path, reference):
    shard = make_shard(tmp_path, "one", "alpha")
    path = shard / "full-run.json"
    doc = read_json(path)
    doc["members"] = [reference]
    write_json(path, doc)
    assert_refusal(tmp_path, [shard], "path_escape")


def test_symlink_escape(tmp_path):
    shard = make_shard(tmp_path, "one", "alpha")
    outside = tmp_path / "outside.json"
    write_json(outside, {})
    try:
        (shard / "escaped.json").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert_refusal(tmp_path, [shard], "path_escape")


@pytest.mark.parametrize("raw", [b'{"schema":', b'[]',
                               b'{"schema":"leaf.ci.full-run.v1","schema":"duplicate"}',
                               b'{"schema":NaN}'])
def test_malformed_manifest(tmp_path, raw):
    shard = make_shard(tmp_path, "one", "alpha")
    (shard / "full-run.json").write_bytes(raw)
    assert_refusal(tmp_path, [shard], "malformed")


def test_catalog_digest_is_recomputed(tmp_path):
    shard = make_shard(tmp_path, "one", "alpha")
    path = shard / "catalog.json"
    doc = read_json(path)
    doc["suites"][0]["test_ids"] = ["tampered"]
    write_json(path, doc)
    assert_refusal(tmp_path, [shard], "lineage_mismatch:catalog_sha256")


def test_readset_lineage_cannot_hide_behind_manifest(tmp_path):
    shard = make_shard(tmp_path, "one", "alpha")
    path = readset_path(shard)
    doc = read_json(path)
    doc["source_sha"] = "9" * 40
    write_json(path, doc)
    assert_refusal(tmp_path, [shard], "lineage_mismatch:source_sha")


def test_certificate_digest_is_checked(tmp_path):
    shard = make_shard(tmp_path, "one", "alpha")
    doc = read_json(readset_path(shard))
    path = shard / doc["process_tree_ref"]
    certificate = read_json(path)
    certificate["complete"] = True
    write_json(path, certificate)
    assert_refusal(tmp_path, [shard], "lineage_mismatch:process_tree_sha256")


def test_epoch_digest_is_checked(tmp_path):
    shard = make_shard(tmp_path, "one", "alpha")
    path = shard / "full-run.json"
    manifest = read_json(path)
    manifest["capture_epoch"] = "0" * 64
    write_json(path, manifest)
    assert_refusal(tmp_path, [shard], "lineage_mismatch:capture_epoch")


def test_missing_image_identity_is_not_invented(tmp_path):
    shard = make_shard(tmp_path, "one", "alpha")
    path = shard / "full-run.json"
    manifest = read_json(path)
    manifest["capture_epoch_manifest"]["image_manifest_digest"] = None
    write_json(path, manifest)
    assert_refusal(tmp_path, [shard], "missing_lineage:image_digest")


def test_missing_claim_is_not_inferred_from_catalog_suite_ids(tmp_path):
    shard = make_shard(tmp_path, "one", "alpha")
    # A real full-run manifest names the entire catalog in suite_ids. The merger
    # must use workers/decision/readsets to find the accepted subset instead.
    path = shard / "full-run.json"
    manifest = read_json(path)
    del manifest["accepted_suite_ids"]
    write_json(path, manifest)
    out = tmp_path / "merged"
    result = invoke(out, shard)
    assert result.returncode == 0, result.stdout + result.stderr
    assert read_json(out / "merged-manifest.json")["uncovered"] == ["beta/suite", "unobserved"]


def test_claimed_suite_without_readsets_refuses(tmp_path):
    shard = make_shard(tmp_path, "one", "alpha")
    path = shard / "full-run.json"
    manifest = read_json(path)
    manifest["accepted_suite_ids"].append("beta/suite")
    write_json(path, manifest)
    assert_refusal(tmp_path, [shard], "missing_readsets")


def test_conflicting_report_paths_refuse(tmp_path):
    first = make_shard(tmp_path, "one", "alpha")
    second = make_shard(tmp_path, "two", "beta/suite")
    for root, value in ((first, 1), (second, 2)):
        write_json(root / "reports/common.json", {"value": value})
    assert_refusal(tmp_path, [first, second], "overlap")


def test_output_inside_input_refuses_without_modifying_input(tmp_path):
    shard = make_shard(tmp_path, "one", "alpha")
    before = snapshot(shard)
    out = shard / "merged"
    result = invoke(out, shard)
    assert result.returncode != 0
    assert json.loads(result.stdout) == {"merged": False, "reason": "path_escape"}
    assert not out.exists()
    assert snapshot(shard) == before


def test_existing_output_is_never_replaced(tmp_path):
    shard = make_shard(tmp_path, "one", "alpha")
    out = tmp_path / "merged"
    out.mkdir()
    (out / "keep").write_bytes(b"original")
    before = snapshot(out)
    result = invoke(out, shard)
    assert result.returncode != 0
    assert json.loads(result.stdout) == {"merged": False, "reason": "output_exists"}
    assert snapshot(out) == before
    assert not list(tmp_path.glob(".merged.*"))


def test_staging_failure_leaves_no_output_or_temporary_tree(tmp_path, monkeypatch, capsys):
    shard = make_shard(tmp_path, "one", "alpha")
    before = snapshot(shard)
    original_copy = merger.shutil.copyfile
    calls = []

    def fail_after_one_copy(source, destination):
        calls.append(destination)
        if len(calls) == 2:
            raise OSError("fixture disk failure")
        return original_copy(source, destination)

    monkeypatch.setattr(merger.shutil, "copyfile", fail_after_one_copy)
    out = tmp_path / "merged"
    assert merger.main(["--out", str(out), str(shard)]) == 1
    assert json.loads(capsys.readouterr().out) == {"merged": False, "reason": "io_error"}
    assert not out.exists()
    assert not list(tmp_path.glob(".merged.*"))
    assert snapshot(shard) == before
