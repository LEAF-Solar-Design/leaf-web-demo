from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("prepare_glug_mushy_artifact.py")


def load_module():
    spec = importlib.util.spec_from_file_location("prepare_glug_mushy_artifact", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def adoption(module, root: Path):
    files = []
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_file():
            payload = path.read_bytes()
            files.append(module.ArtifactFile(
                path.relative_to(root).as_posix(),
                len(payload),
                hashlib.sha256(payload).hexdigest(),
            ))
            total += len(payload)
    return module.GlugAdoption(
        raw={},
        workspace_id="glug",
        repository_slug_env="GLUG_REPOSITORY_SLUG",
        ranglr_base_commit="2" * 40,
        mushy_source_commit="c" * 40,
        package_lock_sha256="d" * 64,
        artifact_component="mushy-author",
        artifact_entrypoint="src/index.js",
        artifact_files=tuple(files),
        artifact_byte_count=total,
        artifact_aggregate_sha256=module.artifact_manifest_digest(files),
        allowed_powers=frozenset(),
        denied_powers=frozenset(),
    )


def make_archive(tmp_path: Path, *, link: bool = False, extra_name: str | None = None):
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "src" / "index.js").write_text("export const pinned = true;\n", encoding="utf-8")
    (source / "build-identity.json").write_text("{}\n", encoding="utf-8")
    archive = tmp_path / "artifact.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(source / "src", arcname="./src")
        bundle.add(source / "build-identity.json", arcname="./build-identity.json")
        if link:
            member = tarfile.TarInfo("./escape")
            member.type = tarfile.SYMTYPE
            member.linkname = "../outside"
            bundle.addfile(member)
        if extra_name is not None:
            member = tarfile.TarInfo(extra_name)
            member.size = 1
            bundle.addfile(member, io.BytesIO(b"x"))
    return source, archive, hashlib.sha256(archive.read_bytes()).hexdigest()


def test_extracts_only_the_exact_manifest_tree(tmp_path):
    module = load_module()
    source, archive, digest = make_archive(tmp_path)
    target = tmp_path / "target"
    module.extract_verified_archive(archive, digest, target, adoption(module, source))
    assert (target / "src" / "index.js").read_bytes() == (source / "src" / "index.js").read_bytes()


def test_rejects_archive_digest_drift_before_extraction(tmp_path):
    module = load_module()
    source, archive, _digest = make_archive(tmp_path)
    with pytest.raises(module.ArtifactPreparationError, match="digest mismatch"):
        module.extract_verified_archive(archive, "0" * 64, tmp_path / "target", adoption(module, source))
    assert not (tmp_path / "target").exists()


def test_rejects_links_even_when_declared_files_match(tmp_path):
    module = load_module()
    source, archive, digest = make_archive(tmp_path, link=True)
    with pytest.raises(module.ArtifactPreparationError, match="unsupported"):
        module.extract_verified_archive(archive, digest, tmp_path / "target", adoption(module, source))


@pytest.mark.parametrize("name", ["../outside", "..\\outside", "C:/outside"])
def test_rejects_cross_platform_traversal_before_writing(tmp_path, name):
    module = load_module()
    source, archive, digest = make_archive(tmp_path, extra_name=name)
    with pytest.raises(module.ArtifactPreparationError, match="unsafe"):
        module.extract_verified_archive(archive, digest, tmp_path / "target", adoption(module, source))


def test_rejects_undeclared_file_before_extracting_its_bytes(tmp_path):
    module = load_module()
    source, archive, digest = make_archive(tmp_path, extra_name="extra.bin")
    target = tmp_path / "target"
    with pytest.raises(module.ArtifactPreparationError, match="undeclared"):
        module.extract_verified_archive(archive, digest, target, adoption(module, source))
    assert not (target / "extra.bin").exists()


def test_existing_volume_requires_exact_labels_and_is_read_only(monkeypatch, tmp_path):
    module = load_module()
    source, _archive, _digest = make_archive(tmp_path)
    contract = adoption(module, source)
    calls = []

    def runner(args, **_kwargs):
        calls.append(args)
        if args[1:3] == ["volume", "inspect"]:
            return subprocess.CompletedProcess(args, 0, json.dumps({
                "com.leaf.glug.mushy-source": contract.mushy_source_commit,
                "com.leaf.glug.mushy-artifact": contract.artifact_aggregate_sha256,
            }), "")
        if args[1] == "cp":
            destination = Path(args[-1])
            (destination / "src").mkdir(parents=True, exist_ok=True)
            for path in source.rglob("*"):
                if path.is_file():
                    target = destination / path.relative_to(source)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(path.read_bytes())
        return subprocess.CompletedProcess(args, 0, "", "")

    status = module.seed_docker_volume(
        source,
        volume="leaf-glug-mushy-artifact",
        seed_image="registry.example/seed@sha256:" + "a" * 64,
        adoption=contract,
        runner=runner,
    )
    assert status == "verified"
    assert not any(args[1:3] == ["volume", "create"] for args in calls)
    create = next(args for args in calls if args[1] == "create")
    assert "type=volume,source=leaf-glug-mushy-artifact,target=/artifact,readonly" in create


def test_new_volume_rolls_back_when_seed_fails(tmp_path):
    module = load_module()
    source, _archive, _digest = make_archive(tmp_path)
    contract = adoption(module, source)
    calls = []

    inspect_count = 0
    labels = {}

    def runner(args, **kwargs):
        nonlocal inspect_count
        calls.append(args)
        if args[1:3] == ["volume", "inspect"]:
            inspect_count += 1
            if inspect_count == 1:
                return subprocess.CompletedProcess(args, 1, "", "No such volume")
            return subprocess.CompletedProcess(args, 0, json.dumps(labels), "")
        if args[1:3] == ["volume", "create"]:
            labels.update({item.split("=", 1)[0]: item.split("=", 1)[1]
                           for item in args if item.startswith("com.leaf.glug.")})
            return subprocess.CompletedProcess(args, 0, "leaf-glug-mushy-artifact\n", "")
        if args[1:3] == ["start", "--attach"]:
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0, "", "")

    with pytest.raises(subprocess.CalledProcessError):
        module.seed_docker_volume(
            source,
            volume="leaf-glug-mushy-artifact",
            seed_image="registry.example/seed@sha256:" + "a" * 64,
            adoption=contract,
            runner=runner,
        )
    assert ["docker", "volume", "rm", "leaf-glug-mushy-artifact"] in calls
    seed_remove = next(index for index, args in enumerate(calls)
                       if args[:3] == ["docker", "rm", "--force"] and "seed" in args[-1])
    volume_remove = calls.index(["docker", "volume", "rm", "leaf-glug-mushy-artifact"])
    assert seed_remove < volume_remove


def test_reports_failed_new_volume_rollback(tmp_path):
    module = load_module()
    source, _archive, _digest = make_archive(tmp_path)
    contract = adoption(module, source)
    inspect_count = 0
    labels = {}

    def runner(args, **_kwargs):
        nonlocal inspect_count
        if args[1:3] == ["volume", "inspect"]:
            inspect_count += 1
            if inspect_count == 1:
                return subprocess.CompletedProcess(args, 1, "", "No such volume")
            return subprocess.CompletedProcess(args, 0, json.dumps(labels), "")
        if args[1:3] == ["volume", "create"]:
            labels.update({item.split("=", 1)[0]: item.split("=", 1)[1]
                           for item in args if item.startswith("com.leaf.glug.")})
        if args[1:3] == ["start", "--attach"]:
            raise subprocess.CalledProcessError(1, args)
        if args[1:3] == ["volume", "rm"]:
            return subprocess.CompletedProcess(args, 1, "", "volume is in use")
        return subprocess.CompletedProcess(args, 0, "", "")

    with pytest.raises(module.ArtifactPreparationError, match="new artifact volume"):
        module.seed_docker_volume(
            source,
            volume="leaf-glug-mushy-artifact",
            seed_image="registry.example/seed@sha256:" + "a" * 64,
            adoption=contract,
            runner=runner,
        )


def test_absent_to_create_race_never_seeds_or_removes_the_other_volume(tmp_path):
    module = load_module()
    source, _archive, _digest = make_archive(tmp_path)
    contract = adoption(module, source)
    calls = []
    inspect_count = 0

    def runner(args, **_kwargs):
        nonlocal inspect_count
        calls.append(args)
        if args[1:3] == ["volume", "inspect"]:
            inspect_count += 1
            if inspect_count == 1:
                return subprocess.CompletedProcess(args, 1, "", "No such volume")
            return subprocess.CompletedProcess(args, 0, json.dumps({
                "com.leaf.glug.mushy-source": contract.mushy_source_commit,
                "com.leaf.glug.mushy-artifact": contract.artifact_aggregate_sha256,
                "com.leaf.glug.mushy-preparer": "another-process",
            }), "")
        if args[1] == "cp":
            destination = Path(args[-1])
            for path in source.rglob("*"):
                if path.is_file():
                    target = destination / path.relative_to(source)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(path.read_bytes())
        return subprocess.CompletedProcess(args, 0, "", "")

    assert module.seed_docker_volume(
        source,
        volume="leaf-glug-mushy-artifact",
        seed_image="registry.example/seed@sha256:" + "a" * 64,
        adoption=contract,
        runner=runner,
    ) == "verified"
    assert not any(args[1:3] == ["start", "--attach"] for args in calls)
    assert ["docker", "volume", "rm", "leaf-glug-mushy-artifact"] not in calls


def test_rejects_mutable_seed_image_before_docker_mutation(tmp_path):
    module = load_module()
    source, _archive, _digest = make_archive(tmp_path)
    calls = []
    with pytest.raises(module.ArtifactPreparationError, match="exact sha256"):
        module.seed_docker_volume(
            source,
            volume="leaf-glug-mushy-artifact",
            seed_image="node:22-slim",
            adoption=adoption(module, source),
            runner=lambda args, **kwargs: calls.append(args),
        )
    assert calls == []
