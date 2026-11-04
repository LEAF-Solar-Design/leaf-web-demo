"""Durable device credentials, replay claims, command leases and bounded audit.

User-facing operations require a tenant. Device/credential entry points derive
their scope from the stored binding, never from a caller-supplied tenant.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import uuid

from jsonschema import Draft202012Validator, FormatChecker

import platform_link
from lab_artifacts import StoreUnavailable, _SQL

_SCHEMA = json.loads((Path(__file__).resolve().parents[1] /
                      "contract/device-bridge.v1.schema.json").read_text(encoding="utf-8"))
_FORMAT_CHECKER = FormatChecker()


@_FORMAT_CHECKER.checks("date-time", raises=ValueError)
def _valid_datetime(value):
    # The default date-time checker requires an optional jsonschema extra.
    if not isinstance(value, str):
        return True
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})", value) is None:
        return False
    datetime.fromisoformat(value.upper().replace("Z", "+00:00"))
    return True


_VALIDATORS = {
    name: Draft202012Validator({"$ref": "#/$defs/" + name, "$defs": _SCHEMA["$defs"]},
                              format_checker=_FORMAT_CHECKER)
    for name in ("RegisterRequest", "HeartbeatRequest", "Command", "Result", "AuditEvent")
}
OPS = tuple(_SCHEMA["$defs"]["Command"]["properties"]["op"]["enum"])
MAX_ENROLLMENTS = 10
MAX_DEVICES = 20
MAX_QUEUED = 64
MAX_RELAY_TOKENS = 10
MAX_AUDIT = 5000

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS device_enrollments (
 enrollment_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,
 label TEXT NOT NULL, token_sha256 TEXT NOT NULL UNIQUE, created_by TEXT NOT NULL,
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL, consumed_at TEXT, device_id TEXT
);
CREATE TABLE IF NOT EXISTS devices (
 device_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL,
 label TEXT NOT NULL, public_key TEXT NOT NULL UNIQUE, agent_version TEXT NOT NULL,
 platform TEXT NOT NULL, hostname TEXT NOT NULL, created_at TEXT NOT NULL,
 last_seen_at TEXT NOT NULL, revoked_at TEXT, revoked_by TEXT,
 packages TEXT NOT NULL DEFAULT '[]', free_mem_mb INTEGER, UNIQUE (tenant_id, device_id)
);
CREATE TABLE IF NOT EXISTS device_commands (
 command_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, device_id TEXT NOT NULL,
 op TEXT NOT NULL CHECK (op IN ('pkg.install','pkg.upgrade','pkg.uninstall','pkg.health','http.proxy','ha.onboard','ha.flow.start','ha.flow.step','ha.flow.list','agent.pause','agent.update')),
 args TEXT NOT NULL, issued_by TEXT NOT NULL, issued_at TEXT NOT NULL,
 expires_at TEXT NOT NULL, leased_at TEXT, lease_until TEXT,
 state TEXT NOT NULL CHECK (state IN ('queued','leased','done','expired')),
 result TEXT, finished_at TEXT,
 FOREIGN KEY (tenant_id, device_id) REFERENCES devices (tenant_id, device_id)
);
CREATE INDEX IF NOT EXISTS device_commands_device_state_issued_idx
 ON device_commands (device_id, state, issued_at);
CREATE TABLE IF NOT EXISTS device_nonces (
 device_id TEXT NOT NULL, nonce TEXT NOT NULL, seen_at TEXT NOT NULL,
 PRIMARY KEY (device_id, nonce)
);
CREATE TABLE IF NOT EXISTS device_relay_tokens (
 token_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, device_id TEXT NOT NULL,
 package TEXT NOT NULL, label TEXT NOT NULL, token_sha256 TEXT NOT NULL UNIQUE,
 created_by TEXT NOT NULL, created_at TEXT NOT NULL, revoked_at TEXT, last_used_at TEXT,
 FOREIGN KEY (tenant_id, device_id) REFERENCES devices (tenant_id, device_id)
);
CREATE TABLE IF NOT EXISTS device_audit (
 event_id INTEGER PRIMARY KEY, tenant_id TEXT NOT NULL, device_id TEXT NOT NULL,
 at TEXT NOT NULL, actor TEXT NOT NULL, op TEXT NOT NULL, target TEXT NOT NULL,
 status TEXT NOT NULL CHECK (status IN ('ok','error','refused')), request_sha256 TEXT
);
CREATE INDEX IF NOT EXISTS device_audit_device_at_idx ON device_audit (device_id, at DESC);
"""


class EnrollmentLimit(ValueError):
    pass


class EnrollmentInvalid(ValueError):
    pass


class DeviceLimit(ValueError):
    pass


class DeviceKeyInUse(ValueError):
    pass


class DeviceRevoked(ValueError):
    pass


class QueueFull(ValueError):
    pass


class RelayTokenLimit(ValueError):
    pass


def store_mode():
    mode = os.environ.get("LEAF_DEVICE_STORE", "sqlite").strip().lower()
    if mode == "postgres_required":
        return "postgres"
    if mode not in {"sqlite", "postgres"}:
        raise StoreUnavailable("device_store_invalid")
    if mode == "sqlite":
        import runtime_posture

        if runtime_posture._deployed_runtime() or platform_link.postgres_required():
            raise StoreUnavailable("device_postgres_required")
    return mode


def _text(value, maximum=200):
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ValueError("invalid_device_field")
    return value


def _validate(name, value):
    # Do not propagate a schema exception: its repr includes credential/body data.
    if not _VALIDATORS[name].is_valid(value):
        raise ValueError("invalid_device_payload")
    json.dumps(value, allow_nan=False)


def _now():
    return datetime.now(timezone.utc)


def _date(value):
    if type(value) in (int, float):
        value = datetime.fromtimestamp(value, timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("invalid_device_time")
    return value.astimezone(timezone.utc)


def _duration(seconds):
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("invalid_device_ttl")
    try:
        duration = timedelta(seconds=seconds)
    except OverflowError:
        raise ValueError("invalid_device_ttl") from None
    if duration <= timedelta(0):
        raise ValueError("invalid_device_ttl")
    return duration


def _limit(value, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError("invalid_device_limit")
    return value


def _secret():
    raw = secrets.token_bytes(32)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii"), hashlib.sha256(raw).hexdigest()


def _hash_secret(value):
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", value) is None:
        return None
    raw = base64.urlsafe_b64decode(value + "=")
    if len(raw) != 32 or base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != value:
        return None
    return hashlib.sha256(raw).hexdigest()


def _lock(sql, key):
    if sql.postgres:
        sql.execute("SELECT pg_advisory_xact_lock(hashtextextended(?, 0))", ("device:" + key,))


def _run(key, operation, *, write=False):
    if store_mode() == "postgres":
        def transaction(conn):
            sql = _SQL(conn, True)
            if write:
                _lock(sql, key)
            return operation(sql)

        return platform_link.platform_db().run_transaction(transaction)
    db_path = os.environ.get("LEAF_DEVICE_DB")
    if not db_path or not db_path.strip():
        raise StoreUnavailable("device_db_unset")
    path = Path(db_path).resolve()
    state_root = Path("/data/state").resolve()
    if path == state_root or state_root in path.parents:
        raise StoreUnavailable("device_sqlite_path_forbidden")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(_SQLITE_SCHEMA)
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        result = operation(_SQL(conn, False))
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _row(row):
    if row is None:
        return None
    result = dict(row)
    for key, value in result.items():
        if isinstance(value, datetime):
            result[key] = value.astimezone(timezone.utc).isoformat()
        elif key in {"platform", "packages", "args", "result"} and isinstance(value, str):
            result[key] = json.loads(value)
    return result


def _json(sql):
    return "?::jsonb" if sql.postgres else "?"


def _device(sql, device_id, tenant_id=None):
    query, params = "SELECT * FROM devices WHERE device_id=?", (device_id,)
    if tenant_id is not None:
        query += " AND tenant_id=?"
        params += (tenant_id,)
    return _row(sql.execute(query, params).fetchone())


def _active(row):
    if row and row["revoked_at"] is not None:
        raise DeviceRevoked("device_revoked")
    return row


def create_enrollment(tenant_id, project_id, label, created_by):
    _text(tenant_id)
    _text(project_id, 128)
    _text(label, 128)
    _text(created_by, 512)

    def operation(sql):
        now = _now()
        count = sql.execute("SELECT COUNT(*) AS n FROM device_enrollments WHERE tenant_id=?"
                            " AND consumed_at IS NULL AND expires_at>?", (tenant_id, now.isoformat())).fetchone()["n"]
        if count >= MAX_ENROLLMENTS:
            raise EnrollmentLimit("enrollment_limit")
        token, digest = _secret()
        enrollment_id = str(uuid.uuid4())
        sql.execute("INSERT INTO device_enrollments (enrollment_id,tenant_id,project_id,label,"
                    "token_sha256,created_by,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?)",
                    (enrollment_id, tenant_id, project_id, label, digest, created_by,
                     now.isoformat(), (now + timedelta(seconds=600)).isoformat()))
        return _row(sql.execute("SELECT * FROM device_enrollments WHERE enrollment_id=?",
                                (enrollment_id,)).fetchone()), token

    return _run("tenant:" + tenant_id, operation, write=True)


def consume_enrollment(token_plaintext, public_key, agent_version, platform, hostname):
    digest = _hash_secret(token_plaintext)
    if digest is None:
        raise EnrollmentInvalid("enrollment_invalid")
    _validate("RegisterRequest", dict(token=token_plaintext, public_key=public_key,
                                    agent_version=agent_version, platform=platform, hostname=hostname))
    if _hash_secret(public_key) is None:
        raise ValueError("invalid_device_public_key")

    def operation(sql):
        enrollment = sql.execute("SELECT * FROM device_enrollments WHERE token_sha256=?", (digest,)).fetchone()
        if enrollment is None:
            raise EnrollmentInvalid("enrollment_invalid")
        tenant = enrollment["tenant_id"]
        _lock(sql, "tenant:" + tenant)
        now, device_id = _now(), str(uuid.uuid4())
        updated = sql.execute("UPDATE device_enrollments SET consumed_at=?,device_id=? WHERE token_sha256=?"
                              " AND consumed_at IS NULL AND expires_at>?",
                              (now.isoformat(), device_id, digest, now.isoformat())).rowcount
        if updated != 1:
            raise EnrollmentInvalid("enrollment_invalid")
        count = sql.execute("SELECT COUNT(*) AS n FROM devices WHERE tenant_id=? AND revoked_at IS NULL",
                            (tenant,)).fetchone()["n"]
        if count >= MAX_DEVICES:
            raise DeviceLimit("device_limit")
        # Serialize the global key claim as well as enforcing its UNIQUE key.
        _lock(sql, "key:" + public_key)
        if sql.execute("SELECT device_id FROM devices WHERE public_key=?", (public_key,)).fetchone():
            raise DeviceKeyInUse("device_key_in_use")
        sql.execute("INSERT INTO devices (device_id,tenant_id,project_id,label,public_key,agent_version,"
                    "platform,hostname,created_at,last_seen_at) VALUES (?,?,?,?,?,?," + _json(sql) + ",?,?,?)",
                    (device_id, tenant, enrollment["project_id"], enrollment["label"], public_key,
                     agent_version, json.dumps(platform), hostname, now.isoformat(), now.isoformat()))
        return _device(sql, device_id)

    return _run("enrollment:" + digest, operation, write=True)


def get_device(device_id):
    return _run("id:" + _text(device_id), lambda sql: _device(sql, device_id))


def list_devices(tenant_id, project_id):
    _text(tenant_id)
    _text(project_id, 128)
    return _run("tenant:" + tenant_id, lambda sql: [_row(row) for row in sql.execute(
        "SELECT * FROM devices WHERE tenant_id=? AND project_id=? ORDER BY created_at,device_id",
        (tenant_id, project_id)).fetchall()])


def touch_device(device_id, agent_version, packages, free_mem_mb):
    _text(device_id)
    _validate("HeartbeatRequest", dict(agent_version=agent_version, packages=packages,
                                     free_mem_mb=free_mem_mb, uptime_s=0))
    if len({item["package"] for item in packages}) != len(packages):
        raise ValueError("duplicate_device_package")
    return _run("id:" + device_id, lambda sql: sql.execute(
        "UPDATE devices SET agent_version=?,packages=" + _json(sql) + ",free_mem_mb=?,last_seen_at=?"
        " WHERE device_id=? AND revoked_at IS NULL",
        (agent_version, json.dumps(packages), free_mem_mb, _now().isoformat(), device_id)).rowcount == 1, write=True)


def revoke_device(tenant_id, device_id, by):
    _text(tenant_id)
    _text(device_id)
    _text(by, 512)

    def operation(sql):
        row = _device(sql, device_id, tenant_id)
        if row is None:
            return False
        now = _now().isoformat()
        sql.execute("UPDATE devices SET revoked_at=COALESCE(revoked_at,?),revoked_by=COALESCE(revoked_by,?)"
                    " WHERE tenant_id=? AND device_id=?", (now, by, tenant_id, device_id))
        sql.execute("UPDATE device_relay_tokens SET revoked_at=COALESCE(revoked_at,?)"
                    " WHERE tenant_id=? AND device_id=?", (now, tenant_id, device_id))
        sql.execute("UPDATE device_commands SET state='expired',finished_at=?,lease_until=NULL"
                    " WHERE tenant_id=? AND device_id=? AND state IN ('queued','leased')", (now, tenant_id, device_id))
        return True

    return _run("id:" + device_id, operation, write=True)


def remember_nonce(device_id, nonce, now):
    _text(device_id)
    if not isinstance(nonce, str) or re.fullmatch(r"[A-Za-z0-9_-]{21}[AQgw]", nonce) is None:
        raise ValueError("invalid_device_nonce")
    now = _date(now)

    def operation(sql):
        if _active(_device(sql, device_id)) is None:
            return False
        sql.execute("DELETE FROM device_nonces WHERE device_id=? AND seen_at<=?",
                    (device_id, (now - timedelta(seconds=120)).isoformat()))
        return sql.execute("INSERT INTO device_nonces (device_id,nonce,seen_at) VALUES (?,?,?)"
                           " ON CONFLICT (device_id,nonce) DO NOTHING",
                           (device_id, nonce, now.isoformat())).rowcount == 1

    return _run("id:" + device_id, operation, write=True)


def _expire(sql, device_id, now):
    sql.execute("UPDATE device_commands SET state='expired',finished_at=?,lease_until=NULL"
                " WHERE device_id=? AND state IN ('queued','leased') AND expires_at<=?",
                (now, device_id, now))


def enqueue_command(tenant_id, device_id, op, args, issued_by, ttl_s=60):
    _text(tenant_id)
    _text(device_id)
    _text(issued_by, 512)
    duration = _duration(ttl_s)

    def operation(sql):
        if _active(_device(sql, device_id, tenant_id)) is None:
            return None
        now, command_id = _now(), str(uuid.uuid4())
        command = dict(command_id=command_id, op=op, args=args, issued_at=now.isoformat(),
                       expires_at=(now + duration).isoformat())
        _validate("Command", command)
        if op == "http.proxy" and "body_json" in args:
            if len(json.dumps(args["body_json"], ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 1048576:
                raise ValueError("device_proxy_payload_too_large")
        _expire(sql, device_id, now.isoformat())
        count = sql.execute("SELECT COUNT(*) AS n FROM device_commands WHERE device_id=? AND state='queued'",
                            (device_id,)).fetchone()["n"]
        if count >= MAX_QUEUED:
            raise QueueFull("device_queue_full")
        sql.execute("INSERT INTO device_commands (command_id,tenant_id,device_id,op,args,issued_by,"
                    "issued_at,expires_at,state) VALUES (?,?,?,?," + _json(sql) + ",?,?,?,'queued')",
                    (command_id, tenant_id, device_id, op, json.dumps(args), issued_by,
                     command["issued_at"], command["expires_at"]))
        return command_id

    return _run("id:" + device_id, operation, write=True)


def lease_commands(device_id, limit=8, lease_s=30):
    _text(device_id)
    _limit(limit, 8)
    duration = _duration(lease_s)

    def operation(sql):
        if _active(_device(sql, device_id)) is None:
            return []
        now = _now()
        _expire(sql, device_id, now.isoformat())
        rows = sql.execute("SELECT * FROM device_commands WHERE device_id=? AND expires_at>?"
                           " AND (state='queued' OR (state='leased' AND lease_until<=?))"
                           " ORDER BY issued_at,command_id LIMIT ?" + (" FOR UPDATE SKIP LOCKED" if sql.postgres else ""),
                           (device_id, now.isoformat(), now.isoformat(), limit)).fetchall()
        result = []
        for raw in rows:
            row = _row(raw)
            until = min(now + duration, _date(row["expires_at"])).isoformat()
            sql.execute("UPDATE device_commands SET state='leased',leased_at=?,lease_until=? WHERE command_id=?",
                        (now.isoformat(), until, row["command_id"]))
            row.update(state="leased", leased_at=now.isoformat(), lease_until=until)
            result.append(row)
        return result

    return _run("id:" + device_id, operation, write=True)


def complete_command(device_id, command_id, result):
    _text(device_id)
    _text(command_id)
    _validate("Result", result)
    if result["command_id"] != command_id:
        return False

    def operation(sql):
        if _active(_device(sql, device_id)) is None:
            return False
        row = _row(sql.execute("SELECT * FROM device_commands WHERE device_id=? AND command_id=?",
                               (device_id, command_id)).fetchone())
        if row is None:
            return False
        if row["state"] == "done":
            # Python equates True and 1; JSON terminal results must not.
            return json.dumps(row["result"], sort_keys=True) == json.dumps(result, sort_keys=True)
        now = _now().isoformat()
        _expire(sql, device_id, now)
        if row["state"] != "leased" or _date(row["expires_at"]) <= _date(now):
            return False
        sql.execute("UPDATE device_commands SET state='done',result=" + _json(sql) +
                    ",finished_at=?,lease_until=NULL WHERE device_id=? AND command_id=? AND state='leased'",
                    (json.dumps(result), now, device_id, command_id))
        return True

    return _run("id:" + device_id, operation, write=True)


def get_command(tenant_id, device_id, command_id):
    _text(tenant_id)
    _text(device_id)
    _text(command_id)
    return _run("id:" + device_id, lambda sql: _row(sql.execute(
        "SELECT * FROM device_commands WHERE tenant_id=? AND device_id=? AND command_id=?",
        (tenant_id, device_id, command_id)).fetchone()))


def create_relay_token(tenant_id, device_id, package, label, created_by):
    _text(tenant_id)
    _text(device_id)
    _text(label, 128)
    _text(created_by, 512)
    if not isinstance(package, str) or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", package) is None:
        raise ValueError("invalid_device_package")

    def operation(sql):
        if _active(_device(sql, device_id, tenant_id)) is None:
            return None
        count = sql.execute("SELECT COUNT(*) AS n FROM device_relay_tokens WHERE device_id=? AND revoked_at IS NULL",
                            (device_id,)).fetchone()["n"]
        if count >= MAX_RELAY_TOKENS:
            raise RelayTokenLimit("relay_token_limit")
        secret, digest = _secret()
        token_id = str(uuid.uuid4())
        sql.execute("INSERT INTO device_relay_tokens (token_id,tenant_id,device_id,package,label,token_sha256,"
                    "created_by,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (token_id, tenant_id, device_id, package, label, digest, created_by, _now().isoformat()))
        return _row(sql.execute("SELECT * FROM device_relay_tokens WHERE token_id=?", (token_id,)).fetchone()), token_id + "." + secret

    return _run("id:" + device_id, operation, write=True)


def resolve_relay_token(token_plaintext):
    if not isinstance(token_plaintext, str) or token_plaintext.count(".") != 1:
        return None
    token_id, secret = token_plaintext.split(".")
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", token_id) is None:
        return None
    digest = _hash_secret(secret)
    if digest is None:
        return None

    def operation(sql):
        row = _row(sql.execute("SELECT * FROM device_relay_tokens WHERE token_id=? AND token_sha256=?"
                               " AND revoked_at IS NULL", (token_id, digest)).fetchone())
        if row is None:
            return None
        _lock(sql, "id:" + row["device_id"])
        # Re-read after the device lock so a concurrent revoke cannot be missed.
        row = _row(sql.execute("SELECT t.* FROM device_relay_tokens t JOIN devices d"
                               " ON d.device_id=t.device_id AND d.tenant_id=t.tenant_id"
                               " WHERE t.token_id=? AND t.token_sha256=? AND t.revoked_at IS NULL"
                               " AND d.revoked_at IS NULL", (token_id, digest)).fetchone())
        if row is None:
            return None
        row["last_used_at"] = _now().isoformat()
        sql.execute("UPDATE device_relay_tokens SET last_used_at=? WHERE token_id=?", (row["last_used_at"], token_id))
        device = _device(sql, row["device_id"], row["tenant_id"])
        row["project_id"] = device["project_id"]
        return row

    return _run("relay:" + token_id, operation, write=True)


def revoke_relay_token(tenant_id, token_id):
    _text(tenant_id)
    _text(token_id)
    return _run("relay:" + token_id, lambda sql: sql.execute(
        "UPDATE device_relay_tokens SET revoked_at=COALESCE(revoked_at,?) WHERE tenant_id=? AND token_id=?",
        (_now().isoformat(), tenant_id, token_id)).rowcount == 1, write=True)


def append_audit(tenant_id, device_id, actor, op, target, status, request_sha256=None):
    _text(tenant_id)
    _text(device_id)
    event = dict(at=_now().isoformat(), actor=actor, op=op, target=target, status=status)
    if request_sha256 is not None:
        event["request_sha256"] = request_sha256
    _validate("AuditEvent", event)

    def operation(sql):
        if _device(sql, device_id, tenant_id) is None:
            return False
        sql.execute("INSERT INTO device_audit (tenant_id,device_id,at,actor,op,target,status,request_sha256)"
                    " VALUES (?,?,?,?,?,?,?,?)", (tenant_id, device_id, event["at"], actor, op, target, status, request_sha256))
        sql.execute("DELETE FROM device_audit WHERE tenant_id=? AND device_id=? AND event_id NOT IN"
                    " (SELECT event_id FROM device_audit WHERE tenant_id=? AND device_id=?"
                    " ORDER BY at DESC,event_id DESC LIMIT ?)", (tenant_id, device_id, tenant_id, device_id, MAX_AUDIT))
        return True

    return _run("id:" + device_id, operation, write=True)


def list_audit(tenant_id, device_id, limit=50):
    _text(tenant_id)
    _text(device_id)
    _limit(limit, 200)
    return _run("id:" + device_id, lambda sql: [_row(row) for row in sql.execute(
        "SELECT * FROM device_audit WHERE tenant_id=? AND device_id=? ORDER BY at DESC,event_id DESC LIMIT ?",
        (tenant_id, device_id, limit)).fetchall()])
