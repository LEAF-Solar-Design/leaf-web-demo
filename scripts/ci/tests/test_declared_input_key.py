"""Declared-input contracts only; these fixtures do not admit real producers."""

import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

import declared_input_key as inputs


@pytest.fixture
def tree(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "src"
    source.mkdir(parents=True)
    (source / "a.txt").write_text("first", encoding="utf-8")
    (source / "b.txt").write_text("second", encoding="utf-8")
    for path in source.iterdir():
        path.chmod(0o644)
    (repo / "outside.txt").write_text("not declared", encoding="utf-8")
    return repo


@pytest.fixture
def declared():
    return {"schema": inputs.SCHEMA, "suites": {"sample": {
        "roots": ["src"], "status": "unverified",
        "evidence": "fixture:1", "notes": "Synthetic tree, not a producer proof.",
    }}}


def key(repo, declared, **overrides):
    args = dict(repo=repo, suite_id="sample", recipe="recipe-sha256",
                toolchain={"python": "3.fixture", "node": "fixture"},
                image_digest="sha256:image", runner_digest="runner-sha256",
                declared=declared)
    args.update(overrides)
    return inputs.compute_key(**args)


def entry(declared):
    return declared["suites"]["sample"]


def test_identical_tree_is_stable_across_order_mtime_and_checkout(tree, declared, tmp_path):
    before = key(tree, declared)
    assert re.fullmatch(r"[0-9a-f]{64}", before)
    os.utime(tree / "src/a.txt", (100, 100))
    reordered = {"node": "fixture", "python": "3.fixture"}
    assert key(tree, declared, toolchain=reordered) == before
    clone = tmp_path / "clone"
    shutil.copytree(tree, clone)
    assert key(clone, copy.deepcopy(declared)) == before


@pytest.mark.parametrize("change", ["content", "mode", "new", "removed"])
def test_each_declared_tree_change_changes_key(tree, declared, change):
    before = key(tree, declared)
    path = tree / "src/a.txt"
    if change == "content":
        path.write_text("changed", encoding="utf-8")
    elif change == "mode":
        path.chmod(0o755)
    elif change == "new":
        (tree / "src/new.txt").write_text("new", encoding="utf-8")
    else:
        path.unlink()
    assert key(tree, declared) != before


@pytest.mark.parametrize("field,value", [
    ("recipe", "new-recipe"),
    ("toolchain", {"python": "another-version", "node": "fixture"}),
    ("image_digest", "sha256:another-image"),
    ("runner_digest", "another-runner"),
    ("suite_id", "other-suite"),
])
def test_each_identity_is_bound(tree, declared, field, value):
    declared["suites"]["other-suite"] = copy.deepcopy(entry(declared))
    assert key(tree, declared, **{field: value}) != key(tree, declared)


def test_outside_edits_do_not_change_key(tree, declared):
    before = key(tree, declared)
    (tree / "outside.txt").write_text("changed outside", encoding="utf-8")
    (tree / "new-outside.txt").write_text("new outside", encoding="utf-8")
    assert key(tree, declared) == before


def test_exact_roots_are_bound_even_when_new_root_is_empty(tree, declared):
    (tree / "empty").mkdir()
    before = key(tree, declared)
    entry(declared)["roots"].append("empty")
    assert key(tree, declared) != before
    after = key(tree, declared)
    entry(declared)["roots"].reverse()
    assert key(tree, declared) == after


def test_file_root_does_not_include_siblings(tree, declared):
    entry(declared)["roots"] = ["src/a.txt"]
    before = key(tree, declared)
    (tree / "src/b.txt").write_text("outside file root", encoding="utf-8")
    assert key(tree, declared) == before
    (tree / "src/a.txt").write_text("inside file root", encoding="utf-8")
    assert key(tree, declared) != before


def test_symlink_retarget_changes_key_even_for_identical_target_bytes(tree, declared):
    (tree / "src/b.txt").write_bytes((tree / "src/a.txt").read_bytes())
    link = tree / "src/link"
    link.symlink_to("a.txt")
    before = key(tree, declared)
    link.unlink()
    link.symlink_to("b.txt")
    assert key(tree, declared) != before


def test_symlink_between_declared_roots_binds_target_bytes(tree, declared):
    target = tree / "other"
    target.mkdir()
    (target / "value").write_text("original", encoding="utf-8")
    entry(declared)["roots"].append("other")
    (tree / "src/linked-dir").symlink_to("../other", target_is_directory=True)
    before = key(tree, declared)
    (target / "value").write_text("different", encoding="utf-8")
    assert key(tree, declared) != before


def test_symlink_in_root_ancestor_is_bound(tree, declared):
    alias = tree / "alias"
    alias.symlink_to("src", target_is_directory=True)
    entry(declared)["roots"].append("alias/a.txt")
    before = key(tree, declared)
    alias.unlink()
    alias.symlink_to("./src", target_is_directory=True)
    assert key(tree, declared) != before


@pytest.mark.parametrize("root", ["/absolute", "../outside", "src/../src", "C:/absolute", "\\\\host\\share"])
def test_absolute_or_parent_roots_fail_closed(tree, declared, root):
    entry(declared)["roots"] = [root]
    with pytest.raises(inputs.DeclaredInputError, match="invalid_root"):
        key(tree, declared)


@pytest.mark.parametrize("root", ["missing", "src/missing"])
def test_missing_root_fails_closed(tree, declared, root):
    entry(declared)["roots"] = [root]
    with pytest.raises(inputs.DeclaredInputError, match="missing_root"):
        key(tree, declared)


def test_deleted_whole_root_is_not_an_empty_success(tree, declared):
    key(tree, declared)
    shutil.rmtree(tree / "src")
    with pytest.raises(inputs.DeclaredInputError, match="missing_root"):
        key(tree, declared)


@pytest.mark.parametrize("target", ["../outside.txt", "../../outside-repo", "../src-sibling/value"])
def test_outside_symlinks_fail_closed(tree, declared, tmp_path, target):
    (tmp_path / "outside-repo").write_text("outside repo", encoding="utf-8")
    (tree / "src-sibling").mkdir()
    (tree / "src-sibling/value").write_text("sibling prefix", encoding="utf-8")
    (tree / "src/link").symlink_to(target)
    with pytest.raises(inputs.DeclaredInputError, match="outside_symlink"):
        key(tree, declared)


def test_outside_intermediate_symlink_hop_fails_closed(tree, declared):
    (tree / "outside-link").symlink_to("src/a.txt")
    (tree / "src/link").symlink_to("../outside-link")
    with pytest.raises(inputs.DeclaredInputError, match="outside_symlink"):
        key(tree, declared)


def test_parent_after_symlink_cannot_hide_an_escape(tree, declared):
    (tree / "src/inner").symlink_to(".", target_is_directory=True)
    (tree / "src/link").symlink_to("inner/../outside.txt")
    with pytest.raises(inputs.DeclaredInputError, match="outside_symlink"):
        key(tree, declared)


def test_root_with_escaping_symlink_ancestor_fails_closed(tree, declared, tmp_path):
    outside = tmp_path / "external"
    outside.mkdir()
    (outside / "value").write_text("outside", encoding="utf-8")
    (tree / "alias").symlink_to(outside, target_is_directory=True)
    entry(declared)["roots"] = ["alias/value"]
    with pytest.raises(inputs.DeclaredInputError, match="outside_symlink"):
        key(tree, declared)


def test_dangling_inside_symlink_fails_closed(tree, declared):
    (tree / "src/link").symlink_to("missing")
    with pytest.raises(inputs.DeclaredInputError, match="missing_input"):
        key(tree, declared)


def test_symlink_cycle_fails_closed(tree, declared):
    (tree / "src/link").symlink_to("link")
    with pytest.raises(inputs.DeclaredInputError, match="symlink_cycle"):
        key(tree, declared)


def test_unreadable_file_fails_closed_even_for_privileged_caller(tree, declared):
    path = tree / "src/a.txt"
    path.chmod(0)
    try:
        with pytest.raises(inputs.DeclaredInputError, match="unreadable_file"):
            key(tree, declared)
    finally:
        path.chmod(0o644)


def test_file_open_error_fails_closed(tree, declared, monkeypatch):
    real_open = inputs.os.open

    def denied(path, *args, **kwargs):
        if Path(path) == tree / "src/a.txt":
            raise PermissionError("fixture denial")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(inputs.os, "open", denied)
    with pytest.raises(inputs.DeclaredInputError, match="unreadable_file"):
        key(tree, declared)


def test_directory_listing_error_fails_closed(tree, declared, monkeypatch):
    real_iterdir = Path.iterdir

    def denied(path):
        if path == tree / "src":
            raise PermissionError("fixture denial")
        return real_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", denied)
    with pytest.raises(inputs.DeclaredInputError, match="unreadable_directory"):
        key(tree, declared)


def test_verification_is_explicit_and_unverified_is_rejected(tree, declared):
    key(tree, declared)
    with pytest.raises(inputs.DeclaredInputError, match="unverified_suite"):
        key(tree, declared, require_verified=True)
    entry(declared)["status"] = "verified"
    assert key(tree, declared, require_verified=True) == key(tree, declared)


def test_unknown_suite_is_not_a_success(tree, declared):
    with pytest.raises(inputs.DeclaredInputError, match="unknown_suite"):
        key(tree, declared, suite_id="unknown")


def test_check_reads_is_complete_only_for_declared_paths(tree):
    result = inputs.check_reads([tree / "src"], [tree / "src/a.txt", tree / "src/b.txt"])
    assert result == {"status": "complete", "outside": []}
    outside = tree / "outside.txt"
    result = inputs.check_reads([tree / "src"], [tree / "src/a.txt", outside])
    assert result == {"status": "incomplete", "outside": [str(outside)]}


def test_check_reads_rejects_sibling_prefix_and_file_children(tree):
    sibling = tree / "src-sibling"
    sibling.mkdir()
    assert inputs.check_reads([tree / "src"], [sibling])["status"] == "incomplete"
    assert inputs.check_reads([tree / "src/a.txt"], [tree / "src/b.txt"])["status"] == "incomplete"


def test_check_reads_rejects_traversal_symlink_escape_and_missing_read(tree):
    link = tree / "src/link"
    link.symlink_to("../outside.txt")
    for observed in [link, tree / "src/../outside.txt", tree / "src/missing"]:
        result = inputs.check_reads([tree / "src"], [observed])
        assert result == {"status": "incomplete", "outside": [str(observed)]}


def test_check_reads_allows_links_within_the_declared_union(tree):
    (tree / "src/link").symlink_to("../outside.txt")
    result = inputs.check_reads([tree / "src", tree / "outside.txt"], [tree / "src/link"])
    assert result == {"status": "complete", "outside": []}


def test_check_reads_rejects_outside_intermediate_symlink_hop(tree):
    (tree / "outside-link").symlink_to("src/a.txt")
    link = tree / "src/link"
    link.symlink_to("../outside-link")
    assert inputs.check_reads([tree / "src"], [link]) == {
        "status": "incomplete", "outside": [str(link)],
    }


def test_check_reads_missing_root_or_no_roots_is_incomplete(tree):
    assert inputs.check_reads([tree / "missing"], [])["status"] == "incomplete"
    assert inputs.check_reads([], [])["status"] == "incomplete"
    assert inputs.check_reads([tree / "src"], [])["status"] == "complete"


def test_seed_manifest_has_only_the_two_unverified_suites():
    path = Path(inputs.__file__).with_name("declared_inputs.json")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert inputs.validate_declared(manifest) is manifest
    assert manifest["schema"] == "leaf.ci.declared-inputs.v1"
    assert set(manifest["suites"]) == {"web-studio-panels-proof", "web-version-restore-proof"}
    for row in manifest["suites"].values():
        assert set(row) == {"roots", "status", "evidence", "notes"}
        assert row["status"] == "unverified"
        assert row["notes"]
        for reference in row["evidence"].split("; "):
            filename, line = reference.rsplit(":", 1)
            source = Path(inputs.__file__).resolve().parents[2] / filename
            assert 0 < int(line) <= len(source.read_text(encoding="utf-8").splitlines())


@pytest.mark.parametrize("change", ["schema", "roots", "status", "evidence"])
def test_invalid_manifest_fails_closed(declared, change):
    if change == "schema":
        declared["schema"] = "other-schema"
    else:
        entry(declared)[change] = None
    with pytest.raises(inputs.DeclaredInputError, match="invalid_declaration"):
        inputs.validate_declared(declared)


def cli_args(tree, declared, tmp_path):
    manifest = tmp_path / "declared.json"
    manifest.write_text(json.dumps(declared), encoding="utf-8")
    return [sys.executable, "-I", "-B", str(Path(inputs.__file__).resolve()), "key",
            "--repo", str(tree), "--declared", str(manifest), "--suite", "sample",
            "--recipe", "recipe-sha256", "--toolchain", "fixture-toolchain",
            "--image-digest", "sha256:image", "--runner-digest", "runner-sha256"]


def test_isolated_cli_prints_only_suite_key_and_roots(tree, declared, tmp_path):
    result = subprocess.run(cli_args(tree, declared, tmp_path), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "suite": "sample", "key": key(tree, declared, toolchain="fixture-toolchain"), "roots": ["src"],
    }


@pytest.mark.parametrize("failure", ["unverified", "missing", "outside"])
def test_cli_failure_is_nonzero_without_success_payload(tree, declared, tmp_path, failure):
    if failure == "missing":
        entry(declared)["roots"] = ["missing"]
    elif failure == "outside":
        (tree / "src/link").symlink_to("../outside.txt")
    args = cli_args(tree, declared, tmp_path)
    if failure == "unverified":
        args.append("--require-verified")
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "error" in json.loads(result.stderr)
