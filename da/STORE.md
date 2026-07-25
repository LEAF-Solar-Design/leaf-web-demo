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
  "checkout": {"holder":"<session-id>","acquired":"<iso>","expires":"<iso>","fence":N}, // or null
  "checkout_fence": N                        // monotonic lock generation; survives release
}
```

- **`checkout.holder` is a DISPLAY LABEL, never proof.** It is caller-supplied and
  `GET /api/drawings/{id}/versions` publishes it, so anyone who can read the drawing can
  name it. Ownership is proved by the opaque capability the acquire route mints
  (`server/checkout_capability.py`), which is bound to tenant + authenticated subject +
  drawing + `fence` and never appears on any read.
- **`checkout_fence` is monotonic and lives at the MANIFEST level**, not inside `checkout`,
  because `release_checkout` clears that dict. A counter that restarted at 1 on the next
  acquire would repeat a generation, and a capability minted for the earlier lease would
  verify against the later one. The postgres authority stores this as a manifest column
  release leaves untouched; the legacy authority mirrors it here.

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
| `undo(be, tenant_id, drawing_id, *, holder=None, fence=None)` | Repoint head → head's parent (no deletion; redo-able) → new head `int`. Raises at root. `holder`/`fence` apply the SAME single-writer check as `put_drawing`: head is drawing state every session reads, so moving it is not a lesser act than publishing. |
| `redo(be, tenant_id, drawing_id, *, holder=None, fence=None)` | Inverse of `undo`; same single-writer check. |
| `acquire_checkout(be, tenant_id, drawing_id, holder, ttl_s, *, expected_fence=None, strict_owner=False)` | Single-writer lock; every acquire stamps a NEW `fence`. Default: held-by-other-and-active → `False`, same holder refreshes. `strict_owner=True` ignores the holder label and refreshes a LIVE lease only when `expected_fence` matches — the rule the routes use, so taking over a live lease needs proof of it rather than knowledge of a public string. Expired lock is free under both → `True`. |
| `release_checkout(be, tenant_id, drawing_id, holder=None, *, expected_fence=None)` | Clear the lock. `expected_fence` (the generation a capability proved) REPLACES the holder comparison, so a release cannot land on a lease that started after the check. |
| `checkout_active(co)` | Is a manifest `checkout` dict a live lease right now? The one "expired means free" rule, read rather than re-implemented. |
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

**Clean-cutover app provisioned 2026-07-19** (`Leaf Design Automation Production`):

- New persistent bucket: `leaf-web-store-czjiu4w9ok9fsowa`, region `US`,
  `policyKey=persistent`, owner `czjIu4W9OK9fSoWA...`.
- Copied all **40 legacy objects / 8,750,523 bytes** into the new bucket without
  modifying the legacy bucket. The new bucket contained **44 objects / 8,815,241
  bytes** after copy (the four additional objects are the new-app smoke inputs and
  outputs).
- Verified the new app with a live AutoCAD extract and `count-by-layer` WorkItem;
  the tool returned one entity on `Panels` in 2.43 engine-seconds (~$0.0068).
- Legacy credentials and bucket remain intact as rollback until the runtime cutover
  has been deployed and observed.

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
