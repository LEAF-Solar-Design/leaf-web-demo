"""Records A/B; B uses only fake effects (no database, processes or listeners)."""
from dataclasses import replace
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
from types import MappingProxyType, SimpleNamespace

import pytest


def _load():
    path = Path(__file__).with_name("proof_project.py")
    spec = importlib.util.spec_from_file_location("_project_boundary_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


p = _load()
HASH = "a" * 64
OTHER_HASH = "b" * 64
ROOT = Path("C:/fake/run-test") if sys.platform == "win32" else Path("/fake/run-test")


def file(name, digest=HASH):
    return p.FileProof(name, digest, 8)


def fake_clock():
    clock = SimpleNamespace(now=0.0)
    clock.monotonic = lambda: clock.now
    clock.utc_now = lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    clock.wait = lambda seconds: setattr(clock, "now", clock.now + seconds)
    return clock


def fake_host():
    host = SimpleNamespace(events=[], collision=False)
    host.facts = p.HostFacts("local", "", False, False, True, True, False, (), (),
            True, True, (), (p.REPO / "vendor/engine",), tuple(map(file, p.REQUIREMENTS)),
            "3.fake", tuple((name, name) for name in p.TOOLS), {"PATH": "fake"},
            tuple(map(file, p.ENGINE_OUTPUTS)), tuple((name, "1.fake") for name in p.TOOLS))
    host.source = p.SourceProof("c" * 40, HASH, OTHER_HASH,
            (file("platform/migrations/0001_fake.sql"), file("vendor/engine/Cargo.toml"),
             file("vendor/engine/worker-browser.mjs"), *map(file, p.REQUIREMENTS), file("server/new.py")),
            ("server/new.py",), ("platform/migrations/0001_fake.sql",))
    def inspect(config):
        host.events.append("inspect")
        return host.facts

    def reserve(plan):
        host.events.append("reserve")
        if host.collision:
            raise FileExistsError("fake")
        return p.Reservation(plan.config.run_root, plan.config.artifact_root)

    def source_snapshot():
        host.events.append("source")
        return host.source

    def authority_inventory(run_id):
        pytest.fail("record A acquired authority")

    def scrub(reservation, *, keep_safe):
        pytest.fail("record A invoked record D")
    host.inspect, host.reserve, host.source_snapshot = inspect, reserve, source_snapshot
    host.authority_inventory, host.scrub = authority_inventory, scrub
    return host


def fake_scope():
    scope = SimpleNamespace(cleanups=[])
    scope.acquire = lambda name, cleanup: scope.cleanups.append((name, cleanup))
    return scope


def case():
    host, clock, scope = fake_host(), fake_clock(), fake_scope()
    launches, journals, reads = [], [], []
    descriptor = p.WorkerDescriptor("solar-settings", "3.fake", "sha256:" + OTHER_HASH,
        OTHER_HASH, ("canonical_worker.py", "solar_tools/solar_settings.json"))

    def forbidden(*args, **kwargs):
        pytest.fail("unowned later-record effect")

    def launch(spec, *, scope):
        launches.append(spec)
        child = SimpleNamespace(poll=lambda: 0, stop=lambda deadline: True)
        scope.acquire(p.ResourceKind.RUNTIME_PROCESS, lambda: child.stop(5))
        return child

    def reader(request):
        reads.append(request)
        return descriptor

    effects = p.Effects(forbidden, forbidden, forbidden, forbidden, launch, forbidden,
        forbidden, reader, clock, host, SimpleNamespace(
            ownership=lambda path, value: journals.append((path, value)),
            publish=forbidden, read=forbidden))
    return SimpleNamespace(config=p.Config("run-test", ROOT, ROOT / "artifacts", p.Ports(8101, 8102, 8103, 8104)),
        host=host, effects=effects, scope=scope, launches=launches, journals=journals,
        reads=reads, descriptor=descriptor)


def admitted(c):
    return p.preflight(c.config, effects=c.effects)


def refused(c, code=p.FailureCode.PREFLIGHT):
    with pytest.raises(p.ProofRefused) as error:
        admitted(c)
    assert error.value.failure == p.Failure(p.Stage.PREFLIGHT, code)
    assert c.launches == [] and c.journals == []
    assert "reserve" not in c.host.events


def profile(c):
    return {"LEAF_PROOF_PROFILE": "project", "LEAF_PROOF_PROJECT_RUN_ID": c.config.run_id,
            "LEAF_PROOF_PROJECT_KEY_SHA256": "fakekey", "LEAF_PROOF_PROJECT_JWKS_FILE": str(ROOT / "public-jwks.json")}


def environment(c, ambient=None):
    return p.project_environment(admitted(c), os_env=ambient or {}, dsn="fakedb",
                                 profile_env=profile(c), organization_id="fakeorg")


class TestProjectBoundary:
    def test_invalid_run_id(self):
        for run in ("bad", "../badxx", "-badxxxx", "UPPERBAD", "x" * 65, None):
            c = case()
            c.config = replace(c.config, run_id=run)
            refused(c)
            assert c.host.events == []
        assert admitted(case()).config.run_id == "run-test"
        with pytest.raises(TypeError):
            p.Failure("preflight", "PREFLIGHT")
        assert len(p.Stage) == 13
        assert {code.value for code in p.FailureCode} == {
            "PREFLIGHT", "ENVIRONMENT", "SOURCE", "RUNTIME", "BROWSER_UNAVAILABLE",
            "ALLOCATION", "TRANSPORT", "MIGRATION_ENTRY", "MIGRATION_CORPUS", "MIGRATION_TIMEOUT",
            "MIGRATION_EXIT", "MIGRATION_RESULT", "SCHEMA", "IDENTITY", "BINDING"}

    def test_deployed_posture(self):
        for facts in ({"posture": "production"}, {"legacy_posture": "staging"}, {"synthetic": True}):
            c = case()
            c.host.facts = replace(c.host.facts, **facts)
            refused(c, p.FailureCode.ENVIRONMENT)
        c = case()
        c.host.facts = replace(c.host.facts, posture="test")
        assert admitted(c).host.posture == "test"

    def test_existing_run_root(self, monkeypatch):
        c = case()
        c.host.facts = replace(c.host.facts, run_root_exists=True)
        refused(c)
        c = case()
        c.host.collision = True
        with pytest.raises(p.ProofRefused):
            admitted(c)
        assert c.host.events[-1] == "reserve" and not c.journals
        c = case()
        plan = admitted(c)
        assert plan.reservation.run_root == ROOT
        assert c.host.events == ["inspect", "source", "reserve"]
        assert c.journals[0][0] == ROOT / "ownership.json"
        # The portable production directory adapter must request exclusive
        # creation too. This fake filesystem already has the requested path.
        monkeypatch.setattr(p, "os", SimpleNamespace(name="posix"))
        def existing_directory(*, mode, exist_ok):
            assert mode == 0o700
            if not exist_ok:
                raise FileExistsError("fake")
        with pytest.raises(FileExistsError):
            p._private_directory(SimpleNamespace(mkdir=existing_directory))

    def test_reparse_escape(self):
        c = case()
        c.host.facts = replace(c.host.facts, paths_safe=False)
        refused(c)
        clean = SimpleNamespace(st_mode=p.stat.S_IFDIR, st_file_attributes=0)
        junction = SimpleNamespace(lstat=lambda: SimpleNamespace(
            st_mode=p.stat.S_IFDIR, st_file_attributes=0x400))
        path = SimpleNamespace(is_absolute=lambda: True, parts=("fake",),
            parents=(junction,), lstat=lambda: clean)
        assert not p._path_safe(path)
        path.parents = ()
        path.lstat = lambda: SimpleNamespace(st_mode=p.stat.S_IFLNK, st_file_attributes=0)
        assert not p._path_safe(path)

    def test_foreign_output_root(self):
        c = case()
        c.config = replace(c.config, artifact_root=ROOT.parent / "foreign")
        c.host.facts = replace(c.host.facts, output_owned=False)
        refused(c)

    def test_existing_receipt(self):
        c = case()
        c.host.facts = replace(c.host.facts, receipt_exists=True)
        refused(c)

    def test_invalid_port_set(self):
        for ports in (p.Ports(0, 2, 3, 4), p.Ports(1, 2, 3, 65536), p.Ports(1, 2, 3, 1),
                      p.Ports(True, 2, 3, 4), p.Ports("1", 2, 3, 4)):
            c = case()
            c.config = replace(c.config, ports=ports)
            refused(c)
            assert c.host.events == []

    def test_occupied_port(self):
        c = case()
        c.host.facts = replace(c.host.facts, occupied_ports=(8102,))
        refused(c)

    def test_missing_module(self):
        c = case()
        c.host.facts = replace(c.host.facts, missing_modules=("jwt",))
        refused(c)

    def test_missing_harness(self):
        c = case()
        c.host.facts = replace(c.host.facts, harness_present=False)
        refused(c)

    def test_missing_browser_dependency(self):
        c = case()
        c.host.facts = replace(c.host.facts, browser_dependency_present=False)
        refused(c, p.FailureCode.BROWSER_UNAVAILABLE)
        effects = p.production_effects()
        with pytest.raises(p.ProofRefused) as error:
            effects.browser_runner(None, scope=c.scope)
        assert error.value.failure == p.Failure(p.Stage.BROWSER, p.FailureCode.BROWSER_UNAVAILABLE)
        with pytest.raises(RuntimeError, match="record C"):
            effects.readiness_probe(None)

    def test_unapproved_dotenv(self):
        c = case()
        c.host.facts = replace(c.host.facts, unapproved_dotenv=(".env.local",))
        refused(c, p.FailureCode.ENVIRONMENT)

    def test_ambient_environment(self, monkeypatch):
        c = case()
        ambient = {"PATH": "fakepath", "SystemRoot": "fakewin",
                   "AWS_SECRET_ACCESS_KEY": "fakeaws", "HTTP_PROXY": "badproxy",
                   "PYTHONPATH": "badpath", "NODE_OPTIONS": "badnode", "PGHOST": "badhost",
                   "pgservice": "badsvc", "PgOptions": "badopt", "LEAF_TOKEN": "token"}
        before = dict(ambient)
        issuer = profile(c)
        issuer_before = dict(issuer)
        monkeypatch.setenv("LEAF_TOKEN", "ambient")
        env = p.project_environment(admitted(c), os_env=MappingProxyType(ambient), dsn="fakedb",
                                    profile_env=MappingProxyType(issuer), organization_id="fakeorg")
        assert ambient == before and issuer == issuer_before
        assert env is not ambient and isinstance(env, dict)
        assert env["PATH"] == "fakepath" and env["SYSTEMROOT"] == "fakewin"
        assert not set(ambient.values()).intersection(env.values()) - {"fakepath", "fakewin", "0"}
        assert not {"PGHOST", "pgservice", "PgOptions", "PYTHONPATH", "LEAF_TOKEN", "HTTP_PROXY",
                    "AWS_SECRET_ACCESS_KEY", "NODE_OPTIONS"}.intersection(env)
        assert "fakedb" not in repr(env)
        spec = p.ProcessSpec("runtime-build", ("fake",), ROOT, {"TOKEN": "secret"}, 1)
        assert "secret" not in repr(spec) and "TOKEN" not in repr(spec)

    def test_explicit_project_flags(self):
        c = case()
        env = environment(c)
        expected = {"LEAF_RUNTIME_ENV": "local", "LEAF_ENV": "local", "LEAF_AUTH_LIVE": "1",
                    "LEAF_SOLAR_PROJECT_RUN_ENABLED": "1", "LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED": "1",
                    "VITE_CAD_EDIT": "1", "VITE_SOLAR_FLOW_RAIL": "1", "VITE_SOLAR_SETTINGS_FORM": "1",
                    "VITE_TENANT_ID": "fakeorg", "LEAF_AUTHORED_EXECUTION": "0",
                    "LEAF_HARNESS_AUTHORING_MODE": "disabled", "LEAF_AGENT_MOCK": "1",
                    "LEAF_GUEST_DXF_EXTRACT": "local", "LEAF_CANONICAL_WORKER_TOOL": "solar-settings",
                    "LEAF_EXPECTED_WORKER_REVISION": "sha256:" + OTHER_HASH,
                    "LEAF_CANONICAL_WORKER_ID": "project:run-test", "LEAF_AGENT_STORE": "postgres",
                    "LEAF_HARNESS_SESSION_STORE": "postgres", "LEAF_TENANT_CLAIM_NS": "https://leafdesign.ai/"}
        assert {key: env.get(key) for key in expected} == expected
        assert p.TIER == "hosted_pro" and p.PROFILE == "project"
        assert env["LEAF_E2E_BASE_URL"] == env["LEAF_CORS_ORIGINS"] == "http://127.0.0.1:8101"
        assert env["VITE_API_BASE"] == env["LEAF_E2E_API_BASE"] == "http://127.0.0.1:8102"
        assert Path(env["LEAF_STORE_DIR"]).is_relative_to(ROOT)
        assert env["LEAF_SOURCE_SHA"] == "c" * 40
        assert all(env[key] == value for key, value in profile(c).items())
        bad = dict(profile(c), LEAF_AUTH_LIVE="0")
        with pytest.raises(p.ProofRefused):
            p.project_environment(admitted(case()), os_env={}, dsn="fakedb", profile_env=bad, organization_id="fakeorg")

    def test_single_database_authority(self):
        c = case()
        env = environment(c)
        assert env["DATABASE_URL"] == env["LEAF_HARNESS_DATABASE_URL"] == "fakedb"
        with pytest.raises(p.ProofRefused) as error:
            p.project_environment(admitted(case()), os_env={}, dsn="", profile_env=profile(c), organization_id="fakeorg")
        assert error.value.failure.fixed_code == p.FailureCode.ENVIRONMENT

    def test_descriptor_authority(self, monkeypatch):
        c = case()
        plan = admitted(c)
        assert plan.descriptor is c.descriptor
        assert c.reads == [p.CanonicalRequest("descriptor", "solar-settings")]
        assert plan.required_test_ids == ("project_browser_create_import", "project_browser_bearer_required",
                                          "project_settings_worker_readback")
        # Exercise the production reader with fake modules, never import server code here.
        loaded, descriptor_calls = [], []
        def descriptor(tool, *, server_dir):
            descriptor_calls.append((tool, server_dir))
            return {"tool_name": tool, "runtime": "3.fake", "source_revision": "sha256:" + HASH, "source_sha256": HASH}
        def loader(name, path, **kwargs):
            loaded.append(path)
            return SimpleNamespace(graph_worker_descriptor=descriptor, GRAPH_WORKER_MANIFEST=("canonical_worker.py",))
        monkeypatch.setattr(p, "_load_file", loader)
        before = list(sys.path)
        actual = p.production_effects().canonical_reader(p.CanonicalRequest())
        assert descriptor_calls == [("solar-settings", p.REPO / "server")]
        assert actual.source_sha256 == HASH and sys.path == before
        assert loaded[-1] == p.REPO / "server/canonical_worker.py"
        assert actual.source_manifest == ("canonical_worker.py", "solar_tools/solar_settings.json")

    def test_dirty_source_binding(self, monkeypatch):
        c = case()
        plan = admitted(c)
        assert plan.source is c.host.source
        assert plan.source.dirty_tree_sha256 == HASH
        assert plan.source.tested_source_manifest_sha256 == OTHER_HASH
        assert "server/new.py" in {f.path for f in plan.source.files}
        c = case()
        c.host.source = replace(c.host.source, files=c.host.source.files[:-1])
        refused(c, p.FailureCode.SOURCE)
        c = case()
        c.host.source = replace(c.host.source, files=c.host.source.files[1:])
        refused(c, p.FailureCode.SOURCE)
        # Exercise the production snapshot producer using only fake Git and
        # fake file bytes, including an untracked file whose contents change.
        def git(*args):
            if args[0] == "rev-parse":
                return b"c" * 40
            if args[0] == "ls-tree" or args == ("ls-files", "--cached"):
                return b"platform/migrations/0001_fake.sql\n"
            if args[:2] == ("ls-files", "--others"):
                return b"server/new.py\n"
            assert args == ("diff", "--binary", "HEAD")
            return b""
        content = {"server/new.py": HASH}
        monkeypatch.setattr(p, "_git", git)
        monkeypatch.setattr(p, "_file_proof", lambda path, name: file(name, content.get(name, HASH)))
        first = p.production_effects().host.source_snapshot()
        assert first.declared_untracked == ("server/new.py",)
        assert "server/new.py" in {f.path for f in first.files}
        content["server/new.py"] = OTHER_HASH
        second = p.production_effects().host.source_snapshot()
        assert first.dirty_tree_sha256 != second.dirty_tree_sha256
        assert first.tested_source_manifest_sha256 != second.tested_source_manifest_sha256

    def test_ambiguous_vendor_shape(self):
        for candidates in ((), (p.REPO / "vendor/engine", p.REPO / "vendor/other")):
            c = case()
            c.host.facts = replace(c.host.facts, vendor_candidates=candidates)
            with pytest.raises(p.ProofRefused) as error:
                p.prepare_runtime(admitted(c), effects=c.effects, scope=c.scope)
            assert error.value.failure == p.Failure(p.Stage.RUNTIME, p.FailureCode.RUNTIME)
            assert c.launches == []

    def test_missing_engine_output(self):
        c = case()
        plan = admitted(c)
        c.host.facts = replace(c.host.facts, engine_outputs=(file("engine.js"),))
        with pytest.raises(p.ProofRefused) as error:
            p.prepare_runtime(plan, effects=c.effects, scope=c.scope)
        assert error.value.failure == p.Failure(p.Stage.RUNTIME, p.FailureCode.RUNTIME)
        assert len(c.launches) == 5
        c = case()
        c.effects = replace(c.effects, process_launcher=lambda spec, *, scope: SimpleNamespace(poll=lambda: 1))
        with pytest.raises(p.ProofRefused):
            p.prepare_runtime(admitted(c), effects=c.effects, scope=c.scope)

    def test_build_provenance(self):
        c = case()
        plan = admitted(c)
        result = p.prepare_runtime(plan, effects=c.effects, scope=c.scope)
        assert result.requirements == tuple(map(file, ("server/requirements.txt", "platform/requirements.txt", "server/requirements-auth.txt")))
        assert result.python == "3.fake" and dict(result.toolchain)["wasm-pack"] == "1.fake"
        assert result.vendor_source == (file("vendor/engine/Cargo.toml"), file("vendor/engine/worker-browser.mjs"))
        assert result.engine_outputs == (file("engine.js"), file("engine_bg.wasm"))
        build = c.launches[-1]
        assert build.argv == ("wasm-pack", "build", "--release", "--target", "web", ".", "--out-dir", str(ROOT / "cad-engine"), "--out-name", "engine")
        assert build.environment["CARGO_TARGET_DIR"] == str(ROOT / "cargo-target")
        assert build.cwd == ROOT / "vendor-source"
        assert result.engine_root == ROOT / "cad-engine" and result.cargo_target_root == ROOT / "cargo-target"
        assert build.output_path is None and build.deadline > 0 and len(c.scope.cleanups) == 5
        for corruption in ({"engine_outputs": (file("engine.js", ""), file("engine_bg.wasm"))},
                           {"requirements": ()}, {"toolchain": ()}):
            c = case()
            plan = admitted(c)
            c.host.facts = replace(c.host.facts, **corruption)
            with pytest.raises(p.ProofRefused):
                p.prepare_runtime(plan, effects=c.effects, scope=c.scope)
        c = case()
        plan = admitted(c)
        c.host.source = replace(c.host.source, dirty_tree_sha256=OTHER_HASH)
        with pytest.raises(p.ProofRefused) as error:
            p.prepare_runtime(plan, effects=c.effects, scope=c.scope)
        assert error.value.failure.fixed_code == p.FailureCode.SOURCE

    def test_import_defers_libpq_file_access(self):
        script = """
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

opened = []
def audit(event, args):
    if event == "open":
        opened.append(str(args[0]))
sys.addaudithook(audit)
path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("fresh_project_boundary", path)
p = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = p
spec.loader.exec_module(p)
def pg_opens():
    return [name for name in opened if "proof_project_pg" in name.lower()]
assert not pg_opens(), pg_opens()
root = path.parent / "unused-import-test-root"
plan = SimpleNamespace(
    config=p.Config("run-test", root, root / "artifacts", p.Ports(8101, 8102, 8103, 8104)),
    source=SimpleNamespace(commit="c" * 40),
    descriptor=SimpleNamespace(source_revision="sha256:" + "a" * 64))
profile = {"LEAF_PROOF_PROFILE": "project", "LEAF_PROOF_PROJECT_RUN_ID": "run-test",
           "LEAF_PROOF_PROJECT_KEY_SHA256": "fakekey",
           "LEAF_PROOF_PROJECT_JWKS_FILE": str(root / "public-jwks.json")}
for attempt in range(2):
    env = p.project_environment(plan, os_env={"PGSERVICE": "ambient-service", "pgpassword": "ambient-password"},
        dsn="fakedb", profile_env=profile, organization_id="fakeorg")
    assert "PGSERVICE" not in env and "pgpassword" not in env
    assert env["DATABASE_URL"] == "fakedb"
    if attempt == 0:
        first_opens = pg_opens()
        assert first_opens
    else:
        assert pg_opens() == first_opens
"""
        result = subprocess.run([sys.executable, "-B", "-c", script, str(Path(p.__file__))],
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_reviewer_ambient_conflict(self):
        ambient = {"LEAF_AUTH_LIVE": "0", "DATABASE_URL": "olddb",
                   "LEAF_HARNESS_DATABASE_URL": "otherdb"}
        before = dict(ambient)
        with pytest.raises(p.ProofRefused) as error:
            environment(case(), ambient)
        assert error.value.failure.fixed_code == p.FailureCode.ENVIRONMENT
        assert ambient == before

    def test_ambient_conflicts_individually(self):
        for ambient in ({"LEAF_AUTH_LIVE": "0"}, {"LEAF_AUTH_LIVE": ""},
                        {"DATABASE_URL": "fakedb"}, {"DATABASE_URL": ""},
                        {"LEAF_HARNESS_DATABASE_URL": "fakedb"}, {"LEAF_HARNESS_DATABASE_URL": ""}):
            with pytest.raises(p.ProofRefused) as error:
                environment(case(), ambient)
            assert error.value.failure.fixed_code == p.FailureCode.ENVIRONMENT
        assert environment(case(), {"LEAF_AUTH_LIVE": "1"})["LEAF_AUTH_LIVE"] == "1"

    def test_process_environment_redacts_values(self, capsys):
        values = {"DATABASE_URL": "fakedb", "SIGNING_KEY": "fakekey", "TOKEN": "faketok"}
        spec = p.ProcessSpec("runtime-build", ("fake",), ROOT, values, 1)
        assert_redacted_mapping(spec.environment, values, capsys)
        values["TOKEN"] = "changed"
        assert spec.environment["TOKEN"] == "faketok"

    def test_host_environment_redacts_values(self, capsys):
        values = {"DATABASE_URL": "fakedb", "SIGNING_KEY": "fakekey", "TOKEN": "faketok"}
        host = replace(fake_host().facts, environment=values)
        assert_redacted_mapping(host.environment, values, capsys)
        values["TOKEN"] = "changed"
        assert host.environment["TOKEN"] == "faketok"

    def test_project_environment_copy_redacts_values(self, capsys):
        env = environment(case())
        copied = env.copy()
        assert_redacted_mapping(copied, dict(env), capsys)
        spec = p.ProcessSpec("runtime-build", ("fake",), ROOT, {}, 1)
        assert type(copied) is type(spec.environment) is type(fake_host().facts.environment)
        env["DATABASE_URL"] = "changed"
        assert copied["DATABASE_URL"] == "fakedb"

    def test_process_argv_refuses_secret_flags(self):
        for flag in SECRET_FLAGS:
            for spelling in (flag, flag.upper(), flag.title()):
                with pytest.raises(p.ProofRefused) as error:
                    p.ProcessSpec("runtime-build", ("fake", spelling, "fakesecret"), ROOT, {}, 1)
                assert error.value.failure == p.Failure(p.Stage.RUNTIME, p.FailureCode.RUNTIME)
        assert p.ProcessSpec("runtime-build", ("fake", "--tokenizer"), ROOT, {}, 1).argv[-1] == "--tokenizer"

    def test_process_argv_refuses_secret_assignments(self):
        for flag in SECRET_FLAGS:
            for spelling in (flag, flag.upper(), flag.title()):
                for value in ("fakesecret", ""):
                    with pytest.raises(p.ProofRefused) as error:
                        p.ProcessSpec("runtime-build", ("fake", spelling + "=" + value), ROOT, {}, 1)
                    assert error.value.failure == p.Failure(p.Stage.RUNTIME, p.FailureCode.RUNTIME)

    def test_production_existing_run_root(self, tmp_path):
        root = tmp_path / "run-test"
        config = p.Config("run-test", root, root / "artifacts", p.Ports(0, 0, 0, 0))
        host = p.production_effects().host
        assert not host.inspect(config).run_root_exists
        root.mkdir()
        assert host.inspect(config).run_root_exists

    def test_production_foreign_output_root(self, tmp_path):
        root = tmp_path / "run-test"
        root.mkdir()
        config = p.Config("run-test", root, root / "artifacts", p.Ports(0, 0, 0, 0))
        host = p.production_effects().host
        assert host.inspect(config).output_owned
        assert not host.inspect(replace(config, artifact_root=tmp_path / "foreign")).output_owned

    def test_production_occupied_port(self, tmp_path):
        root = tmp_path / "run-test"
        root.mkdir()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            if sys.platform == "win32":
                occupied.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            occupied.bind(("127.0.0.1", 0))
            port = occupied.getsockname()[1]
            config = p.Config("run-test", root, root / "artifacts", p.Ports(port, 0, 0, 0))
            assert p.production_effects().host.inspect(config).occupied_ports == (port,)

    def test_project_environment_libpq_scrub(self, monkeypatch):
        ambient = {"PGSERVICE": "ambient-service", "PATH": "fakepath"}
        assert "PGSERVICE" not in environment(case(), ambient)
        # Exercise the scrub independently of the redundant OS allowlist.
        # If that allowlist expands, libpq policy must still protect the child.
        monkeypatch.setattr(p, "_OS_KEYS", p._OS_KEYS | {"PGSERVICE"})
        env = environment(case(), ambient)
        assert "PGSERVICE" not in env and "ambient-service" not in env.values()
        assert env["PATH"] == "fakepath" and ambient["PGSERVICE"] == "ambient-service"


SECRET_FLAGS = ("--token", "--password", "--secret", "--dsn", "--database-url", "--signing-key")


def authority_case():
    c = case()
    c.plan = admitted(c)
    c.events, c.mints, c.requests, c.stops = [], [], [], []
    c.allocated, c.entry, c.exit_code, c.hang = False, True, 0, False
    c.corpus, c.tracked = c.plan.migrations, c.plan.source.expected_migrations
    c.status = dict(ok=True, database="leaf", schema="public", migration_count=1,
        applied_migration_count=1, missing={}, missing_migrations=[], migration_hash_drift=[],
        invalid_constraints=[], invalid_indexes=[], invalid_triggers=[])
    c.migration_proof = p.MigrationProof("run-test", p._manifest_hash(c.plan.migrations), True, "leaf", "public", 1, 1)
    c.binding = p.BindingProof("fakeorg", "fakebind", "leaf-proof-project|run-test", "hosted_pro", "auth0", "owner", "active")
    c.ready = dict(ok=True, server_version="16.fake", round_trip_bytes=256)
    c.published = set()
    c.profile = profile(c)
    c.db = SimpleNamespace(
        apply_migration=lambda: c.events.append("apply"),
        assert_schema_current=lambda: (c.events.append("assert-schema"), c.status)[1],
        schema_status=lambda: c.status,
        reset_pool=lambda: c.events.append("reset"))
    c.org = SimpleNamespace(org_id="fakeorg", tier="hosted_pro", status="active")
    c.row = SimpleNamespace(binding_id="fakebind", platform_tenant_id="fakeorg",
        external_authority="auth0", external_subject=c.binding.subject, status="active")
    c.seed_calls = []
    def create(name, authority, subject, *, tier):
        c.seed_calls.append((name, authority, subject, tier))
        return c.org
    c.store = SimpleNamespace(create_org_with_identity=create,
        resolve_active_identity_binding=lambda authority, subject: c.row,
        active_identity_role=lambda org, binding: "owner")
    def load_db(environment):
        c.events.append("db-import")
        assert environment["DATABASE_URL"] == "fakedb"
        return c.db
    c.host.database_module = load_db
    c.host.store_module = lambda: c.store
    c.host.migration_entry_present = lambda: c.entry
    def corpus():
        c.events.append("corpus")
        return c.corpus, c.tracked
    c.host.migration_corpus = corpus
    def inventory(run_id):
        c.events.append("inventory")
        name = "leafproof-" + run_id
        return p.AuthorityOwnership(run_id, (name, name + "-pg", name + "-relay"),
            (("leaf.proof-project.run", run_id), ("leaf.proof-project.owner", "proof_project_pg")),
            ("a" * 12, "b" * 12) if c.allocated else (), ("c" * 12,) if c.allocated else ())
    c.host.authority_inventory = inventory
    c.host.cleanup_authority = lambda run_id: c.events.append("emergency-cleanup")
    c.host.close_identity = lambda lease: object.__setattr__(lease, "issuer", None)
    c.host.read_child_result = lambda path: p._proof_public(c.migration_proof) if path.name == "migration-result.json" else p._proof_public(c.binding)
    def journal(path, value):
        c.events.append(path.stem)
        c.journals.append((path, value))
    def allocate(run_id):
        c.events.append("allocate")
        c.allocated = True
        return SimpleNamespace(dsn=lambda: "fakedb", readiness=lambda: c.ready,
                               stop=lambda: c.events.append("stop-authority"))
    def write_jwks(path):
        c.events.append("jwks")
        if path in c.published:
            raise FileExistsError("fakekey")
        c.published.add(path)
    c.issuer = SimpleNamespace(run_id="run-test", issuer="https://proof-project.leaf.invalid/run-test/",
        audience="leaf-proof-project:run-test", thumbprint="fakekey", write_public_jwks=write_jwks,
        profile_env=lambda path: c.profile,
        mint=lambda suffix, **kwargs: (c.mints.append((suffix, kwargs)), "faketok")[1])
    def issue(run_id):
        c.events.append("issuer")
        return c.issuer
    def launch(spec, *, scope):
        c.events.append("launch:" + spec.role)
        c.launches.append(spec)
        return SimpleNamespace(poll=lambda: None if c.hang and c.effects.clock.now < 2 else c.exit_code,
                               stop=lambda deadline: (c.stops.append(deadline), True)[1])
    def migrate(request, *, scope):
        c.requests.append(request)
        return p._run_database_child(request, effects=c.effects, scope=scope)
    def seed(request, *, scope):
        c.requests.append(request)
        return p._seed_child(request, effects=c.effects)
    c.effects = replace(c.effects, authority_factory=allocate, issuer_factory=issue,
        migration_runner=migrate, seed_operation=seed, process_launcher=launch,
        receipt_writer=SimpleNamespace(ownership=journal))
    def acquire(name, cleanup):
        c.events.append("acquire:" + name.value)
        c.scope.cleanups.append((name, cleanup))
    c.scope.acquire = acquire
    c.request = p.MigrationRequest("run-test", p.REPO / "platform/migrations", c.plan.migrations,
        {"DATABASE_URL": "fakedb", "PGSERVICE": "badsvc"}, 1.0, ROOT / "migration-result.json")
    return c


def prepare(c):
    return p.prepare_authority(c.plan, effects=c.effects, scope=c.scope)


def authority_refused(c, code, operation=prepare):
    with pytest.raises(p.ProofRefused) as error:
        operation(c)
    assert error.value.failure.fixed_code == code
    assert all(spec.role in {"migration-child", "seed-child"} for spec in c.launches)
    assert c.mints == []
    return error.value


class TestProjectAuthority:
    def test_allocation_journal(self, monkeypatch):
        c = authority_case()
        prepared = prepare(c)
        assert c.events.index("corpus") < c.events.index("acquire:authority") < c.events.index("authority-planned") < c.events.index("allocate")
        assert c.events.index("allocate") < c.events.index("authority-observed") < c.events.index("launch:migration-child")
        assert prepared.database.authority_run_id == "run-test"
        assert prepared.database.ownership.containers == ("a" * 12, "b" * 12)
        assert prepared.database.schema_status == c.migration_proof
        assert prepared.database.migration_manifest == c.plan.migrations
        assert prepared.database.migration_manifest_sha256 == p._manifest_hash(c.plan.migrations)
        assert (prepared.database.allocation_seconds, prepared.database.relay_seconds, prepared.database.migration_seconds) == (0, 0, 0)
        assert "fakedb" not in repr(prepared) and "faketok" not in repr(prepared.identity_lease)
        assert "fakedb" not in repr(c.journals) and c.mints == []
        c = authority_case()
        def fail(run_id):
            c.events.append("allocate")
            raise RuntimeError("fakedb")
        c.effects = replace(c.effects, authority_factory=fail)
        error = authority_refused(c, p.FailureCode.ALLOCATION)
        assert "fakedb" not in str(error)
        c.scope.cleanups[0][1]()
        assert c.events[-1] == "emergency-cleanup" and c.launches == []
        c = authority_case()
        original_inventory = c.host.authority_inventory
        c.host.authority_inventory = lambda run_id: replace(original_inventory(run_id), run_id="run-else")
        authority_refused(c, p.FailureCode.ALLOCATION)
        assert "allocate" not in c.events
        c = authority_case()
        original_inventory = c.host.authority_inventory
        c.host.authority_inventory = lambda run_id: replace(original_inventory(run_id), containers=())
        authority_refused(c, p.FailureCode.ALLOCATION)
        assert "allocate" in c.events and c.launches == []
        # Production factory reuses PP-1, through its lazy seam, with no start here.
        called = []
        monkeypatch.setattr(p, "_pg_module", lambda: SimpleNamespace(PgAuthority=SimpleNamespace(start=lambda run_id: called.append(run_id))))
        p.production_effects().authority_factory("run-test")
        assert called == ["run-test"]
        for role, kind in (("migration-child", p.ResourceKind.MIGRATION_PROCESS), ("seed-child", p.ResourceKind.SEED_PROCESS)):
            scope = fake_scope()
            child = SimpleNamespace(start=lambda: called.append(len(scope.cleanups)), stop=lambda deadline: True)
            monkeypatch.setattr(p, "_RuntimeProcess", lambda spec: child)
            spec = p.ProcessSpec(role, ("fake",), ROOT, {"DATABASE_URL": "fakedb"}, 1)
            assert p._launch_runtime(spec, scope=scope) is child
            assert scope.cleanups[0][0] == kind and called[-1] == 1

    def test_transport_barrier(self):
        c = authority_case()
        c.ready["ok"] = False
        authority_refused(c, p.FailureCode.TRANSPORT)
        assert c.requests == [] and c.launches == [] and "issuer" not in c.events
        for corruption in ({"round_trip_bytes": True}, {"round_trip_bytes": 255}, {"server_version": "fakedb://"}, {"unknown": "fakedb"}):
            c = authority_case()
            c.ready.update(corruption)
            authority_refused(c, p.FailureCode.TRANSPORT)

    def test_empty_dsn_before_import(self, monkeypatch, capsys):
        for dsn in ("", "  ", None):
            c = authority_case()
            request = replace(c.request, environment={"DATABASE_URL": dsn})
            authority_refused(c, p.FailureCode.ENVIRONMENT,
                lambda c: p.migration_child(request, effects=c.effects))
            assert c.events == [] and c.launches == []
        c = authority_case()
        original = c.effects.authority_factory
        def empty_authority(run_id):
            authority = original(run_id)
            authority.dsn = lambda: ""
            return authority
        c.effects = replace(c.effects, authority_factory=empty_authority)
        authority_refused(c, p.FailureCode.ENVIRONMENT)
        assert c.launches == [] and "db-import" not in c.events
        # The production entry refuses before the PG sibling (libpq cleaner) is loaded.
        for dsn in (" " + chr(9) + chr(10), "", None):
            c = authority_case()
            loads = []
            monkeypatch.setattr(p, "_libpq_cleaner", None)
            monkeypatch.setattr(p, "_load_file", lambda *args, **kwargs:
                loads.append(args) or SimpleNamespace(clean_libpq_environment=dict))
            monkeypatch.setattr(p, "production_effects", lambda: c.effects)
            monkeypatch.setenv("LEAF_PROJECT_CHILD_REQUEST", json.dumps(p._request_public(c.request)))
            if dsn is None:
                monkeypatch.delenv("DATABASE_URL", raising=False)
            else:
                monkeypatch.setenv("DATABASE_URL", dsn)
            assert p._database_child_entry() == 1
            assert json.loads(capsys.readouterr().out) == {"failure": "ENVIRONMENT"}
            assert loads == [] and c.events == [] and c.launches == []

    def test_migration_entry_missing(self):
        c = authority_case()
        c.entry = False
        authority_refused(c, p.FailureCode.MIGRATION_ENTRY)
        assert "allocate" not in c.events and "db-import" not in c.events
        authority_refused(c, p.FailureCode.MIGRATION_ENTRY, lambda c: p.migration_child(c.request, effects=c.effects))
        c = authority_case()
        c.db.apply_migration = None
        authority_refused(c, p.FailureCode.MIGRATION_ENTRY, lambda c: p.migration_child(c.request, effects=c.effects))
        assert c.events[-1] == "reset"

    def test_migration_file_missing(self, monkeypatch):
        c = authority_case()
        c.corpus = ()
        authority_refused(c, p.FailureCode.MIGRATION_CORPUS)
        assert "allocate" not in c.events
        c = authority_case()
        original = c.effects.authority_factory
        def allocate(run_id):
            result = original(run_id)
            c.corpus = ()
            return result
        c.effects = replace(c.effects, authority_factory=allocate)
        authority_refused(c, p.FailureCode.MIGRATION_CORPUS)
        assert c.launches == []
        # The tracked oracle is independent of the surviving-file glob.
        monkeypatch.setattr(p, "_git", lambda *args: b"platform/migrations/0001_fake.sql\n")
        monkeypatch.setattr(Path, "glob", lambda *args: iter(()))
        actual, tracked = p.production_effects().host.migration_corpus()
        assert actual == () and tracked == ("platform/migrations/0001_fake.sql",)

    def test_migration_file_unexpected(self):
        c = authority_case()
        c.corpus += (file("platform/migrations/9999_fake.sql"),)
        authority_refused(c, p.FailureCode.MIGRATION_CORPUS)
        assert "allocate" not in c.events
        c = authority_case()
        c.corpus = (replace(c.corpus[0], sha256=OTHER_HASH),)
        authority_refused(c, p.FailureCode.MIGRATION_CORPUS,
            lambda c: p.migration_child(c.request, effects=c.effects))
        assert "db-import" not in c.events

    def test_migration_nonzero(self):
        c = authority_case()
        c.exit_code = 1
        authority_refused(c, p.FailureCode.MIGRATION_EXIT)
        assert len(c.stops) == 1 and "issuer" not in c.events
        spec = c.launches[0]
        assert spec.cwd == p.REPO.parent and spec.argv[1:3] == ("-B", "-c")
        assert "fakedb" not in repr(spec.argv) and "fakedb" not in repr(spec.environment)
        assert spec.environment["DATABASE_URL"] == "fakedb" and "PGSERVICE" not in spec.environment
        c = authority_case()
        c.exit_code = 1
        c.host.read_child_result = lambda path: {"failure": "SCHEMA"}
        authority_refused(c, p.FailureCode.SCHEMA)

    def test_migration_timeout(self):
        c = authority_case()
        c.hang = True
        authority_refused(c, p.FailureCode.MIGRATION_TIMEOUT,
            lambda c: p._run_database_child(c.request, effects=c.effects, scope=c.scope))
        assert 1 <= c.effects.clock.now < 1.1 and len(c.stops) == 1
        c = authority_case()
        def late(request, *, scope):
            c.effects.clock.now = request.deadline
            return c.migration_proof
        c.effects = replace(c.effects, migration_runner=late)
        authority_refused(c, p.FailureCode.MIGRATION_TIMEOUT)
        assert "issuer" not in c.events
        # Stopping the child past the deadline is a timeout, even after a valid result.
        c = authority_case()
        def late_launch(spec, *, scope):
            c.launches.append(spec)
            def stop(deadline):
                c.stops.append(deadline)
                c.effects.clock.now = 2.0
                return True
            return SimpleNamespace(poll=lambda: 0, stop=stop)
        c.effects = replace(c.effects, process_launcher=late_launch)
        authority_refused(c, p.FailureCode.MIGRATION_TIMEOUT,
            lambda c: p._run_database_child(c.request, effects=c.effects, scope=c.scope))
        assert len(c.stops) == 1

    def test_migration_result_binding(self, monkeypatch, capsys):
        for corruption in ({"run_id": "run-else"}, {"manifest_sha256": OTHER_HASH}, {"ok": 1},
                           {"database": "otherdb"}, {"schema": "other"}, {"migration_count": True},
                           {"migration_count": 2}, {"applied_migration_count": 0},
                           {"applied_migration_count": True}):
            c = authority_case()
            c.migration_proof = replace(c.migration_proof, **corruption)
            authority_refused(c, p.FailureCode.MIGRATION_RESULT)
            assert "issuer" not in c.events and len(c.stops) == 1
        for payload in (None, {}, {"unknown": "fakedb"}):
            c = authority_case()
            c.host.read_child_result = lambda path: payload
            authority_refused(c, p.FailureCode.MIGRATION_RESULT)
        c = authority_case()
        raw = dict(p._proof_public(c.migration_proof), extra="fake")
        c.host.read_child_result = lambda path: raw
        authority_refused(c, p.FailureCode.MIGRATION_RESULT)
        # Exercise the production wire entry using fake environment, IO and DB.
        c = authority_case()
        monkeypatch.setenv("LEAF_PROJECT_CHILD_REQUEST", json.dumps(p._request_public(c.request)))
        monkeypatch.setenv("DATABASE_URL", "fakedb")
        monkeypatch.setattr(p, "open", lambda *args, **kwargs: io.StringIO(), raising=False)
        monkeypatch.setattr(p, "production_effects", lambda: c.effects)
        c.db.apply_migration = lambda: print("fakedb")
        assert p._database_child_entry() == 0
        output = capsys.readouterr().out
        assert json.loads(output) == p._proof_public(c.migration_proof) and "fakedb" not in output
        monkeypatch.setattr(p, "_path_safe", lambda path: True)
        monkeypatch.setattr(Path, "open", lambda *args, **kwargs: io.BytesIO(b'{"ok":true,"ok":false}'))
        with pytest.raises(ValueError, match="duplicate"):
            p._Host().read_child_result(ROOT / "migration-result.json")
        monkeypatch.setattr(Path, "open", lambda *args, **kwargs: io.BytesIO(b"x" * (p.MAX_OUTPUT_BYTES + 1)))
        with pytest.raises(RuntimeError, match="bound"):
            p._Host().read_child_result(ROOT / "migration-result.json")

    def test_schema_refusal(self):
        c = authority_case()
        def refuse():
            c.events.append("assert-schema")
            raise RuntimeError("fakedb")
        c.db.assert_schema_current = refuse
        authority_refused(c, p.FailureCode.SCHEMA, lambda c: p.migration_child(c.request, effects=c.effects))
        assert c.events[-3:] == ["apply", "assert-schema", "reset"]
        for key in ("missing", "missing_migrations", "migration_hash_drift", "invalid_constraints", "invalid_indexes", "invalid_triggers"):
            c = authority_case()
            c.status[key] = {"fake": ["fake"]} if key == "missing" else ["fake"]
            authority_refused(c, p.FailureCode.SCHEMA, lambda c: p.migration_child(c.request, effects=c.effects))
        c = authority_case()
        assert p.migration_child(c.request, effects=c.effects) == c.migration_proof
        assert c.events[-1] == "reset"

    def test_migration_pool_finally(self):
        c = authority_case()
        def fail():
            raise RuntimeError("fakedb")
        c.db.apply_migration = fail
        error = authority_refused(c, p.FailureCode.MIGRATION_EXIT, lambda c: p.migration_child(c.request, effects=c.effects))
        assert c.events.count("reset") == 1 and "fakedb" not in str(error)
        c = authority_case()
        c.db.reset_pool = fail
        authority_refused(c, p.FailureCode.MIGRATION_EXIT, lambda c: p.migration_child(c.request, effects=c.effects))
        c = authority_case()
        c.db.assert_schema_current = fail
        c.db.reset_pool = fail
        authority_refused(c, p.FailureCode.SCHEMA, lambda c: p.migration_child(c.request, effects=c.effects))
        # A pool reset that outlives the deadline is a timeout, not a success.
        c = authority_case()
        def slow_reset():
            c.events.append("reset")
            c.effects.clock.now = 2.0
        c.db.reset_pool = slow_reset
        authority_refused(c, p.FailureCode.MIGRATION_TIMEOUT, lambda c: p.migration_child(c.request, effects=c.effects))
        assert c.events.count("reset") == 1

    def test_issuer_failure(self, monkeypatch):
        c = authority_case()
        def fail(run_id):
            c.events.append("issuer-failure")
            raise RuntimeError("fakekey")
        c.effects = replace(c.effects, issuer_factory=fail)
        error = authority_refused(c, p.FailureCode.IDENTITY)
        assert "fakekey" not in str(error) and c.seed_calls == [] and c.published == set()
        called = []
        monkeypatch.setattr(p, "_load_file", lambda *args, **kwargs: SimpleNamespace(
            FixtureIssuer=SimpleNamespace(create=lambda run_id: called.append(run_id))))
        monkeypatch.delitem(sys.modules, "_leaf_project_fixture_identity", raising=False)
        p.production_effects().issuer_factory("run-test")
        assert called == ["run-test"]

    def test_frozen_profile_jwks(self):
        c = authority_case()
        prepared = prepare(c)
        assert c.published == {ROOT / "public-jwks.json"}
        assert dict(prepared.identity_lease.profile) == profile(c)
        with pytest.raises(TypeError):
            prepared.identity_lease.profile["LEAF_PROOF_PROFILE"] = "other"
        c.profile["LEAF_PROOF_PROFILE"] = "other"
        assert prepared.identity_lease.profile["LEAF_PROOF_PROFILE"] == "project"
        assert prepared.identity.public_key_thumbprint == "fakekey"
        c = authority_case()
        c.published.add(ROOT / "public-jwks.json")
        authority_refused(c, p.FailureCode.IDENTITY)
        assert c.seed_calls == []
        for changed in ({"LEAF_PROOF_PROFILE": "other"}, {"LEAF_AUTH_LIVE": "0"}, {"LEAF_PROOF_PROJECT_RUN_ID": "run-else"}):
            c = authority_case()
            c.profile.update(changed)
            authority_refused(c, p.FailureCode.IDENTITY)
            assert c.seed_calls == []

    def test_atomic_owner_binding(self):
        c = authority_case()
        result = prepare(c)
        assert c.seed_calls == [("Project proof run-test", "auth0", "leaf-proof-project|run-test", "hosted_pro")]
        assert result.binding == c.binding and c.events[-1] == "reset"
        for attribute, value in (("external_authority", "other"), ("external_subject", "wrong"),
                                 ("platform_tenant_id", "otherorg"), ("status", "revoked")):
            c = authority_case()
            setattr(c.row, attribute, value)
            authority_refused(c, p.FailureCode.BINDING)
            assert c.events[-1] == "reset"
        c = authority_case()
        c.store.active_identity_role = lambda org, binding: "viewer"
        authority_refused(c, p.FailureCode.BINDING)
        assert c.events[-1] == "reset"

    def test_matching_subject_claims(self):
        c = authority_case()
        prepared = prepare(c)
        assert c.mints == []
        token = prepared.identity_lease.mint(now=123, lifetime_s=60)
        assert token.reveal() == "faketok"
        assert "faketok" not in repr(token) and "faketok" not in str(token) and "faketok" not in f"{token}"
        assert c.mints == [("run-test", {"tenant_id": "fakeorg", "org_id": "fakeorg", "tier": "hosted_pro",
            "claim_ns": "https://leafdesign.ai/", "lifetime_s": 60, "now": 123})]
        assert prepared.identity.subject == prepared.binding.subject == "leaf-proof-project|run-test"
        assert prepared.identity.organization_id == prepared.binding.organization_id == "fakeorg"
        assert "faketok" not in repr(prepared) and "fakedb" not in repr(c.requests)
        c = authority_case()
        c.effects = replace(c.effects, seed_operation=lambda request, *, scope: replace(c.binding, subject="wrong"))
        authority_refused(c, p.FailureCode.BINDING)


def assert_redacted_mapping(mapping, values, capsys):
    print(mapping)
    rendered = (repr(mapping), str(mapping), capsys.readouterr().out)
    expected = "{" + ", ".join(repr(key) + ": <redacted>" for key in values) + "}"
    for text in rendered:
        assert text.rstrip("\n") == expected
    assert dict(mapping) == values
    with pytest.raises(TypeError):
        mapping["TOKEN"] = "changed"
