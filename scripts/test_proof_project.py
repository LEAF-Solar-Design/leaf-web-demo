"""Record A boundary rows; only import subprocesses and bound loopback probes."""
from dataclasses import replace
from datetime import datetime, timezone
import importlib.util
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
            "PREFLIGHT", "ENVIRONMENT", "SOURCE", "RUNTIME", "BROWSER_UNAVAILABLE"}

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
        with pytest.raises(RuntimeError, match="record B"):
            effects.authority_factory("run-test")

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


def assert_redacted_mapping(mapping, values, capsys):
    print(mapping)
    rendered = (repr(mapping), str(mapping), capsys.readouterr().out)
    expected = "{" + ", ".join(repr(key) + ": <redacted>" for key in values) + "}"
    for text in rendered:
        assert text.rstrip("\n") == expected
    assert dict(mapping) == values
    with pytest.raises(TypeError):
        mapping["TOKEN"] = "changed"
