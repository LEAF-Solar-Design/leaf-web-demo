"""Pure CLI checks and real PostgreSQL coverage of the pinned administrative rail."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, current_thread
import time
from uuid import uuid4

import pytest

from leaf_platform import native_service_binding as native


def test_arguments():
    assert native.parse_mode([]) == "plan"
    for mode in ("plan", "apply", "revoke"):
        assert native.parse_mode(["--mode", mode]) == mode
    for args in (["--help"], ["--mode", "prod"], ["--org", "other"],
                 ["--mode=apply"], ["--mo", "apply"], ["apply"],
                 ["--mode", "plan", "--mode", "apply"]):
        with pytest.raises(native.Refused, match="arguments"):
            native.parse_mode(args)


def _output(capsys):
    captured = capsys.readouterr()
    assert captured.err == ""
    assert len(captured.out.splitlines()) == 1
    assert captured.out.startswith(native.PREFIX)
    return json.loads(captured.out[len(native.PREFIX):])


@pytest.mark.parametrize("environment", [None, "production", "Staging", ""])
def test_environment_refuses_before_database(monkeypatch, capsys, environment):
    if environment is None:
        monkeypatch.delenv("LEAF_RUNTIME_ENV", raising=False)
    else:
        monkeypatch.setenv("LEAF_RUNTIME_ENV", environment)
    monkeypatch.setattr(native, "_store", lambda: pytest.fail("must not access DB"))
    assert native.main(["--mode", "apply"]) == 2
    result = _output(capsys)
    assert result["outcome"] == "refused"
    assert result["reason"] == "environment"


def test_receipt_shape_and_no_secrets(monkeypatch, capsys):
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "staging")

    def fail():
        raise RuntimeError(" ".join(("postgresql://user:" + "pass" + "word@host/db", "bear" + "er", "secret-" + "token")))

    monkeypatch.setattr(native, "_store", fail)
    assert native.main([]) == 2
    result = _output(capsys)
    assert set(result) == {
        "schema", "mode", "outcome", "reason", "binding_id", "tenant_role",
        "membership_id", "project_role", "org_id", "project_id", "subject_suffix",
        "time", "platform_user_id_present",
    }
    assert result["schema"] == "leaf.native-service-binding.v1"
    assert result["reason"] == "database"
    assert result["subject_suffix"] == "@clients"
    assert result["platform_user_id_present"] is False
    for secret in ("password", "postgresql://", "secret-token", native.SUBJECT):
        assert secret not in json.dumps(result)


def test_invalid_arguments_emit_only_sanitized_receipt(capsys):
    assert native.main(["--token", "secret-token"]) == 2
    result = _output(capsys)
    assert result["reason"] == "arguments"
    assert "secret-token" not in json.dumps(result)


def test_app_image_entry_preserves_stdlib_platform(tmp_path):
    # The image's final WORKDIR is /app/server; -P keeps the sibling directory
    # off sys.path, and the entry establishes the same alias as server/app.py.
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env["LEAF_RUNTIME_ENV"] = "production"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    entry = Path(native.__file__).resolve()
    process = subprocess.run(
        [sys.executable, "-P", str(entry), "--mode", "plan"],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=False,
    )
    assert process.returncode == 2
    assert process.stderr == ""
    assert len(process.stdout.splitlines()) == 1
    assert process.stdout.startswith(native.PREFIX)
    assert json.loads(process.stdout[len(native.PREFIX):])["reason"] == "environment"


@pytest.fixture
def rail(make_org, monkeypatch):
    # Reuse the repository's migration and fresh-org fixtures. Never touch the
    # approved live org/subject even when this suite uses a shared PostgreSQL DB.
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("PostgreSQL integration test requires DATABASE_URL")
    from leaf_platform import store
    org = make_org(name="Native service binding test")
    project = store.create_project(org.org_id, "Approved test project")
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "staging")
    monkeypatch.setattr(native, "ORG", org.org_id)
    monkeypatch.setattr(native, "PROJECT", project.project_id)
    monkeypatch.setattr(native, "SUBJECT", f"{uuid4()}@clients")
    return store


def _snapshot(store):
    with store.cursor() as cur:
        cur.execute("SELECT *, xmin::text AS row_version FROM identity_bindings "
                    "WHERE external_subject = %s ORDER BY binding_id", (native.SUBJECT,))
        bindings = cur.fetchall()
        cur.execute("SELECT *, xmin::text AS row_version FROM project_member_bindings "
                    "WHERE org_id = %s ORDER BY membership_id", (native.ORG,))
        memberships = cur.fetchall()
        cur.execute("SELECT * FROM project_lifecycle_receipts WHERE org_id = %s "
                    "ORDER BY receipt_id", (native.ORG,))
        return bindings, memberships, cur.fetchall()


def test_create_and_replay_without_any_write(rail):
    created = native.execute("apply")
    assert created["outcome"] == "created"
    assert created["tenant_role"] == "read_only"
    assert created["project_role"] == "editor"
    assert created["platform_user_id_present"] is True
    before = _snapshot(rail)
    bindings, memberships, audit = before
    assert len(bindings) == len(memberships) == len(audit) == 1
    assert memberships[0]["invited_by_binding_id"] == bindings[0]["binding_id"]
    assert audit[0]["actor_binding_id"] == bindings[0]["binding_id"]
    assert audit[0]["result_json"]["provenance"] == native.PROVENANCE
    assert audit[0]["action"] == "member_invited"
    replay = native.execute("apply")
    assert replay["outcome"] == "unchanged"
    assert replay["binding_id"] == created["binding_id"]
    assert replay["membership_id"] == created["membership_id"]
    assert _snapshot(rail) == before
    assert native.execute("plan")["outcome"] == "unchanged"
    assert _snapshot(rail) == before


@pytest.mark.parametrize("drift", ["role", "tenant", "other_authority"])
def test_binding_drift_is_read_only(rail, make_org, drift):
    org = native.ORG if drift == "role" else make_org().org_id
    authority = "other" if drift == "other_authority" else native.AUTHORITY
    rail.create_identity_binding(org, authority, native.SUBJECT,
                                 role="editor" if drift == "role" else native.TENANT_ROLE)
    before = _snapshot(rail)
    for mode in ("plan", "apply", "revoke"):
        with pytest.raises(native.Refused, match="binding_drift"):
            native.execute(mode)
        assert _snapshot(rail) == before


def test_membership_drift_is_read_only(rail):
    native.execute("apply")
    with rail.cursor() as cur:
        cur.execute("UPDATE project_member_bindings SET role = 'owner' WHERE org_id = %s",
                    (native.ORG,))
    before = _snapshot(rail)
    for mode in ("plan", "apply", "revoke"):
        with pytest.raises(native.Refused, match="membership_drift"):
            native.execute(mode)
        assert _snapshot(rail) == before


@pytest.mark.parametrize("linkage", ["other_subject", "other_authority", "other_tenant"])
def test_foreign_user_linkage_is_read_only(rail, make_org, linkage):
    created = native.execute("apply")
    with rail.cursor() as cur:
        cur.execute("SELECT platform_user_id FROM identity_bindings WHERE binding_id = %s",
                    (created["binding_id"],))
        user_id = cur.fetchone()["platform_user_id"]
    org = make_org().org_id if linkage == "other_tenant" else native.ORG
    rail.create_identity_binding(
        org, "other" if linkage == "other_authority" else native.AUTHORITY,
        native.SUBJECT if linkage == "other_authority" else f"{uuid4()}@clients",
        user_id, role=native.TENANT_ROLE,
    )
    before = _snapshot(rail)
    for mode in ("plan", "apply", "revoke"):
        with pytest.raises(native.Refused, match="binding_drift"):
            native.execute(mode)
        assert _snapshot(rail) == before


def test_foreign_membership_provenance_is_read_only(rail):
    native.execute("apply")
    inviter = rail.create_identity_binding(
        native.ORG, native.AUTHORITY, f"{uuid4()}@clients", role=native.TENANT_ROLE)
    with rail.cursor() as cur:
        cur.execute("UPDATE project_member_bindings SET invited_by_binding_id = %s "
                    "WHERE org_id = %s", (inviter.binding_id, native.ORG))
    before = _snapshot(rail)
    for mode in ("plan", "apply", "revoke"):
        with pytest.raises(native.Refused, match="membership_drift"):
            native.execute(mode)
        assert _snapshot(rail) == before


@pytest.mark.parametrize("status", ["missing", "offboarding", "deleted"])
def test_org_inactive_is_read_only(rail, monkeypatch, status):
    if status == "missing":
        monkeypatch.setattr(native, "ORG", uuid4())
    else:
        native.execute("apply")
        with rail.cursor() as cur:
            cur.execute("UPDATE orgs SET status = %s WHERE org_id = %s", (status, native.ORG))
    before = _snapshot(rail)
    for mode in ("plan", "apply", "revoke"):
        with pytest.raises(native.Refused, match="org_inactive"):
            native.execute(mode)
        assert _snapshot(rail) == before


def test_other_project_refuses(rail):
    native.execute("apply")
    other = rail.create_project(native.ORG, "Out of scope")
    with rail.cursor() as cur:
        cur.execute("INSERT INTO project_member_bindings "
                    "(membership_id, org_id, project_id, binding_id, role, invited_by_binding_id) "
                    "SELECT %s, org_id, %s, binding_id, role, invited_by_binding_id "
                    "FROM project_member_bindings WHERE org_id = %s",
                    (uuid4(), other.project_id, native.ORG))
    before = _snapshot(rail)
    for mode in ("plan", "apply", "revoke"):
        with pytest.raises(native.Refused, match="other_project"):
            native.execute(mode)
        assert _snapshot(rail) == before


def test_plan_writes_nothing(rail):
    before = _snapshot(rail)
    assert native.execute("plan")["outcome"] == "would_create"
    assert _snapshot(rail) == before


def test_revoke_readback_and_reapply(rail):
    created = native.execute("apply")
    revoked = native.execute("revoke")
    assert revoked["outcome"] == "revoked"
    assert revoked["binding_id"] == created["binding_id"]
    assert revoked["membership_id"] == created["membership_id"]
    bindings, memberships, audit = _snapshot(rail)
    assert bindings[0]["status"] == memberships[0]["status"] == "revoked"
    assert bindings[0]["revoked_at"] is not None
    assert memberships[0]["revoked_at"] is not None
    assert {row["action"] for row in audit} == {"member_invited", "member_revoked"}
    before = _snapshot(rail)
    assert native.execute("revoke")["outcome"] == "unchanged"
    assert _snapshot(rail) == before
    assert native.execute("apply")["outcome"] == "created"


@pytest.mark.parametrize("invalid", ["archived", "deleted_at", "wrong_org"])
def test_project_checks_before_write(rail, make_org, invalid):
    with rail.cursor() as cur:
        if invalid == "deleted_at":
            cur.execute("UPDATE projects SET deleted_at = NOW() WHERE project_id = %s",
                        (native.PROJECT,))
        elif invalid == "wrong_org":
            other = make_org()
            cur.execute("UPDATE projects SET org_id = %s WHERE project_id = %s",
                        (other.org_id, native.PROJECT))
        else:
            cur.execute("UPDATE projects SET status = 'archived' WHERE project_id = %s",
                        (native.PROJECT,))
    before = _snapshot(rail)
    with pytest.raises(native.Refused, match="project"):
        native.execute("apply")
    assert _snapshot(rail) == before


def test_failure_after_insert_rolls_back_whole_transaction(rail, monkeypatch):
    before = _snapshot(rail)

    def fail(*args):
        raise native.Refused("readback")

    monkeypatch.setattr(native, "_audit", fail)
    with pytest.raises(native.Refused, match="readback"):
        native.execute("apply")
    assert _snapshot(rail) == before


def test_second_apply_waits_on_locks_then_is_unchanged(rail, monkeypatch):
    inserted = Event()
    release = Event()
    second_connected = Event()
    pids = []
    real_writer = rail._create_identity_binding_on_cursor
    real_cursor = rail.cursor

    def writer(*args, **kwargs):
        binding = real_writer(*args, **kwargs)
        inserted.set()
        assert release.wait(10), "test did not release first transaction"
        return binding

    @contextmanager
    def cursor():
        with real_cursor() as cur:
            if current_thread().name.startswith("second"):
                pids.append(cur.connection.info.backend_pid)
                second_connected.set()
            yield cur

    monkeypatch.setattr(rail, "_create_identity_binding_on_cursor", writer)
    monkeypatch.setattr(rail, "cursor", cursor)
    with ThreadPoolExecutor(1, thread_name_prefix="first") as first_pool, \
            ThreadPoolExecutor(1, thread_name_prefix="second") as second_pool:
        first = first_pool.submit(native.execute, "apply")
        try:
            assert inserted.wait(10)
            second = second_pool.submit(native.execute, "apply")
            assert second_connected.wait(10)
            deadline = time.monotonic() + 5
            blocked = False
            while time.monotonic() < deadline:
                with real_cursor() as cur:
                    cur.execute("SELECT cardinality(pg_blocking_pids(%s)) > 0 AS blocked",
                                (pids[0],))
                    blocked = cur.fetchone()["blocked"]
                if blocked:
                    break
                time.sleep(0.01)
            assert blocked, "second apply never waited on a PostgreSQL lock"
            assert not second.done()
        finally:
            release.set()
        assert first.result(timeout=10)["outcome"] == "created"
        assert second.result(timeout=10)["outcome"] == "unchanged"
    bindings, memberships, audit = _snapshot(rail)
    assert len(bindings) == len(memberships) == len(audit) == 1
