"""Records A/B/C; authority and execution rows use fake effects only."""
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
from unittest.mock import ANY

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
            "MIGRATION_EXIT", "MIGRATION_RESULT", "SCHEMA", "IDENTITY", "BINDING",
            "LAUNCH", "OWNERSHIP", "READINESS", "HEARTBEAT", "AUTH", "BROWSER_EXIT",
            "BROWSER_EVIDENCE", "CANONICAL", "CANCELLED", "GUARDIAN", "CLEANUP_PROCESS",
            "CLEANUP_IDENTITY", "CLEANUP_POOL", "CLEANUP_AUTHORITY", "CLEANUP_PRIVATE",
            "RECEIPT", "INTERNAL", "USAGE"}

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
        with pytest.raises(p.ProofRefused) as readiness_error:
            effects.readiness_probe(None)
        assert readiness_error.value.failure == p.Failure(p.Stage.STACK, p.FailureCode.LAUNCH)

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
        mint=lambda suffix, **kwargs: (c.mints.append((suffix, kwargs)),
            "faketok" if len(c.mints) == 1 else "faketok-browser")[1])
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
        assert c.events.index("corpus") < c.events.index("authority-planned") < c.events.index("allocate") < c.events.index("acquire:authority")
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
        assert c.scope.cleanups == [] and c.launches == []
        assert "emergency-cleanup" not in c.events
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


def execution_case():
    c = authority_case()
    c.prepared = prepare(c)
    c.launches.clear()
    c.mints.clear()
    c.events.clear()
    c.launcher_exit, c.browser_exit, c.browser_polls = None, 0, 0
    c.dead, c.foreign = False, False
    c.identity = p.ProcessIdentity("stack-launcher", 100, "start100", 50)
    c.pids = dict(zip(p.STACK_ROLES, range(101, 106)))
    c.stack_ready = {"launcher_pid": 100, "pids": dict(c.pids),
        "ports": dict(zip(("web", "app", "broker", "harness"), c.plan.config.ports.values()))}
    c.app = p.AppReadiness(c.plan.source.commit, tuple((name, "ready", True) for name in p.APP_DEPENDENCIES))
    d = c.plan.descriptor
    c.heartbeat = p.HeartbeatProof("project:run-test", d.tool, d.runtime, d.source_revision,
        d.source_sha256, c.effects.clock.utc_now())
    c.auth, c.denied = (200, "fakeorg", "hosted_pro", "active"), 401
    c.browser_requests, c.canonical_requests, c.readiness_requests = [], [], []
    c.evidence = p.CanonicalEvidence("fakeorg", "project", "drawing", "input", "output", "job",
        "fakebind", "project:run-test", 1, HASH, OTHER_HASH, OTHER_HASH)
    c.result = p.BrowserResult("run-test", c.plan.source, "fakekey", 0,
        p.REQUIRED_TEST_IDS, p.REQUIRED_TEST_IDS, p.REQUIRED_TEST_IDS,
        tuple(p.TestOutcome(name, "passed", 0) for name in p.REQUIRED_TEST_IDS), (),
        dict.fromkeys(p.CHECKPOINTS, True), c.evidence)
    c.canonical = p.CanonicalProof(c.evidence, OTHER_HASH, OTHER_HASH)
    def children(pids):
        if c.dead:
            raise RuntimeError("dead")
        return tuple(p.ProcessIdentity(role, pid, "start" + str(pid), 999 if c.foreign else 100)
                     for role, pid in pids.items())
    c.launcher = SimpleNamespace(identity=c.identity, poll=lambda: c.launcher_exit,
        children=children, stop=lambda deadline: True)
    def launch(spec, *, scope):
        c.launches.append(spec)
        scope.acquire(p.ResourceKind.STACK_PROCESS, lambda: c.launcher.stop(5))
        return c.launcher
    def readiness(request):
        c.readiness_requests.append(request)
        c.events.append("ready-start")
        result = p.check_readiness(request, effects=c.effects)
        c.effects.clock.now += 1
        c.events.append("ready-done")
        return result
    def browser(request, *, scope):
        c.events.append("browser")
        c.browser_requests.append(request)
        handle = SimpleNamespace(process=SimpleNamespace(poll=lambda: c.browser_exit),
            poll=lambda: c.result, stop=lambda deadline: True)
        scope.acquire(p.ResourceKind.BROWSER_PROCESS, lambda: handle.stop(5))
        return handle
    def canonical(request):
        c.canonical_requests.append(request)
        return c.canonical
    c.host.read_stack_ready = lambda path: c.stack_ready
    c.host.app_readiness = lambda request: c.app
    c.host.worker_heartbeat = lambda request: c.heartbeat
    c.host.organization_read = lambda request, *, authenticated: c.auth if authenticated else c.denied
    c.effects = replace(c.effects, process_launcher=launch, readiness_probe=readiness,
        browser_runner=browser, canonical_reader=canonical)
    return c


def stat_row(pid, *, state="S", ppid=100, session=None, comm="python", start="4242"):
    fields = [state, str(ppid), str(pid), str(pid if session is None else session)]
    return f"{pid} ({comm}) " + " ".join(fields + ["0"] * 15 + [start] + ["0"] * 30)


def execute(c):
    return p.run_project_pipeline(c.prepared, effects=c.effects, scope=c.scope)


def execution_refused(c, code):
    with pytest.raises(p.ProofRefused) as error:
        execute(c)
    assert error.value.failure.fixed_code == code
    assert "fakedb" not in str(error.value) and "faketok" not in str(error.value)


def canonical_case():
    import hashlib
    from uuid import UUID
    from contextlib import contextmanager
    c = SimpleNamespace(connections=0, closes=0, verifications=[], reads=[])
    ids = [str(UUID(int=i)) for i in range(1, 8)]
    c.before = SimpleNamespace(graph={"settings": {"pitch": 10}})
    c.stored = SimpleNamespace(graph={"settings": {"pitch": 20}})
    c.reopened = c.stored
    c.digest = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    c.evidence = p.CanonicalEvidence(*ids[:6], ids[6], "project:run-test", 2,
        p._settings_hash(c.before, digest=c.digest), p._settings_hash(c.stored, digest=c.digest),
        p._settings_hash(c.stored, digest=c.digest))
    e = c.evidence
    c.commit = dict(schema="fake", attempt=1, tool="solar-settings", tool_manifest_sha256=HASH,
        parameters={"pitch": 20}, parent_intake_sha256=HASH, before_graph_sha256=HASH,
        graph_project_id="graph", initialized=True, organization_id=e.organization_id,
        project_id=e.project_id, drawing_id=e.drawing_id, parent_version_id=e.input_version_id,
        actor_binding_id=e.binding_id, checkout_fence="2", job_id=e.job_id)
    c.job = dict(status="succeeded", tool_name="solar-settings", org_id=e.organization_id,
        project_id=e.project_id, request_tenant_id=e.organization_id, input_version_id=e.input_version_id,
        output_version_id=e.output_version_id, provenance={"worker_id": e.worker_id},
        result={"graph_commit": c.commit})
    def verify(job_id, result, *, expected, conn):
        c.verifications.append((job_id, result, expected))
        assert expected == c.commit and result is c.commit
        return {"output_version_id": e.output_version_id}
    def context(expected, version_id, conn):
        c.reads.append(str(version_id))
        return (c.before if str(version_id) == e.input_version_id else
                c.stored if c.connections == 1 else c.reopened), None
    c.graph = SimpleNamespace(project_graph_job_provenance=verify, _stored_context=context,
                              local=SimpleNamespace(digest=c.digest))
    @contextmanager
    def connection(environment, deadline):
        assert environment["DATABASE_URL"] == "fakedb" and deadline == 10
        c.connections += 1
        try:
            yield SimpleNamespace(execute=lambda sql, args: SimpleNamespace(fetchone=lambda: c.job))
        finally:
            c.closes += 1
    c.connection = connection
    c.request = p.CanonicalRequest("execution", p.WORKER_TOOL, {"DATABASE_URL": "fakedb"}, e, 10)
    return c


class TestProjectExecution:
    def test_launcher_contract(self, monkeypatch):
        c = execution_case()
        proof = execute(c)
        spec = c.launches[0]
        assert spec.role == "stack-launcher" and spec.cwd == p.REPO
        assert spec.argv == (sys.executable, "-B", str(p.REPO / "scripts/start-leaf.py"),
            "--strict-ports", "--with-harness", "--canonical-worker-tool", "solar-settings",
            "--canonical-worker-id", "project:run-test", "--ready-file", str(ROOT / "stack-ready.json"),
            "--web-port", "8101", "--app-port", "8102", "--broker-port", "8103", "--harness-port", "8104")
        assert spec.environment["DATABASE_URL"] == spec.environment["LEAF_HARNESS_DATABASE_URL"] == "fakedb"
        assert "fakedb" not in repr(spec) + repr(spec.argv) + repr(spec.environment)
        assert isinstance(proof, p.ExecutionProof) and not hasattr(proof, "ok")
        assert proof.observed_rows == ("P01", "P04", "P08", "P09", "P11")
        assert proof.stack_seconds == 1 and proof.browser_seconds == 0
        assert proof.initial_heartbeat == proof.final_heartbeat == c.heartbeat
        assert len(proof.stack.owned_process_identities) == 6
        calls = []
        child = SimpleNamespace(start=lambda: calls.append("start"), stop=lambda deadline: True)
        monkeypatch.setattr(p, "_RuntimeProcess", lambda spec: child)
        scope = fake_scope()
        p._launch_runtime(spec, scope=scope)
        assert scope.cleanups[0][0] == p.ResourceKind.STACK_PROCESS and calls == ["start"]

    def test_partial_launch(self):
        c = execution_case()
        del c.stack_ready["pids"]["harness"]
        execution_refused(c, p.FailureCode.LAUNCH)
        assert c.browser_requests == []
        c = execution_case()
        c.stack_ready = None
        execution_refused(c, p.FailureCode.LAUNCH)
        assert p.STACK_TIMEOUT_S <= c.effects.clock.now <= p.STACK_TIMEOUT_S + 0.1
        c = execution_case()
        c.stack_ready["ports"]["app"] += 1
        execution_refused(c, p.FailureCode.LAUNCH)

    def test_wrong_launcher_identity(self):
        c = execution_case()
        c.stack_ready["launcher_pid"] = 999
        execution_refused(c, p.FailureCode.OWNERSHIP)
        assert c.browser_requests == []

    def test_foreign_child(self):
        c = execution_case()
        c.foreign = True
        execution_refused(c, p.FailureCode.OWNERSHIP)
        assert c.browser_requests == []
        c = execution_case()
        c.stack_ready["pids"]["worker"] = c.stack_ready["pids"]["app"]
        execution_refused(c, p.FailureCode.OWNERSHIP)
        own = p._posix_child_identity(stat_row(101), 100, "app", 101)
        assert own == p.ProcessIdentity("app", 101, "4242", 100)
        assert p._posix_child_identity(stat_row(101, session=100), 100, "app", 101) == own
        assert p._posix_child_identity(stat_row(101, comm="a) b"), 100, "app", 101) == own
        assert p._posix_child_identity(stat_row(101) + " 0 0", 100, "app", 101) == own
        short = "101 (python) S 100 101 101 " + " ".join(["0"] * 15 + ["4242"])
        refused = (stat_row(101, ppid=999), stat_row(101, session=555), stat_row(101, session=102),
                   stat_row(999, session=100), stat_row(999), short,
                   stat_row(101, ppid="+100"), stat_row(101, ppid="1_00"), stat_row(101, ppid="0100"),
                   stat_row(101, ppid="١٠٠"), stat_row(101, session="+101"),
                   stat_row(101, start="+4242"), stat_row(101, start="42_42"),
                   "+" + stat_row(101), "0" + stat_row(101), stat_row(101).replace(")", "]"),
                   stat_row(101).replace(" (", " ["))
        for row in refused:
            with pytest.raises(RuntimeError):
                p._posix_child_identity(row, 100, "app", 101)

    def test_dead_child(self):
        c = execution_case()
        c.dead = True
        execution_refused(c, p.FailureCode.OWNERSHIP)
        assert c.browser_requests == []
        c = execution_case()
        c.launcher_exit = 0
        execution_refused(c, p.FailureCode.LAUNCH)
        for state in ("Z", "X"):
            with pytest.raises(RuntimeError):
                p._posix_child_identity(stat_row(101, state=state), 100, "app", 101)
        with pytest.raises(RuntimeError):
            p._posix_child_identity("101 (python) S 100 101 101", 100, "app", 101)
        with pytest.raises(RuntimeError):
            p._posix_child_identity(stat_row(101).encode(), 100, "app", 101)

    def test_degraded_app_dependency(self, monkeypatch):
        for dependency in p.APP_DEPENDENCIES:
            c = execution_case()
            c.app = replace(c.app, dependencies=tuple((name, "degraded" if name == dependency else state, required)
                for name, state, required in c.app.dependencies))
            execution_refused(c, p.FailureCode.READINESS)
            assert c.browser_requests == []
        c = execution_case()
        c.app = replace(c.app, dependencies=())
        execution_refused(c, p.FailureCode.READINESS)
        c = execution_case()
        execute(c)
        payload = {"ready": True, "status": "ready", "source_revision": c.plan.source.commit,
            "dependencies": {name: {"state": "ready", "required": True, "latency_ms": 1}
                             for name in p.APP_DEPENDENCIES}, "unknown": "fakedb"}
        monkeypatch.setattr(p, "_project_http", lambda *args: (200, payload))
        projected = p._Host.app_readiness(c.readiness_requests[0])
        assert "fakedb" not in repr(projected) and set(x[0] for x in projected.dependencies) == set(p.APP_DEPENDENCIES)
        payload["status"] = "degraded"
        with pytest.raises(p.ProofRefused):
            p._Host.app_readiness(c.readiness_requests[0])

    def test_wrong_app_source(self):
        c = execution_case()
        c.app = replace(c.app, source_revision="d" * 40)
        execution_refused(c, p.FailureCode.READINESS)

    def test_missing_heartbeat(self, monkeypatch, capsys):
        c = execution_case()
        c.heartbeat = None
        execution_refused(c, p.FailureCode.HEARTBEAT)
        assert c.browser_requests == []
        # The actual child selects the owner, not the latest worker for the tool.
        from contextlib import nullcontext
        queries = []
        connection = SimpleNamespace(execute=lambda sql, args:
            (queries.append((sql, args)) or SimpleNamespace(fetchone=lambda: None)))
        monkeypatch.setattr(p, "_project_connection", lambda *args: nullcontext(connection))
        monkeypatch.setattr(p, "open", lambda *args, **kw: io.StringIO(), raising=False)
        environment = {"DATABASE_URL": "fakedb", "LEAF_PROJECT_CANONICAL_REQUEST": json.dumps(
            {"purpose": "heartbeat", "worker_id": "project:run-test", "deadline": 10, "evidence": None})}
        monkeypatch.setattr(p.os, "environ", environment)
        assert p._canonical_child_entry() == 0 and json.loads(capsys.readouterr().out) is None
        assert "WHERE worker_id = %s" in queries[0][0] and queries[0][1] == ("project:run-test",)

    def test_wrong_heartbeat_owner(self):
        c = execution_case()
        c.heartbeat = replace(c.heartbeat, worker_id="foreign")
        execution_refused(c, p.FailureCode.HEARTBEAT)

    def test_invalid_heartbeat_age(self):
        from datetime import timedelta
        for seconds in (-31, 1):
            c = execution_case()
            c.heartbeat = replace(c.heartbeat, observed_at=c.heartbeat.observed_at + timedelta(seconds=seconds))
            execution_refused(c, p.FailureCode.HEARTBEAT)
        c = execution_case()
        c.heartbeat = replace(c.heartbeat, observed_at=c.heartbeat.observed_at.replace(tzinfo=None))
        execution_refused(c, p.FailureCode.HEARTBEAT)
        # Each organization read takes 1.5 s; the heartbeat is re-read before readiness returns.
        for aged, refused in ((29, True), (26, False)):
            c = execution_case()
            base, c.offset = c.effects.clock.utc_now(), 0.0
            c.effects.clock.utc_now = lambda c=c, base=base: base + timedelta(seconds=c.offset)
            c.heartbeat = replace(c.heartbeat, observed_at=base - timedelta(seconds=aged))
            def slow_read(request, *, authenticated, c=c):
                c.offset += 1.5
                return c.auth if authenticated else c.denied
            c.host.organization_read = slow_read
            if refused:
                execution_refused(c, p.FailureCode.HEARTBEAT)
                assert c.browser_requests == []
            else:
                assert execute(c).initial_heartbeat.observed_at == base - timedelta(seconds=aged)
                assert c.offset == 3.0 and len(c.browser_requests) == 1

    def test_wrong_worker_descriptor(self):
        for field in ("tool_name", "runtime", "source_revision", "source_sha256"):
            c = execution_case()
            c.heartbeat = replace(c.heartbeat, **{field: "foreign"})
            execution_refused(c, p.FailureCode.HEARTBEAT)

    def test_authenticated_org_read(self, monkeypatch):
        for auth in ((403, None, None, None), (200, "other", "hosted_pro", "active"),
                     (200, "fakeorg", "free", "active"), (200, "fakeorg", "hosted_pro", "revoked")):
            c = execution_case()
            c.auth = auth
            execution_refused(c, p.FailureCode.AUTH)
            assert c.browser_requests == []
        c = execution_case()
        execute(c)
        seen = []
        monkeypatch.setattr(p, "_project_http", lambda request, path, **kw:
            (seen.append((path, kw)) or (200, {"org": {"org_id": "fakeorg", "tier": "hosted_pro", "status": "active"}})))
        assert p._Host.organization_read(c.readiness_requests[0], authenticated=True) == c.auth
        assert seen[0][0] == "/api/orgs/fakeorg" and seen[0][1]["bearer"].reveal() == "faketok"

    def test_bearerless_refusal(self):
        for status in (200, 404, 500, True):
            c = execution_case()
            c.denied = status
            execution_refused(c, p.FailureCode.AUTH)
            assert c.browser_requests == []
        c = execution_case()
        c.denied = 403
        assert execute(c).stack.bearerless_refused

    def test_late_browser_token(self):
        c = execution_case()
        order = []
        old_mint = c.prepared.identity_lease.issuer.mint
        def mint(*args, **kwargs):
            order.append(tuple(c.events))
            return old_mint(*args, **kwargs)
        c.prepared.identity_lease.issuer.mint = mint
        execute(c)
        assert len(c.mints) == 2 and c.mints[0][1]["lifetime_s"] == 180 and c.mints[1][1]["lifetime_s"] == 360
        assert "ready-done" not in order[0] and "ready-done" in order[1]
        assert "LEAF_E2E_TOKEN" not in c.launches[0].environment
        request = c.browser_requests[0]
        assert request.environment["LEAF_E2E_TOKEN"] == "faketok-browser"
        assert c.readiness_requests[0].bearer.reveal() == "faketok"
        assert "DATABASE_URL" not in request.environment
        assert "faketok" not in repr(request) + repr(request.environment) + repr(c.readiness_requests[0])
        assert request.web_origin == "http://127.0.0.1:8101" and request.app_origin == "http://127.0.0.1:8102"

    def test_missing_browser_runner(self):
        for runner in (None, p.production_effects().browser_runner):
            c = execution_case()
            c.effects = replace(c.effects, browser_runner=runner)
            execution_refused(c, p.FailureCode.BROWSER_UNAVAILABLE)
            assert c.launches == [] and c.mints == []

    def test_stack_exit_during_browser(self):
        c = execution_case()
        original = c.effects.browser_runner
        def browser(request, *, scope):
            handle = original(request, scope=scope)
            c.launcher_exit = 1
            return handle
        c.effects = replace(c.effects, browser_runner=browser)
        execution_refused(c, p.FailureCode.LAUNCH)
        assert c.canonical_requests == []
        c = execution_case()
        original = c.effects.browser_runner
        def browser(request, *, scope):
            handle = original(request, scope=scope)
            c.launcher.children = lambda pids: tuple(p.ProcessIdentity(role, pid, "reused", 100) for role, pid in pids.items())
            return handle
        c.effects = replace(c.effects, browser_runner=browser)
        execution_refused(c, p.FailureCode.OWNERSHIP)

    def test_heartbeat_loss_during_browser(self):
        c = execution_case()
        original = c.effects.browser_runner
        def browser(request, *, scope):
            handle = original(request, scope=scope)
            c.heartbeat = None
            return handle
        c.effects = replace(c.effects, browser_runner=browser)
        execution_refused(c, p.FailureCode.HEARTBEAT)
        assert c.canonical_requests == []

    def test_browser_nonzero(self):
        c = execution_case()
        c.browser_exit = 1
        execution_refused(c, p.FailureCode.BROWSER_EXIT)
        c = execution_case()
        c.result = replace(c.result, exit_code=1)
        execution_refused(c, p.FailureCode.BROWSER_EXIT)
        c = execution_case()
        c.result, c.browser_exit = None, None
        execution_refused(c, p.FailureCode.BROWSER_EXIT)
        assert c.effects.clock.now == 1 + p.BROWSER_TIMEOUT_S

    def test_execution_completeness(self):
        for field in ("selected_test_ids", "started_test_ids", "completed_test_ids"):
            for ids in ((), p.REQUIRED_TEST_IDS[:-1], p.REQUIRED_TEST_IDS + (p.REQUIRED_TEST_IDS[0],)):
                c = execution_case()
                c.result = replace(c.result, **{field: ids})
                execution_refused(c, p.FailureCode.BROWSER_EVIDENCE)
        for changes in ({"outcomes": ()}, {"annotation_types": ("skip",)}, {"run_id": "foreign"},
                        {"public_key_thumbprint": "other"}, {"source_binding": None},
                        {"outcomes": tuple(p.TestOutcome(name, "failed" if i == 0 else "passed", 0)
                                           for i, name in enumerate(p.REQUIRED_TEST_IDS))},
                        {"outcomes": tuple(p.TestOutcome(name, "passed", 1) for name in p.REQUIRED_TEST_IDS)}):
            c = execution_case()
            c.result = replace(c.result, **changes)
            execution_refused(c, p.FailureCode.BROWSER_EVIDENCE)
        for result in (None, {}, {"ok": True}, SimpleNamespace(exit_code=0)):
            c = execution_case()
            c.result = result
            execution_refused(c, p.FailureCode.BROWSER_EVIDENCE)
        c = execution_case()
        c.result = replace(c.result, canonical=replace(c.evidence, checkout_fence=True))
        execution_refused(c, p.FailureCode.BROWSER_EVIDENCE)
        for changes in ({"outcomes": None}, {"outcomes": ({"status": "passed"},)},
                        {"selected_test_ids": None}, {"canonical": {"secret": "faketok"}}):
            c = execution_case()
            c.result = replace(c.result, **changes)
            execution_refused(c, p.FailureCode.BROWSER_EVIDENCE)
        class _Passed(str):
            pass

        for status in (ANY, _Passed("passed")):
            c = execution_case()
            c.result = replace(c.result, outcomes=tuple(
                p.TestOutcome(name, status if i == 0 else "passed", 0)
                for i, name in enumerate(p.REQUIRED_TEST_IDS)))
            execution_refused(c, p.FailureCode.BROWSER_EVIDENCE)
        bad = replace(execution_case().result, canonical={"token": "SYNTHETIC_TOKEN"})
        assert "SYNTHETIC_TOKEN" not in repr(bad) + str(bad) + f"{bad}"

    def test_missing_checkpoint(self):
        for name in p.CHECKPOINTS:
            c = execution_case()
            checkpoints = dict(c.result.checkpoints)
            del checkpoints[name]
            c.result = replace(c.result, checkpoints=checkpoints)
            execution_refused(c, p.FailureCode.BROWSER_EVIDENCE)
        c = execution_case()
        c.result = replace(c.result, checkpoints=dict(c.result.checkpoints, extra=True))
        execution_refused(c, p.FailureCode.BROWSER_EVIDENCE)
        for name in p.CHECKPOINTS:
            c = execution_case()
            checkpoints = dict(c.result.checkpoints)
            checkpoints[name] = False
            c.result = replace(c.result, checkpoints=checkpoints)
            execution_refused(c, p.FailureCode.BROWSER_EVIDENCE)

    def test_canonical_relationships(self):
        c = execution_case()
        c.canonical = replace(c.canonical, evidence=replace(c.evidence, job_id="foreign"))
        execution_refused(c, p.FailureCode.CANONICAL)
        c = execution_case()
        c.canonical = {"ok": True}
        execution_refused(c, p.FailureCode.CANONICAL)
        c = execution_case()
        proof = execute(c)
        assert len(c.canonical_requests) == 1
        request = c.canonical_requests[0]
        assert request.purpose == "execution" and request.environment["DATABASE_URL"] == "fakedb"
        assert request.evidence == c.evidence and proof.canonical is c.canonical
        assert "fakedb" not in repr(request) + repr(request.environment)
        # Exercise the independent production verifier with fake SQL and canonical library.
        c = canonical_case()
        proof = p._canonical_database_proof(c.request, graph=c.graph, connection=c.connection)
        assert proof.evidence == c.evidence and len(c.verifications) == 1
        assert c.connections == c.closes == 2
        for field, value in (("status", "running"), ("tool_name", "other"), ("org_id", "other"),
                             ("project_id", "other"), ("request_tenant_id", "other"),
                             ("input_version_id", "other"), ("output_version_id", "other"),
                             ("provenance", {"worker_id": "foreign"})):
            c = canonical_case()
            c.job[field] = value
            with pytest.raises(p.ProofRefused) as error:
                p._canonical_database_proof(c.request, graph=c.graph, connection=c.connection)
            assert error.value.failure.fixed_code == p.FailureCode.CANONICAL
            assert c.connections == c.closes == 1 and c.verifications == []

    def test_persisted_settings_readback(self):
        for field in ("stored_settings_sha256", "reopened_settings_sha256"):
            c = execution_case()
            c.canonical = replace(c.canonical, **{field: HASH})
            execution_refused(c, p.FailureCode.CANONICAL)
        c = execution_case()
        c.result = replace(c.result, canonical=replace(c.evidence, reopened_settings_sha256=HASH))
        execution_refused(c, p.FailureCode.BROWSER_EVIDENCE)
        c = canonical_case()
        c.reopened = SimpleNamespace(graph={"settings": {"pitch": 30}})
        proof = p._canonical_database_proof(c.request, graph=c.graph, connection=c.connection)
        assert proof.stored_settings_sha256 == c.evidence.settings_sha256
        assert proof.reopened_settings_sha256 != c.evidence.settings_sha256
        assert c.reads == [c.evidence.input_version_id, c.evidence.output_version_id, c.evidence.output_version_id]
        assert c.closes == 2


def assert_redacted_mapping(mapping, values, capsys):
    print(mapping)
    rendered = (repr(mapping), str(mapping), capsys.readouterr().out)
    expected = "{" + ", ".join(repr(key) + ": <redacted>" for key in values) + "}"
    for text in rendered:
        assert text.rstrip("\n") == expected
    assert dict(mapping) == values
    with pytest.raises(TypeError):
        mapping["TOKEN"] = "changed"

# D's injected operation handles never spawn a thread, process or listener.
class ImmediateOperation:
    def __init__(self, callback):
        self.value, self.error = None, None
        try:
            self.value = callback()
        except BaseException as error:
            self.error = error

    def done(self):
        return True

    def result(self):
        if self.error is not None:
            raise self.error
        return self.value


def lifetime_case():
    c = authority_case()
    c.teardown, c.leases = [], []
    c.private = True
    c.host.begin_operation = ImmediateOperation
    def scrub(reservation, *, keep_safe):
        c.teardown.append("private")
        c.private = False
        return True
    def cleanup(run_id):
        c.teardown.append("authority")
        c.allocated = False
    def close_identity(lease):
        c.teardown.append("identity")
        c.leases.append(lease)
        object.__setattr__(lease, "issuer", None)
        c.published.discard(lease.jwks_path)
    c.host.scrub = scrub
    c.host.private_absent = lambda reservation: not c.private
    c.host.cleanup_authority = cleanup
    c.host.cleanup_owned_authority = lambda run_id, ownership: cleanup(run_id)
    c.host.close_identity = close_identity
    c.scope = p.RunScope(effects=c.effects)
    return c


def cancelled_stage(stage):
    c = lifetime_case()
    c.scope.own_reservation(c.plan.reservation)
    clock = c.effects.clock
    # An outstanding external operation becomes ready only after cancellation.
    task = SimpleNamespace(done=lambda: clock.now >= 0.1, result=lambda: "done")
    original = c.host.begin_operation
    c.host.begin_operation = lambda callback: task
    c.scope.cancelled = lambda: clock.now >= 0.05
    with pytest.raises(p.ProofRefused) as error:
        c.scope.perform(stage, lambda: None, deadline=1.0)
    assert error.value.failure == p.Failure(stage, p.FailureCode.CANCELLED)
    assert clock.now == 0.05  # Refused while pending, not after it completed.
    c.host.begin_operation = original
    proof = c.scope.close()
    assert proof.ok and c.teardown == ["private"]
    assert c.scope.failure == error.value.failure
    assert 0.1 <= clock.now < 0.2
    with pytest.raises(p.ProofRefused):
        c.scope.commit_guard(lambda: pytest.fail("cancelled publication"))


def acquisition_child(monkeypatch, role, kind):
    c = lifetime_case()
    stops = []
    def start():
        raise RuntimeError("fakedb")
    child = SimpleNamespace(start=start, stop=lambda deadline: (stops.append(deadline), True)[1])
    monkeypatch.setattr(p, "_RuntimeProcess", lambda spec: child)
    spec = p.ProcessSpec(role, ("fake",), ROOT, {}, 1)
    with pytest.raises(RuntimeError):
        p._launch_runtime(spec, scope=c.scope)
    assert [name for name, _ in c.scope._actions] == [kind]
    proof = c.scope.close()
    assert proof.processes_stopped and len(stops) == 1
    assert stops[0] == p.CLEANUP_TIMEOUT_S or stops[0] > 0
    assert c.scope.close() is proof and len(stops) == 1


def cleanup_failure(kind, expected):
    c = lifetime_case()
    def fail():
        c.teardown.append("failed")
        raise RuntimeError("faketok")
    c.scope.acquire(kind, fail)
    c.scope.own_pool(SimpleNamespace(close=lambda: c.teardown.append("pool")))
    c.scope.acquire(p.ResourceKind.AUTHORITY, lambda: c.teardown.append("authority"))
    c.scope.own_reservation(c.plan.reservation)
    proof = c.scope.close()
    assert c.teardown[-2:] == ["authority", "private"]
    assert "pool" in c.teardown and "failed" in c.teardown
    assert proof.failures == (p.Failure(p.Stage.TEARDOWN, expected),)
    assert not proof.ok and proof.observed_rows == ()
    assert "faketok" not in repr(proof)
    return c, proof


class TestProjectLifetime:
    def test_d_c1_disarm_live_job(self, monkeypatch):
        c = lifetime_case()
        c.private = False
        child = p._RuntimeProcess(p.ProcessSpec("stack-launcher", ("fake",), ROOT, {}, 1))
        child.identity = p.ProcessIdentity("stack-launcher", 7, "old", 1)
        child.process = SimpleNamespace(poll=lambda: 0)
        checked, acknowledged = [], []
        child.job = SimpleNamespace(
            stop=lambda deadline: (checked.append(deadline), False)[1], close=lambda: None)
        monkeypatch.setattr(p, "production_effects", lambda: c.effects)
        channel = SimpleNamespace(receive=lambda timeout: "disarm",
                                  acknowledge=lambda: acknowledged.append(True))
        request = p.GuardianRequest("run-test", c.plan.reservation,
            c.host.authority_inventory("run-test"), channel, (child,), 1)
        assert child.poll() == 0
        assert p.guardian_main(request, effects=c.effects) == 1
        assert acknowledged == [] and checked == [p.CLEANUP_TIMEOUT_S]
        assert not child._stopped

    def test_d_c1_disarm_stopped_job(self, monkeypatch):
        c = lifetime_case()
        c.private = False
        child = p._RuntimeProcess(p.ProcessSpec("stack-launcher", ("fake",), ROOT, {}, 1))
        child.identity = p.ProcessIdentity("stack-launcher", 7, "old", 1)
        child.process = SimpleNamespace(poll=lambda: 0)
        checked, acknowledged = [], []
        child.job = SimpleNamespace(
            stop=lambda deadline: (checked.append(deadline), True)[1], close=lambda: None)
        monkeypatch.setattr(p, "production_effects", lambda: c.effects)
        channel = SimpleNamespace(receive=lambda timeout: "disarm",
                                  acknowledge=lambda: acknowledged.append(True))
        request = p.GuardianRequest("run-test", c.plan.reservation,
            c.host.authority_inventory("run-test"), channel, (child,), 1)
        assert child.poll() == 0
        assert p.guardian_main(request, effects=c.effects) == 0
        assert acknowledged == [True] and checked == [p.CLEANUP_TIMEOUT_S]
        assert child._stopped

    def test_d_c1_deadline_before_pool(self):
        c = lifetime_case()
        calls = []
        assert c.effects.clock.now == 0
        with pytest.raises(p.ProofRefused) as error:
            c.scope.perform(p.Stage.MIGRATION, lambda: calls.append("operation"), deadline=0)
        primary = p.Failure(p.Stage.MIGRATION, p.FailureCode.GUARDIAN)
        assert error.value.failure == primary
        c.scope.own_pool(SimpleNamespace(close=lambda: False))
        proof = c.scope.close()
        assert proof.failures == (p.Failure(p.Stage.TEARDOWN, p.FailureCode.CLEANUP_POOL),)
        assert c.scope.failure == primary and calls == []

    def test_d_c1_deadline_blocks_commit(self):
        c = lifetime_case()
        calls = []
        assert c.effects.clock.now == 0
        with pytest.raises(p.ProofRefused) as error:
            c.scope.perform(p.Stage.MIGRATION, lambda: calls.append("operation"), deadline=0)
        assert error.value.failure == p.Failure(p.Stage.MIGRATION, p.FailureCode.GUARDIAN)
        assert c.scope.close().ok
        with pytest.raises(p.ProofRefused):
            c.scope.commit_guard(lambda: (calls.append("commit"), "COMMITTED")[1])
        assert calls == [] and c.scope.failure == error.value.failure

    def test_d_c1_begin_operation_error(self):
        c = lifetime_case()
        calls = []
        def begin(operation):
            raise OSError("fake begin failure")
        c.host.begin_operation = begin
        with pytest.raises(p.ProofRefused) as error:
            c.scope.perform(p.Stage.MIGRATION, lambda: calls.append("operation"), deadline=1)
        assert error.value.failure == p.Failure(p.Stage.MIGRATION, p.FailureCode.GUARDIAN)
        assert c.scope.failure == error.value.failure
        assert calls == [] and c.scope._pending == []
        assert c.scope.close().ok

    def test_d_c1_begin_operation_refusal(self):
        c = lifetime_case()
        primary = p.Failure(p.Stage.ALLOCATION, p.FailureCode.ALLOCATION)
        def begin(operation):
            raise p.ProofRefused(primary)
        c.host.begin_operation = begin
        with pytest.raises(p.ProofRefused) as error:
            c.scope.perform(p.Stage.MIGRATION, lambda: pytest.fail("operation ran"), deadline=1)
        assert error.value.failure == primary and c.scope.failure == primary

    def test_d_c1_acquire_refusal(self):
        c = lifetime_case()
        assert c.scope.close().ok
        with pytest.raises(p.ProofRefused) as error:
            c.scope.acquire(p.ResourceKind.POOL, lambda: None)
        assert error.value.failure == p.Failure(p.Stage.TEARDOWN, p.FailureCode.GUARDIAN)
        assert c.scope.failure == error.value.failure

    def test_d_c1_checkpoint_refusal(self):
        c = lifetime_case()
        c.scope.cancel()
        with pytest.raises(p.ProofRefused) as error:
            c.scope.checkpoint(p.Stage.MIGRATION)
        assert error.value.failure == p.Failure(p.Stage.MIGRATION, p.FailureCode.CANCELLED)
        assert c.scope.failure == error.value.failure
        with pytest.raises(p.ProofRefused):
            c.scope.checkpoint(p.Stage.ALLOCATION)
        assert c.scope.failure == error.value.failure

    def test_d_c1_close_refusal(self):
        c = lifetime_case()
        refused = []
        def close_again():
            with pytest.raises(p.ProofRefused) as error:
                c.scope.close()
            refused.append(error.value.failure)
        c.scope.own_pool(SimpleNamespace(close=close_again))
        assert c.scope.close().ok
        assert refused == [p.Failure(p.Stage.TEARDOWN, p.FailureCode.GUARDIAN)]
        assert c.scope.failure == refused[0]

    def test_d_c1_commit_guard_refusal(self):
        c = lifetime_case()
        with pytest.raises(p.ProofRefused) as error:
            c.scope.commit_guard(lambda: pytest.fail("commit before cleanup"))
        assert error.value.failure == p.Failure(p.Stage.TEARDOWN, p.FailureCode.CLEANUP_PRIVATE)
        assert c.scope.failure == error.value.failure
        assert c.scope.close().ok
        with pytest.raises(p.ProofRefused):
            c.scope.commit_guard(lambda: pytest.fail("forgotten refusal"))

    def test_d_c1_guardian_arm_refusal(self):
        c = lifetime_case()
        primary = p.Failure(p.Stage.ALLOCATION, p.FailureCode.GUARDIAN)
        def arm():
            raise p.ProofRefused(primary)
        guardian = SimpleNamespace(arm=arm, disarm=lambda: c.teardown.append("disarm"))
        with pytest.raises(p.ProofRefused) as error:
            c.scope.own_guardian(guardian)
        assert error.value.failure == primary and c.scope.failure == primary
        assert c.scope.close().ok and c.teardown == ["disarm"]

    def test_d_c1_commit_callback_refusal(self):
        c = lifetime_case()
        primary = p.Failure(p.Stage.VERIFICATION, p.FailureCode.CANCELLED)
        def commit():
            raise p.ProofRefused(primary)
        assert c.scope.close().ok
        with pytest.raises(p.ProofRefused) as error:
            c.scope.commit_guard(commit)
        assert error.value.failure == primary and c.scope.failure == primary

    def test_d_c1_planned_journal_collision(self):
        c = lifetime_case()
        assert not c.allocated
        def journal(path, value):
            assert path.name == "authority-planned.json"
            assert value.authority.containers == value.authority.networks == ()
            c.allocated = True  # Another allocation wins after the empty inventory.
            raise OSError("fake planned journal failure")
        c.effects = replace(c.effects, receipt_writer=SimpleNamespace(ownership=journal))
        c.scope = p.RunScope(effects=c.effects)
        with pytest.raises(p.ProofRefused) as error:
            c.scope.perform(p.Stage.ALLOCATION, lambda: prepare(c), deadline=1)
        assert error.value.failure == p.Failure(p.Stage.ALLOCATION, p.FailureCode.ALLOCATION)
        c.scope.close()
        assert c.allocated and "authority" not in c.teardown
        assert "allocate" not in c.events
        assert c.scope.failure == error.value.failure

    def test_d_c1_owned_authority_cleanup(self):
        c = lifetime_case()
        journal = c.effects.receipt_writer.ownership
        def fail_observed(path, value):
            journal(path, value)
            if path.name == "authority-observed.json":
                assert c.allocated
                raise OSError("fake observed journal failure")
        c.effects = replace(c.effects, receipt_writer=SimpleNamespace(ownership=fail_observed))
        c.scope = p.RunScope(effects=c.effects)
        with pytest.raises(p.ProofRefused) as error:
            c.scope.perform(p.Stage.ALLOCATION, lambda: prepare(c), deadline=1)
        assert error.value.failure == p.Failure(p.Stage.ALLOCATION, p.FailureCode.ALLOCATION)
        assert "allocate" in c.events
        proof = c.scope.close()
        assert proof.authority_removed and not c.allocated
        assert c.teardown == ["authority"] and c.scope.failure == error.value.failure

    def test_d_c1_migration_before_runtime(self, monkeypatch):
        c = lifetime_case()
        def child(spec):
            return SimpleNamespace(start=lambda: None,
                stop=lambda deadline: (c.teardown.append(spec.role), True)[1])
        monkeypatch.setattr(p, "_RuntimeProcess", child)
        for role in ("migration-child", "runtime-build"):
            p._launch_runtime(p.ProcessSpec(role, ("fake",), ROOT, {}, 1), scope=c.scope)
        assert c.scope.close().processes_stopped
        assert c.teardown == ["migration-child", "runtime-build"]

    def test_reverse_cleanup_order(self):
        c = lifetime_case()
        expected = ["browser_process", "stack_process", "seed_process", "migration_process",
                    "runtime_process", "identity", "pool", "authority", "root", "guardian"]
        # Scramble registration: order must follow dependencies, then LIFO.
        for name in reversed(expected):
            c.scope.acquire(p.ResourceKind(name), lambda name=name: c.teardown.append(name))
        c.scope.acquire(p.ResourceKind.STACK_PROCESS, lambda: c.teardown.append("new-stack"))
        proof = c.scope.close()
        assert c.teardown == [expected[0], "new-stack", *expected[1:]]
        assert proof.ok and proof.observed_rows == ("P10",) and proof.teardown_seconds == 0
        assert c.scope.close() is proof
        with pytest.raises(p.ProofRefused):
            c.scope.acquire(p.ResourceKind.POOL, lambda: None)

    def test_failure_after_root(self):
        c = lifetime_case()
        def journal(*args):
            raise RuntimeError("fake")
        c.effects = replace(c.effects, receipt_writer=SimpleNamespace(ownership=journal))
        c.scope = p.RunScope(effects=c.effects)
        with pytest.raises(p.ProofRefused):
            p.preflight(c.config, effects=c.scope.bind_effects())
        assert c.scope.close().private_material_removed
        assert c.teardown == ["private"] and not c.private

    def test_failure_after_guardian(self):
        c = lifetime_case()
        def arm():
            c.teardown.append("arm")
            raise RuntimeError("fake")
        guardian = SimpleNamespace(arm=arm, disarm=lambda: c.teardown.append("disarm"))
        with pytest.raises(RuntimeError):
            c.scope.own_guardian(guardian)
        assert c.scope.close().guardian_disarmed
        assert c.teardown == ["arm", "disarm"]

    def test_partial_allocation_failure(self):
        c = lifetime_case()
        def allocate(run_id):
            c.allocated = True
            # Match PP-2: failed startup cleans only its own creation receipt.
            created = c.host.authority_inventory(run_id)
            try:
                raise RuntimeError("fakedb")
            finally:
                c.host.cleanup_owned_authority(run_id, created)
        c.effects = replace(c.effects, authority_factory=allocate)
        c.scope = p.RunScope(effects=c.effects)
        with pytest.raises(p.ProofRefused) as error:
            prepare(c)
        c.scope.remember(error.value.failure)
        proof = c.scope.close()
        assert proof.authority_removed and not c.allocated
        assert c.teardown == ["authority"]
        assert c.scope.failure.fixed_code == p.FailureCode.ALLOCATION

    def test_failure_after_migration_child(self, monkeypatch):
        acquisition_child(monkeypatch, "migration-child", p.ResourceKind.MIGRATION_PROCESS)

    def test_failure_after_issuer(self, monkeypatch):
        c = lifetime_case()
        monkeypatch.setattr(Path, "unlink", lambda *args, **kw: None)
        monkeypatch.setattr(p, "_path_absent", lambda path: True)
        def close(lease):
            c.leases.append(lease)
            c.teardown.append("identity")
            p._Host.close_identity(lease)
        c.host.close_identity = close
        c.issuer.thumbprint = ""
        with pytest.raises(p.ProofRefused):
            prepare(c)
        assert c.scope.close().identity_delivery_closed
        assert len(c.leases) == 1 and c.leases[0].issuer is None
        assert c.teardown == ["identity", "authority"]

    def test_failure_after_jwks(self, monkeypatch):
        c = lifetime_case()
        lease = p.IdentityLease(c.issuer, "run-test", ROOT / "public-jwks.json", {})
        exists = [True]
        monkeypatch.setattr(Path, "unlink", lambda *args, **kw: exists.__setitem__(0, False))
        monkeypatch.setattr(p, "_path_absent", lambda path: not exists[0])
        c.scope.acquire(p.ResourceKind.IDENTITY, lambda: p._Host.close_identity(lease))
        assert c.scope.close().identity_delivery_closed
        assert lease.issuer is None and exists == [False]

    def test_failure_after_seed_pool(self):
        c = lifetime_case()
        pool = SimpleNamespace(close=lambda: c.teardown.append("pool"))
        c.scope.own_pool(pool)
        c.scope.remember(p.Failure(p.Stage.BINDING, p.FailureCode.BINDING))
        assert c.scope.close().pools_closed and c.teardown == ["pool"]

    def test_failure_after_launcher(self, monkeypatch):
        acquisition_child(monkeypatch, "stack-launcher", p.ResourceKind.STACK_PROCESS)

    def test_failure_after_browser(self):
        c = lifetime_case()
        stops = []
        browser = SimpleNamespace(stop=lambda deadline: (stops.append(deadline), True)[1])
        c.scope.own_browser(browser)
        c.scope.remember(p.Failure(p.Stage.BROWSER, p.FailureCode.BROWSER_EXIT))
        assert c.scope.close().processes_stopped
        assert stops == [p.CLEANUP_TIMEOUT_S]

    def test_cancel_allocation(self):
        cancelled_stage(p.Stage.ALLOCATION)

    def test_cancel_migration(self):
        cancelled_stage(p.Stage.MIGRATION)

    def test_cancel_identity(self):
        cancelled_stage(p.Stage.IDENTITY)

    def test_cancel_readiness(self):
        cancelled_stage(p.Stage.READINESS)

    def test_cancel_browser(self):
        cancelled_stage(p.Stage.BROWSER)

    def test_cancel_publication(self):
        c = lifetime_case()
        assert c.scope.close().ok
        c.scope.cancel()
        commits = []
        with pytest.raises(p.ProofRefused) as error:
            c.scope.commit_guard(lambda: commits.append("commit"))
        assert error.value.failure == p.Failure(p.Stage.VERIFICATION, p.FailureCode.CANCELLED)
        assert commits == []
        c = lifetime_case()
        c.scope.close()
        assert c.scope.commit_guard(lambda: "committed") == "committed"

    def test_process_cleanup_failure(self):
        c, proof = cleanup_failure(p.ResourceKind.STACK_PROCESS, p.FailureCode.CLEANUP_PROCESS)
        assert not proof.processes_stopped

    def test_identity_cleanup_failure(self):
        c, proof = cleanup_failure(p.ResourceKind.IDENTITY, p.FailureCode.CLEANUP_IDENTITY)
        assert not proof.identity_delivery_closed

    def test_pool_cleanup_failure(self):
        c, proof = cleanup_failure(p.ResourceKind.POOL, p.FailureCode.CLEANUP_POOL)
        assert not proof.pools_closed

    def test_authority_stop_failure(self):
        c = lifetime_case()
        original = c.effects.authority_factory
        def allocate(run_id):
            authority = original(run_id)
            def stop():
                raise RuntimeError("fakedb")
            authority.stop = stop
            return authority
        c.effects = replace(c.effects, authority_factory=allocate)
        c.scope = p.RunScope(effects=c.effects)
        prepare(c)
        proof = c.scope.close()
        assert not c.allocated and "authority" in c.teardown
        assert proof.failures == (p.Failure(p.Stage.TEARDOWN, p.FailureCode.CLEANUP_AUTHORITY),)
        assert not proof.authority_removed and not proof.ok

    def test_removal_verification(self, monkeypatch):
        reservation = p.Reservation(ROOT, ROOT / "artifacts", root_identity=(1, 2))
        removals = []
        monkeypatch.setattr(p, "_path_safe", lambda path: True)
        original_stat = Path.stat
        monkeypatch.setattr(Path, "stat", lambda path, *args, **kw:
            SimpleNamespace(st_dev=1, st_ino=2) if path == ROOT else original_stat(path, *args, **kw))
        assert not p._path_absent(SimpleNamespace(lstat=lambda: SimpleNamespace()))
        def denied():
            raise PermissionError("fake")
        assert not p._path_absent(SimpleNamespace(lstat=denied))
        def missing():
            raise FileNotFoundError("fake")
        assert p._path_absent(SimpleNamespace(lstat=missing))
        monkeypatch.setattr(p.os.path, "lexists", lambda path: True)
        monkeypatch.setattr(p, "_path_absent", lambda path: False)
        monkeypatch.setattr(p.shutil, "rmtree", lambda path: removals.append(path))
        assert p._Host.scrub(reservation, keep_safe=False) is False
        assert removals == [ROOT]
        # Identity replacement refuses before removal too.
        assert p._Host.scrub(replace(reservation, root_identity=(1, 3)), keep_safe=False) is False
        assert removals == [ROOT]

    def test_scrub_failure(self):
        c = lifetime_case()
        c.host.scrub = lambda *args, **kw: False
        c.scope.own_reservation(c.plan.reservation)
        proof = c.scope.close()
        assert not proof.private_material_removed and not proof.ok
        assert proof.failures == (p.Failure(p.Stage.TEARDOWN, p.FailureCode.CLEANUP_PRIVATE),)
        with pytest.raises(p.ProofRefused):
            c.scope.commit_guard(lambda: pytest.fail("success despite scrub failure"))

    def test_primary_failure_preserved(self):
        c = lifetime_case()
        primary = p.Failure(p.Stage.MIGRATION, p.FailureCode.MIGRATION_EXIT)
        c.scope.remember(primary)
        for kind in (p.ResourceKind.ROOT, p.ResourceKind.POOL, p.ResourceKind.STACK_PROCESS):
            c.scope.acquire(kind, lambda: False)
        proof = c.scope.close()
        assert c.scope.failure is primary
        assert [f.fixed_code for f in proof.failures] == [p.FailureCode.CLEANUP_PROCESS,
            p.FailureCode.CLEANUP_POOL, p.FailureCode.CLEANUP_PRIVATE]

    def test_reused_process_identity(self):
        c = lifetime_case()
        owner = p.ProcessIdentity("fake", 7, "old", 1)
        signals = []
        request = p.StopRequest(owner, lambda: replace(owner, start_id="new"),
            lambda: signals.append("terminate"), lambda: signals.append("kill"), lambda timeout: True)
        assert p.stop_owned(request, effects=c.effects, deadline=5)
        assert signals == []
        identities = iter((owner, replace(owner, start_id="new")))
        request = replace(request, observe=lambda: next(identities), wait=lambda timeout: False)
        assert p.stop_owned(request, effects=c.effects, deadline=5)
        assert signals == ["terminate"]

    def test_guardian_parent_loss(self):
        c = lifetime_case()
        c.allocated = True
        ownership = c.host.authority_inventory("run-test")
        def receive(timeout):
            assert 0 < timeout <= 0.1
            raise EOFError
        channel = SimpleNamespace(receive=receive, acknowledge=lambda: pytest.fail("EOF disarmed"))
        process = SimpleNamespace(stop=lambda deadline: (c.teardown.append("process"), True)[1])
        request = p.GuardianRequest("run-test", c.plan.reservation, ownership, channel, (process,), 1)
        assert p.guardian_main(request, effects=c.effects) == 0
        assert c.teardown == ["process", "authority", "private"] and not c.allocated
        assert c.effects.clock.now == 0  # EOF triggers immediate emergency cleanup.
        # A stale guardian may not remove resources belonging to a later run.
        c.allocated, c.private = True, True
        c.teardown.clear()
        stale = replace(request, ownership=replace(ownership, containers=("d" * 12,)))
        assert p.guardian_main(stale, effects=c.effects) == 1
        assert c.allocated and "authority" not in c.teardown

    def test_guardian_early_disarm(self):
        c = lifetime_case()
        c.scope.own_guardian(SimpleNamespace(arm=lambda: None, disarm=lambda: c.teardown.append("disarm")))
        c.scope.acquire(p.ResourceKind.ROOT, lambda: False)
        proof = c.scope.close()
        assert not proof.guardian_disarmed and c.teardown == []
        assert [f.fixed_code for f in proof.failures] == [p.FailureCode.CLEANUP_PRIVATE, p.FailureCode.GUARDIAN]
        c = lifetime_case()
        channel = SimpleNamespace(receive=lambda timeout: "disarm", acknowledge=lambda: c.teardown.append("ack"))
        request = p.GuardianRequest("run-test", c.plan.reservation,
            c.host.authority_inventory("run-test"), channel, deadline=1)
        assert p.guardian_main(request, effects=c.effects) == 1 and c.teardown == []
        c.private = False
        assert p.guardian_main(request, effects=c.effects) == 0 and c.teardown == ["ack"]

    def test_preexisting_run_collision(self, monkeypatch):
        c = lifetime_case()
        c.allocated = True
        with pytest.raises(p.ProofRefused) as error:
            prepare(c)
        assert error.value.failure.fixed_code == p.FailureCode.ALLOCATION
        c.scope.close()
        assert c.allocated and c.teardown == [] and "allocate" not in c.events
        # Collision after the empty inventory but inside PP-2's start lock.
        c = lifetime_case()
        def collide(run_id):
            c.allocated = True
            raise RuntimeError("run already exists")
        monkeypatch.setattr(p, "_pg_module", lambda: SimpleNamespace(
            PgAuthority=SimpleNamespace(start=collide)))
        c.effects = replace(c.effects, authority_factory=p._authority_factory)
        c.scope = p.RunScope(effects=c.effects)
        with pytest.raises(p.ProofRefused):
            prepare(c)
        c.scope.close()
        assert c.allocated and c.teardown == []

    def test_bounded_escalation(self, monkeypatch):
        c = lifetime_case()
        owner = p.ProcessIdentity("fake", 7, "old", 1)
        calls = []
        def wait(timeout):
            assert timeout is not None and 0 < timeout <= 3
            calls.append(("wait", timeout))
            c.effects.clock.wait(timeout)
            return len(calls) > 3
        request = p.StopRequest(owner, lambda: owner, lambda: calls.append("terminate"),
            lambda: calls.append("kill"), wait)
        assert p.stop_owned(request, effects=c.effects, deadline=3)
        assert calls == ["terminate", ("wait", 1.0), "kill", ("wait", 2.0)]
        assert c.effects.clock.now == 3
        # An uncooperative pending operation stays owned and prevents deletion.
        c = lifetime_case()
        c.scope._pending.append(SimpleNamespace(done=lambda: False))
        c.scope.own_reservation(c.plan.reservation)
        c.scope.own_guardian(SimpleNamespace(arm=lambda: None, disarm=lambda: c.teardown.append("disarm")))
        proof = c.scope.close()
        assert not proof.ok and not proof.guardian_disarmed and c.private
        assert c.teardown == [] and c.effects.clock.now == p.CLEANUP_TIMEOUT_S
        # A parent exit does not establish absence of its owned descendants.
        spec = p.ProcessSpec("stack-launcher", ("fake",), ROOT, {}, 5)
        owned = p._RuntimeProcess(spec)
        owned.identity = owner
        owned.process = SimpleNamespace(poll=lambda: 0)
        jobs = []
        owned.job = SimpleNamespace(stop=lambda deadline: (jobs.append("verify-job"), False)[1],
                                    close=lambda: jobs.append("close-job"))
        monkeypatch.setattr(p, "production_effects", lambda: c.effects)
        assert owned.stop(c.effects.clock.now + 3) is False
        assert jobs == ["verify-job", "close-job"]
        owned = p._RuntimeProcess(spec)
        owned.identity = owner
        owned.process = SimpleNamespace(poll=lambda: 0)
        jobs.clear()
        owned.job = SimpleNamespace(stop=lambda deadline: (jobs.append("verify-job"), True)[1],
                                    close=lambda: jobs.append("close-job"))
        assert owned.stop(c.effects.clock.now + 3)
        assert owned.stop(c.effects.clock.now + 3)
        assert jobs == ["verify-job", "close-job"]
        # Acquisitions completed while settling a cancelled stage join teardown.
        c = lifetime_case()
        registered = []
        def done():
            if c.effects.clock.now >= 0.05 and not registered:
                c.scope.own_pool(SimpleNamespace(close=lambda: c.teardown.append("late-pool")))
                registered.append(True)
            return c.effects.clock.now >= 0.1
        c.scope._pending.append(SimpleNamespace(done=done))
        assert c.scope.close().ok and c.teardown == ["late-pool"]


# E: every external operation, including receipt filesystem operations, is fake.
class ReceiptFiles:
    def __init__(self):
        self.files, self.directories, self.next_inode = {}, {}, 0
        self.root = ReceiptPath(self, ROOT / "receipt-files")
        for path in reversed((self.root.path, *self.root.path.parents)):
            self.next_inode += 1
            self.directories[path] = self.next_inode

    def install(self, monkeypatch):
        monkeypatch.setattr(p, "_private_directory", lambda path: path.mkdir())
        real_lexists, real_rmtree = p.os.path.lexists, p.shutil.rmtree
        monkeypatch.setattr(p.os.path, "lexists", lambda path: path.exists() if isinstance(path, ReceiptPath) else real_lexists(path))
        def remove(root):
            if not isinstance(root, ReceiptPath):
                return real_rmtree(root)
            assert root.path.is_relative_to(self.root.path) and root.path != self.root.path
            self.files = {path: value for path, value in self.files.items() if not path.is_relative_to(root.path)}
            self.directories = {path: value for path, value in self.directories.items() if not path.is_relative_to(root.path)}
        monkeypatch.setattr(p.shutil, "rmtree", remove)
        rename, overwrite = p.os.rename, p.os.replace
        def move(source, target, *, replace=False):
            if not isinstance(source, ReceiptPath):
                return (overwrite if replace else rename)(source, target)
            if target.exists() and not replace:
                raise FileExistsError("fake collision")
            self.files[target.path] = self.files.pop(source.path)
        monkeypatch.setattr(p.os, "rename", move)
        monkeypatch.setattr(p.os, "replace", lambda source, target: move(source, target, replace=True))
        monkeypatch.setattr(p.os, "fsync", lambda fd: None)


class ReceiptPath:
    def __init__(self, files, path):
        self.files, self.path = files, path
    def __str__(self):
        return str(self.path)
    def __fspath__(self):
        return str(self.path)
    def __repr__(self):
        return "<fake receipt path>"
    def __hash__(self):
        return hash(self.path)
    def __eq__(self, other):
        return self.path == (other.path if isinstance(other, ReceiptPath) else other)
    def __truediv__(self, part):
        return ReceiptPath(self.files, self.path / part)
    def __getattr__(self, name):
        return getattr(self.path, name)
    @property
    def parent(self):
        return ReceiptPath(self.files, self.path.parent)
    @property
    def parents(self):
        return tuple(ReceiptPath(self.files, path) for path in self.path.parents)
    def with_name(self, name):
        return ReceiptPath(self.files, self.path.with_name(name))
    def exists(self):
        return self.path in self.files.files or self.path in self.files.directories
    def stat(self):
        if self.path in self.files.directories:
            return SimpleNamespace(st_dev=1, st_ino=self.files.directories[self.path],
                st_mode=p.stat.S_IFDIR, st_file_attributes=0, st_size=0)
        if self.path in self.files.files:
            return SimpleNamespace(st_dev=1, st_ino=1000, st_mode=p.stat.S_IFREG,
                st_file_attributes=0, st_size=len(self.files.files[self.path]))
        raise FileNotFoundError("fake missing")
    lstat = stat
    def mkdir(self, **kwargs):
        if self.exists() or self.path.parent not in self.files.directories:
            raise FileExistsError("fake collision")
        self.files.next_inode += 1
        self.files.directories[self.path] = self.files.next_inode
    def unlink(self, *, missing_ok=False):
        if self.path not in self.files.files and not missing_ok:
            raise FileNotFoundError("fake missing")
        self.files.files.pop(self.path, None)
    def write_bytes(self, value):
        self.files.files[self.path] = value
    def read_bytes(self):
        if self.path not in self.files.files:
            raise FileNotFoundError("fake missing")
        return self.files.files[self.path]
    def write_text(self, value, **kwargs):
        self.write_bytes(value.encode("utf-8"))
    def open(self, mode):
        if mode == "rb":
            return io.BytesIO(self.read_bytes())
        assert mode == "xb"
        if self.exists():
            raise FileExistsError("fake collision")
        path = self
        class Stream(io.BytesIO):
            def fileno(self):
                return 7
            def close(self):
                if not self.closed:
                    path.write_bytes(self.getvalue())
                super().close()
        return Stream()


def receipt_case():
    from contextlib import nullcontext
    c = execution_case()
    c.allocated, c.private = False, True
    c.published.clear()
    c.launches.clear()
    c.events.clear()
    c.journals.clear()
    c.receipts, c.leases, c.retained = {}, [], []
    c.on_publish = lambda: None
    c.host.begin_operation = ImmediateOperation
    def cancellation(scope):
        c.run_scope = scope
        return nullcontext()
    c.host.cancellation = cancellation
    def scrub(reservation, *, keep_safe):
        c.events.append("scrub")
        c.private = False
        c.published.clear()
        c.retained.clear()
        return True
    c.host.scrub = scrub
    c.host.private_absent = lambda reservation: not c.private
    def remove(run_id):
        c.events.append("remove-authority")
        c.allocated = False
    c.host.cleanup_authority = remove
    c.host.cleanup_owned_authority = lambda run_id, ownership: remove(run_id)
    def close_identity(lease):
        c.events.append("close-identity")
        c.leases.append(lease)
        object.__setattr__(lease, "issuer", None)
        c.published.discard(lease.jwks_path)
    c.host.close_identity = close_identity
    def disarm():
        c.events.append("disarm")
        return not c.private and not c.allocated
    c.guardian = SimpleNamespace(arm=lambda: c.events.append("arm"), disarm=disarm,
        own_process=lambda child: c.events.append("own-process"),
        observe=lambda ownership: c.events.append("guardian-authority"))
    c.host.guardian = lambda plan: c.guardian
    c.host.receipt_binding = lambda run_id, path, writer: p.ReceiptBinding(run_id, c.plan.source, "fakekey")
    original_launcher = c.effects.process_launcher
    def launch(spec, *, scope):
        if spec.role == "stack-launcher":
            return original_launcher(spec, scope=scope)
        c.launches.append(spec)
        kind = p.ResourceKind.MIGRATION_PROCESS if spec.role == "migration-child" else p.ResourceKind.RUNTIME_PROCESS
        scope.acquire(kind, lambda: c.events.append("stop:" + spec.role))
        return SimpleNamespace(poll=lambda: 0, stop=lambda deadline: True)
    def canonical(request):
        return c.descriptor if request.purpose == "descriptor" else c.canonical
    def journal(path, value):
        c.journals.append((path, value))
        c.events.append("intent" if type(value) is p.PublicationIntent else path.stem)
        if type(value) is p.PublicationIntent:
            c.intent = value
    def publish(path, publication):
        c.events.append("publish-prepare")
        c.on_publish()
        def commit():
            c.events.append("publish-commit")
            if path in c.receipts:
                raise FileExistsError("fake")
            c.receipts[path] = json.loads(json.dumps(publication.document))
        publication.commit(commit)
    def read(path):
        if path not in c.receipts:
            raise FileNotFoundError("fake")
        return c.receipts[path]
    c.effects = replace(c.effects, process_launcher=launch, canonical_reader=canonical,
        receipt_writer=SimpleNamespace(ownership=journal, publish=publish, read=read))
    return c


def successful_receipt():
    c = receipt_case()
    c.outcome = p.supervise(c.config, effects=c.effects)
    assert c.outcome.exit_code == 0, c.outcome
    c.document = c.receipts[c.outcome.receipt_path]
    c.expected = p.ReceiptBinding(c.config.run_id, c.plan.source, "fakekey")
    assert p.verify_receipt(c.outcome.receipt_path, expected=c.expected, effects=c.effects).ok
    return c


def verify_document(c, value):
    c.receipts[c.outcome.receipt_path] = value
    return p.verify_receipt(c.outcome.receipt_path, expected=c.expected, effects=c.effects)


def clone_document(value):
    return json.loads(json.dumps(value))


def disk_publication(c, tmp_path, monkeypatch):
    root = tmp_path / "owned-run"
    root.mkdir()
    (root / "artifacts").mkdir()
    info = root.stat()
    reservation = p.Reservation(root, root / "artifacts", root_identity=(info.st_dev, info.st_ino))
    intent = replace(c.intent, reservation=reservation,
        journal=replace(c.intent.journal, run_root=root, artifact_root=root / "artifacts"))
    writer = p._ReceiptWriter()
    writer.ownership(root / "publication-intent.json", intent)
    assert p._Host.scrub(reservation, keep_safe=False)
    return writer, root / "artifacts" / p.RECEIPT_NAME


def canary_world(value, capsys):
    from urllib.parse import quote
    c = receipt_case()
    c.retained.append(value)
    c.host.facts = replace(c.host.facts, environment={"PATH": "fake", "PGPASSWORD": value,
        "UNUSED_DATABASE_URL": value, "TOKEN": value, "CHECKOUT": value})
    allocate = c.effects.authority_factory
    def authority(run_id):
        result = allocate(run_id)
        result.dsn = lambda: value
        return result
    c.effects = replace(c.effects, authority_factory=authority)
    c.host.database_module = lambda environment: c.db
    c.issuer.private_key = value
    result = p.supervise(c.config, effects=c.effects)
    assert result.exit_code == 0 and not c.private and not c.retained
    event = p._RetainedOutput(p._approved_event)
    raw = json.dumps({"stage": "browser", "fixed_code": "BROWSER_EXIT", "diagnostic": value}).encode()
    event.feed(raw)
    retained = event.finish()
    assert retained == b""
    assert value not in repr(event)
    good = p._RetainedOutput(p._approved_event)
    good.feed(b'{"stage":"browser","fixed_code":"BROWSER_EXIT"}')
    assert json.loads(good.finish()) == {"stage": "browser", "fixed_code": "BROWSER_EXIT"}
    spec = p.ProcessSpec("runtime-build", ("fake",), ROOT,
        {"DATABASE_URL": value, "TOKEN": value, "CHECKOUT": value, "KEY": value}, 1)
    objects = (spec, spec.environment, p._SecretToken(value),
        p.CanonicalRequest(environment={"DATABASE_URL": value}),
        p.ReceiptBinding("run-test", c.plan.source, value))
    rendered = repr(objects) + repr(spec.argv) + repr(c.journals) + json.dumps(list(c.receipts.values()))
    assert value not in rendered and quote(value, safe="") not in rendered
    # The parser must not echo arbitrary subcommands, options, values or an
    # unambiguous prefix. Injection remains a Python API, not a CLI switch.
    for argv in ([value], ["supervise", "--run-id", "run-test", "--run-root", str(ROOT), "--effects", value],
                 ["supervise", "--run", value], ["verify-receipt", "--receipt", value]):
        assert p.main(argv, effects=c.effects) == 2
        output = capsys.readouterr()
        assert output.out == "" and output.err == "USAGE: Invalid project proof command.\n"
    # Raw exceptions can originate at any seam; no diagnostic string is kept.
    failed = receipt_case()
    failed.stack_ready["ports"] = dict(web=5275, app=8230, broker=8240, harness=8250)
    def crash(*args, **kwargs):
        raise RuntimeError(value)
    failed.effects = replace(failed.effects, browser_runner=crash)
    assert p.main(["supervise", "--run-id", "run-test", "--run-root", str(ROOT)], effects=failed.effects) == 1
    output = capsys.readouterr()
    assert output.out == "BROWSER_EXIT: Project proof command failed.\n" and output.err == ""
    assert value not in repr(failed.journals) + json.dumps(list(failed.receipts.values()))
    assert not failed.private and not failed.retained


class TestProjectReceipt:
    def test_success_after_cleanup(self, monkeypatch):
        c = successful_receipt()
        assert c.events.index("intent") < c.events.index("scrub") < c.events.index("disarm") < c.events.index("publish-commit")
        assert c.document["ok"] is True and c.document["failure"] is None
        assert all(c.document["rows"].values())
        assert all(c.document["cleanup"][name] for name in p._CLEANUP_FIELDS)
        assert not c.allocated and not c.private and all(lease.issuer is None for lease in c.leases)
        c = receipt_case()
        c.on_publish = lambda: c.run_scope.cancel()
        outcome = p.supervise(c.config, effects=c.effects)
        assert outcome.failure == p.Failure(p.Stage.VERIFICATION, p.FailureCode.CANCELLED)
        assert outcome.receipt_path is None and c.receipts == {} and not c.private
        c = receipt_case()
        c.host.facts = replace(c.host.facts, output_owned=False)
        outcome = p.supervise(c.config, effects=c.effects)
        assert outcome.exit_code != 0 and not c.journals and not c.receipts
        assert c.events == []
        # The private guardian entry receives only public ownership over a
        # fake pipe. Its adapter must actually reach D's guardian_main.
        files = ReceiptFiles()
        files.install(monkeypatch)
        root = files.root / "guardian"
        root.mkdir()
        info = root.stat()
        messages = [{"event": "init", "run_id": "run-test", "run_root": str(root),
            "root_identity": [info.st_dev, info.st_ino], "deadline": 10}, {"event": "disarm"}]
        output = io.StringIO()
        monkeypatch.setattr(p, "sys", SimpleNamespace(stdin=SimpleNamespace(buffer=io.BytesIO(
            b"".join((json.dumps(item) + "\n").encode() for item in messages))), stdout=output))
        monkeypatch.setattr(p, "threading", SimpleNamespace(Thread=lambda *, target, daemon: SimpleNamespace(start=target)))
        monkeypatch.setattr(p, "production_effects", lambda: c.effects)
        real_path = p.Path
        monkeypatch.setattr(p, "Path", lambda value: root if value == str(root) else real_path(value))
        calls = []
        def guardian(request, *, effects):
            calls.append(request.run_id)
            assert request.channel.receive(1) == "disarm"
            request.channel.acknowledge()
            return 0
        monkeypatch.setattr(p, "guardian_main", guardian)
        assert p._guardian_entry() == 0 and calls == ["run-test"] and output.getvalue() == "OK\nOK\n"

    def test_receipt_missing(self, capsys):
        c = successful_receipt()
        c.receipts.clear()
        result = p.verify_receipt(c.outcome.receipt_path, expected=c.expected, effects=c.effects)
        assert result == p.Verification(False, p.Failure(p.Stage.VERIFICATION, p.FailureCode.RECEIPT))
        with pytest.raises(Exception):
            result.ok = True
        assert p.main(["verify-receipt", "--run-id", "run-test", "--receipt", str(c.outcome.receipt_path)], effects=c.effects) == 1
        assert capsys.readouterr().out == "RECEIPT: Project proof command failed.\n"

    def test_receipt_malformed(self, monkeypatch):
        c = successful_receipt()
        for malformed in (None, [], {}, {"schema": p.SCHEMA}):
            assert not verify_document(c, malformed).ok
        for field, value in (("checkout_fence", "1"), ("settings_readback_sha256", "bad")):
            malformed = clone_document(c.document)
            malformed["canonical"][field] = value
            assert not verify_document(c, malformed).ok
        malformed = clone_document(c.document)
        malformed["timings"]["migration"] = float("nan")
        assert not verify_document(c, malformed).ok
        malformed = clone_document(c.document)
        malformed["stack"]["owned_process_identities"][1]["parent_pid"] = 999
        assert not verify_document(c, malformed).ok
        files = ReceiptFiles()
        files.install(monkeypatch)
        path = files.root / "receipt.json"
        for raw in (b"{broken", b'{"ok":true,"ok":false}', b"x" * (p.RECEIPT_LIMIT + 1)):
            path.write_bytes(raw)
            effects = replace(c.effects, receipt_writer=p._ReceiptWriter())
            assert not p.verify_receipt(path, expected=c.expected, effects=effects).ok

    def test_receipt_binding(self):
        c = successful_receipt()
        expected = (replace(c.expected, run_id="run-other"), replace(c.expected, profile="account"),
            replace(c.expected, schema=p.MIGRATION_SCHEMA), replace(c.expected, public_key_thumbprint="otherkey"),
            replace(c.expected, source=replace(c.plan.source, commit="d" * 40)),
            replace(c.expected, source=replace(c.plan.source, dirty_tree_sha256=OTHER_HASH)),
            replace(c.expected, source=replace(c.plan.source, tested_source_manifest_sha256=HASH)))
        for binding in expected:
            assert not p.verify_receipt(c.outcome.receipt_path, expected=binding, effects=c.effects).ok

    def test_receipt_unknown_fields(self, monkeypatch):
        c = successful_receipt()
        assert set(c.document) == p._RECEIPT_FIELDS
        for key in (None, "source", "identity", "database", "stack", "worker", "playwright", "rows", "canonical", "timings", "cleanup"):
            malformed = clone_document(c.document)
            (malformed if key is None else malformed[key])["diagnostic"] = "raw@key"
            assert not verify_document(c, malformed).ok
        files = ReceiptFiles()
        files.install(monkeypatch)
        writer, path = disk_publication(c, files.root, monkeypatch)
        malformed = dict(c.document, diagnostic="raw@key")
        with pytest.raises(p.ProofRefused):
            writer.publish(path, p._Publication(malformed, lambda commit: commit()))
        assert not path.exists()

    def test_receipt_no_clobber(self, monkeypatch):
        c = successful_receipt()
        files = ReceiptFiles()
        files.install(monkeypatch)
        writer, path = disk_publication(c, files.root, monkeypatch)
        events = []
        sync = p.os.fsync
        def fsync(fd):
            events.append("fsync")
            sync(fd)
        monkeypatch.setattr(p.os, "fsync", fsync)
        def race(commit):
            assert len(events) == 3  # Recreated intent, absence marker, sibling temp.
            path.write_bytes(b"older")
            events.append("race")
            return commit()
        with pytest.raises((OSError, p.ProofRefused)):
            writer.publish(path, p._Publication(c.document, race))
        assert path.read_bytes() == b"older" and events[-1] == "race"
        assert not path.with_name(path.name + ".pending").exists()
        with pytest.raises(p.ProofRefused):
            writer.publish(path.parent / "foreign.json", p._Publication(c.document, lambda commit: commit()))
        # The same production decision path uses fake no-replace filesystem effects.
        other = files.root / "control"
        other.mkdir()
        writer, path = disk_publication(c, other, monkeypatch)
        writer.publish(path, p._Publication(c.document, lambda commit: commit()))
        assert p.verify_receipt(path, expected=c.expected, effects=replace(c.effects, receipt_writer=writer)).ok

    def test_receipt_failed_rows(self):
        c = successful_receipt()
        for name in p.REQUIRED_ROWS:
            malformed = clone_document(c.document)
            malformed["rows"][name] = False
            assert not verify_document(c, malformed).ok
        for name in p._CLEANUP_FIELDS:
            malformed = clone_document(c.document)
            malformed["cleanup"][name] = False
            assert not verify_document(c, malformed).ok
        for key in ("runtime", "database", "stack", "worker", "playwright", "canonical"):
            malformed = clone_document(c.document)
            malformed[key] = None
            assert not verify_document(c, malformed).ok
        for status in ("skipped", "flaky", "failed"):
            malformed = clone_document(c.document)
            malformed["playwright"]["outcomes"][0]["status"] = status
            assert not verify_document(c, malformed).ok
        failed = receipt_case()
        def browser(*args, **kwargs):
            raise p.ProofRefused(p.Failure(p.Stage.BROWSER, p.FailureCode.BROWSER_EXIT))
        failed.effects = replace(failed.effects, browser_runner=browser)
        failed.host.scrub = lambda *args, **kwargs: False
        outcome = p.supervise(failed.config, effects=failed.effects)
        assert outcome.failure == p.Failure(p.Stage.BROWSER, p.FailureCode.BROWSER_EXIT)
        assert [item.fixed_code for item in outcome.cleanup_failures] == [p.FailureCode.CLEANUP_PRIVATE, p.FailureCode.GUARDIAN]
        assert not failed.receipts

    def test_migration_not_project(self):
        c = receipt_case()
        def forbidden(*args, **kwargs):
            pytest.fail("migration-only command acquired identity or a stack")
        c.effects = replace(c.effects, issuer_factory=forbidden, seed_operation=forbidden, browser_runner=forbidden)
        result = p.measure_migration(c.config, effects=c.effects)
        assert result.exit_code == 0 and result.receipt_path.name == p.MIGRATION_RECEIPT_NAME
        document = c.receipts[result.receipt_path]
        assert document["schema"] == p.MIGRATION_SCHEMA and document["ok"] is True
        assert document["rows"]["P04"] and document["rows"]["P10"] and not document["rows"]["P09"]
        assert document["identity"] is None and document["runtime"] is None
        assert all(spec.role == "migration-child" for spec in c.launches)
        assert not p.verify_receipt(result.receipt_path,
            expected=p.ReceiptBinding(c.config.run_id, c.plan.source, "fakekey"), effects=c.effects).ok
        assert not c.private and not c.allocated and c.mints == []

    def test_raw_dsn_canary(self, capsys):
        canary_world("db@:/pw", capsys)

    def test_encoded_dsn_canary(self, capsys):
        from urllib.parse import quote
        canary_world(quote("db@:/pw", safe=""), capsys)

    def test_bearer_canary(self, capsys):
        canary_world("br+/?x", capsys)

    def test_encoded_bearer_canary(self, capsys):
        from urllib.parse import quote
        canary_world(quote("br+/?x", safe=""), capsys)

    def test_capability_canary(self, capsys):
        canary_world("cp+/?x", capsys)

    def test_encoded_capability_canary(self, capsys):
        from urllib.parse import quote
        canary_world(quote("cp+/?x", safe=""), capsys)

    def test_private_key_canary(self, capsys):
        canary_world("key+/=", capsys)
        c = receipt_case()
        c.issuer.private_key = "key+/="
        outcome = p.supervise(c.config, effects=c.effects)
        assert outcome.exit_code == 0 and c.leases and all(lease.issuer is None for lease in c.leases)
        assert "key+/=" not in json.dumps(list(c.receipts.values())) + repr(c.journals)

    def test_split_chunk_canaries(self, capsys):
        from urllib.parse import quote
        values = ("db@:/pw", "br+/?x", "cp+/?x", "key+/=")
        for value in (*values, *(quote(item, safe="") for item in values)):
            payload = json.dumps({"stage": "browser", "fixed_code": "BROWSER_EXIT", "diagnostic": value}).encode()
            for split in range(1, len(value)):
                boundary = payload.index(value.encode()) + split
                output = p._RetainedOutput(p._approved_event)
                output.feed(payload[:boundary])
                output.feed(payload[boundary:])
                retained = output.finish()
                assert value.encode() not in retained
                assert retained == b""
                assert not output._raw
            canary_world(value, capsys)

    def test_bounded_output(self, monkeypatch):
        output = p._RetainedOutput(p._approved_event, limit=64)
        for _ in range(10000):
            output.feed(b"x" * 32)
            assert len(output._raw) <= 64
        assert output.overflow and output.finish() == b"" and not output._raw
        # Overflow cannot recover by accepting a later valid suffix.
        output.feed(b'{"stage":"browser","fixed_code":"BROWSER_EXIT"}')
        assert output.finish() == b""
        output = p._RetainedOutput(p._approved_event)
        output.feed(b'{"stage":"browser","fixed_code":"BROWSER_EXIT"}')
        assert len(output.finish()) <= p.MAX_OUTPUT_BYTES
        # Exercise the production launcher wiring with fake handles and pipes.
        sinks = []
        class Pipe:
            def __init__(self, spec):
                self.capture = p._RetainedOutput(lambda raw: p._child_public(spec, raw))
                self.closed, self.retained, self.launched_count = False, None, 0
                sinks.append(self)
            def launched(self):
                self.launched_count += 1
            def close(self):
                if not self.closed:
                    self.retained = self.capture.finish()
                    self.closed = True
        def spawn(argv, **kwargs):
            assert kwargs["stdout"] is sinks[0] and kwargs["stderr"] == p.subprocess.DEVNULL
            kwargs["stdout"].capture.feed(b'{"diagnostic":"db@:/pw"}')
            return SimpleNamespace(pid=77, poll=lambda: 0)
        monkeypatch.setattr(p, "_OutputPipe", Pipe)
        monkeypatch.setattr(p.subprocess, "Popen", spawn)
        monkeypatch.setattr(p, "os", SimpleNamespace(name="nt", getpid=lambda: 66))
        monkeypatch.setattr(p, "_WindowsJob", lambda: SimpleNamespace(attach_and_resume=lambda child: "start77"))
        child = p._RuntimeProcess(p.ProcessSpec("migration-child", ("fake",), ROOT, {}, 5, ROOT / "result.json"))
        child.start()
        assert child.poll() == 0 and sinks[0].launched_count == 1 and sinks[0].retained == b""

    def test_safe_retention(self, monkeypatch):
        files = ReceiptFiles()
        files.install(monkeypatch)
        for keep in (False, True):
            for fail in (False, True):
                c = receipt_case()
                c.config = replace(c.config, keep_run_root=keep)
                c.retained.extend(("db@:/pw", "br+/?x", "cp+/?x", "key+/="))
                if fail:
                    def crash(*args, **kwargs):
                        raise RuntimeError("key+/=")
                    c.effects = replace(c.effects, browser_runner=crash)
                outcome = p.supervise(c.config, effects=c.effects)
                assert (outcome.exit_code != 0) == fail
                assert not c.private and not c.retained and all(lease.issuer is None for lease in c.leases)
                assert all("key+/=" not in json.dumps(value) for value in c.receipts.values())
                root = files.root / (str(keep) + str(fail))
                root.mkdir()
                (root / "artifacts").mkdir()
                (root / "artifacts" / "raw.json").write_text("key+/=", encoding="utf-8")
                info = root.stat()
                reservation = p.Reservation(root, root / "artifacts", root_identity=(info.st_dev, info.st_ino))
                assert p._SupervisorHost().scrub(reservation, keep_safe=keep)
                assert not root.exists()
        c = receipt_case()
        c.host.recovery = lambda run_id, root: (c.plan.reservation, c.host.authority_inventory(run_id))
        c.host.recovery_processes_absent = lambda run_id, root: True
        proof = p.cleanup("run-test", run_root=ROOT, effects=c.effects)
        assert proof.ok and not c.private and not c.allocated and c.receipts == {}
        c = receipt_case()
        c.host.recovery = lambda run_id, root: (c.plan.reservation, c.host.authority_inventory(run_id))
        c.host.recovery_processes_absent = lambda run_id, root: False
        proof = p.cleanup("run-test", run_root=ROOT, effects=c.effects)
        assert not proof.ok and proof.failures[0].fixed_code == p.FailureCode.CLEANUP_PROCESS
        assert c.private and c.receipts == {} and "remove-authority" not in c.events
