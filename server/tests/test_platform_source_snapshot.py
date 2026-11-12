"""Real local Git proof for the offline platform-authoring source snapshot."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import pytest

import platform_source_snapshot as snapshot


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-c", "core.hooksPath=", "-C", str(root), *args],
                            check=True, capture_output=True, text=True, timeout=30)
    return result.stdout.strip()


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    root = tmp_path_factory.mktemp("source")
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Snapshot test")
    git(root, "config", "user.email", "snapshot@example.invalid")
    (root / "README.md").write_text("old source\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-m", "base")
    base = git(root, "rev-parse", "HEAD")
    (root / "README.md").write_text("deployed source\n", encoding="utf-8")
    git(root, "commit", "-am", "deployed")
    sha = git(root, "rev-parse", "HEAD")
    tree = git(root, "rev-parse", "HEAD^{tree}")
    git(root, "checkout", "-b", "private", base)
    (root / "private.txt").write_text("retained tenant history\n", encoding="utf-8")
    git(root, "add", "private.txt")
    git(root, "commit", "-m", "private")
    private = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "main")
    return {"root": root, "base": base, "sha": sha, "tree": tree, "private": private}


@pytest.fixture()
def material(tmp_path, source):
    output = tmp_path / "snapshot"
    manifest = snapshot.create_snapshot(source["root"], output, source["sha"], source["tree"])
    target = tmp_path / "target.git"
    git(tmp_path, "init", "--bare", str(target))
    git(target, "fetch", str(source["root"]),
        "refs/heads/main:refs/heads/main", "refs/heads/private:refs/heads/tenant/private")
    git(target, "update-ref", "refs/heads/main", source["base"])
    git(target, "symbolic-ref", "HEAD", "refs/heads/main")
    git(target, "config", "remote.unchanged.url", "https://example.invalid/untouched")
    return {"directory": output, "manifest": manifest, "target": target}


def write_manifest(material, **changes):
    value = {**material["manifest"], **changes}
    (material["directory"] / "manifest.json").write_text(json.dumps(value), encoding="utf-8")


def refs(target):
    return git(target, "for-each-ref", "--format=%(refname) %(objectname)").splitlines()


def test_create_install_and_repeat_preserve_existing_repository(material, source):
    target = material["target"]
    before_refs = refs(target)
    before_config = (target / "config").read_bytes()
    before_head = (target / "HEAD").read_bytes()
    before_fetch = (target / "FETCH_HEAD").read_bytes()
    ref = snapshot.install_snapshot(material["directory"], target, source["sha"])
    assert ref == "refs/leaf/deployed/" + source["sha"]
    assert git(target, "show", ref + ":README.md") == "deployed source"
    assert git(target, "rev-parse", ref + "^") == source["base"]
    assert snapshot.install_snapshot(material["directory"], target, source["sha"]) == ref
    assert refs(target) == sorted(before_refs + [ref + " " + source["sha"]])
    assert (target / "config").read_bytes() == before_config
    assert (target / "HEAD").read_bytes() == before_head
    assert (target / "FETCH_HEAD").read_bytes() == before_fetch
    assert git(target, "rev-parse", "main") == source["base"]
    assert git(target, "rev-parse", "refs/heads/tenant/private") == source["private"]
    assert material["manifest"] == {
        "schema": snapshot.SCHEMA, "source_sha": source["sha"],
        "source_tree": source["tree"],
        "bundle_sha256": hashlib.sha256((material["directory"] / "snapshot.bundle").read_bytes()).hexdigest(),
    }


def test_create_refuses_wrong_checkout_and_existing_output(tmp_path, source):
    output = tmp_path / "snapshot"
    with pytest.raises(snapshot.SnapshotError, match="^snapshot_checkout_mismatch$"):
        snapshot.create_snapshot(source["root"], output, source["base"], source["tree"])
    assert not output.exists()
    output.mkdir()
    (output / "keep").write_text("preserve", encoding="utf-8")
    with pytest.raises(snapshot.SnapshotError, match="^snapshot_output_exists_or_unavailable$"):
        snapshot.create_snapshot(source["root"], output, source["sha"], source["tree"])
    assert (output / "keep").read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize("value", ["", "a" * 39, "A" * 40, "../main", None, True])
def test_invalid_source_is_refused_before_output(tmp_path, source, value):
    output = tmp_path / "snapshot"
    with pytest.raises(snapshot.SnapshotError, match="^snapshot_source_invalid$"):
        snapshot.create_snapshot(source["root"], output, value, source["tree"])
    assert not output.exists()


@pytest.mark.parametrize("change,code", [
    ({"bundle_sha256": "0" * 64}, "snapshot_bundle_hash_mismatch"),
    ({"source_tree": "0" * 40}, "snapshot_tree_mismatch"),
    ({"source_sha": "0" * 40}, "snapshot_source_mismatch"),
    ({"unexpected": True}, "snapshot_manifest_invalid"),
])
def test_invalid_manifest_cannot_create_a_ref(material, source, change, code):
    before = refs(material["target"])
    write_manifest(material, **change)
    with pytest.raises(snapshot.SnapshotError, match="^" + code + "$"):
        snapshot.install_snapshot(material["directory"], material["target"], source["sha"])
    assert refs(material["target"]) == before


def test_wrong_advertised_head_is_refused(material, source):
    bundle = material["directory"] / "snapshot.bundle"
    bundle.unlink()
    git(source["root"], "bundle", "create", str(bundle), "refs/heads/main")
    write_manifest(material, bundle_sha256=hashlib.sha256(bundle.read_bytes()).hexdigest())
    before = refs(material["target"])
    with pytest.raises(snapshot.SnapshotError, match="^snapshot_bundle_heads_mismatch$"):
        snapshot.install_snapshot(material["directory"], material["target"], source["sha"])
    assert refs(material["target"]) == before


def test_prerequisite_bundle_refused_even_when_target_has_parent(material, source):
    bundle = material["directory"] / "snapshot.bundle"
    bundle.unlink()
    git(source["root"], "bundle", "create", str(bundle), "HEAD", "^" + source["base"])
    write_manifest(material, bundle_sha256=hashlib.sha256(bundle.read_bytes()).hexdigest())
    before = refs(material["target"])
    assert git(material["target"], "cat-file", "-t", source["base"]) == "commit"
    with pytest.raises(snapshot.SnapshotError, match="^snapshot_bundle_incomplete$"):
        snapshot.install_snapshot(material["directory"], material["target"], source["sha"])
    assert refs(material["target"]) == before


def test_conflicting_versioned_ref_is_never_moved(material, source):
    ref = "refs/leaf/deployed/" + source["sha"]
    git(material["target"], "update-ref", ref, source["private"])
    before = refs(material["target"])
    with pytest.raises(snapshot.SnapshotError, match="^snapshot_ref_conflict$"):
        snapshot.install_snapshot(material["directory"], material["target"], source["sha"])
    assert refs(material["target"]) == before


def test_existing_ref_still_requires_valid_bundle(material, source):
    snapshot.install_snapshot(material["directory"], material["target"], source["sha"])
    bundle = material["directory"] / "snapshot.bundle"
    bundle.write_bytes(bundle.read_bytes() + b"changed")
    with pytest.raises(snapshot.SnapshotError, match="^snapshot_bundle_hash_mismatch$"):
        snapshot.install_snapshot(material["directory"], material["target"], source["sha"])


def test_symbolic_versioned_ref_is_refused_without_moving_main(material, source):
    ref = "refs/leaf/deployed/" + source["sha"]
    git(material["target"], "symbolic-ref", ref, "refs/heads/main")
    before = refs(material["target"])
    with pytest.raises(snapshot.SnapshotError, match="^snapshot_ref_conflict$"):
        snapshot.install_snapshot(material["directory"], material["target"], source["sha"])
    assert refs(material["target"]) == before
    assert git(material["target"], "symbolic-ref", ref) == "refs/heads/main"


def test_missing_target_is_not_created(material, source, tmp_path):
    target = tmp_path / "missing.git"
    with pytest.raises(snapshot.SnapshotError, match="^snapshot_target_unavailable$"):
        snapshot.install_snapshot(material["directory"], target, source["sha"])
    assert not target.exists()


def test_git_failure_contains_no_stderr_or_command(monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["private argv"], stderr=b"private stderr")
    monkeypatch.setattr(snapshot.subprocess, "run", fail)
    with pytest.raises(snapshot.SnapshotError) as caught:
        snapshot.create_snapshot(tmp_path, tmp_path / "out", "a" * 40, "b" * 40)
    assert str(caught.value) == "snapshot_git_failed"
