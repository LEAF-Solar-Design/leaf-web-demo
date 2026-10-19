"""Production posture coverage without a database, network, or app import."""
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


SERVER = Path(__file__).resolve().parents[1]
CHECKS = (
    (None, "validate_postgres_required"),
    ("platform_link", "postgres_authorities_selected"),
    ("platform_link", "validate_canonical_upload_authority"),
    ("platform_link", "validate_session_annex_authority"),
    (None, "auth_live"),
    (None, "validate_database_url"),
    ("jobs", "validate_store_startup"),
    ("guest_uploads", "upload_store_mode"),
    ("write_loop", "blob_store_mode"),
)


@pytest.fixture
def probe(monkeypatch):
    # Match the flat-import image entrypoint, retaining stdlib/site-packages.
    monkeypatch.setattr(sys, "path", [str(SERVER)] + [
        p for p in sys.path if p not in {str(SERVER), str(SERVER.parent), ""}
    ])
    monkeypatch.delitem(sys.modules, "app", raising=False)
    spec = importlib.util.spec_from_file_location("production_boot_probe", SERVER / "boot_probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert sys.path[0] == str(SERVER)
    assert str(SERVER.parent) not in sys.path
    # This is the only validator that can contact a DB. The Step 1 pin is
    # legacy; exercise the real other posture validators, not mirrored rules.
    jobs = importlib.import_module("jobs")
    monkeypatch.setattr(jobs, "validate_store_startup", lambda: None)
    return module


@pytest.fixture
def env():
    fixture = json.loads((SERVER / "tests/fixtures/production_app_env_shape.json").read_text())
    shape = fixture["production_app_env"]
    # Synthetic placeholders represent presence only, never production values.
    return {
        **dict.fromkeys(shape["env_names_value_withheld"], "withheld"),
        **dict.fromkeys(shape["secret_names"], "synthetic-secret-value"),
        **shape["env_values"], **fixture["pins"],
    }


def status(pending=(), drift=(), unknown=(), **extra):
    return {"ok": not (pending or drift or unknown),
            "missing_migrations": list(pending), "migration_hash_drift": list(drift),
            "unknown_migrations": list(unknown), **extra}


def result(capsys, code):
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 1
    payload = json.loads(output)
    assert set(payload) == {"probe", "ok", "exit", "failures", "schema"}
    assert payload["probe"] == "boot"
    assert payload["exit"] == code
    assert payload["ok"] is (code == 0)
    assert code not in (2, 3)
    for failure in payload["failures"]:
        assert set(failure) == {"check", "error"}
        assert len(failure["error"]) <= 300
    return payload


@pytest.mark.parametrize("schema,expected", [
    (status(), 0),
    (status(pending=["0070.sql", "0075.sql"]), 0),
    (status(drift=["0070.sql"]), 10),
    (status(pending=["0075.sql"], drift=["0070.sql"]), 10),
    (status(unknown=["0099.sql"]), 10),
    (status(ok=False, missing={"jobs": ["job_id"]}), 10),
])
def test_production_shape_schema_outcomes(probe, env, capsys, schema, expected):
    before = dict(os.environ)
    path = list(sys.path)
    code = probe.run(env, lambda: schema)
    assert code == expected
    payload = result(capsys, code)
    assert payload["schema"] == {
        "pending": schema["missing_migrations"],
        "drift": sorted(schema["migration_hash_drift"] + schema["unknown_migrations"]),
    }
    assert dict(os.environ) == before
    assert sys.path == path
    assert "app" not in sys.modules


@pytest.mark.parametrize("module,name", CHECKS)
def test_each_posture_failure_is_named_and_checks_continue(
        probe, env, monkeypatch, capsys, module, name):
    calls = []
    for check_module, check_name in CHECKS:
        target = importlib.import_module(check_module) if check_module else probe

        def validator(check_name=check_name):
            calls.append(check_name)
            if check_name == name:
                print(env["LEAF_BROKER_SECRET"])
                raise RuntimeError(env["LEAF_BROKER_SECRET"] * 100)

        monkeypatch.setattr(target, check_name, validator)
    code = probe.run(env, lambda: status(pending=["0075.sql"]))
    assert code == 10
    payload = result(capsys, code)
    assert [failure["check"] for failure in payload["failures"]] == [name]
    assert calls == [check_name for _, check_name in CHECKS]
    assert env["LEAF_BROKER_SECRET"] not in json.dumps(payload)
    assert "app" not in sys.modules


@pytest.mark.parametrize("changes,check", [
    ({"LEAF_PLATFORM_POSTGRES_REQUIRED": "0"}, "validate_postgres_required"),
    ({"LEAF_AUTH_LIVE": "0"}, "auth_live"),
    ({"DATABASE_URL": ""}, "validate_database_url"),
    ({"LEAF_UPLOAD_STORE": "legacy"}, "validate_canonical_upload_authority"),
    ({"LEAF_SESSIONS_STORE": "postgres"}, "validate_session_annex_authority"),
    ({"LEAF_AGENT_STORE": "typo"}, "postgres_authorities_selected"),
    ({"LEAF_BLOB_STORE": "typo"}, "blob_store_mode"),
])
def test_real_posture_rejections(probe, env, capsys, changes, check):
    code = probe.run({**env, **changes}, lambda: status())
    assert code == 10
    assert check in [failure["check"] for failure in result(capsys, code)["failures"]]


def test_schema_exception_does_not_disclose_values(probe, env, capsys):
    def unavailable():
        print(env["DATABASE_URL"])
        raise RuntimeError(env["DATABASE_URL"])

    code = probe.run(env, unavailable)
    assert code == 10
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 1
    assert env["DATABASE_URL"] not in output
    assert json.loads(output)["failures"][0]["check"] == "schema_status"


def test_default_schema_reader_detects_unknown_ledger_names(probe, monkeypatch):
    db = SimpleNamespace(
        schema_status=lambda: status(pending=["0075.sql"]),
        migration_manifest=lambda: [{"name": "0075.sql"}],
        _MIGRATION_LEDGER_TABLE="leaf_schema_migrations",
        get_pool=MagicMock(),
    )
    cursor = db.get_pool.return_value.connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [{"name": "0099.sql"}]
    monkeypatch.setattr(importlib.import_module("platform_link"), "platform_db", lambda: db)
    assert probe._schema_status()["unknown_migrations"] == ["0099.sql"]
    cursor.execute.assert_called_once_with("SELECT name FROM leaf_schema_migrations ORDER BY name")


def test_main_exits_with_run_code(probe, env, monkeypatch):
    monkeypatch.setattr(os, "environ", env)
    seen = []
    monkeypatch.setattr(probe, "run", lambda received: seen.append(received) or 10)
    with pytest.raises(SystemExit) as raised:
        probe.main()
    assert raised.value.code == 10
    assert seen == [env]
    assert seen[0] is not env
