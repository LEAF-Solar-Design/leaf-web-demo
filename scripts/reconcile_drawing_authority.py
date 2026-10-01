"""Reconcile one explicitly scoped, drained schema-1 filesystem drawing.

Run with writers drained. This command never changes selectors or blob bytes.
Reverse emits manifests only; use --blob-dir when reimporting that output tree.
Unsupported upload/intake metadata and unfinished reservations fail closed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from datetime import datetime, timezone


def _store():
    da_dir = str(Path(__file__).resolve().parents[1] / "da")
    if da_dir not in sys.path:
        sys.path.insert(0, da_dir)
    import store
    return store


def _integer(value, name, minimum=0, maximum=2**63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"invalid {name}")
    return value


def _timestamp(value):
    if type(value) is not str:
        raise ValueError("timestamp must be an explicit timezone-aware string")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid timestamp") from exc
    if stamp.tzinfo is None:
        raise ValueError("timestamp requires timezone")
    return stamp.astimezone(timezone.utc).isoformat()


def _fields(value, allowed, required):
    if type(value) is not dict or set(value) - allowed or required - set(value):
        raise ValueError("unsupported or missing metadata fields")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate metadata field")
        result[key] = value
    return result


def _path(root, key):
    root = Path(root).resolve()
    path = (root / key).resolve()
    if not path.is_relative_to(root):
        raise ValueError("path escapes explicit directory")
    return path


def validate_manifest(manifest, *, tenant_id, drawing_id, blob_dir):
    """Return semantic canonical metadata after verifying every referenced blob."""
    st = _store()
    st.sanitize_id(tenant_id)
    st.sanitize_id(drawing_id)
    required = {"schema", "tenant_id", "drawing_id", "head", "latest", "versions", "checkout"}
    _fields(manifest, required | {"checkout_fence", "created_at", "updated_at"}, required)
    if type(manifest["schema"]) is not int or manifest["schema"] != 1:
        raise ValueError("unsupported manifest schema")
    if manifest["tenant_id"] != tenant_id or manifest["drawing_id"] != drawing_id:
        raise ValueError("manifest scope mismatch")
    if manifest["checkout"] is not None:
        raise ValueError("checkout lease must be drained")
    fence = _integer(manifest.get("checkout_fence", 0), "checkout fence")
    latest = _integer(manifest["latest"], "latest", 1, 99999999)
    head = _integer(manifest["head"], "head", 1, latest)
    if type(manifest["versions"]) is not list or not manifest["versions"]:
        raise ValueError("drawing requires ready versions")
    versions = []
    seen = set()
    for entry in manifest["versions"]:
        required_version = {"v", "parent", "created", "bytes", "sha256"}
        _fields(entry, required_version | {
            "workitem_id", "tool", "note", "source_ref", "ready_at", "state",
            "reservation_token", "reservation_expires_at",
        }, required_version)
        if entry.get("state", "ready") != "ready" or any(
            entry.get(key) is not None for key in ("reservation_token", "reservation_expires_at")
        ):
            raise ValueError("reservation state must be drained and ready")
        v = _integer(entry["v"], "version", 1, latest)
        if v in seen:
            raise ValueError("duplicate version")
        seen.add(v)
        parent = entry["parent"]
        if parent is not None:
            _integer(parent, "parent", 1, v - 1)
        elif v != 1:
            raise ValueError("invalid ancestry")
        size = _integer(entry["bytes"], "byte count")
        sha = entry["sha256"]
        if type(sha) is not str or re.fullmatch(r"[0-9a-f]{64}", sha) is None:
            raise ValueError("invalid content hash")
        metadata = {}
        for key in ("workitem_id", "tool", "note", "source_ref"):
            value = entry.get(key)
            if value is not None and (type(value) is not str or "\x00" in value):
                raise ValueError(f"invalid {key} metadata")
            metadata[key] = value
        if metadata["source_ref"] is not None and re.fullmatch(r"[0-9a-f]{64}", metadata["source_ref"]) is None:
            raise ValueError("invalid source_ref metadata")
        # Graph bundles are immutable companions outside this drawing-only slice.
        if (metadata["note"] or "").startswith("solar-bundle:"):
            raise ValueError("unsupported graph bundle metadata")
        key = st.drawing_version_key(tenant_id, drawing_id, v)
        path = _path(blob_dir, key)
        digest = hashlib.sha256()
        measured = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                measured += len(chunk)
        if measured != size or digest.hexdigest() != sha:
            raise ValueError("blob hash/size mismatch")
        created = _timestamp(entry["created"])
        ready = entry.get("ready_at", created)
        versions.append({"v": v, "parent": parent, "created": created,
                         "ready_at": _timestamp(ready) if ready is not None else None,
                         "bytes": size, "sha256": sha, **metadata})
    if max(seen) != latest or head not in seen or 1 not in seen:
        raise ValueError("invalid head/latest ancestry")
    if any(entry["parent"] is not None and entry["parent"] not in seen for entry in versions):
        raise ValueError("missing ancestor")
    versions.sort(key=lambda entry: entry["v"])
    created = _timestamp(manifest.get("created_at", versions[0]["created"]))
    updated = _timestamp(manifest.get("updated_at", max(entry["created"] for entry in versions)))
    return {"schema": 1, "tenant_id": tenant_id, "drawing_id": drawing_id,
            "head": head, "latest": latest, "checkout": None,
            "checkout_fence": fence, "created_at": created, "updated_at": updated,
            "versions": versions}


def _snapshot(conn, tenant, drawing):
    params = (tenant, drawing)
    row = conn.execute(
        "SELECT * FROM drawing_store_manifests WHERE tenant_id = %s AND drawing_id = %s",
        params,
    ).fetchone()
    rows = conn.execute(
        "SELECT * FROM drawing_store_versions WHERE tenant_id = %s AND drawing_id = %s ORDER BY version",
        params,
    ).fetchall()
    upload = conn.execute(
        "SELECT 1 FROM drawing_upload_attempts WHERE tenant_id = %s AND drawing_id = %s "
        "AND (status IN ('extracting', 'purging') OR extraction_owner IS NOT NULL OR purge_owner IS NOT NULL)",
        params,
    ).fetchone()
    if upload:
        raise ValueError("upload leases must be drained")
    if row is None:
        return None
    if any(row[key] is not None for key in (
        "checkout_holder", "checkout_acquired_at", "checkout_expires_at",
    )):
        raise ValueError("checkout lease must be drained")
    versions = []
    for version in rows:
        if version["state"] != "ready" or any(version[key] is not None for key in (
            "reservation_token", "reservation_expires_at", "intake_ref", "intake_sha256",
        )):
            raise ValueError("unsupported intake or undrained reservation metadata")
        if version["object_key"] != _store().drawing_version_key(tenant, drawing, version["version"]):
            raise ValueError("unsupported version object key")
        versions.append({"v": version["version"], "parent": version["parent_version"],
                         "created": version["created_at"].isoformat(),
                         "ready_at": version["ready_at"].isoformat() if version["ready_at"] else None,
                         "bytes": version["byte_count"], "sha256": version["content_sha256"],
                         **{key: version[key] for key in ("workitem_id", "tool", "note", "source_ref")}})
    return {"schema": 1, "tenant_id": tenant, "drawing_id": drawing,
            "head": row["head"], "latest": row["latest"], "checkout": None,
            "checkout_fence": row["checkout_fence"], "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(), "versions": versions}


def _insert(conn, source, target):
    tenant, drawing = source["tenant_id"], source["drawing_id"]
    inserted = {"manifests": 0, "versions": 0}
    if target is not None:
        if {k: v for k, v in source.items() if k != "versions"} != {k: v for k, v in target.items() if k != "versions"}:
            raise ValueError("manifest conflict")
        incoming = {entry["v"]: entry for entry in source["versions"]}
        if any(incoming.get(entry["v"]) != entry for entry in target["versions"]):
            raise ValueError("version conflict")
    else:
        conn.execute(
            "INSERT INTO drawing_store_manifests "
            "(tenant_id,drawing_id,head,latest,checkout_fence,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (tenant, drawing, source["head"], source["latest"], source["checkout_fence"],
             source["created_at"], source["updated_at"]),
        )
        inserted["manifests"] = 1
    existing = {entry["v"] for entry in target["versions"]} if target else set()
    for entry in source["versions"]:
        if entry["v"] in existing:
            continue
        conn.execute(
            "INSERT INTO drawing_store_versions "
            "(tenant_id,drawing_id,version,parent_version,object_key,byte_count,content_sha256,"
            "workitem_id,tool,note,source_ref,state,created_at,ready_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'ready',%s,%s)",
            (tenant, drawing, entry["v"], entry["parent"],
             _store().drawing_version_key(tenant, drawing, entry["v"]), entry["bytes"],
             entry["sha256"], entry["workitem_id"], entry["tool"], entry["note"],
             entry["source_ref"], entry["created"], entry["ready_at"]),
        )
        inserted["versions"] += 1
    return inserted


def _publish(path, manifest):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".drawing-manifest-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def reconcile(*, mode, source_dir, tenant_id, drawing_id, output_dir=None, blob_dir=None, database=None):
    """Import, compare, or export a single drawing; exceptions abort all inserts."""
    if mode not in {"backfill", "parity", "reverse"}:
        raise ValueError("unsupported mode")
    if not os.environ.get("DATABASE_URL"):
        raise RuntimeError("explicit DATABASE_URL required")
    st = _store()
    st.sanitize_id(tenant_id)
    st.sanitize_id(drawing_id)
    source_dir = Path(source_dir).resolve()
    blob_dir = Path(blob_dir).resolve() if blob_dir is not None else source_dir
    key = st.manifest_key(tenant_id, drawing_id)
    source = None
    raw = None
    if mode != "reverse":
        raw = _path(source_dir, key).read_bytes()
        source = validate_manifest(json.loads(raw, object_pairs_hook=_pairs), tenant_id=tenant_id,
                                   drawing_id=drawing_id, blob_dir=blob_dir)
    else:
        output_dir = Path(output_dir).resolve() if output_dir is not None else source_dir.with_name(source_dir.name + "-drawing-reverse")
        if any(output_dir == root or output_dir.is_relative_to(root) or root.is_relative_to(output_dir)
               for root in (source_dir, blob_dir)):
            raise ValueError("reverse requires a separate output tree")
        _path(output_dir, key)
    db = database if database is not None else st._db()
    db.assert_schema_current()
    inserted = {"manifests": 0, "versions": 0}
    with db.transaction(isolation="serializable", read_only=mode != "backfill") as conn:
        # All reconciler runs for this identity coordinate; application writers
        # do not take this lock, so operators must drain them separately.
        fence = int.from_bytes(hashlib.sha256(f"{tenant_id}/{drawing_id}".encode()).digest()[:8], "big", signed=True)
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (fence,))
        target = _snapshot(conn, tenant_id, drawing_id)
        if target is not None:
            target = validate_manifest(target, tenant_id=tenant_id, drawing_id=drawing_id, blob_dir=blob_dir)
        if mode == "backfill":
            inserted = _insert(conn, source, target)
            target = validate_manifest(_snapshot(conn, tenant_id, drawing_id), tenant_id=tenant_id,
                                       drawing_id=drawing_id, blob_dir=blob_dir)
            if target != source:
                raise ValueError("post-import conflict")
        if mode != "reverse":
            if _path(source_dir, key).read_bytes() != raw:
                raise ValueError("source manifest changed during reconciliation")
            # Recheck bytes before committing, without publishing any blobs.
            validate_manifest(source, tenant_id=tenant_id, drawing_id=drawing_id, blob_dir=blob_dir)
        elif target is None:
            raise ValueError("drawing not found")
    if mode == "reverse":
        _publish(_path(output_dir, key), target)
    parity = target == source if mode != "reverse" else True
    return {"schema": "leaf.drawing-authority-reconciliation.v1", "mode": mode,
            "parity": parity, "inserted": inserted, "inserted_total": sum(inserted.values()),
            "version_count": len(target["versions"]) if target else 0,
            "output_dir": str(output_dir) if mode == "reverse" else None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("backfill", "parity", "reverse"), required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--blob-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--drawing-id", required=True)
    args = parser.parse_args(argv)
    try:
        receipt = reconcile(**vars(args))
    except Exception as exc:
        print(f"drawing authority reconciliation failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["parity"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
