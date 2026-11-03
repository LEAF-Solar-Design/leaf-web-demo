"""The same credential, tenant, capacity and concurrent lease proofs on both authorities."""
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
import json
import os
import threading
import uuid

import pytest

import device_store as store


PLATFORM = {"os": "darwin", "os_version": "15", "arch": "arm64"}
NONCE = base64.urlsafe_b64encode(bytes(range(16))).rstrip(b"=").decode()


@pytest.fixture(params=["sqlite", "postgres"])
def authority(request, monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "test")
    monkeypatch.setenv("LEAF_PLATFORM_POSTGRES_REQUIRED", "0")
    monkeypatch.setenv("LEAF_DEVICE_STORE", request.param)
    monkeypatch.setenv("LEAF_DEVICE_DB", str(tmp_path / "devices.db"))
    tenant = "device-store-test-" + uuid.uuid4().hex
    if request.param == "postgres":
        if not os.environ.get("DATABASE_URL"):
            pytest.skip("platform DB unreachable: DATABASE_URL unset")
        monkeypatch.setenv("LEAF_DEVICE_STORE", "postgres_required")
        db = store.platform_link.platform_db()
        db.apply_migration()
        db.assert_schema_current()
    yield tenant
    if request.param == "postgres":
        def cleanup(sql):
            sql.execute("DELETE FROM device_nonces WHERE device_id IN"
                        " (SELECT device_id FROM devices WHERE tenant_id LIKE ?)", (tenant + "%",))
            for table in ("device_audit", "device_relay_tokens", "device_commands", "device_enrollments", "devices"):
                sql.execute("DELETE FROM " + table + " WHERE tenant_id LIKE ?", (tenant + "%",))

        try:
            store._run("cleanup:" + tenant, cleanup, write=True)
        finally:
            db.reset_pool()


def enrollment(tenant, project="project"):
    return store.create_enrollment(tenant, project, "Mac mini", "user:test")


def register(token, key=None):
    key = key or base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    return store.consume_enrollment(token, key, "1.0", PLATFORM, "mini.local")


def device(tenant, project="project"):
    return register(enrollment(tenant, project)[1])


def command(tenant, device_id, ttl_s=60):
    return store.enqueue_command(tenant, device_id, "pkg.health", {"package": "homeassistant"}, "user:test", ttl_s)


def relay(tenant, device_id):
    return store.create_relay_token(tenant, device_id, "homeassistant", "Ops", "user:test")


def result(command_id):
    return {"command_id": command_id, "status": "ok", "finished_at": store._now().isoformat(), "data": {"ok": True}}


def parallel(action):
    barrier = threading.Barrier(8)

    def run(index):
        barrier.wait(timeout=15)
        return action(index)

    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(run, range(8)))


def test_enrollment_is_single_use_and_only_raw_secret_hash_is_stored(authority):
    row, token = enrollment(authority)
    assert len(token) == 43 and "=" not in token
    assert row["token_sha256"] == hashlib.sha256(base64.urlsafe_b64decode(token + "=")).hexdigest()
    assert token not in json.dumps(row)
    assert (store._date(row["expires_at"]) - store._date(row["created_at"])).total_seconds() == 600
    enrolled = register(token)
    assert enrolled["tenant_id"] == authority and enrolled["project_id"] == "project"
    assert enrolled["label"] == "Mac mini" and enrolled["packages"] == []
    with pytest.raises(store.EnrollmentInvalid):
        register(token)


def test_invalid_expired_and_noncanonical_enrollments_are_rejected(authority, monkeypatch):
    row, token = enrollment(authority)
    for bad in ("", "invalid", "A" * 43, token[:-1] + "B", None):
        with pytest.raises(store.EnrollmentInvalid):
            register(bad)
    monkeypatch.setattr(store, "_now", lambda: store._date(row["expires_at"]))
    with pytest.raises(store.EnrollmentInvalid):
        register(token)
    assert store.list_devices(authority, "project") == []


def test_enrollment_cap_is_per_tenant_and_excludes_expired_and_consumed(authority, monkeypatch):
    row, token = enrollment(authority)
    register(token)
    for _ in range(10):
        enrollment(authority)
    with pytest.raises(store.EnrollmentLimit):
        enrollment(authority)
    assert enrollment(authority + "-foreign")
    monkeypatch.setattr(store, "_now", lambda: store._date(row["expires_at"]) + timedelta(seconds=1))
    assert enrollment(authority)


def test_enrollment_cap_is_atomic_under_eight_creators(authority):
    for _ in range(6):
        enrollment(authority)

    def create(_):
        try:
            enrollment(authority)
            return True
        except store.EnrollmentLimit:
            return False

    assert parallel(create).count(True) == 4


def test_enrollment_consumption_is_atomic_under_eight_registrations(authority):
    _, token = enrollment(authority)

    def consume(_):
        try:
            register(token)
            return True
        except store.EnrollmentInvalid:
            return False

    assert parallel(consume).count(True) == 1
    assert len(store.list_devices(authority, "project")) == 1


def test_device_cap_rolls_back_consumption_and_revocation_frees_slot(authority):
    devices = [device(authority) for _ in range(20)]
    _, token = enrollment(authority)
    with pytest.raises(store.DeviceLimit):
        register(token)
    assert store.revoke_device(authority, devices[0]["device_id"], "user:test")
    assert register(token)["tenant_id"] == authority
    assert len([row for row in store.list_devices(authority, "project") if row["revoked_at"] is None]) == 20


def test_public_key_uniqueness_survives_revoke_and_failed_claim_preserves_token(authority):
    enrolled = device(authority)
    _, token = enrollment(authority + "-foreign")
    for revoked in (False, True):
        if revoked:
            store.revoke_device(authority, enrolled["device_id"], "user:test")
        with pytest.raises(store.DeviceKeyInUse):
            register(token, enrolled["public_key"])
    assert register(token)["tenant_id"] == authority + "-foreign"


def test_tenant_boundaries_cover_all_user_reads_and_writes(authority):
    enrolled = device(authority)
    device_id = enrolled["device_id"]
    command_id = command(authority, device_id)
    token_row, token = relay(authority, device_id)
    assert store.append_audit(authority, device_id, "user:test", "device.register", device_id, "ok")
    foreign = authority + "-foreign"
    assert store.list_devices(foreign, "project") == []
    assert store.list_devices(authority, "other-project") == []
    assert store.get_command(foreign, device_id, command_id) is None
    assert store.get_command(authority, str(uuid.uuid4()), command_id) is None
    assert store.list_audit(foreign, device_id) == []
    assert not store.revoke_device(foreign, device_id, "user:other")
    assert not store.revoke_relay_token(foreign, token_row["token_id"])
    assert store.enqueue_command(foreign, device_id, "agent.pause", {}, "user:other") is None
    assert relay(foreign, device_id) is None
    assert not store.append_audit(foreign, device_id, "user:other", "device.revoke", device_id, "ok")
    assert store.get_device(device_id)["revoked_at"] is None
    assert store.resolve_relay_token(token)["tenant_id"] == authority
    assert store.get_command(authority, device_id, command_id)["state"] == "queued"
    assert len(store.list_audit(authority, device_id)) == 1


def test_touch_persists_heartbeat_and_rejects_duplicate_packages(authority, monkeypatch):
    enrolled = device(authority)
    device_id = enrolled["device_id"]
    now = store._now() + timedelta(seconds=31)
    monkeypatch.setattr(store, "_now", lambda: now)
    packages = [{"package": "homeassistant", "version": "2026.10.0", "status": "running", "last_health_at": None}]
    assert store.touch_device(device_id, "1.1", packages, 4096)
    row = store.get_device(device_id)
    assert row["agent_version"] == "1.1" and row["free_mem_mb"] == 4096
    assert row["packages"] == packages and row["platform"] == PLATFORM
    assert store._date(row["last_seen_at"]) == now
    with pytest.raises(ValueError):
        store.touch_device(device_id, "1.1", packages * 2, 4096)
    assert not store.touch_device(str(uuid.uuid4()), "1.1", [], 1)


def test_nonce_replay_window_prunes_only_the_bound_device(authority):
    device_id = device(authority)["device_id"]
    other_id = device(authority)["device_id"]
    now = store._now()
    assert store.remember_nonce(device_id, NONCE, now)
    assert store.remember_nonce(other_id, NONCE, now)
    assert not store.remember_nonce(device_id, NONCE, now + timedelta(seconds=119.999))
    assert store.remember_nonce(device_id, NONCE, now + timedelta(seconds=120))
    assert not store.remember_nonce(other_id, NONCE, now + timedelta(seconds=119))
    rows = store._run("inspect", lambda sql: sql.execute(
        "SELECT * FROM device_nonces WHERE device_id=?", (device_id,)).fetchall())
    assert len(rows) == 1


def test_nonce_claim_is_atomic_under_eight_connections(authority):
    device_id = device(authority)["device_id"]
    now = store._now()
    assert parallel(lambda _: store.remember_nonce(device_id, NONCE, now)).count(True) == 1


def test_queue_cap_is_per_device_and_expiry_frees_capacity(authority, monkeypatch):
    device_id = device(authority)["device_id"]
    now = store._now()
    monkeypatch.setattr(store, "_now", lambda: now)
    for _ in range(64):
        command(authority, device_id)
    with pytest.raises(store.QueueFull):
        command(authority, device_id)
    assert command(authority, device(authority)["device_id"])
    monkeypatch.setattr(store, "_now", lambda: now + timedelta(seconds=60))
    assert command(authority, device_id)


def test_queue_cap_is_atomic_under_eight_enqueuers(authority):
    device_id = device(authority)["device_id"]
    for _ in range(60):
        command(authority, device_id)

    def enqueue(_):
        try:
            command(authority, device_id)
            return True
        except store.QueueFull:
            return False

    assert parallel(enqueue).count(True) == 4


def test_eight_concurrent_leasers_never_receive_a_command_twice(authority):
    device_id = device(authority)["device_id"]
    expected = {command(authority, device_id) for _ in range(64)}
    batches = parallel(lambda _: store.lease_commands(device_id))
    ids = [row["command_id"] for batch in batches for row in batch]
    assert all(len(batch) == 8 for batch in batches)
    assert len(ids) == len(set(ids)) == 64
    assert set(ids) == expected
    assert store.lease_commands(device_id) == []


def test_expired_commands_are_marked_and_never_delivered(authority, monkeypatch):
    device_id = device(authority)["device_id"]
    now = store._now()
    monkeypatch.setattr(store, "_now", lambda: now)
    leased = command(authority, device_id, 2)
    assert store.lease_commands(device_id)
    queued = command(authority, device_id, 1)
    monkeypatch.setattr(store, "_now", lambda: now + timedelta(seconds=2))
    assert store.lease_commands(device_id) == []
    for command_id in (queued, leased):
        assert store.get_command(authority, device_id, command_id)["state"] == "expired"
        assert not store.complete_command(device_id, command_id, result(command_id))


def test_delivery_repeats_only_after_lease_expiry_and_before_command_expiry(authority, monkeypatch):
    device_id = device(authority)["device_id"]
    now = store._now()
    monkeypatch.setattr(store, "_now", lambda: now)
    command_id = command(authority, device_id)
    assert store.lease_commands(device_id)[0]["command_id"] == command_id
    monkeypatch.setattr(store, "_now", lambda: now + timedelta(seconds=29))
    assert store.lease_commands(device_id) == []
    monkeypatch.setattr(store, "_now", lambda: now + timedelta(seconds=30))
    retry = store.lease_commands(device_id, lease_s=300)[0]
    assert retry["command_id"] == command_id and retry["lease_until"] == retry["expires_at"]


def test_completion_requires_leased_bound_command_and_identical_retries(authority):
    device_id = device(authority)["device_id"]
    other_id = device(authority)["device_id"]
    command_id = command(authority, device_id)
    payload = result(command_id)
    assert not store.complete_command(device_id, command_id, payload)
    assert store.lease_commands(device_id)
    assert not store.complete_command(other_id, command_id, payload)
    assert store.complete_command(device_id, command_id, payload)
    assert store.complete_command(device_id, command_id, dict(reversed(list(payload.items()))))
    assert not store.complete_command(device_id, command_id, {**payload, "data": {"ok": False}})
    assert not store.complete_command(device_id, command_id, {**payload, "data": {"ok": 1}})
    assert not store.complete_command(device_id, command_id, result(str(uuid.uuid4())))
    assert store.get_command(authority, device_id, command_id)["result"] == payload


def test_revoke_cascades_and_preserves_original_revocation(authority, monkeypatch):
    device_id = device(authority)["device_id"]
    other_id = device(authority)["device_id"]
    _, token = relay(authority, device_id)
    _, other_token = relay(authority, other_id)
    leased = command(authority, device_id)
    assert store.lease_commands(device_id)
    queued = command(authority, device_id)
    assert store.revoke_device(authority, device_id, "user:first")
    revoked = store.get_device(device_id)
    later = store._now() + timedelta(seconds=1)
    monkeypatch.setattr(store, "_now", lambda: later)
    assert store.revoke_device(authority, device_id, "user:second")
    assert store.get_device(device_id)["revoked_at"] == revoked["revoked_at"]
    assert store.get_device(device_id)["revoked_by"] == "user:first"
    assert store.resolve_relay_token(token) is None
    assert store.resolve_relay_token(other_token) is not None
    for command_id in (queued, leased):
        assert store.get_command(authority, device_id, command_id)["state"] == "expired"
    for action in (lambda: command(authority, device_id), lambda: relay(authority, device_id),
                   lambda: store.lease_commands(device_id), lambda: store.complete_command(device_id, leased, result(leased)),
                   lambda: store.remember_nonce(device_id, NONCE, later)):
        with pytest.raises(store.DeviceRevoked):
            action()
    assert not store.touch_device(device_id, "1.0", [], 1)


def test_relay_secret_format_hash_resolution_and_malformed_tokens(authority):
    device_id = device(authority)["device_id"]
    row, token = relay(authority, device_id)
    token_id, secret = token.split(".")
    assert token_id == row["token_id"] and len(secret) == 43
    assert row["token_sha256"] == hashlib.sha256(base64.urlsafe_b64decode(secret + "=")).hexdigest()
    assert secret not in json.dumps(row) and row["last_used_at"] is None
    resolved = store.resolve_relay_token(token)
    assert resolved["device_id"] == device_id and resolved["last_used_at"] is not None
    assert resolved["project_id"] == "project"
    for bad in (None, "", secret, token_id + "." + "A" * 43, str(uuid.uuid4()) + "." + secret, token + ".x"):
        assert store.resolve_relay_token(bad) is None


def test_relay_token_cap_is_atomic_and_revocation_frees_slot(authority):
    device_id = device(authority)["device_id"]
    rows = [relay(authority, device_id)[0] for _ in range(6)]

    def create(_):
        try:
            relay(authority, device_id)
            return True
        except store.RelayTokenLimit:
            return False

    assert parallel(create).count(True) == 4
    with pytest.raises(store.RelayTokenLimit):
        relay(authority, device_id)
    assert store.revoke_relay_token(authority, rows[0]["token_id"])
    assert relay(authority, device_id)


def test_relay_revoke_is_tenant_scoped_and_idempotent(authority):
    device_id = device(authority)["device_id"]
    row, token = relay(authority, device_id)
    assert not store.revoke_relay_token(authority + "-foreign", row["token_id"])
    assert store.resolve_relay_token(token)
    assert store.revoke_relay_token(authority, row["token_id"])
    assert store.revoke_relay_token(authority, row["token_id"])
    assert store.resolve_relay_token(token) is None
    assert not store.revoke_relay_token(authority, str(uuid.uuid4()))


def test_audit_prunes_to_newest_5000_and_lists_at_most_200(authority, monkeypatch):
    device_id = device(authority)["device_id"]
    other_id = device(authority)["device_id"]
    now = store._now()
    assert store.append_audit(authority, other_id, "device", "pkg.health", "homeassistant", "ok")

    def seed(sql):
        for index in range(5000):
            sql.execute("INSERT INTO device_audit (tenant_id,device_id,at,actor,op,target,status) VALUES (?,?,?,?,?,?,?)",
                        (authority, device_id, (now + timedelta(seconds=index)).isoformat(), "device", "pkg.health", str(index), "ok"))

    store._run("id:" + device_id, seed, write=True)
    monkeypatch.setattr(store, "_now", lambda: now + timedelta(seconds=5000))
    assert store.append_audit(authority, device_id, "user:test", "device.revoke", "newest", "ok", "a" * 64)
    rows = store.list_audit(authority, device_id, limit=200)
    assert len(rows) == 200 and rows[0]["target"] == "newest" and rows[0]["request_sha256"] == "a" * 64
    assert len(store.list_audit(authority, other_id)) == 1
    stats = store._run("inspect", lambda sql: dict(sql.execute(
        "SELECT COUNT(*) AS n,MIN(at) AS oldest FROM device_audit WHERE device_id=?", (device_id,)).fetchone()))
    assert stats["n"] == 5000 and store._date(stats["oldest"]) == now + timedelta(seconds=1)


def test_database_op_check_rejects_operations_outside_exact_v1_set(authority):
    device_id = device(authority)["device_id"]
    command_id = command(authority, device_id)
    for op in store.OPS:
        store._run("id:" + device_id, lambda sql: sql.execute(
            "UPDATE device_commands SET op=? WHERE command_id=?", (op, command_id)), write=True)
    with pytest.raises(Exception) as caught:
        store._run("id:" + device_id, lambda sql: sql.execute(
            "UPDATE device_commands SET op='shell.exec' WHERE command_id=?", (command_id,)), write=True)
    if store.store_mode() == "postgres":
        assert caught.value.sqlstate == "23514"
    else:
        assert isinstance(caught.value, store.sqlite3.IntegrityError)
    assert store.get_command(authority, device_id, command_id)["op"] == store.OPS[-1]


def test_wire_bounds_limits_and_store_selection_fail_closed(authority, monkeypatch):
    device_id = device(authority)["device_id"]
    for bad in (0, 9, True, 1.5):
        with pytest.raises(ValueError):
            store.lease_commands(device_id, limit=bad)
    for bad in (0, 201, True):
        with pytest.raises(ValueError):
            store.list_audit(authority, device_id, limit=bad)
    for bad in (0, -1, True, float("inf"), 1e-9):
        with pytest.raises(ValueError):
            command(authority, device_id, ttl_s=bad)
        with pytest.raises(ValueError):
            store.lease_commands(device_id, lease_s=bad)
    for project, label in (("", "label"), ("p" * 129, "label"), ("project", "x" * 129)):
        with pytest.raises(ValueError):
            store.create_enrollment(authority, project, label, "user:test")
    for op, args in (("shell.exec", {}), ("pkg.install", {"package": "ha"}), ("agent.pause", {"path": "/tmp"})):
        with pytest.raises(ValueError):
            store.enqueue_command(authority, device_id, op, args, "user:test")
    with pytest.raises(ValueError):
        store.touch_device(device_id, "1.0", [], -1)
    with pytest.raises(ValueError):
        store.touch_device(device_id, "1.0", [{"package": "ha", "version": "1", "status": "running",
                                              "last_health_at": "invalid-date"}], 1)
    with pytest.raises(ValueError):
        store.complete_command(device_id, str(uuid.uuid4()), {**result(str(uuid.uuid4())), "finished_at": "invalid-date"})
    with pytest.raises(ValueError, match="device_proxy_payload_too_large"):
        store.enqueue_command(authority, device_id, "http.proxy",
                              {"package": "homeassistant", "method": "POST", "path": "/api/states", "body_json": "x" * 1048576},
                              "user:test")
    with pytest.raises(ValueError):
        store.remember_nonce(device_id, "bad", store._now())
    with pytest.raises(ValueError):
        store.append_audit(authority, device_id, "invalid-actor", "device.revoke", device_id, "ok")
    with monkeypatch.context() as env:
        env.setenv("LEAF_DEVICE_STORE", "unknown")
        with pytest.raises(store.StoreUnavailable, match="device_store_invalid"):
            store.get_device(device_id)
        env.setenv("LEAF_DEVICE_STORE", "sqlite")
        env.setenv("LEAF_PLATFORM_POSTGRES_REQUIRED", "1")
        with pytest.raises(store.StoreUnavailable, match="device_postgres_required"):
            store.get_device(device_id)
    assert store.get_device(device_id)["device_id"] == device_id
    assert store.get_device(str(uuid.uuid4())) is None
