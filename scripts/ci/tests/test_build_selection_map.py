"""Candidate-only contracts using actual S1 merges of minimal producer shards."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

import build_selection_map as builder
import full_run_manifest
import merge_capture_shards as merger
import select_tests


SCRIPT = Path(builder.__file__)
SUITES = ["alpha", "mandatory", "unobserved"]
ADDITIONS = {"catalog_snapshot", "catalog_sha256", "capture_epoch", "external_root_digests",
             "capture_proposals", "proposal_exclusions", "builder"}


def write_json(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(merger.canonical(document) + b"\n")


def read_json(path):
    return json.loads(path.read_bytes())


def snapshot(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*")) if path.is_file()}


def make_merged(tmp_path, index, *, change=None, unproven=None, reads=None, accepted=None):
    change = change or {}
    accepted = ["alpha", "mandatory"] if accepted is None else accepted
    source = tmp_path / ("shard-" + str(index))
    source.mkdir()
    catalog = {"schema": "leaf.ci.test-catalog.v1", "kind": "web",
               "runner_blob_sha": change.get("runner", "d" * 40),
               "suites": [{"id": sid, "test_ids": []} for sid in SUITES]}
    if "catalog_sha256" in change:
        catalog["fixture_revision"] = change["catalog_sha256"]
    catalog["catalog_sha256"] = select_tests.catalog_info(catalog)[1]
    inputs = {"repo": "leaf-web-demo", "run_id": change.get("run_id", "build:" + str(index)),
              "source_sha": change.get("source_sha", "a" * 40),
              "source_tree": change.get("source_tree", "b" * 40),
              "capture_sha": change.get("capture_sha", "c" * 40),
              "image": change.get("image_digest", "sha256:" + "e" * 64),
              "execution_mode": "selected", "full_run_complete": False,
              "test_id_reporting_complete": False}
    epoch = {"schema": "leaf.ci.capture-epoch.v1", "external_root_table": {},
             "external_root_digests": change.get("external_root_digests", {}),
             "image_manifest_digest": inputs["image"],
             "interpreter_identity": {"version": change.get("python_version", sys.version)},
             "toolchain_fingerprint": full_run_manifest.toolchain_fingerprint(inputs["image"], inputs["capture_sha"]),
             "parser_version": change.get("capture_epoch", "fixture-parser")}
    binding = {key: inputs[key] for key in merger.BINDINGS if key in inputs}
    binding["catalog_sha256"] = catalog["catalog_sha256"]
    readsets, certificates = [], []
    for sid in accepted:
        complete = sid != unproven
        ref = "reports/process-tree-" + sid + ".json"
        outcome = "readsets/" + sid + "/1/attempts.jsonl"
        capture_group = sid + "-" + str(index)
        certificate = dict(binding, schema="leaf.ci.process-tree.v1",
                           capture_group=capture_group,
                           transcript_sha256=hashlib.sha256(capture_group.encode("utf-8")).hexdigest(),
                           capture_epoch=select_tests.normalized_epoch(epoch), capture_epoch_manifest=epoch,
                           complete=complete, decoder_complete=complete, trace_loss=False,
                           reasons=[] if complete else ["fixture_incomplete"])
        paths = [{"path": "src/" + sid + ".py", "kind": "open"},
                 {"path": "data/run-" + str(index) + ".json", "kind": "open"}]
        readset = dict(binding, schema="leaf.ci.readset.v1", suite_id=sid, worker="gw0", attempt=1,
                       trace_kind="linux-process-tree", python_only=False,
                       capture_epoch=select_tests.normalized_epoch(epoch), capture_epoch_manifest=epoch,
                       outcomes_ref=outcome, process_tree_ref=ref, union_ref=ref,
                       process_tree_sha256=merger.digest(certificate),
                       reads=paths if reads is None or sid != "alpha" else reads,
                       capture_complete=complete, children_complete=complete,
                       generated_inputs_resolved=complete, shared_state_closure_resolved=complete,
                       external_inputs_resolved=complete, decoder_complete=complete, trace_loss=False,
                       incomplete_reasons=[] if complete else ["fixture_incomplete"])
        write_json(source / ref, certificate)
        write_json(source / ("readsets/" + sid + "/1/worker.json"), readset)
        (source / outcome).write_bytes(b'{"status":"PASS","test_id":"fixture::test"}\n')
        readsets.append(readset)
        certificates.append(certificate)
    manifest = full_run_manifest.build_manifest(inputs, catalog, readsets, certificates=certificates)
    manifest["accepted_suite_ids"] = accepted
    write_json(source / "full-run.json", manifest)
    write_json(source / "catalog.json", catalog)
    write_json(source / "capture/capture-epoch.json", epoch)
    write_json(source / "decision.json", {"executed_suite_ids": accepted})
    merged = tmp_path / ("merged-" + str(index))
    merger.merge(merged, [source])
    return merged


@pytest.fixture
def trusted(tmp_path):
    mapping = {"schema": "leaf.ci.test-selection-map.v1", "repo": "LEAF-Solar-Design/leaf-web-demo",
               "selection_enabled": False, "phase": "shadow", "minimum_full_runs": 3,
               "mandatory_suite_ids": ["mandatory", "alpha", "unobserved"],
               "always_select_suite_ids": ["mandatory"], "force_full_rules": ["scripts/ci/**", "**/*.lock"],
               "merge_gate": "github-merge-queue", "mandatory_justification": {"alpha": ["keep", {"why": "trusted"}]},
               "unresolved_mandatory": ["unobserved"],
               "suites": {"mandatory": {"mappable": False, "read_paths": ["trusted/input"],
                                         "external_inputs": [{"class": "s3", "resolved": False}]}},
               "future_policy": {"retain": [None, False, 19, "policy"]}}
    path = tmp_path / "trusted.json"
    path.write_text(json.dumps(mapping, indent=2) + "\n", encoding="utf-8")
    return path


def captures(tmp_path, **last):
    return [make_merged(tmp_path, index, **(last if index == 2 else {})) for index in range(3)]


def invoke(trusted, out, directories):
    return subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "--trusted-map", str(trusted),
                           "--out", str(out), *(str(path) for path in directories)],
                          capture_output=True, text=True)


def assert_refusal(tmp_path, trusted, directories, reason, out=None):
    out = tmp_path / "candidate.json" if out is None else out
    originals = [snapshot(directory) for directory in directories]
    trusted_raw = trusted.read_bytes()
    prior = out.read_bytes() if out.is_file() else None
    result = invoke(trusted, out, directories)
    assert result.returncode == 1
    assert result.stderr == ""
    assert result.stdout.splitlines() == [json.dumps({"built": False, "reason": reason}, sort_keys=True)]
    if prior is None:
        assert not out.exists()
    else:
        assert out.read_bytes() == prior
    assert trusted.read_bytes() == trusted_raw
    assert [snapshot(directory) for directory in directories] == originals
    assert not list(out.parent.glob("." + out.name + ".*"))


def test_three_runs_preserve_all_trusted_controls_and_load_deterministically(tmp_path, trusted):
    directories = captures(tmp_path)
    original_inputs = [snapshot(directory) for directory in directories]
    trusted_raw = trusted.read_bytes()
    out, reverse = tmp_path / "candidate.json", tmp_path / "reverse.json"
    for target, ordered in ((out, directories), (reverse, list(reversed(directories)))):
        result = invoke(trusted, target, ordered)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout == result.stderr == ""
    assert out.read_bytes() == reverse.read_bytes()
    # This is the actual loader called by decide(), not a substitute decoder.
    candidate, raw = select_tests.load_json(out)
    original = read_json(trusted)
    assert {key: candidate[key] for key in original} == original
    assert set(candidate) - set(original) == ADDITIONS
    assert candidate["catalog_snapshot"] == read_json(directories[0] / "catalog.json")
    assert select_tests.catalog_info(candidate["catalog_snapshot"])[1] == candidate["catalog_sha256"]
    select_tests.verify_capture_epoch(candidate, directories[0] / "capture/capture-epoch.json")
    assert set(candidate["capture_proposals"]) == {"alpha", "mandatory"}
    assert candidate["capture_proposals"]["alpha"] == {
        "read_paths": ["data/run-0.json", "data/run-1.json", "data/run-2.json", "src/alpha.py"],
        "runs": 3, "status": "empirical", "closure_proven": False}
    assert candidate["proposal_exclusions"] == {"unobserved": ["not_accepted"]}
    provenance = candidate["builder"]
    assert provenance["run_ids"] == ["build:0", "build:1", "build:2"]
    assert provenance["trusted_map_sha256"] == hashlib.sha256(trusted_raw).hexdigest()
    assert provenance["merged_manifest_sha256"] == sorted(
        hashlib.sha256((directory / "merged-manifest.json").read_bytes()).hexdigest() for directory in directories)
    for run, directory in zip(provenance["runs"], directories):
        manifest_raw = (directory / "shards/0/full-run.json").read_bytes()
        assert run["manifest"] == json.loads(manifest_raw)
        assert run["manifest_sha256"] == hashlib.sha256(manifest_raw).hexdigest()
    assert raw == select_tests.canonical(candidate) + b"\n"
    assert trusted.read_bytes() == trusted_raw
    assert [snapshot(directory) for directory in directories] == original_inputs


def test_too_few_merged_dirs(tmp_path, trusted):
    assert_refusal(tmp_path, trusted, captures(tmp_path)[:2], "minimum_full_runs")


def test_duplicate_run_ids(tmp_path, trusted):
    assert_refusal(tmp_path, trusted, captures(tmp_path, change={"run_id": "build:0"}), "duplicate_run")


def test_multiple_disjoint_shards_keep_every_run_provenance(tmp_path, trusted):
    directories = []
    for index in range(3):
        first, second = str(index) + "a", str(index) + "b"
        make_merged(tmp_path, first, accepted=["alpha"])
        make_merged(tmp_path, second, accepted=["mandatory"])
        merged = tmp_path / ("combined-" + str(index))
        merger.merge(merged, [tmp_path / ("shard-" + second), tmp_path / ("shard-" + first)])
        directories.append(merged)
    out = tmp_path / "candidate.json"
    result = invoke(trusted, out, directories)
    assert result.returncode == 0, result.stdout + result.stderr
    candidate = read_json(out)
    assert candidate["builder"]["run_ids"] == ["build:" + str(index) + suffix
                                               for index in range(3) for suffix in ("a", "b")]
    assert len(candidate["builder"]["runs"]) == 6
    assert set(candidate["capture_proposals"]) == {"alpha", "mandatory"}
    assert candidate["capture_proposals"]["alpha"]["runs"] == 3


def test_duplicate_run_within_one_merged_directory(tmp_path, trusted):
    directories = captures(tmp_path)
    make_merged(tmp_path, "other", accepted=["unobserved"], change={"run_id": "build:0"})
    combined = tmp_path / "combined"
    merger.merge(combined, [tmp_path / "shard-0", tmp_path / "shard-other"])
    assert_refusal(tmp_path, trusted, [combined, *directories[1:]], "duplicate_run")


@pytest.mark.parametrize("field,value", [
    ("source_sha", "1" * 40), ("source_tree", "2" * 40), ("runner", "3" * 40),
    ("catalog_sha256", "different-recipe"), ("image_digest", "sha256:" + "4" * 64),
    ("python_version", "different-python"), ("capture_epoch", "different-parser"),
    ("external_root_digests", {"/managed": {"sha256": "5" * 64}}), ("capture_sha", "6" * 40),
])
def test_exact_lineage_required(tmp_path, trusted, field, value):
    directories = captures(tmp_path, change={field: value})
    if field in ("source_sha", "source_tree"):
        assert read_json(directories[0] / "catalog.json") == read_json(directories[2] / "catalog.json")
    assert_refusal(tmp_path, trusted, directories, "lineage_mismatch:" + field)


@pytest.mark.parametrize("raw", [b'{"schema":', b'[]', b'{"schema":NaN}',
                               b'{"schema":"leaf.ci.capture-merge.v1","schema":"duplicate"}'])
def test_malformed_manifest(tmp_path, trusted, raw):
    directories = captures(tmp_path)
    (directories[1] / "merged-manifest.json").write_bytes(raw)
    assert_refusal(tmp_path, trusted, directories, "malformed")


def test_retained_manifest_hash_is_checked(tmp_path, trusted):
    directories = captures(tmp_path)
    path = directories[1] / "shards/0/full-run.json"
    path.write_bytes(path.read_bytes() + b" ")
    assert_refusal(tmp_path, trusted, directories, "malformed")


def test_readset_source_drift_is_not_hidden_by_merged_lineage(tmp_path, trusted):
    directories = captures(tmp_path)
    path = directories[1] / "readsets/alpha/1/worker.json"
    doc = read_json(path)
    doc["source_sha"] = "7" * 40
    write_json(path, doc)
    assert_refusal(tmp_path, trusted, directories, "lineage_mismatch:source_sha")


def test_missing_readset_refuses(tmp_path, trusted):
    directories = captures(tmp_path)
    (directories[1] / "readsets/alpha/1/worker.json").unlink()
    assert_refusal(tmp_path, trusted, directories, "missing_readsets")


def test_unproven_in_one_run_has_no_proposal(tmp_path, trusted):
    directories = captures(tmp_path, unproven="alpha")
    out = tmp_path / "candidate.json"
    assert invoke(trusted, out, directories).returncode == 0
    candidate = read_json(out)
    assert "alpha" not in candidate["capture_proposals"]
    assert candidate["proposal_exclusions"]["alpha"] == ["unproven"]
    assert candidate["mandatory_suite_ids"] == read_json(trusted)["mandatory_suite_ids"]


def test_absent_from_one_run_has_no_proposal(tmp_path, trusted):
    directories = captures(tmp_path, accepted=["mandatory"])
    out = tmp_path / "candidate.json"
    assert invoke(trusted, out, directories).returncode == 0
    assert read_json(out)["proposal_exclusions"]["alpha"] == ["not_accepted"]


def test_process_union_cannot_hide_an_external_input(tmp_path, trusted):
    directories = captures(tmp_path)
    certificate_path = directories[1] / "reports/process-tree-alpha.json"
    certificate = read_json(certificate_path)
    certificate["external_inputs"] = [{"class": "s3", "resolved": False}]
    write_json(certificate_path, certificate)
    readset_path = directories[1] / "readsets/alpha/1/worker.json"
    readset = read_json(readset_path)
    readset["process_tree_sha256"] = merger.digest(certificate)
    write_json(readset_path, readset)
    original_path = directories[1] / "shards/0/full-run.json"
    original = read_json(original_path)
    original["process_tree_sha256s"] = sorted([
        merger.digest(certificate), merger.digest(read_json(directories[1] / "reports/process-tree-mandatory.json"))])
    write_json(original_path, original)
    merged_path = directories[1] / "merged-manifest.json"
    merged = read_json(merged_path)
    merged["shards"][0]["manifest_sha256"] = hashlib.sha256(original_path.read_bytes()).hexdigest()
    write_json(merged_path, merged)
    out = tmp_path / "candidate.json"
    result = invoke(trusted, out, directories)
    assert result.returncode == 0, result.stdout + result.stderr
    candidate = read_json(out)
    assert "alpha" not in candidate["capture_proposals"]
    assert candidate["proposal_exclusions"]["alpha"] == ["outside_read"]
    assert candidate["suites"] == read_json(trusted)["suites"]


def test_actual_unhashed_runtime_root_provenance_is_preserved(tmp_path, trusted):
    roots = {"/proc": {"class": "container-runtime", "sha256": None, "complete": True,
                       "reason": None, "files": 0, "bytes": 0, "symlinks_outside": 0}}
    directories = [make_merged(tmp_path, index, change={"external_root_digests": roots}) for index in range(3)]
    out = tmp_path / "candidate.json"
    result = invoke(trusted, out, directories)
    assert result.returncode == 0, result.stdout + result.stderr
    candidate = read_json(out)
    assert candidate["external_root_digests"] == roots
    assert all(row["closure_proven"] is False for row in candidate["capture_proposals"].values())


@pytest.mark.parametrize("reads,reason", [
    ([{"path": "../outside", "kind": "open"}], "outside_read"),
    ([{"path": "/absolute/input", "kind": "open"}], "absolute_read"),
    ([{"path": "C:/absolute/input", "kind": "open"}], "absolute_read"),
    ([{"path": "C:drive-relative", "kind": "open"}], "absolute_read"),
    ([{"path": "looks/relative", "kind": "open", "scope": "outside"}], "outside_read"),
    ([{"path": "unknown", "kind": "unknown"}], "unknown_read"),
    ([{"kind": "open"}], "unknown_read"),
    ([], "missing_read"),
])
def test_outside_absolute_unknown_and_empty_reads_are_excluded(tmp_path, trusted, reads, reason):
    directories = captures(tmp_path, reads=reads)
    out = tmp_path / "candidate.json"
    assert invoke(trusted, out, directories).returncode == 0
    candidate = read_json(out)
    assert "alpha" not in candidate["capture_proposals"]
    assert reason in candidate["proposal_exclusions"]["alpha"]
    assert candidate["mandatory_suite_ids"] == read_json(trusted)["mandatory_suite_ids"]


@pytest.mark.parametrize("update", [{"selection_enabled": True}, {"phase": "active"},
                                   {"selection_enabled": 0}])
def test_only_disabled_shadow_trusted_maps(tmp_path, trusted, update):
    mapping = read_json(trusted)
    mapping.update(update)
    write_json(trusted, mapping)
    assert_refusal(tmp_path, trusted, captures(tmp_path), "trusted_map_enabled")


def test_existing_output_is_untouched(tmp_path, trusted):
    out = tmp_path / "candidate.json"
    out.write_bytes(b"original candidate bytes\n")
    assert_refusal(tmp_path, trusted, captures(tmp_path), "output_exists", out)


def test_output_may_not_equal_trusted_map(tmp_path, trusted):
    assert_refusal(tmp_path, trusted, captures(tmp_path), "path_escape", trusted)


def test_output_may_not_be_a_committed_map_path(tmp_path, trusted):
    out = tmp_path / "other-checkout/scripts/ci/test-selection-map.json"
    assert_refusal(tmp_path, trusted, captures(tmp_path), "path_escape", out)


def test_output_may_not_live_inside_any_input(tmp_path, trusted):
    directories = captures(tmp_path)
    assert_refusal(tmp_path, trusted, directories, "path_escape", directories[0] / "candidate.json")


def test_referenced_path_escape_refuses(tmp_path, trusted):
    directories = captures(tmp_path)
    path = directories[0] / "readsets/alpha/1/worker.json"
    doc = read_json(path)
    doc["outcomes_ref"] = "../outside.jsonl"
    write_json(path, doc)
    assert_refusal(tmp_path, trusted, directories, "path_escape")


def test_exclusive_publication_race_keeps_the_other_output(tmp_path, trusted, monkeypatch, capsys):
    directories = captures(tmp_path)
    out = tmp_path / "candidate.json"
    original_link = builder.os.link

    def competing_link(source, destination):
        Path(destination).write_bytes(b"other writer")
        return original_link(source, destination)

    monkeypatch.setattr(builder.os, "link", competing_link)
    assert builder.main(["--trusted-map", str(trusted), "--out", str(out),
                         *(str(path) for path in directories)]) == 1
    assert json.loads(capsys.readouterr().out) == {"built": False, "reason": "output_exists"}
    assert out.read_bytes() == b"other writer"
    assert not list(tmp_path.glob(".candidate.json.*"))
