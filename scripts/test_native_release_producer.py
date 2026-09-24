import copy
"""Recipe unit tests. No Docker builds or provider operations occur here."""
from pathlib import Path
from types import SimpleNamespace
import json
import hashlib
import threading
import os
import subprocess

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
    assert sorted(command[command.index("--shard-index") + 1] for command in shards) == list(map(str, range(8)))
    assert all("--only" not in command for command in shards)
    assert "--emit-proof" in calls[-2][0]
    assert calls[-1][0][-2:] == ["--expect-tree", "a" * 40]
    assert all(kwargs["env"] == ({"PATH": "test", "LEAF_NATIVE_GATE_WORKER":
        str(int(command[command.index("--shard-index") + 1]) % 4)}
        if "--shard-index" in command else {"PATH": "test"}) for command, kwargs in calls)
    assert all(0 < kwargs["timeout"] <= 2700 for _, kwargs in calls)
    assert result.name == "gate-proof.json"


def test_native_gate_cannot_use_missing_proof(tmp_path, monkeypatch):
    fake_gate(monkeypatch, emit=False)
    with pytest.raises(ValueError, match="did not emit"):
        producer.run_gate(tmp_path, tmp_path / "results", env={})


def test_gate_restores_canonical_import_path_without_mutating_admission_env(tmp_path, monkeypatch):
    calls = fake_gate(monkeypatch)
    env = {"PYTHONSAFEPATH": "1", "PATH": "test"}
    producer.run_gate(tmp_path, tmp_path / "results", env=env)
    assert env["PYTHONSAFEPATH"] == "1"
    assert all(kwargs["env"] == ({"PATH": "test", "LEAF_NATIVE_GATE_WORKER":
        str(int(command[command.index("--shard-index") + 1]) % 4)}
        if "--shard-index" in command else {"PATH": "test"}) for command, kwargs in calls)


def test_failed_gate_prints_bounded_suite_diagnostic(tmp_path, monkeypatch, capsys):
    fake_gate(monkeypatch, fail_shard="3")
    original = producer.subprocess.run

    def run(command, **kwargs):
        if "--shard-index" in command and command[command.index("--shard-index") + 1] == "3":
            report = Path(command[command.index("--result-json") + 1])
            report.write_text(json.dumps({"results": [{"id": "broken", "status": "FAIL"}]}))
            logs = Path(command[command.index("--log-dir") + 1])
            logs.mkdir()
            (logs / "broken.log").write_text("x" * 10000 + "ModuleNotFoundError")
        return original(command, **kwargs)

    monkeypatch.setattr(producer.subprocess, "run", run)
    with pytest.raises(ValueError, match="shards failed"):
        producer.run_gate(tmp_path, tmp_path / "results", env={})
    output = capsys.readouterr().out
    assert "ModuleNotFoundError" in output
    assert len(output) < 8300


def test_native_gate_preserves_failed_shard_despite_fan_in_exit_zero(tmp_path, monkeypatch):
    calls = fake_gate(monkeypatch, fail_shard="3")
    with pytest.raises(ValueError, match="shards failed"):
        producer.run_gate(tmp_path, tmp_path / "results", env={})
    assert len([command for command, _ in calls if "--shard-index" in command]) == 8


def test_native_gate_refuses_reused_result_directory(tmp_path):
    with pytest.raises(FileExistsError):
        producer.run_gate(tmp_path, tmp_path, env={})


@pytest.mark.parametrize("worker", [None, "0", "1", "3", "7", "8", "-1", "1;echo x", ""])
def test_browser_config_uses_private_worker_port(tmp_path, worker):
    # Evaluate the real config. Only Playwright's identity wrapper is stubbed;
    # no browser, development server or dependency install runs in this test.
    package = tmp_path / "node_modules/@playwright/test"
    package.mkdir(parents=True)
    (package / "package.json").write_text('{"type":"module","exports":"./index.mjs"}')
    (package / "index.mjs").write_text('export const defineConfig = value => value;')
    config = tmp_path / "playwright.config.mjs"
    config.write_bytes((Path(__file__).resolve().parents[1] / "web/playwright.config.mjs").read_bytes())
    env = dict(os.environ)
    env.pop("LEAF_NATIVE_GATE_WORKER", None)
    if worker is not None:
        env["LEAF_NATIVE_GATE_WORKER"] = worker
    result = subprocess.run(["node", "--input-type=module", "-e",
        "import c from './playwright.config.mjs'; console.log(JSON.stringify(c));"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)
    if worker not in (None, "0", "1", "3", "7"):
        assert result.returncode != 0
        assert "Invalid native gate worker" in result.stderr
        return
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    port = 5185 + 100 * int(worker or "0")
    assert value["use"]["baseURL"] == f"http://127.0.0.1:{port}"
    assert value["webServer"]["url"] == f"http://127.0.0.1:{port}/app"
    assert value["webServer"]["command"].endswith(f"--port {port} --strictPort")
    assert value["webServer"]["reuseExistingServer"] is False


@pytest.mark.parametrize("count", [True, False, 0, 3, 5, 16, 4.0, "4", None])
def test_native_gate_rejects_invalid_worker_count(tmp_path, count):
    with pytest.raises(ValueError, match="worker count"):
        producer.run_gate(tmp_path, tmp_path / "results", env={}, worker_count=count)
    assert not (tmp_path / "results").exists()


@pytest.mark.parametrize("count", [1, 2, 8])
def test_native_gate_supports_bounded_worker_counts(tmp_path, monkeypatch, count):
    calls = fake_gate(monkeypatch)
    producer.run_gate(tmp_path, tmp_path / "results", env={}, worker_count=count)
    shards = [(int(cmd[cmd.index("--shard-index") + 1]), kw["cwd"])
              for cmd, kw in calls if "--shard-index" in cmd]
    assert sorted(shard for shard, _ in shards) == list(range(8))
    assert len({cwd for _, cwd in shards}) == count
    for worker in range(count):
        assigned = [(shard, cwd) for shard, cwd in shards if shard % count == worker]
        assert [shard for shard, _ in assigned] == list(range(worker, 8, count))
        assert len({cwd for _, cwd in assigned}) == 1


def test_native_gate_overlaps_four_isolated_serial_workers(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    root.mkdir()
    results = root / "results"
    barrier = threading.Barrier(4, timeout=5)
    lock = threading.Lock()
    active = set()
    peak = 0
    completed = []
    seen = {}
    copies = []
    executor_sizes = []
    executor = producer.ThreadPoolExecutor

    def pool(*, max_workers):
        executor_sizes.append(max_workers)
        return executor(max_workers=max_workers)

    def run(command, **kwargs):
        nonlocal peak
        cwd = kwargs["cwd"]
        assert kwargs["timeout"] > 0
        if command[0] == "cp":
            assert command[:3] == ["cp", "-a", str(root)]
            destination = Path(command[-1])
            assert destination.name == root.name
            assert not destination.is_relative_to(root)
            assert not destination.is_relative_to(results)
            copies.append(destination)
        if "--shard-index" in command:
            shard = int(command[command.index("--shard-index") + 1])
            assert len(copies) == 3
            assert kwargs["env"]["LEAF_NATIVE_GATE_WORKER"] == str(shard % 4)
            assert Path(command[command.index("--result-json") + 1]).parent == results
            with lock:
                assert cwd not in active
                active.add(cwd)
                peak = max(peak, len(active))
                seen.setdefault(cwd, []).append(shard)
            barrier.wait()
            with lock:
                active.remove(cwd)
                completed.append(shard)
        if "--emit-proof" in command:
            assert sorted(completed) == list(range(8))
            assert not active
            assert cwd == root
            Path(command[-1]).write_text("unit proof")
        if "--verify-gate-proof" in command:
            assert sorted(completed) == list(range(8))
            assert cwd == root
        return SimpleNamespace(stdout="a" * 40, returncode=0)

    monkeypatch.setattr(producer, "ThreadPoolExecutor", pool)
    monkeypatch.setattr(producer.subprocess, "run", run)
    producer.run_gate(root, results, env={})
    assert executor_sizes == [4]
    assert peak == 4
    assert len(seen) == 4
    assert {cwd.name for cwd in seen} == {root.name}
    assert len({cwd.parent for cwd in seen}) == 4
    assert seen[root] == [0, 4]
    assert sorted(seen.values()) == [[0, 4], [1, 5], [2, 6], [3, 7]]
    assert all(not path.parent.parent.exists() for path in copies)


def test_native_gate_joins_workers_and_cleans_scratch_on_exception(tmp_path, monkeypatch):
    barrier = threading.Barrier(4, timeout=5)
    completed = []
    copies = []

    def run(command, **kwargs):
        if command[0] == "cp":
            copies.append(Path(command[-1]))
        if "--shard-index" in command:
            shard = int(command[command.index("--shard-index") + 1])
            if shard < 4:
                barrier.wait()
            if shard == 0:
                raise RuntimeError("shard exploded")
            completed.append(shard)
        assert "--emit-proof" not in command
        return SimpleNamespace(stdout="a" * 40, returncode=0)

    monkeypatch.setattr(producer.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="shard exploded"):
        producer.run_gate(tmp_path, tmp_path / "results", env={})
    assert sorted(completed) == list(range(1, 8))
    assert all(not path.parent.parent.exists() for path in copies)


@pytest.mark.parametrize("worker_count", [1, 2])
def test_native_gate_copy_and_shards_share_deadline(tmp_path, monkeypatch, worker_count):
    now = [100.0]
    calls = []
    copies = []

    def run(command, **kwargs):
        assert kwargs["timeout"] == 5 - len(calls)
        calls.append(command)
        now[0] += 4 if command[0] == "cp" else 1
        if command[0] == "cp":
            copies.append(Path(command[-1]))
        return SimpleNamespace(stdout="a" * 40, returncode=0)

    monkeypatch.setattr(producer.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(producer.subprocess, "run", run)
    with pytest.raises(TimeoutError, match="total runtime"):
        producer.run_gate(tmp_path, tmp_path / "results", env={},
                          timeout_seconds=5, worker_count=worker_count)
    if worker_count == 2:
        assert len(calls) == 2
        assert calls[1][0] == "cp"
    else:
        assert len(calls) == 5
        assert all("--shard-index" in command for command in calls[1:])
    assert all(not path.parent.parent.exists() for path in copies)


def test_native_gate_cleans_scratch_after_copy_failure(tmp_path, monkeypatch):
    copies = []

    def run(command, **kwargs):
        if command[0] == "cp":
            copies.append(Path(command[-1]))
            raise RuntimeError("copy failed")
        assert "--shard-index" not in command
        return SimpleNamespace(stdout="a" * 40, returncode=0)

    monkeypatch.setattr(producer.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="copy failed"):
        producer.run_gate(tmp_path, tmp_path / "results", env={})
    assert copies
    assert all(not path.parent.parent.exists() for path in copies)


@pytest.mark.parametrize("inside_results", [False, True])
def test_native_gate_rejects_scratch_inside_source_before_copy(tmp_path, monkeypatch, inside_results):
    original = producer.tempfile.TemporaryDirectory
    allocated = []
    root = tmp_path / "checkout"
    root.mkdir()
    results = tmp_path / "results"

    def temporary(**kwargs):
        scratch = original(dir=results if inside_results else root, **kwargs)
        allocated.append(Path(scratch.name))
        return scratch

    calls = fake_gate(monkeypatch)
    monkeypatch.setattr(producer.tempfile, "TemporaryDirectory", temporary)
    with pytest.raises(ValueError, match="outside source and results"):
        producer.run_gate(root, results, env={})
    assert len(calls) == 1
    assert all(not path.exists() for path in allocated)


def test_native_gate_prints_failures_in_shard_order(tmp_path, monkeypatch, capsys):
    original_calls = fake_gate(monkeypatch)
    original = producer.subprocess.run

    def run(command, **kwargs):
        result = original(command, **kwargs)
        if "--shard-index" in command:
            shard = int(command[command.index("--shard-index") + 1])
            if shard in (1, 6):
                report = Path(command[command.index("--result-json") + 1])
                report.write_text(json.dumps({"results": [{"id": f"broken{shard}", "status": "FAIL"}]}))
                logs = Path(command[command.index("--log-dir") + 1])
                logs.mkdir()
                (logs / f"broken{shard}.log").write_bytes(b"x" * 10000)
                result.returncode = 1
        return result

    monkeypatch.setattr(producer.subprocess, "run", run)
    with pytest.raises(ValueError, match=r"shards failed: \[1, 6\]"):
        producer.run_gate(tmp_path, tmp_path / "results", env={})
    output = capsys.readouterr().out
    assert output.index("broken1.log") < output.index("broken6.log")
    assert len(output) < 2 * 8300
    assert "--emit-proof" in original_calls[-1][0]


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


def test_runtime_identity_rejects_gate_on_publisher_role():
    identity = native_identity("leaf-studio-native-gate")
    record = dict(arn=identity["build_arn"], buildNumber=7, projectName="leaf-studio-native-gate",
                  resolvedSourceVersion="a" * 40, buildStatus="IN_PROGRESS",
                  serviceRole="arn:aws:iam::807034087062:role/leaf-studio-native-release-role")
    client = SimpleNamespace(batch_get_builds=lambda **kwargs: {"builds": [record]})
    with pytest.raises(ValueError, match="identity"):
        producer.runtime_identity("gate", "a" * 40,
                                  {"CODEBUILD_BUILD_ARN": identity["build_arn"], "CODEBUILD_BUILD_NUMBER": "7"}, client)


def test_external_solver_context_preserves_exact_revision():
    command = producer.image_build_command(
        "canonical-worker", "a" * 40, 7, {key: "b" * 64 for key in TRIXIE}, Path("metadata.json"),
        solver_revision="c" * 40, solver_root=Path("/secondary/solver"),
    )
    assert "autofill_solver=" + str(Path("/secondary/solver")) in command


@pytest.mark.parametrize("gate_succeeds", [True, False])
def test_composed_release_verifies_gate_before_all_five_builds(tmp_path, monkeypatch, gate_succeeds):
    inputs = assembly_inputs(tmp_path)
    gate_identity = native_identity("leaf-studio-native-gate")
    gate_request = dict(gate_identity, source_revision="a" * 40,
                        service_role="arn:aws:iam::807034087062:role/leaf-studio-native-gate-role",
                        repository_url="https://github.com/LEAF-Solar-Design/leaf-web-demo.git",
                        buildspec=".codebuild/release.yml", bucket="unit-gates", key="gate.zip",
                        version_id="unit-version", sha256="f" * 64)
    request = {"source_revision": "a" * 40, "source_tree": "b" * 40,
               "gate": gate_request, "contract_revision": "d" * 40}
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy/autofill-solver-sources.json").write_text(json.dumps({"e" * 40: "f" * 64}))
    work = tmp_path / "scratch"
    work.mkdir()
    events = []
    def gate_read(*args, **kwargs):
        events.append("gate-provider")
        if not gate_succeeds:
            raise ValueError("gate failed")
        return {}, b"provider archive unit fixture"
    contract = SimpleNamespace(NativeRelease=SimpleNamespace, read_native_release=gate_read,
                               _members=lambda *args, **kwargs: {"gate-proof.json": b"canonical proof unit fixture"})
    monkeypatch.setattr(producer, "admit_checkout", lambda *args: events.append("admit"))
    monkeypatch.setattr(producer, "runtime_identity", lambda *args: native_identity("leaf-studio-native-release"))
    monkeypatch.setattr(producer, "load_evidence_contract", lambda *args: contract)
    monkeypatch.setattr(producer.tempfile, "mkdtemp", lambda **kwargs: str(work))
    monkeypatch.setattr(producer.subprocess, "run", lambda *args, **kwargs: events.append("canonical-proof"))
    monkeypatch.setattr(producer, "_git", lambda root, *args: "e" * 40 if args[0] == "rev-parse" else "")
    monkeypatch.setattr(producer, "resolve_freshness", lambda *args: {service: {} for service in SERVICES})
    def build(root, service, source, number, freshness, metadata, **kwargs):
        events.append(service)
        if service == "canonical-worker":
            assert kwargs["solver_revision"] == "e" * 40
            assert kwargs["solver_root"] == Path("/unit/solver")
        return inputs["images"][service]
    monkeypatch.setattr(producer, "build_image", build)
    monkeypatch.setattr(producer, "package_web_image", lambda *args: inputs["web"])
    env = {"CODEBUILD_SRC_DIR_provider_contract": "/unit/contract", "CODEBUILD_SRC_DIR_autofill_solver": "/unit/solver"}
    output = tmp_path / "artifacts"
    if not gate_succeeds:
        with pytest.raises(ValueError, match="gate failed"):
            producer.produce_release(tmp_path, output, request, env, object(), object())
        assert events == ["admit", "gate-provider"]
        assert not output.exists()
    else:
        result = producer.produce_release(tmp_path, output, request, env, object(), object())
        assert events[:3] == ["admit", "gate-provider", "canonical-proof"]
        assert sorted(events[3:]) == sorted(SERVICES)
        assert set(result) == {"staging-supply-set.json", "web-dist.zip"}
        manifest = json.loads((output / "staging-supply-set.json").read_bytes())
        assert manifest["gate"]["producer"] == gate_identity
        assert manifest["producer"]["project_arn"].endswith("/leaf-studio-native-release")


def full_release_case(tmp_path, monkeypatch, build):
    inputs = assembly_inputs(tmp_path)
    gate_request = dict(native_identity("leaf-studio-native-gate"), source_revision="a" * 40,
                        service_role="arn:aws:iam::807034087062:role/leaf-studio-native-gate-role",
                        repository_url="https://github.com/LEAF-Solar-Design/leaf-web-demo.git",
                        buildspec=".codebuild/release.yml", bucket="unit-gates", key="gate.zip",
                        version_id="unit-version", sha256="f" * 64)
    request = {"source_revision": "a" * 40, "source_tree": "b" * 40,
               "gate": gate_request, "contract_revision": "d" * 40}
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy/autofill-solver-sources.json").write_text(json.dumps({"e" * 40: "f" * 64}))
    work = tmp_path / "scratch"
    work.mkdir()
    contract = SimpleNamespace(NativeRelease=SimpleNamespace,
                               read_native_release=lambda *a, **k: ({}, b"provider archive unit fixture"),
                               _members=lambda *a, **k: {"gate-proof.json": b"canonical proof unit fixture"})
    monkeypatch.setattr(producer, "admit_checkout", lambda *a: None)
    monkeypatch.setattr(producer, "runtime_identity", lambda *a: native_identity("leaf-studio-native-release"))
    monkeypatch.setattr(producer, "load_evidence_contract", lambda *a: contract)
    monkeypatch.setattr(producer.tempfile, "mkdtemp", lambda **k: str(work))
    monkeypatch.setattr(producer.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(producer, "_git", lambda root, *args: "e" * 40 if args[0] == "rev-parse" else "")
    monkeypatch.setattr(producer, "resolve_freshness", lambda *a: {service: {} for service in SERVICES})
    monkeypatch.setattr(producer, "build_image", lambda *a, **k: build(inputs, *a, **k))
    monkeypatch.setattr(producer, "package_web_image", lambda *a: inputs["web"])
    env = {"CODEBUILD_SRC_DIR_provider_contract": "/unit/contract", "CODEBUILD_SRC_DIR_autofill_solver": "/unit/solver"}
    return request, env, work


def test_release_raises_first_failure_in_services_order_after_all_builds(tmp_path, monkeypatch, capsys):
    attempted = []
    lock = threading.Lock()

    def build(inputs, root, service, source, number, freshness, metadata, **kwargs):
        with lock:
            attempted.append(service)
        assert kwargs["log_path"] == metadata.with_suffix(".log")
        if service == "web":
            raise RuntimeError("web build failed")
        if service == "broker":
            raise RuntimeError("broker build failed")
        if service == "canonical-worker":
            kwargs["log_path"].write_text("worker buildx output")
        return inputs["images"][service]

    request, env, _ = full_release_case(tmp_path, monkeypatch, build)
    output = tmp_path / "artifacts"
    with pytest.raises(RuntimeError, match="broker build failed"):
        producer.produce_release(tmp_path, output, request, env, object(), object())
    assert sorted(attempted) == sorted(SERVICES)
    assert not output.exists()
    printed = capsys.readouterr().out
    headers = [printed.index(f"==== image build log: {service} ====") for service in SERVICES]
    assert headers == sorted(headers)
    assert "worker buildx output" in printed
    assert printed.count("(no log)") == len(SERVICES) - 1


def test_release_builds_all_images_concurrently(tmp_path, monkeypatch):
    barrier = threading.Barrier(len(SERVICES), timeout=5)

    def build(inputs, root, service, source, number, freshness, metadata, **kwargs):
        barrier.wait()
        return inputs["images"][service]

    request, env, _ = full_release_case(tmp_path, monkeypatch, build)
    output = tmp_path / "artifacts"
    producer.produce_release(tmp_path, output, request, env, object(), object())
    manifest = json.loads((output / "staging-supply-set.json").read_bytes())
    assert set(manifest["services"]) == set(SERVICES)


def test_build_logs_print_only_the_bounded_tail(tmp_path, capsys):
    (tmp_path / "app.log").write_bytes(b"HEAD" + b"x" * producer.BUILD_LOG_TAIL_BYTES + b"TAIL")
    producer._print_build_logs(tmp_path, ["app", "web"])
    printed = capsys.readouterr().out
    assert "HEAD" not in printed and "TAIL" in printed
    assert printed.index("==== image build log: app ====") < printed.index("==== image build log: web ====")
    assert printed.rstrip().endswith("(no log)")


def fake_buildx(monkeypatch, tmp_path):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[:2] == ["git", "rev-parse"]:
            return SimpleNamespace(stdout="a" * 40 + "\n")
        (tmp_path / "image.json").write_text(json.dumps({"containerimage.digest": "sha256:" + "c" * 64}))
        if "stdout" in kwargs:
            kwargs["stdout"].write(b"buildx output")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(producer.subprocess, "run", run)
    return calls


def test_build_image_writes_buildx_output_to_exclusive_log(tmp_path, monkeypatch):
    calls = fake_buildx(monkeypatch, tmp_path)
    log = tmp_path / "app.log"
    freshness = {name: "b" * 64 for name in TRIXIE}
    result = producer.build_image(tmp_path, "app", "a" * 40, 7, freshness, tmp_path / "image.json", log_path=log)
    command, kwargs = calls[-1]
    assert command[:3] == ["docker", "buildx", "build"]
    assert os.fspath(kwargs["stdout"].name) == str(log)
    assert kwargs["stderr"] == subprocess.STDOUT
    assert kwargs["timeout"] == 45 * 60 and kwargs["check"] is True
    assert log.read_bytes() == b"buildx output"
    assert result["image_digest"] == "sha256:" + "c" * 64
    (tmp_path / "image.json").unlink()
    with pytest.raises(FileExistsError):
        producer.build_image(tmp_path, "app", "a" * 40, 7, freshness, tmp_path / "image.json", log_path=log)


def test_build_image_without_log_inherits_console(tmp_path, monkeypatch):
    calls = fake_buildx(monkeypatch, tmp_path)
    freshness = {name: "b" * 64 for name in TRIXIE}
    producer.build_image(tmp_path, "app", "a" * 40, 7, freshness, tmp_path / "image.json")
    command, kwargs = calls[-1]
    assert command[:3] == ["docker", "buildx", "build"]
    assert "stdout" not in kwargs and "stderr" not in kwargs


@pytest.mark.parametrize("total,expected", [
    (1, (1, 1)), (2, (2, 1)), (3, (2, 1)), (4, (4, 1)), (5, (4, 1)), (8, (8, 1)),
    (12, (8, 1)), (16, (8, 2)), (200, (8, 16)),
])
def test_gate_parallelism_splits_host_budget(total, expected):
    assert producer.gate_parallelism(total) == expected


@pytest.mark.parametrize("total", [0, -1, True, 4.0, "4", None])
def test_gate_parallelism_rejects_invalid_budget(total):
    with pytest.raises(ValueError, match="job budget"):
        producer.gate_parallelism(total)


def test_native_gate_passes_jobs_to_every_shard(tmp_path, monkeypatch):
    calls = fake_gate(monkeypatch)
    producer.run_gate(tmp_path, tmp_path / "results", env={}, jobs_per_shard=2)
    shards = [command for command, _ in calls if "--shard-index" in command]
    assert len(shards) == 8
    assert all(command[-2:] == ["--jobs", "2"] and command.count("--jobs") == 1 for command in shards)
    assert all("--jobs" not in command for command, _ in calls if "--shard-index" not in command)


def test_native_gate_single_job_shard_command_is_unchanged(tmp_path, monkeypatch):
    calls = fake_gate(monkeypatch)
    producer.run_gate(tmp_path, tmp_path / "results", env={}, jobs_per_shard=1)
    shards = [command for command, _ in calls if "--shard-index" in command]
    assert len(shards) == 8
    assert all("--jobs" not in command and command[-2] == "--log-dir" for command in shards)


@pytest.mark.parametrize("jobs", [0, 17, True, 2.0, "2", None])
def test_native_gate_rejects_invalid_jobs_per_shard(tmp_path, jobs):
    with pytest.raises(ValueError, match="jobs per shard"):
        producer.run_gate(tmp_path, tmp_path / "results", env={}, jobs_per_shard=jobs)
    assert not (tmp_path / "results").exists()


def test_host_gate_jobs_falls_back_to_four_without_runner(tmp_path, capsys):
    assert producer.host_gate_jobs(tmp_path) == 4
    assert "native gate: auto sizing unavailable (FileNotFoundError), using 4" in capsys.readouterr().out
    assert "leaf_run_all_gates_auto" not in producer.sys.modules


def test_host_gate_jobs_uses_runner_auto_sizing(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/run-all-gates.py").write_text(
        "def _effective_cpus():\n    return 36.0\n"
        "def _effective_mem_bytes():\n    return 72 * 2 ** 30\n"
        "def resolve_auto_jobs(cpus, mem):\n    return 16 if (cpus, mem) == (36.0, 72 * 2 ** 30) else 0\n")
    assert producer.host_gate_jobs(tmp_path) == 16
    assert producer.gate_parallelism(producer.host_gate_jobs(tmp_path)) == (8, 2)


def test_host_gate_jobs_rejects_invalid_runner_answer(tmp_path, capsys):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/run-all-gates.py").write_text(
        "def _effective_cpus():\n    return 8.0\n"
        "def _effective_mem_bytes():\n    return None\n"
        "def resolve_auto_jobs(cpus, mem):\n    return 0\n")
    assert producer.host_gate_jobs(tmp_path) == 4
    assert "(ValueError), using 4" in capsys.readouterr().out


@pytest.mark.parametrize("extra", [{"selection": "app", "predecessor": {}}, {"selection": "harness"},
    {"predecessor": {}}, {"selection": "harness", "predecessor": {}, "command": "echo invalid"}])
def test_selective_request_is_closed_before_any_work(tmp_path, extra):
    request = dict(source_revision="a" * 40, source_tree="b" * 40, gate={}, contract_revision="c" * 40, **extra)
    with pytest.raises(ValueError, match="fields"):
        producer.produce_release(tmp_path, tmp_path / "output", request, {}, None, None)


def selective_case(tmp_path, monkeypatch, *, predecessor_ok=True, supports=True, selection="harness"):
    inputs = assembly_inputs(tmp_path)
    previous = producer.assemble_release(**inputs)
    original = copy.deepcopy(previous)
    pins = {"release": {"unit": "independent release pins"}, "gate": {"unit": "independent gate pins"}, "source_tree": "b" * 40}
    identity = native_identity("leaf-studio-native-release")
    identity["build_number"] = 8
    gate_identity = native_identity("leaf-studio-native-gate")
    gate = dict(gate_identity, source_revision="f" * 40,
        service_role="arn:aws:iam::807034087062:role/leaf-studio-native-gate-role",
        repository_url="https://github.com/LEAF-Solar-Design/leaf-web-demo.git", buildspec=".codebuild/release.yml",
        bucket="leaf-studio-release-artifacts-807034087062-us-east-1", key="gate/new.zip",
        version_id="immutable-version", sha256="e" * 64)
    request = dict(source_revision="f" * 40, source_tree="1" * 40, gate=gate,
                   contract_revision="c" * 40, selection=selection, predecessor=pins)
    events = []
    def prior(actual, cb, s3):
        events.append("predecessor")
        assert actual == pins and cb == "cb" and s3 == "s3"
        if not predecessor_ok:
            raise ValueError("predecessor refused")
        return previous, {}, Path(inputs["web"]["path"]).read_bytes()
    contract = SimpleNamespace(NativeRelease=SimpleNamespace,
        HARNESS_SUPPLY_SCHEMA="leaf.native-release.harness.v1" if supports else None,
        APP_HARNESS_SUPPLY_SCHEMA="leaf.native-release.app-harness.v1" if supports else None,
        read_native_predecessor=prior, fixed_native_lane=lambda *a: events.append("fixed-gate"),
        read_native_release=lambda *a, **k: ({}, b"gate"),
        _members=lambda *a, **k: {"gate-proof.json": b"proof"})
    monkeypatch.setattr(producer, "admit_checkout", lambda *a: events.append("checkout"))
    monkeypatch.setattr(producer, "runtime_identity", lambda *a: identity)
    monkeypatch.setattr(producer, "load_evidence_contract", lambda *a: contract)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(producer.tempfile, "mkdtemp", lambda **k: str(work))
    monkeypatch.setattr(producer.subprocess, "run", lambda *a, **k: events.append("canonical-gate"))
    def freshness(root, *, harness_only=False, app_harness=False):
        assert harness_only == (selection == "harness")
        assert app_harness == (selection == "app-harness")
        events.append("freshness")
        return {"app": {}, "harness": {}}
    monkeypatch.setattr(producer, "resolve_freshness", freshness)
    def build(root, service, source, number, freshness, metadata, **kwargs):
        events.append(service)
        assert service in (("harness",) if selection == "harness" else ("app", "harness"))
        assert set(kwargs) == {"log_path"}
        return dict(repository=f"leaf-platform-{service}", image_digest="sha256:" + "0" * 64,
                    source_revision=source, native_build_number=number)
    monkeypatch.setattr(producer, "build_image", build)
    monkeypatch.setattr(producer, "package_web_image", lambda *a: pytest.fail("web must not rebuild"))
    return inputs, previous, original, request, events


def test_harness_build_preserves_complete_verified_members_and_web(tmp_path, monkeypatch):
    inputs, previous, original, request, events = selective_case(tmp_path, monkeypatch)
    output = tmp_path / "output"
    producer.produce_release(tmp_path, output, request, {"CODEBUILD_SRC_DIR_provider_contract": "contract"}, "cb", "s3")
    manifest = json.loads((output / "staging-supply-set.json").read_bytes())
    assert events == ["checkout", "canonical-gate", "fixed-gate", "predecessor", "freshness", "harness"]
    assert previous == original
    assert manifest["schema"] == "leaf.native-release.harness.v1"
    assert manifest["predecessor"] == request["predecessor"]
    assert manifest["services"]["harness"]["source_revision"] == request["source_revision"]
    assert manifest["services"]["harness"]["native_build_number"] == 8
    for name in SERVICES:
        if name != "harness":
            assert manifest["services"][name] == previous["services"][name]
    assert manifest["web"] == previous["web"] and manifest["solver"] == previous["solver"]
    assert (output / "web-dist.zip").read_bytes() == Path(inputs["web"]["path"]).read_bytes()
    assert set(p.name for p in output.iterdir()) == {"web-dist.zip", "staging-supply-set.json"}


@pytest.mark.parametrize("supports", [False, True])
def test_harness_rejects_old_contract_or_failed_predecessor_before_build(tmp_path, monkeypatch, supports):
    _, _, _, request, events = selective_case(tmp_path, monkeypatch, supports=supports, predecessor_ok=False)
    with pytest.raises(ValueError, match="predecessor|contract"):
        producer.produce_release(tmp_path, tmp_path / "output", request,
                                 {"CODEBUILD_SRC_DIR_provider_contract": "contract"}, "cb", "s3")
    assert "freshness" not in events and "harness" not in events
    assert not (tmp_path / "output").exists()


def test_harness_freshness_does_not_resolve_other_image_channels(tmp_path, monkeypatch):
    import io
    urls = []
    def read(url, timeout):
        urls.append(url)
        return io.BytesIO(b"signed channel fixture")
    monkeypatch.setattr(producer.urllib.request, "urlopen", read)
    monkeypatch.setattr(producer.subprocess, "run", lambda *a, **k: pytest.fail("no nginx Docker run"))
    result = producer.resolve_freshness(tmp_path, harness_only=True)
    assert set(result) == {"harness"}
    assert set(result["harness"]) == set(FRESHNESS["harness"])
    assert len(urls) == 2 and all("bookworm" in url for url in urls)


@pytest.mark.parametrize("selection", ["harness", "app-harness"])
def test_selective_builds_only_changed_services_and_preserves_origins(tmp_path, monkeypatch, selection):
    inputs, previous, original, request, events = selective_case(tmp_path, monkeypatch, selection=selection)
    output = tmp_path / "selective-output"
    producer.produce_release(tmp_path, output, request, {"CODEBUILD_SRC_DIR_provider_contract": "contract"}, "cb", "s3")
    manifest = json.loads((output / "staging-supply-set.json").read_bytes())
    selected = ["harness"] if selection == "harness" else ["app", "harness"]
    assert events[:5] == ["checkout", "canonical-gate", "fixed-gate", "predecessor", "freshness"]
    assert sorted(events[5:]) == sorted(selected)
    assert manifest["schema"] == f"leaf.native-release.{selection}.v1"
    assert manifest["selection"] == selection and manifest["source_tree"] == request["source_tree"]
    assert manifest["predecessor"] == request["predecessor"]
    for name in SERVICES:
        if name in selected:
            assert manifest["services"][name]["source_revision"] == request["source_revision"]
            assert manifest["services"][name]["native_build_number"] == 8
        else:
            assert manifest["services"][name] == original["services"][name]
    assert previous == original
    assert manifest["solver"] == previous["solver"] and manifest["web"] == previous["web"]
    assert (output / "web-dist.zip").read_bytes() == Path(inputs["web"]["path"]).read_bytes()


@pytest.mark.parametrize("supports", [True, False])
def test_app_harness_refuses_unverified_predecessor_before_build(tmp_path, monkeypatch, supports):
    _, _, _, request, events = selective_case(tmp_path, monkeypatch,
        selection="app-harness", supports=supports, predecessor_ok=False)
    with pytest.raises(ValueError, match="predecessor|contract"):
        producer.produce_release(tmp_path, tmp_path / "output", request,
            {"CODEBUILD_SRC_DIR_provider_contract": "contract"}, "cb", "s3")
    assert not any(service in events for service in SERVICES)


def test_app_harness_freshness_uses_only_existing_debian_channels(tmp_path, monkeypatch):
    import io
    urls = []
    def read(url, **kwargs):
        urls.append(url); return io.BytesIO(b"signed-channel-fixture")
    monkeypatch.setattr(producer.urllib.request, "urlopen", read)
    monkeypatch.setattr(producer.subprocess, "run", lambda *a, **k: pytest.fail("no Docker allowed"))
    result = producer.resolve_freshness(tmp_path, app_harness=True)
    assert set(result) == {"app", "harness"}
    assert set(result["app"]) == set(TRIXIE)
    assert set(result["harness"]) == set(FRESHNESS["harness"])
    assert len(urls) == 4 and all("debian" in url for url in urls)


@pytest.mark.parametrize("fault", ["source", "tree", "retained", "web"])
def test_selective_composition_rejects_substitutions(tmp_path, fault):
    inputs = assembly_inputs(tmp_path)
    previous = producer.assemble_release(**inputs)
    images = {name: copy.deepcopy(inputs["images"][name]) for name in ("app", "harness")}
    web = Path(inputs["web"]["path"]).read_bytes()
    gate = copy.deepcopy(inputs["gate"])
    if fault == "source": images["app"]["source_revision"] = "f" * 40
    if fault == "tree": gate["source_tree"] = "f" * 40
    if fault == "retained": images["broker"] = inputs["images"]["broker"]
    if fault == "web": web += b"substituted"
    with pytest.raises(ValueError):
        producer.stage_selective_release(tmp_path / "output", selection="app-harness",
            source=inputs["source"], tree=inputs["tree"], identity=inputs["producer"],
            images=images, gate=gate, predecessor={}, previous=previous, web_bytes=web)
    assert not (tmp_path / "output").exists()


def retained_web_case(tmp_path, monkeypatch):
    inputs, previous, original, request, events = selective_case(tmp_path, monkeypatch, selection="app-harness")
    contract = producer.load_evidence_contract(None, None)
    pins = {"source": {"manifest": {"sha256": "a" * 64}}}
    request["retained_web"] = {"pins": pins, "build_id": "independent-web:build2",
        "archive": {"bucket": "release", "key": "web.zip", "version_id": "v2", "sha256": "b" * 64}}
    payload = b"independently authenticated newer web ZIP"
    image = dict(repository="leaf-platform-web", image_digest="sha256:" + "5" * 64,
                 source_revision="4" * 40, native_build_number=2)
    web = dict(path="web-dist.zip", artifact_sha256="6" * 64,
               archive_sha256=hashlib.sha256(payload).hexdigest(),
               image_digest=image["image_digest"], source_revision=image["source_revision"])
    manifest = dict(schema="leaf.native-release.web.v1", selection="web", pins=pins,
        producer={"build_id": "independent-web:build2", "build_number": 2},
        source_revision="4" * 40, source_tree="7" * 40, services={"web": image}, web=web)
    admitted = copy.deepcopy(request["retained_web"])
    def read(actual, cb, s3):
        events.append("retained-web")
        assert actual == admitted and cb == "cb" and s3 == "s3"
        return manifest, {"provider": "aws.codebuild"}, payload
    contract.read_web_supply = read
    return previous, original, request, events, contract, manifest, payload


def test_app_harness_retains_independent_web_without_relabel_or_rebuild(tmp_path, monkeypatch):
    previous, original, request, events, _, web, payload = retained_web_case(tmp_path, monkeypatch)
    output = tmp_path / "output"
    producer.produce_release(tmp_path, output, request, {"CODEBUILD_SRC_DIR_provider_contract": "contract"}, "cb", "s3")
    actual = json.loads((output / "staging-supply-set.json").read_bytes())
    assert events[:6] == ["checkout", "canonical-gate", "fixed-gate", "predecessor", "retained-web", "freshness"]
    assert sorted(events[6:]) == ["app", "harness"]
    assert actual["retained_web"] == request["retained_web"]
    assert actual["web"] == {"member": web["web"]["path"],
                             "artifact_sha256": web["web"]["artifact_sha256"],
                             "archive_sha256": web["web"]["archive_sha256"]}
    assert actual["services"]["web"] == web["services"]["web"]
    assert actual["services"]["web"]["source_revision"] != actual["source_revision"]
    assert (output / "web-dist.zip").read_bytes() == payload
    for name in ("broker", "canonical-worker"):
        assert actual["services"][name] == original["services"][name]
    assert actual["solver"] == original["solver"] and previous == original


@pytest.mark.parametrize("fault", ["missing-verifier", "provider-refusal"])
def test_retained_web_verifier_failure_precedes_build(tmp_path, monkeypatch, fault):
    _, _, request, events, contract, _, _ = retained_web_case(tmp_path, monkeypatch)
    if fault == "missing-verifier":
        del contract.read_web_supply
    else:
        def refuse(*args):
            raise ValueError("independent web verifier refused " + fault)
        contract.read_web_supply = refuse
    with pytest.raises(ValueError, match="verifier"):
        producer.produce_release(tmp_path, tmp_path / "output", request,
            {"CODEBUILD_SRC_DIR_provider_contract": "contract"}, "cb", "s3")
    assert "freshness" not in events and "app" not in events
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("selection", [None, "harness", "other"])
def test_retained_web_rejects_unsupported_selection(tmp_path, monkeypatch, selection):
    _, _, request, events, _, _, _ = retained_web_case(tmp_path, monkeypatch)
    if selection is None:
        del request["selection"]
        del request["predecessor"]
    else:
        request["selection"] = selection
    with pytest.raises(ValueError, match="fields"):
        producer.produce_release(tmp_path, tmp_path / "output", request, {}, "cb", "s3")
    assert events == []


@pytest.mark.parametrize("fault", ["pins", "build", "repository", "source", "image", "bytes"])
def test_retained_web_composition_refuses_substituted_admitted_output(tmp_path, monkeypatch, fault):
    _, _, request, _, _, manifest, _ = retained_web_case(tmp_path, monkeypatch)
    if fault == "pins": manifest["pins"] = {}
    if fault == "build": manifest["producer"]["build_id"] = "substitute"
    if fault == "repository": manifest["services"]["web"]["repository"] = "other"
    if fault == "source": manifest["web"]["source_revision"] = "9" * 40
    if fault == "image": manifest["web"]["image_digest"] = "sha256:" + "9" * 64
    if fault == "bytes": manifest["web"]["archive_sha256"] = "9" * 64
    with pytest.raises(ValueError, match="web"):
        producer.produce_release(tmp_path, tmp_path / "output", request,
            {"CODEBUILD_SRC_DIR_provider_contract": "contract"}, "cb", "s3")
    assert not (tmp_path / "output").exists()
