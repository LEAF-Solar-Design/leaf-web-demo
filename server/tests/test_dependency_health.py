from __future__ import annotations

import json
import threading
import time

import dependency_health


def _spec(name, required=True, probe=lambda: None):
    return dependency_health.DependencySpec(name, required, probe)


def test_all_success_is_ready_and_preserves_dependency_order():
    report = dependency_health.readiness_report(
        {"LEAF_READINESS_TIMEOUT_S": "0.2", "LEAF_BUILD_REVISION": "abcdef123456"},
        [_spec("broker"), _spec("harness", False), _spec("database")],
    )
    assert report["ready"] is True
    assert report["status"] == "ready"
    assert list(report["dependencies"]) == ["broker", "harness", "database"]
    assert {item["state"] for item in report["dependencies"].values()} == {"ready"}


def test_required_timeout_is_bounded_and_not_ready():
    release = threading.Event()
    started = time.monotonic()
    report = dependency_health.readiness_report(
        {"LEAF_READINESS_TIMEOUT_S": "0.05"},
        [_spec("broker", probe=lambda: release.wait(0.5))],
    )
    release.set()
    assert time.monotonic() - started < 0.2
    assert report["ready"] is False
    assert report["dependencies"]["broker"]["state"] == "timeout"


def test_required_unavailable_is_not_ready_without_exception_text():
    secret = "postgresql://user:password@private.example/leaf"

    def fail():
        raise RuntimeError(secret)

    report = dependency_health.readiness_report(
        {"LEAF_READINESS_TIMEOUT_S": "0.2"}, [_spec("database", probe=fail)])
    encoded = json.dumps(report)
    assert report["status"] == "unavailable"
    assert report["dependencies"]["database"]["state"] == "unavailable"
    assert "password" not in encoded
    assert "private.example" not in encoded


def test_optional_degraded_keeps_readiness_true():
    def degraded():
        raise dependency_health.DependencyDegraded()

    report = dependency_health.readiness_report(
        {"LEAF_READINESS_TIMEOUT_S": "0.2"},
        [_spec("broker"), _spec("harness", False, degraded)],
    )
    assert report["ready"] is True
    assert report["status"] == "degraded"
    assert report["dependencies"]["harness"] == {
        "state": "degraded", "required": False,
        "latency_ms": report["dependencies"]["harness"]["latency_ms"],
    }


def test_source_revision_precedence_and_invalid_value_redaction():
    valid = dependency_health.readiness_report(
        {
            "LEAF_READINESS_TIMEOUT_S": "0.2",
            "LEAF_BUILD_REVISION": "abcdef123456",
            "GIT_COMMIT": "deadbee",
        },
        [_spec("build", False)],
    )
    invalid = dependency_health.readiness_report(
        {
            "LEAF_READINESS_TIMEOUT_S": "0.2",
            "LEAF_BUILD_REVISION": "secret value with spaces",
            "GIT_COMMIT": "1234567",
        },
        [_spec("build", False)],
    )
    assert valid["source_revision"] == "abcdef123456"
    assert invalid["source_revision"] == "1234567"
    assert "secret value" not in json.dumps(invalid)
    digest = dependency_health.readiness_report(
        {
            "LEAF_READINESS_TIMEOUT_S": "0.2",
            "SOURCE_REVISION": "sha256:" + "a" * 64,
        },
        [_spec("build", False)],
    )
    assert digest["source_revision"] == "sha256:" + "a" * 64

    image_source = dependency_health.readiness_report(
        {
            "LEAF_READINESS_TIMEOUT_S": "0.2",
            "LEAF_SOURCE_SHA": "b" * 40,
        },
        [_spec("build", True)],
    )
    assert image_source["ready"] is True
    assert image_source["source_revision"] == "b" * 40


def test_default_contract_has_all_dependency_classes_without_targets(monkeypatch):
    env = {
        "BROKER_URL": "http://broker.internal:8140",
        "LEAF_AUTHOR_HARNESS_URL": "http://harness.internal:8150",
        "DATABASE_URL": "postgresql://user:password@db.internal/leaf",
        "JOBS_DB": "C:/secret/jobs.db",
        "LEAF_STORE_DIR": "C:/secret/drawings",
        "LEAF_BUILD_REVISION": "abcdef123456",
        "LEAF_READINESS_TIMEOUT_S": "0.2",
    }
    monkeypatch.setattr(dependency_health, "_http_probe", lambda *_args: None)
    monkeypatch.setattr(dependency_health, "_database_probe", lambda *_args: None)
    monkeypatch.setattr(dependency_health, "_worker_probe", lambda *_args: None)
    monkeypatch.setattr(dependency_health, "_durable_stores_probe", lambda *_args: None)
    report = dependency_health.readiness_report(env)
    assert list(report["dependencies"]) == [
        "broker", "harness", "database", "worker", "durable_stores", "build",
    ]
    encoded = json.dumps(report)
    for forbidden in (
            "broker.internal", "harness.internal", "db.internal",
            "password", "C:/secret"):
        assert forbidden not in encoded


def test_explicit_production_gates_mark_optional_dependencies_required():
    specs = dependency_health.default_specs({
        "LEAF_AUTHORED_EXECUTION": "1",
        "LEAF_PLATFORM_POSTGRES_REQUIRED": "true",
        "LEAF_CANONICAL_WORKER_REQUIRED": "yes",
        "LEAF_BUILD_REVISION_REQUIRED": "on",
    }, timeout=0.1)
    required = {spec.name: spec.required for spec in specs}
    assert required == {
        "broker": True,
        "harness": True,
        "database": True,
        "worker": True,
        "durable_stores": True,
        "build": True,
    }


def test_worker_revision_compares_only_explicit_immutable_expectation(monkeypatch):
    observed = []
    monkeypatch.setattr(dependency_health, "_http_probe", lambda *_args: None)
    monkeypatch.setattr(dependency_health, "_database_probe", lambda *_args: None)
    monkeypatch.setattr(dependency_health, "_durable_stores_probe", lambda *_args: None)
    monkeypatch.setattr(
        dependency_health, "_worker_probe",
        lambda _timeout, _tool, _age, expected: observed.append(expected))
    base = {
        "DATABASE_URL": "postgresql://user:password@db.internal/leaf",
        "LEAF_BUILD_REVISION": "abcdef123456",
        "LEAF_READINESS_TIMEOUT_S": "0.2",
    }
    dependency_health.readiness_report(base)
    dependency_health.readiness_report({
        **base, "LEAF_EXPECTED_WORKER_REVISION": "deadbee",
    })
    assert observed == [None, "deadbee"]


def test_durable_store_requires_exact_mount_and_runs_clean_sentinel_cycle(tmp_path):
    directories = {
        name: tmp_path / name
        for name in ("jobs", "drawings", "guest", "agent", "tenants")
    }
    for directory in directories.values():
        directory.mkdir()
    env = {
        "JOBS_DB": str(directories["jobs"] / "jobs.db"),
        "LEAF_STORE_DIR": str(directories["drawings"]),
        "LEAF_GUEST_STORE_DIR": str(directories["guest"]),
        "LEAF_AGENT_STATE_DIR": str(directories["agent"]),
        "LEAF_TENANTS_DIR": str(directories["tenants"]),
    }
    dependency_health._durable_stores_probe(env)
    assert all(list(directory.iterdir()) == [] for directory in directories.values())
    missing = tmp_path / "missing-mount"
    env["LEAF_TENANTS_DIR"] = str(missing)
    try:
        dependency_health._durable_stores_probe(env)
    except dependency_health.DependencyUnavailable:
        pass
    else:
        raise AssertionError("a writable ancestor accepted an absent configured mount")
    assert not missing.exists()


def test_durable_store_initializes_only_guest_subdirectory(tmp_path):
    directories = {
        name: tmp_path / name
        for name in ("jobs", "drawings", "agent", "tenants")
    }
    for directory in directories.values():
        directory.mkdir()
    guest = directories["drawings"] / "guest"
    env = {
        "JOBS_DB": str(directories["jobs"] / "jobs.db"),
        "LEAF_STORE_DIR": str(directories["drawings"]),
        "LEAF_GUEST_STORE_DIR": str(guest),
        "LEAF_AGENT_STATE_DIR": str(directories["agent"]),
        "LEAF_TENANTS_DIR": str(directories["tenants"]),
    }

    dependency_health._durable_stores_probe(env)

    assert guest.is_dir()
    assert list(guest.iterdir()) == []


def test_repeated_timeout_calls_keep_process_probe_threads_bounded():
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def held_probe():
        nonlocal calls
        with calls_lock:
            calls += 1
        release.wait(5)

    specs = [_spec("broker", probe=held_probe)]
    env = {"LEAF_READINESS_TIMEOUT_S": "0.05"}
    for _ in range(dependency_health._PROBE_WORKERS):
        dependency_health.readiness_report(env, specs)
    before = [
        thread for thread in threading.enumerate()
        if thread.name.startswith("leaf-ready-probe")
    ]
    for _ in range(10):
        dependency_health.readiness_report(env, specs)
    after = [
        thread for thread in threading.enumerate()
        if thread.name.startswith("leaf-ready-probe")
    ]
    slots_before_release = dependency_health._probe_slot_snapshot()[0]
    release.set()
    deadline = time.monotonic() + 1
    while dependency_health._probe_slot_snapshot()[0] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(before) == dependency_health._PROBE_WORKERS
    assert len(after) == len(before)
    assert calls == dependency_health._PROBE_WORKERS
    assert slots_before_release == dependency_health._PROBE_WORKERS
    assert dependency_health._probe_slot_snapshot()[0] == 0


def test_saturated_bulkhead_classifies_timeout_without_executor_submit(monkeypatch):
    acquired = 0
    try:
        for _ in range(dependency_health._PROBE_WORKERS):
            assert dependency_health._try_acquire_probe_slot() is True
            acquired += 1
        submits = 0

        def forbidden_submit(*_args, **_kwargs):
            nonlocal submits
            submits += 1
            raise AssertionError("saturated probe was enqueued")

        monkeypatch.setattr(dependency_health._PROBE_EXECUTOR, "submit", forbidden_submit)
        report = dependency_health.readiness_report(
            {"LEAF_READINESS_TIMEOUT_S": "0.05"}, [_spec("broker")])
        assert report["dependencies"]["broker"]["state"] == "timeout"
        assert submits == 0
        in_use, high_water = dependency_health._probe_slot_snapshot()
        assert in_use == dependency_health._PROBE_WORKERS
        assert high_water == dependency_health._PROBE_WORKERS
    finally:
        for _ in range(acquired):
            dependency_health._release_probe_slot()


def test_configured_harness_is_required_and_outage_blocks_readiness(monkeypatch):
    env = {
        "BROKER_URL": "http://broker.internal:8140",
        "LEAF_CONVERSE_HARNESS_URL": "http://harness.internal:8150",
        "LEAF_BUILD_REVISION": "abcdef123456",
        "LEAF_READINESS_TIMEOUT_S": "0.2",
    }

    def http_probe(url, _timeout):
        if "harness.internal" in url:
            raise dependency_health.DependencyUnavailable()

    monkeypatch.setattr(dependency_health, "_http_probe", http_probe)
    monkeypatch.setattr(dependency_health, "_durable_stores_probe", lambda *_args: None)
    report = dependency_health.readiness_report(env)
    assert report["dependencies"]["harness"] == {
        "state": "unavailable",
        "required": True,
        "latency_ms": report["dependencies"]["harness"]["latency_ms"],
    }
    assert report["ready"] is False
    assert report["status"] == "unavailable"


def test_ready_route_is_separate_from_unchanged_liveness(monkeypatch):
    import app

    original_health = app.health()
    monkeypatch.setattr(
        app.dependency_health,
        "readiness_report",
        lambda: {
            "ok": False,
            "ready": False,
            "status": "unavailable",
            "dependencies": {
                "broker": {"state": "unavailable", "required": True, "latency_ms": 1},
            },
            "source_revision": None,
            "duration_ms": 1,
        },
    )
    response = app.ready()
    body = json.loads(response.body)
    assert response.status_code == 503
    assert body["degraded_mode"] is True
    assert app.health() == original_health
    assert original_health["ok"] is True
    assert set(original_health) == {
        "ok", "aps_live", "data_file_present", "engine_registry_present",
        "da_client_present", "n_tools", "n_authored", "error", "degraded_mode",
        "source_sha",
    }
    paths = set(app.app.openapi()["paths"])
    assert "/api/ready" in paths
    assert "/api/projects" in paths


def test_liveness_ignores_unsupported_shared_customization_while_disabled(
        monkeypatch):
    import app

    monkeypatch.setenv("LEAF_CUSTOMIZATION_DB", "/data/state/customization.db")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "off")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "off")

    response = app.health()

    assert response["ok"] is True
    assert response["n_tools"] > 0
