"""Recipe unit tests. No Docker builds or provider operations occur here."""
from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from ci.native_release_producer import image_build_command, SERVICES, FRESHNESS, TRIXIE
from ci import native_release_producer as producer


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
