"""Recipe unit tests. No Docker builds or provider operations occur here."""
from pathlib import Path
from types import SimpleNamespace
import json
import hashlib

import pytest

from ci.native_release_producer import image_build_command, SERVICES, FRESHNESS, TRIXIE
from ci import native_release_producer as producer


def native_identity(project):
    prefix = "arn:aws:codebuild:us-east-1:807034087062:"
    return {"project_arn": prefix + "project/" + project,
            "build_arn": prefix + "build/" + project + ":12345678-1234-1234-1234-123456789abc",
            "build_number": 7}


def assembly_inputs(tmp_path):
    archive = tmp_path / "web.zip"
    archive.write_bytes(b"unit fixture, not a real web ZIP")
    return dict(
        source="a" * 40, tree="b" * 40, producer=native_identity("release"),
        images={service: {"repository": f"leaf-platform-{service}",
                          "image_digest": "sha256:" + "c" * 64,
                          "source_revision": "a" * 40, "native_build_number": 7}
                for service in SERVICES},
        web={"path": str(archive), "artifact_sha256": "d" * 64,
             "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
             "image_digest": "sha256:" + "c" * 64, "source_revision": "a" * 40},
        solver={"revision": "e" * 40, "source_sha256": "f" * 64},
        gate={"producer": native_identity("gate"), "source_revision": "a" * 40,
              "source_tree": "b" * 40, "proof_sha256": "1" * 64,
              "archive": {"bucket": "unit-gates", "key": "gate.zip",
                          "version_id": "immutable-unit-version", "sha256": "2" * 64}},
    )


def test_native_manifest_binds_all_artifacts_without_github_ids(tmp_path):
    inputs = assembly_inputs(tmp_path)
    manifest = producer.assemble_release(**inputs)
    assert manifest["schema"] == "leaf.native-release.v1"
    assert set(manifest["services"]) == set(SERVICES)
    assert manifest["web"]["member"] == "web-dist.zip"
    assert "path" not in manifest["web"]
    assert "build_run_id" not in json.dumps(manifest)
    inputs["gate"]["archive"]["key"] = "changed"
    assert manifest["gate"]["archive"]["key"] == "gate.zip"


@pytest.mark.parametrize("mutation,match", [
    (lambda x: x["images"].pop("broker"), "five"),
    (lambda x: x["images"]["app"].update(source_revision="f" * 40), "source"),
    (lambda x: x["images"]["app"].update(native_build_number=8), "producer"),
    (lambda x: x["gate"].update(producer=native_identity("release")), "separate"),
    (lambda x: x["gate"].update(source_tree="f" * 40), "tree"),
    (lambda x: x["gate"]["archive"].update(version_id="null"), "immutable"),
    (lambda x: x["producer"].update(build_arn=native_identity("other")["build_arn"]), "belong"),
    (lambda x: x["web"].update(image_digest="sha256:" + "f" * 64), "image"),
    (lambda x: Path(x["web"]["path"]).write_bytes(b"changed"), "bytes changed"),
])
def test_native_manifest_rejects_mixed_release(tmp_path, mutation, match):
    inputs = assembly_inputs(tmp_path)
    mutation(inputs)
    with pytest.raises(ValueError, match=match):
        producer.assemble_release(**inputs)


def test_stage_release_writes_only_exact_archive_members(tmp_path):
    inputs = assembly_inputs(tmp_path)
    output = tmp_path / "managed-artifacts"
    members = producer.stage_release(output, **inputs)
    assert set(members) == {"web-dist.zip", "staging-supply-set.json"}
    assert {p.name for p in output.iterdir()} == set(members)
    for name, receipt in members.items():
        data = (output / name).read_bytes()
        assert receipt == {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    assert json.loads((output / "staging-supply-set.json").read_bytes())["provider"] == "aws.codebuild"
    with pytest.raises(FileExistsError):
        producer.stage_release(output, **inputs)


def recipe(service, **kwargs):
    freshness = {name: "b" * 64 for name in FRESHNESS.get(service, TRIXIE)}
    return image_build_command(service, "a" * 40, 7, freshness, Path("image.json"), **kwargs)


@pytest.mark.parametrize("service", SERVICES)
def test_preserves_image_recipe_and_uses_native_tag(service):
    command = recipe(service, **({"solver_revision": "c" * 40} if service == "canonical-worker" else {}))
    assert command[:4] == ["docker", "buildx", "build", "--pull"]
    assert f"deploy/Dockerfile.{service}" in command
    assert "linux/amd64" in command
    assert command[-1] == "."
    assert f":native-7-{'a' * 40}" in command[-2]
    assert command.count("--build-context") == (7 if service == "canonical-worker" else 6)
    if service == "app":
        assert "--push" not in command
        assert "type=image,push=true,compression=zstd,force-compression=true,oci-mediatypes=true" in command
    else:
        assert "--push" in command


def test_solver_is_required_not_invented():
    with pytest.raises(ValueError, match="solver"):
        recipe("canonical-worker")


def test_missing_freshness_cannot_silently_build():
    with pytest.raises(ValueError, match="freshness"):
        image_build_command("app", "a" * 40, 7, {}, Path("image.json"))


def test_invalid_source_cannot_enter_shell_or_tag():
    with pytest.raises(ValueError, match="source"):
        image_build_command("app", "main;echo unsafe", 7, {}, Path("image.json"))


def test_bool_is_not_a_native_build_number():
    with pytest.raises(ValueError, match="number"):
        image_build_command("app", "a" * 40, True, {}, Path("image.json"))


def fake_gate(monkeypatch, *, fail_shard=None, emit=True):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if "--emit-proof" in command and emit:
            Path(command[-1]).write_text("unit-only fake proof", encoding="utf-8")
        index = command[command.index("--shard-index") + 1] if "--shard-index" in command else None
        return SimpleNamespace(stdout="a" * 40, returncode=1 if index == fail_shard else 0)

    monkeypatch.setattr(producer.subprocess, "run", run)
    return calls


def test_native_gate_calls_all_shards_then_canonical_verifiers(tmp_path, monkeypatch):
    calls = fake_gate(monkeypatch)
    result = producer.run_gate(tmp_path, tmp_path / "results", env={"PATH": "test"})
    shards = [command for command, _ in calls if "--shard-index" in command]
    assert [command[command.index("--shard-index") + 1] for command in shards] == list(map(str, range(8)))
    assert all("--only" not in command for command in shards)
    assert "--emit-proof" in calls[-2][0]
    assert calls[-1][0][-2:] == ["--expect-tree", "a" * 40]
    assert all(kwargs["env"] == {"PATH": "test"} for _, kwargs in calls)
    assert all(0 < kwargs["timeout"] <= 2700 for _, kwargs in calls)
    assert result.name == "gate-proof.json"


def test_native_gate_cannot_use_missing_proof(tmp_path, monkeypatch):
    fake_gate(monkeypatch, emit=False)
    with pytest.raises(ValueError, match="did not emit"):
        producer.run_gate(tmp_path, tmp_path / "results", env={})


def test_native_gate_preserves_failed_shard_despite_fan_in_exit_zero(tmp_path, monkeypatch):
    calls = fake_gate(monkeypatch, fail_shard="3")
    with pytest.raises(ValueError, match="shards failed"):
        producer.run_gate(tmp_path, tmp_path / "results", env={})
    assert len([command for command, _ in calls if "--shard-index" in command]) == 8


def test_native_gate_refuses_reused_result_directory(tmp_path):
    with pytest.raises(FileExistsError):
        producer.run_gate(tmp_path, tmp_path, env={})


def test_web_package_reads_exact_image_without_starting_it(tmp_path, monkeypatch):
    calls = []
    container = "d" * 64

    def run(command, **kwargs):
        calls.append(command)
        if command[1] == "create":
            return SimpleNamespace(stdout=container)
        if command[1] == "cp":
            (Path(command[-1]) / "health.json").write_text(
                json.dumps({"ok": True, "source_sha": "a" * 40}), encoding="utf-8")
        if "pack-web-dist" in command:
            return SimpleNamespace(stdout=json.dumps({"artifact_sha256": "b" * 64, "archive_sha256": "c" * 64}))
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(producer.subprocess, "run", run)
    result = producer.package_web_image(tmp_path, "sha256:" + "e" * 64, "a" * 40, tmp_path / "web")
    assert calls[0][-1].endswith("@sha256:" + "e" * 64)
    assert not any("start" in command or "run" in command for command in calls)
    assert ["docker", "rm", container] in calls
    assert result["archive_sha256"] == "c" * 64


def test_web_package_cleans_container_after_copy_failure(tmp_path, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1] == "cp":
            raise RuntimeError("copy failed")
        return SimpleNamespace(stdout="d" * 64)

    monkeypatch.setattr(producer.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="copy failed"):
        producer.package_web_image(tmp_path, "sha256:" + "e" * 64, "a" * 40, tmp_path / "web")
    assert calls[-1] == ["docker", "rm", "d" * 64]
