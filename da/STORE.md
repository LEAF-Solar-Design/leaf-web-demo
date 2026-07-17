# da/store.py — persistent, versioned per-tenant DWG drawing store

> Additive spec for `da/store.py`. It does **not** change the FROZEN `contract/CONTRACT.md`;
> the §5 `extract`/`run_tool`/`auth_token` interface stays intact (see "client.py changes").

## Why

The old model used a **transient** OSS bucket and re-uploaded the DWG to a throwaway
timestamped key (`in/<ts>_<name>.dwg`) on every call. Transient OSS objects **expire**
on APS's schedule — fatal for a drawing that must survive, carry a version history, and
support undo. This store replaces that with:

1. A **persistent** OSS bucket (objects never expire).
2. A **per-tenant, per-drawing, immutable-versioned** object-key scheme + a `manifest.json`
   version index we maintain ourselves (APS OSS has **no native versioning** and **no
   atomic compare-and-swap**).

This is the foundation the DWG **write path** (additive/subtractive edits, undo/redo)
builds on. `store.put_drawing` is the primitive that path calls.

## Persistent bucket (recorded from live provisioning)

| field | value |
|-------|-------|
| bucket key | `leaf-web-store-ibzfsm0zj8sgcjm4` |
| region | `US` |
| policy | `persistent` (immutable at creation) |
| engine | `Autodesk.AutoCAD+26_0` |

- The bucket key stem changed from `leaf-web-demo-` (transient) to `leaf-web-store-`
  (persistent). OSS bucket policy is **immutable at creation**, so you cannot flip the
  old bucket — a **fresh** persistent bucket key is provisioned and the old transient
  `leaf-web-demo-*` bucket is **abandoned to expire on its own** (not deleted).
- Override with env `APS_BUCKET`. Provision with: `python da/provision_live.py`.
- Live provisioning + round-trip receipt is at the bottom of this file.

## Object-key scheme

```
tenants/{tenant_id}/drawings/{drawing_id}/v/{version:08d}.dwg   # immutable DWG bytes, 1/version
tenants/{tenant_id}/drawings/{drawing_id}/manifest.json         # version index + checkout lock
```

- `tenant_id` / `drawing_id` are sanitized to `[a-z0-9-]` (lowercased, disallowed runs → `-`).
  `drawing_id` is ideally a **UUIDv7** (`store.new_drawing_id()`) to match cadwalk-studio
  `src/lib/tenancy/types.ts` `UuidV7`; a uuid string is already `[a-z0-9-]`.
- `drawing_version_key(t, d, v)` is deterministic and matches
  `^tenants/[a-z0-9-]+/drawings/[a-z0-9-]+/v/\d{8}\.dwg$`.
- **Immutability is the undo guarantee: we NEVER PUT over an existing `.../v/NNNNNNNN.dwg`
  key.** Every version is a distinct, permanent object.

## Manifest (the version chain)

```jsonc
{
  "schema": 1,
  "tenant_id": "...", "drawing_id": "...",
  "head": 3,       // current version pointer; undo repoints this (redo stays possible)
  "latest": 3,     // highest version ever written; monotonic immutability guard
  "versions": [
    {"v":1,"parent":null,"created":"<iso>","bytes":N,"sha256":"...","workitem_id":null,"tool":null,"note":"initial ingest"},
    {"v":2,"parent":1,   "created":"<iso>","bytes":N,"sha256":"...","workitem_id":"...","tool":"add-panel-row","note":null},
    {"v":3,"parent":2, ...}
  ],
  "checkout": {"holder":"<session-id>","acquired":"<iso>","expires":"<iso>"}   // or null
}
```

- **`head` vs `latest`:** `undo` sets `head=2` but `latest` stays `3` (v3's object still
  exists → **redo** is possible). A new write from `head=2` creates `v=latest+1` with
  `parent=2` — a branch in the DAG. `parent` linkage is the whole model; we keep it simple.

## Storage backend abstraction (so tests run offline)

`store.StorageBackend` is a 3-method blob interface — `get(key)`, `put(key, data)`,
`exists(key)`:

- `store.OSSBackend` — delegates to `client.upload_object` / `download_object` /
  `signed_download_url`. Used on the live path. `exists()` uses a signed-download probe
  (404 → absent).
- `store.InMemoryBackend` — a dict, used by `da/test_store.py`; the whole suite makes
  **ZERO network/APS calls**.

Every store primitive takes a `backend`, so tests inject the in-memory one and the live
path injects OSS.

## Primitives (`da/store.py`)

| function | contract |
|----------|----------|
| `ingest_drawing(be, tenant_id, local_path, drawing_id=None)` | PUT v1 + write initial manifest → `{"drawing_id","version":1}`. Refuses to clobber an existing drawing. |
| `put_drawing(be, tenant_id, drawing_id, local_path, parent_version, meta=None)` | Append immutable `v=latest+1` (parent=`parent_version`), advance head+latest → `int`. **The write-path primitive.** |
| `resolve_version(be, tenant_id, drawing_id, version="head")` | `version` = int, `"head"`, or `"latest"` → `(version_int, object_key)`. |
| `undo(be, tenant_id, drawing_id)` | Repoint head → head's parent (no deletion; redo-able) → new head `int`. Raises at root. |
| `acquire_checkout(be, tenant_id, drawing_id, holder, ttl_s)` | Single-writer lock. Held-by-other-and-active → `False`; same holder refreshes; expired lock is free → `True`. |
| `release_checkout(be, tenant_id, drawing_id, holder=None)` | Clear the lock; a given `holder` may only release an active lock it owns. |
| `drawing_version_key`, `manifest_key`, `sanitize_id`, `new_drawing_id`, `load_manifest`, `save_manifest` | key + manifest helpers. |

## client.py changes (additive, FROZEN §5 preserved)

- `create_bucket(policy="persistent")` — default flipped from `"transient"`; POST body
  carries `policyKey == "persistent"`.
- `bucket_key()` — stem `leaf-web-store-` (fresh persistent bucket; old transient one
  abandoned).
- `extract(dwg_local_path, dry_run=False, *, tenant_id=None, drawing_id=None, version="head", backend=None)`
  and `run_tool(..., *, tenant_id=None, drawing_id=None, version="head", backend=None)` —
  when `tenant_id` **and** `drawing_id` are supplied, `HostDwg` references the persistent
  **versioned store key** (a signed download URL over `/v/NNNNNNNN.dwg`) instead of
  re-uploading a throwaway `in/<ts>_` object. When omitted → **exact legacy behavior**.
  The `run_tool` **Result** output stays ephemeral (`out/<ts>_...result.json`) for read
  tools; the write path (out of scope) turns a result into a new version via `put_drawing`.
- Dry-run now builds bodies **without minting a token** (`_input_arg(..., live=False)`),
  so a dry run makes no live call — the Authorization header was redacted in the returned
  body anyway, so the output shape is unchanged.

## Concurrency limitation (read this)

OSS manifest writes are **NON-ATOMIC** — there is no compare-and-swap on OSS, so the
checkout lock and version index are **best-effort**. Under truly concurrent writers a
lost-update on `manifest.json` is possible. The write path serializes on the checkout
lock (single-writer model, appropriate for unmergeable DWG blobs), which is sufficient
for the demo. **Production promotes the version index + lock to Postgres for atomic CAS**
— that hardening is explicitly out of scope here.

## Out of scope

The DWG-mutating write path (consumes `put_drawing`); Postgres promotion of the index for
atomic CAS/locking; tenant identity/auth resolution; billing/quota; old-version GC /
retention; strict multi-writer CAS correctness beyond single-writer checkout.

---

## Live provisioning + round-trip receipt

**Provisioned 2026-07-17** (`python da/provision_live.py`, exit 0):

- Persistent bucket **created fresh**: `leaf-web-store-ibzfsm0zj8sgcjm4`, region `US`,
  `policyKey=persistent`, owner `iBZFSm0zj8SGcjm4...`. (Extract activity/alias already
  existed → 409, tolerated.)

**Pure-OSS persistence round-trip (ZERO WorkItems, spend ≈ $0):**

| check | value |
|-------|-------|
| tenant / drawing_id | `acme` / `019f724b-bcf3-7980-9a66-98521d8971aa` (UUIDv7) |
| v1 object key | `tenants/acme/drawings/019f724b-bcf3-7980-9a66-98521d8971aa/v/00000001.dwg` |
| manifest key | `tenants/acme/drawings/019f724b-bcf3-7980-9a66-98521d8971aa/manifest.json` |
| local bytes / downloaded bytes | 153522 / 153522 |
| sha256 (both) | `16390e082b6c73d835f21199e7777ae3a60680deae612f81d92048fd26544d21` ✅ match |
| manifest lists v1 | yes, `parent=null`, `head=1`, `latest=1`, `checkout=null` |
| WorkItems used | 0 (OSS PUT/GET + manifest only) |

Round-trip proves the drawing survives a store ingest and re-downloads byte-identical
from the **persistent** bucket. (The old transient `leaf-web-demo-*` bucket is left to
expire on its own.)
