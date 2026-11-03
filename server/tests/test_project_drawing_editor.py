"""Mounted browser-editor custody proof on an explicitly private PostgreSQL."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from uuid import UUID, uuid4

# This cross-package private-PG proof runs from platform/ as well as server/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
import psycopg
import pytest

import checkout_capability
import deps
import dxf_intake
import platform_link
import project_drawing_editor as editor
import solar_project_context as project_context
from envelopes import install_error_handlers
from routers import project_drawings
from test_w1_design_graph import graph

platform_link._ensure_platform_package()
from leaf_platform import db, store


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def drawing(x=120):
    rows = ["0", "SECTION", "2", "ENTITIES"]
    for index in range(74):
        px = x if index == 0 else 120 + index * 60
        py = 126 if index == 0 and x != 120 else 120
        rows.extend(["0", "LWPOLYLINE", "5", f"{0xA100 + index:X}", "8", "Panels",
                     "90", "4", "70", "1", "10", str(px), "20", str(py),
                     "10", str(px + 40), "20", str(py), "10", str(px + 40),
                     "20", str(py + 78), "10", str(px), "20", str(py + 78)])
    for index in range(6):
        rows.extend(["0", "TEXT", "5", f"{0xB100 + index:X}", "8", "Labels",
                     "10", str(index * 80), "20", "650", "40", "8", "1", f"Label {index}"])
    return ("\n".join(rows + ["0", "ENDSEC", "0", "EOF"]) + "\n").encode()


class BytesStore:
    def __init__(self):
        self.data = {}
    def exists(self, key):
        return key in self.data
    def get(self, key):
        return self.data[key]
    def put_if_absent_or_verify(self, key, raw):
        if key in self.data and self.data[key] != raw:
            raise ValueError("immutable object conflict")
        self.data[key] = raw


def canonical_ci_pg():
    """Bind the canonical disposable producer's state to this exact server.

    run-all-gates intentionally strips LEAF_GATE_* flags from suite children.
    Its injected DATABASE_URL and the producer's real state remain available.
    Never accept a loopback DSN, CI flag or state file alone as migration authority.
    """
    try:
        port = os.environ.get("LEAF_CI_PG_PORT", "54329")
        database = os.environ.get("LEAF_CI_PG_DB", "leaf_gate")
        if not re.fullmatch(r"[0-9]+", port) or not re.fullmatch(r"[a-z_][a-z0-9_]*", database):
            return False
        dsn = os.environ.get("DATABASE_URL", "")
        # This is the exact password-free DSN emitted by start_ephemeral_postgres.sh.
        if dsn != f"postgresql://postgres@127.0.0.1:{port}/{database}":
            return False
        state_path = Path(os.environ.get("LEAF_CI_PG_STATE", "/tmp/leaf-ci-postgres.state"))
        if not state_path.is_absolute() or state_path.is_symlink():
            return False
        rows = state_path.read_text().splitlines()
        if len(rows) > 3 or any("=" not in row for row in rows):
            return False
        state = dict(row.split("=", 1) for row in rows)
        if len(state) != len(rows):
            return False
        if state.get("mode") == "native":
            data = Path(state.get("data", ""))
            if set(state) != {"mode", "bin", "data"} or not re.fullmatch(
                    r"/tmp/leaf-ci-postgres\.[A-Za-z0-9]{8}", str(data)):
                return False
            if data.is_symlink() or data.resolve().parent != Path("/tmp").resolve():
                return False
            binary = re.fullmatch(r"/usr/lib/postgresql/([0-9]+)/bin", state["bin"])
            if not binary or int(binary[1]) < 16:
                return False
        elif state == {"mode": "docker", "name": "leaf-ci-postgres"}:
            result = subprocess.run(["docker", "inspect", "leaf-ci-postgres"],
                                    capture_output=True, text=True, timeout=10, check=True)
            container, = json.loads(result.stdout)
            bindings = container["NetworkSettings"]["Ports"]["5432/tcp"]
            if (not container["State"]["Running"] or container["Config"]["Image"] != "postgres:16"
                    or bindings != [{"HostIp": "127.0.0.1", "HostPort": port}]
                    or "POSTGRES_HOST_AUTH_METHOD=trust" not in container["Config"]["Env"]
                    or f"POSTGRES_DB={database}" not in container["Config"]["Env"]):
                return False
            result = subprocess.run(["docker", "exec", "leaf-ci-postgres", "psql", "-h", "127.0.0.1",
                                     "-U", "postgres", "-d", database, "-Atc",
                                     "select system_identifier::text from pg_control_system()"],
                                    capture_output=True, text=True, timeout=10, check=True)
            system_id = result.stdout.strip()
            if not system_id.isdecimal():
                return False
        else:
            return False
        # Read-only identity query precedes db.apply_migration(). Native data
        # directory or Docker system identifier must match the actual producer.
        with psycopg.connect(dsn, connect_timeout=5, autocommit=True) as connection:
            with connection.cursor() as cur:
                cur.execute("SELECT current_database(), current_user, current_setting('data_directory'), "
                            "system_identifier::text FROM pg_control_system()")
                actual_database, user, directory, actual_system_id = cur.fetchone()
                if state["mode"] == "native":
                    if actual_database != database or user != "postgres" or directory != str(data):
                        return False
                    # The real producer owns its data directory with mode 0700.
                    # Read these fixed, bounded files through that same server,
                    # so an unprivileged test needs no filesystem ownership.
                    cur.execute("SELECT pg_read_file('PG_VERSION', 0, 64, false), "
                                "pg_read_file('postmaster.pid', 0, 4096, false), "
                                "current_setting('port'), current_setting('server_version_num')")
                    version_file, pid_file, actual_port, server_version = cur.fetchone()
                    major = version_file.strip()
                    pid = pid_file.splitlines()
                    if (not major.isdecimal() or major != binary[1]
                            or not str(server_version).isdecimal()
                            or int(server_version) // 10000 != int(major)
                            or actual_port != port or not str(actual_system_id).isdecimal()
                            or len(pid) < 4 or not pid[0].isdecimal()
                            or pid[1] != str(data) or pid[3] != port):
                        return False
        return (actual_database == database and user == "postgres" and
                (directory == str(data) if state["mode"] == "native" else actual_system_id == system_id))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, psycopg.Error):
        return False


@pytest.fixture(scope="module")
def private_pg():
    # Keep the managed producer path; ordinary CI uses its own verified native
    # or Docker disposable producer instead of forging the managed marker.
    managed = os.environ.get("LEAF_E364B_PG_PARENT") and os.environ.get("DATABASE_URL")
    if not managed and not canonical_ci_pg():
        pytest.skip("requires managed or verified canonical disposable PostgreSQL producer")
    db.reset_pool()
    db.apply_migration()
    yield
    db.reset_pool()


@pytest.fixture
def canonical_pg_guard(monkeypatch, tmp_path):
    """Mirror the real producer's state; never start PG or run migrations."""
    for key in ("DATABASE_URL", "LEAF_CI_PG_PORT", "LEAF_CI_PG_DB", "LEAF_GATE_DATABASE_URL",
                "LEAF_GATE_REQUIRE_DATABASE", "LEAF_E364B_PG_PARENT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres@127.0.0.1:54329/leaf_gate")
    state_file = tmp_path / "leaf-ci-postgres.state"
    monkeypatch.setenv("LEAF_CI_PG_STATE", str(state_file))
    with tempfile.TemporaryDirectory(prefix="leaf-ci-postgres.", dir="/tmp") as root:
        data = Path(root)
        (data / "PG_VERSION").write_text("16\n")
        (data / "postmaster.pid").write_text(f"12345\n{data}\n123\n54329\n{data}\n127.0.0.1\n")
        state_file.write_text(f"mode=native\nbin=/usr/lib/postgresql/16/bin\ndata={data}\n")
        evidence = SimpleNamespace(data=data, state_file=state_file,
            identity=["leaf_gate", "postgres", str(data), "7384140640359123456"],
            container={"State": {"Running": True}, "Config": {"Image": "postgres:16",
                "Env": ["POSTGRES_HOST_AUTH_METHOD=trust", "POSTGRES_DB=leaf_gate"]},
                "NetworkSettings": {"Ports": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "54329"}]}}},
            queries=[], commands=[])

        read_text = Path.read_text
        evidence.server_port = "54329"
        evidence.server_version = "160015"

        @contextmanager
        def connection(*args, **kwargs):
            @contextmanager
            def cursor():
                def fetchone():
                    if "pg_read_file" in evidence.queries[-1]:
                        return (read_text(data / "PG_VERSION"), read_text(data / "postmaster.pid"),
                                evidence.server_port, evidence.server_version)
                    return evidence.identity
                yield SimpleNamespace(execute=evidence.queries.append, fetchone=fetchone)
            yield SimpleNamespace(cursor=cursor)

        def docker(command, **kwargs):
            evidence.commands.append(command)
            output = (json.dumps([evidence.container]) if command[1] == "inspect"
                      else "7384140640359123456\n")
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

        monkeypatch.setattr(psycopg, "connect", connection)
        monkeypatch.setattr(subprocess, "run", docker)
        yield evidence


@pytest.mark.parametrize("mode", ["native", "docker"])
def test_canonical_ci_pg_guard_accepts_real_producer_shapes(canonical_pg_guard, mode):
    proof = canonical_pg_guard
    if mode == "docker":
        proof.state_file.write_text("mode=docker\nname=leaf-ci-postgres\n")
        proof.identity[2] = "/var/lib/postgresql/data"
    assert canonical_ci_pg()
    assert len(proof.queries) == (1 if mode == "docker" else 2)
    assert all(query.startswith("SELECT ") for query in proof.queries)
    assert len(proof.commands) == (2 if mode == "docker" else 0)
    assert not os.environ.get("LEAF_E364B_PG_PARENT")
    assert not os.environ.get("LEAF_GATE_REQUIRE_DATABASE")


def test_canonical_ci_pg_guard_accepts_server_owned_native_files(canonical_pg_guard, monkeypatch):
    proof = canonical_pg_guard
    read_text = Path.read_text

    def client_read(path, *args, **kwargs):
        if path in (proof.data / "PG_VERSION", proof.data / "postmaster.pid"):
            raise PermissionError("producer-owned directory")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", client_read)
    assert canonical_ci_pg()
    assert len(proof.queries) == 2


@pytest.mark.parametrize("mismatch", ["server_port", "server_major", "file_major", "system_id"])
def test_canonical_ci_pg_guard_refuses_native_server_mismatch(canonical_pg_guard, mismatch):
    proof = canonical_pg_guard
    if mismatch == "server_port":
        proof.server_port = "5432"
    elif mismatch == "server_major":
        proof.server_version = "170001"
    elif mismatch == "file_major":
        (proof.data / "PG_VERSION").write_text("17\n")
    else:
        proof.identity[3] = "not-a-system-id"
    assert not canonical_ci_pg()


@pytest.mark.parametrize("mismatch", ["ambient", "remote", "credentials", "missing_state", "duplicate_state",
    "native_pid_port", "native_directory", "database", "docker_binding", "docker_identity"])
def test_canonical_ci_pg_guard_refuses_unbound_servers(canonical_pg_guard, monkeypatch, mismatch):
    proof = canonical_pg_guard
    if mismatch == "ambient":
        monkeypatch.setenv("DATABASE_URL", "postgresql://postgres@127.0.0.1:5432/developer")
    elif mismatch == "remote":
        monkeypatch.setenv("DATABASE_URL", "postgresql://postgres@database.example:54329/leaf_gate")
    elif mismatch == "credentials":
        monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:synthetic@127.0.0.1:54329/leaf_gate")
    elif mismatch == "missing_state":
        proof.state_file.unlink()
    elif mismatch == "duplicate_state":
        proof.state_file.write_text("mode=docker\nname=leaf-ci-postgres\nmode=docker\n")
    elif mismatch == "native_pid_port":
        (proof.data / "postmaster.pid").write_text(f"12345\n{proof.data}\n123\n5432\n")
    elif mismatch == "native_directory":
        proof.identity[2] = "/var/lib/postgresql/live"
    elif mismatch == "database":
        proof.identity[0] = "developer"
    else:
        proof.state_file.write_text("mode=docker\nname=leaf-ci-postgres\n")
        if mismatch == "docker_binding":
            proof.container["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostIp"] = "0.0.0.0"
        else:
            proof.identity[3] = "other-server"
    assert not canonical_ci_pg()
    assert all(query.startswith("SELECT ") for query in proof.queries)


@pytest.fixture
def lane(private_pg, monkeypatch, tmp_path):
    org = store.create_org("Project editor synthetic proof")
    project = store.create_project(org.org_id, "Editor", authority_mode="postgres_canonical")
    subject = "auth0|project-editor-" + uuid4().hex
    actor = store.create_identity_binding(org.org_id, "auth0", subject, role="editor")
    with db.cursor() as cur:
        cur.execute("INSERT INTO project_member_bindings "
                    "(membership_id,org_id,project_id,binding_id,role,invited_by_binding_id) "
                    "VALUES (%s,%s,%s,%s,'editor',%s)",
                    (uuid4(), org.org_id, project.project_id, actor.binding_id, actor.binding_id))
    source = str(uuid4())
    raw = drawing()
    intake = json.dumps(dxf_intake.parse_dxf_bytes(raw, source_name=source), separators=(",", ":")).encode()
    object_key = f"tenants/{org.org_id}/drawings/{source}/v/00000001.dwg"
    intake_key = object_key[:-4] + ".intake.json"
    marker = {"schema": 1, "status": "ready", "attempt": uuid4().hex[:16],
              "tenant_kind": "account", "content_sha256": digest(raw), "bytes": len(raw),
              "extracted_version": 1, "intake_ref": intake_key, "intake_sha256": digest(intake)}
    # Seed the same ready-upload producer rows, then use the REAL adoption
    # function; no hand-authored canonical publication proof or fake SQL store.
    with db.cursor() as cur:
        cur.execute("INSERT INTO drawing_store_manifests(tenant_id,drawing_id,head,latest) VALUES(%s,%s,1,1)",
                    (str(org.org_id), source))
        cur.execute("INSERT INTO drawing_store_versions "
                    "(tenant_id,drawing_id,version,parent_version,object_key,byte_count,content_sha256,state,ready_at,intake_ref,intake_sha256) "
                    "VALUES(%s,%s,1,NULL,%s,%s,%s,'ready',NOW(),%s,%s)",
                    (str(org.org_id), source, object_key, len(intake), digest(intake), intake_key, digest(intake)))
        cur.execute("INSERT INTO drawing_upload_attempts(tenant_id,drawing_id,attempt,marker,status) "
                    "VALUES(%s,%s,%s,%s,'ready')", (str(org.org_id), source, marker["attempt"], Jsonb(marker)))
    parent, _ = store.import_ready_account_upload(org.org_id, project.project_id,
        source_drawing_id=source, source_version=1, name="rooftop.dxf",
        idempotency_key="initial-" + uuid4().hex, actor_binding_id=actor.binding_id)
    backend = BytesStore()
    backend.data[intake_key] = intake
    backend.data[object_key] = intake
    original_key = editor.upload_source_key(org.org_id, source, digest(raw))
    backend.data[original_key] = raw
    monkeypatch.setattr(project_context.write_loop, "upload_backend_for_tenant", lambda _tenant: backend)
    monkeypatch.setattr(checkout_capability, "_secret", lambda: "synthetic-editor-test-signing-key")
    fence = tmp_path / "mutation-fence"
    fence.write_text("1")
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "1")
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_FENCE_FILE", str(fence))
    tenant = deps.TenantContext(str(org.org_id), org_id=str(org.org_id),
                              subject=subject, authority_resolved=True)
    app = FastAPI()
    install_error_handlers(app)
    app.middleware("http")(project_drawings.no_store_responses)
    app.include_router(project_drawings.router)
    app.dependency_overrides[deps.require_active_tenant] = lambda: tenant
    with TestClient(app) as http:
        base = f"/api/projects/{project.project_id}/drawings/{parent.drawing_id}"
        yield SimpleNamespace(org=org.org_id, project=project.project_id, drawing=parent.drawing_id,
            actor=actor.binding_id, tenant=tenant, parent=parent, raw=raw, backend=backend,
            source=source, original_key=original_key, base=base, http=http, app=app)


def checkout(lane):
    response = lane.http.post(lane.base + "/checkout")
    assert response.status_code == 200
    return response.json()["checkout_capability"]


def post(lane, raw=None, *, parent=None, cap=None, key=None, **headers):
    headers = {"Content-Type": "application/dxf", "Idempotency-Key": key or str(uuid4()), **headers}
    if cap is not None:
        headers["X-Checkout-Capability"] = cap
    return lane.http.post(lane.base + f"/versions/{parent or lane.parent.version_id}/editor",
                          content=raw if raw is not None else drawing(132), headers=headers)


def versions(lane):
    return store.list_drawing_versions(lane.org, lane.project)


def test_import_read_save_exact_reopen_and_project_restore(lane):
    initial = lane.http.get(lane.base + f"/versions/{lane.parent.version_id}/dxf")
    assert initial.status_code == 200 and initial.content == lane.raw
    assert initial.headers["x-leaf-source-sha256"] == digest(lane.raw)
    cap = checkout(lane)
    saved = post(lane, cap=cap)
    assert saved.status_code == 200, saved.text
    receipt = saved.json()
    child = receipt["drawing_version"]
    assert child["drawing_id"] == str(lane.drawing)
    assert child["project_id"] == str(lane.project) and child["seq"] == 2
    assert receipt["source_sha256"] == digest(drawing(132))
    reopened = lane.http.get(lane.base + f"/versions/{child['version_id']}/dxf")
    assert reopened.status_code == 200 and reopened.content == drawing(132)
    context = project_context.resolve_context(lane.tenant, lane.project, UUID(child["version_id"]), drawing_id=lane.drawing)
    before = dxf_intake.parse_dxf_bytes(lane.raw, source_name=str(lane.drawing))
    assert context.intake["polylines"][0]["pts"][0][:2] == [132.0, 126.0]
    assert context.intake["polylines"][1:] == before["polylines"][1:]
    assert context.intake["texts"] == before["texts"]
    assert len(context.intake["polylines"]) + len(context.intake["texts"]) == 80
    restored = lane.http.post(lane.base + f"/versions/{child['version_id']}/editor/restore",
        json={"version_id": str(lane.parent.version_id)},
        headers={"Idempotency-Key": str(uuid4()), "X-Checkout-Capability": cap})
    assert restored.status_code == 200, restored.text
    restored_id = restored.json()["drawing_version"]["version_id"]
    assert restored.json()["drawing_version"]["seq"] == 3
    assert lane.http.get(lane.base + f"/versions/{restored_id}/dxf").content == lane.raw


def test_exact_retry_after_release_and_changed_body_conflict(lane):
    cap, key = checkout(lane), str(uuid4())
    first = post(lane, cap=cap, key=key)
    assert first.status_code == 200, first.text
    assert lane.http.delete(lane.base + "/checkout", headers={"X-Checkout-Capability": cap}).status_code == 200
    replay = post(lane, cap=cap, key=key)
    assert replay.status_code == 200 and replay.json() == first.json()
    conflict = post(lane, raw=drawing(140), cap=cap, key=key)
    assert conflict.status_code == 409
    assert len(versions(lane)) == 2


def test_concurrent_exact_request_replays_one_publication(lane):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    cap, key = checkout(lane), str(uuid4())
    start = Barrier(2)

    def save():
        start.wait(timeout=10)
        return post(lane, cap=cap, key=key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _index: save(), range(2)))
    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    assert len(versions(lane)) == 2


def test_missing_wrong_and_stale_checkout_do_not_publish(lane):
    assert post(lane).status_code == 409
    cap = checkout(lane)
    assert post(lane, cap="lco1." + "0" * 64).status_code == 403
    renewed = lane.http.post(lane.base + "/checkout", headers={"X-Checkout-Capability": cap})
    assert renewed.status_code == 200
    assert post(lane, cap=cap).status_code == 403
    assert len(versions(lane)) == 1


def test_stale_parent_and_foreign_version_do_not_publish(lane):
    cap = checkout(lane)
    assert post(lane, cap=cap).status_code == 200
    assert post(lane, raw=drawing(140), cap=cap).status_code == 409
    assert post(lane, parent=uuid4(), cap=cap).status_code == 404
    assert len(versions(lane)) == 2


def test_foreign_scope_and_revoked_member_do_not_read_or_save(lane):
    cap = checkout(lane)
    assert post(lane, cap=cap, **{"X-Project-Id": str(uuid4())}).status_code == 404
    with db.cursor() as cur:
        cur.execute("UPDATE project_member_bindings SET status='revoked',revoked_at=NOW() WHERE project_id=%s AND binding_id=%s",
                    (lane.project, lane.actor))
    assert post(lane, cap=cap).status_code == 403
    assert lane.http.get(lane.base + f"/versions/{lane.parent.version_id}/dxf").status_code == 403
    assert len(versions(lane)) == 1


@pytest.mark.parametrize("content,headers,status", [
    (b"not a DXF", {}, 422),
    (drawing(), {"Content-Type": "application/json"}, 415),
    (drawing(), {"Idempotency-Key": "not-a-uuid"}, 400),
    (drawing(), {"Content-Length": str(editor.MAX_DXF_BYTES + 1)}, 413),
])
def test_untrusted_body_refused_without_publication(lane, content, headers, status):
    response = post(lane, raw=content, cap=checkout(lane), **headers)
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert "capability" not in response.text and "tenants/" not in response.text
    assert len(versions(lane)) == 1


def test_original_bytes_survive_staging_removal_and_corruption_refuses(lane, monkeypatch):
    monkeypatch.setattr(editor.guest_uploads, "staged_path", lambda *_a: (_ for _ in ()).throw(AssertionError("staging used")))
    url = lane.base + f"/versions/{lane.parent.version_id}/dxf"
    assert lane.http.get(url).content == lane.raw
    key = project_context.graph_store().publication_source_key(lane.org, lane.project, lane.drawing, digest(lane.raw))
    lane.backend.data[key] = b"corrupt"
    refused = lane.http.get(url)
    assert refused.status_code == 409
    assert len(versions(lane)) == 1


def test_wrong_restore_artifact_refused(lane):
    cap = checkout(lane)
    response = lane.http.post(lane.base + f"/versions/{lane.parent.version_id}/editor/restore",
        json={"version_id": str(uuid4())},
        headers={"Idempotency-Key": str(uuid4()), "X-Checkout-Capability": cap})
    assert response.status_code == 404 and len(versions(lane)) == 1


def test_enriched_parent_preserves_metadata_invalidates_geometry_and_restores_exact_graph(lane, graph):
    from copy import deepcopy
    from solar_design_graph import entities
    from solar_solve_results import digest as graph_digest
    cap = checkout(lane)
    parent = project_context.resolve_context(lane.tenant, lane.project, lane.parent.version_id,
                                              drawing_id=lane.drawing)
    enriched = deepcopy(parent.intake)
    enriched["operator_notes"] = {"keep": ["roof fixture", 17]}
    enriched["solar_design_graph"] = graph
    enriched["solar_design_graph_sha256"] = graph_digest(graph)
    payload = json.dumps(enriched, sort_keys=True, separators=(",", ":")).encode()
    intake_digest = digest(payload)
    key = project_context.graph_store().publication_intake_key(lane.org, lane.project, lane.drawing, intake_digest)
    lane.backend.put_if_absent_or_verify(key, payload)
    lease = project_context.verify_at_admission(lane.tenant, lane.project, lane.drawing,
        lane.parent.version_id, checkout_capability=cap).checkout
    enriched_version = project_context.publish_project_version(lane.tenant, lane.project, lane.drawing,
        expected_parent_version_id=lane.parent.version_id, expected_fence=lease.fence,
        request_id=uuid4(), intake_sha256=intake_digest)
    saved = post(lane, parent=enriched_version.version_id, cap=cap)
    assert saved.status_code == 200, saved.text
    saved_id = UUID(saved.json()["drawing_version"]["version_id"])
    current = project_context.resolve_context(lane.tenant, lane.project, saved_id, drawing_id=lane.drawing).intake
    assert current["operator_notes"] == enriched["operator_notes"]
    current_graph = current["solar_design_graph"]
    assert current_graph["settings"]["panel_layer_contains"] == graph["settings"]["panel_layer_contains"]
    assert current_graph["settings"]["strings_per_mppt"] == graph["settings"]["strings_per_mppt"]
    assert current_graph["rev"] == graph["rev"] + 1
    assert all(e["validity"] == {"state": "stale", "reasons": ["source_geometry_changed"]}
               for e in entities(current_graph) if e["kind"] not in ("project", "settings"))
    assert current["polylines"][0]["pts"][0][:2] == [132.0, 126.0]
    restored = lane.http.post(lane.base + f"/versions/{saved_id}/editor/restore",
        json={"version_id": str(enriched_version.version_id)},
        headers={"Idempotency-Key": str(uuid4()), "X-Checkout-Capability": cap})
    assert restored.status_code == 200, restored.text
    restored_id = UUID(restored.json()["drawing_version"]["version_id"])
    restored_context = project_context.resolve_context(lane.tenant, lane.project, restored_id, drawing_id=lane.drawing)
    assert restored_context.intake_bytes == payload
    assert restored_context.intake["solar_design_graph"] == graph
    assert lane.http.get(lane.base + f"/versions/{restored_id}/dxf").content == lane.raw
