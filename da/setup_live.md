# Lane A — Live APS setup (ROOT runs this once)

These are the **only mutating APS calls**, and they belong to ROOT. Lanes never run them.
After this one-time provisioning, `da/client.py` can submit WorkItems with `APS_LIVE=1`.

## Confirmed real values (verified live 2026-07-17, read-only)

| Thing | Value |
|---|---|
| Auth | 2-legged, creds at `~/.aps/credentials.json` (client_id/client_secret) |
| Token endpoint | `POST https://developer.api.autodesk.com/authentication/v2/token` |
| Scopes | `code:all data:read data:write bucket:create bucket:read` |
| App nickname (owner) | `iBZFSm0zj8SGcjm40Xpm7IQHNQMSmNi6rBmAJSp9k7WtCOGp` |
| Engine (live-available) | `Autodesk.AutoCAD+26_0` (also present: +25_1, +24_3, +25_0, +24_2, +24_1, +24) |
| DA host | `https://developer.api.autodesk.com/da/us-east/v3` |
| OSS host | `https://developer.api.autodesk.com/oss/v2` |
| Bucket key (derived) | `leaf-web-demo-ibzfsm0zj8sgcjm4` (override with `APS_BUCKET`) |
| Activity ids | `<nickname>.LeafExtract+prod`, `<nickname>.LeafTool_<op>+prod` |

> The nickname above is the **default** (client-id derived). If you prefer a short
> nickname, `PATCH /da/us-east/v3/forgeapps/me` with `{"nickname":"leafdemo"}` **once,
> before creating any appbundle/activity** (irreversible if you already have any).
> If you do, set `APS_NICKNAME`… actually the client reads the live nickname each run,
> so no client change is needed — just re-run provisioning.

---

## Multi-tenant model (aps-multitenant-provisioning)

The APS leg is multi-tenant. Two ROOT-assumed defaults are in force (both
reversible; flip only after operator confirmation for a production promotion):

- **Isolation strategy = shared bucket + per-tenant key PREFIX** (default). Every
  per-run scratch object is keyed `t/<tenant_id>/in|out/...` in the single shared
  persistent bucket `leaf-web-store-ibzfsm0zj8sgcjm4`. A tenant id is validated to
  a safe `[a-z0-9-]` segment (UuidV7 or slug) so it can never traverse out of its
  prefix. `tenant_id=None` reproduces the exact legacy single-tenant keys
  (`in/...`, `out/...`) byte-for-byte, so the existing live path is unchanged.
  **Reversible alternative:** a per-tenant BUCKET (`da.tenant.tenant_bucket`,
  e.g. `leaf-web-store-t-<tenant>`) — cleaner blast radius, but bounded by
  OSS bucket-count limits + per-tenant provisioning cost. Provision it with
  `python da/provision_live.py --tenant <id> --per-bucket`.

- **Billing posture = central broker-side HARD pre-flight cap** (default). The
  broker (`server/broker.py`) is the attribution + kill-switch chokepoint: it
  reads prior spend from its authoritative ledger (`server/broker_ledger.jsonl`)
  and calls `da/usage.check_cap` BEFORE any APS call; over-cap tenants get a
  `{error_code:"quota_exceeded", retryable:false}` envelope (HTTP 402) and never
  touch APS. `da/usage.py` is the local fallback + cap logic. Caps are **OFF**
  unless a positive cap is configured (`LEAF_TENANT_CAP_USD`, or per-tenant
  `LEAF_USAGE_CAPS`/`LEAF_USAGE_CAPS_FILE`). **Reversible alternative:** tenant
  BYO-APS credentials, which would make the cap advisory instead of a hard gate.

### Two OSS key namespaces (no collision)

| Namespace | Key shape | Owner | Lifetime |
|---|---|---|---|
| Persistent versioned drawing store | `tenants/<t>/drawings/<d>/v/<n>.dwg` | `da/store.py` (Wave 1) | permanent, immutable versions |
| Per-run ephemeral scratch | `t/<t>/in/<ts>_...`, `t/<t>/out/<ts>_...` | `da/tenant.py` + `da/client.py` | throwaway per WorkItem |

The roots `tenants/` and `t/` never overlap, so the versioned store and per-run
scratch space are isolated from each other and per tenant.

### Concurrency ceiling

`APS_MAX_CONCURRENCY` (env, default **1**) is the account Flex ceiling: at most
that many WorkItems are in flight across ALL tenants. `da/queue.py` provides the
round-robin-fair scheduler (`FairQueue`) and the process-global `admit()` gate
that `da/client.submit_workitem` wraps around the LIVE submit. Raise it **in
lockstep** with the Autodesk Flex-limit raise drafted in
`docs/aps-concurrency-raise-request.md`.

### Orphan reaping

A closed tab / expired lease must not leave a WorkItem billing forever. The
app/jobs side marks orphaned jobs (`server/jobs.mark_job_closed`, tab-close
endpoint `POST /api/jobs/{job_id}/close`, heartbeat-stale reaper). The
credential-holding broker performs the actual `DELETE /workitems/{id}` via
`POST /broker/reap` → `da/reaper.sweep` → `da/client.cancel_workitem`, gated by
`APS_LIVE=1` + `BROKER_REAP_LIVE=1`. `da/callbacks.py` is the event-driven
(`onComplete`) upgrade path for when the platform has a public callback host.

### Dry-run (no APS mutation, no network)

```powershell
python da/provision_live.py --dry-run --tenant demo-a           # prints per-tenant ids
python da/provision_live.py --dry-run --tenant demo-a --per-bucket
```

---

## Fast path (recommended): one script

```powershell
# from C:/tmp/leaf-web-demo
python da/provision_live.py
# optional: also create per-tool script Activities from Lane B's registry
python da/provision_live.py --tools engine/registry.json
```

`provision_live.py` creates the bucket, posts the `LeafExtract` Activity (pure-LISP,
no AppBundle), and adds the `prod` alias. It is idempotent (409 = already exists).
For `--tools`, each `kind:"script"` tool needs an inline `engine_script` (LISP that
writes `result.json`); Lane B supplies those.

Verify, then run one real extract:

```powershell
$env:APS_LIVE = "1"
python da/da_extract.py C:/tmp/leaf-web-demo/data/rooftop_demo.dwg rooftop_live.intake.json
# expect: ~2345 polylines, 4 layers, matching the golden sample
```

---

## Manual path (exact ordered curl-equivalents, if you want to see each call)

All requests need `-H "Authorization: Bearer $TOKEN"` where `$TOKEN` is a fresh
2-legged token (mint it with the scopes above; `da/client.py auth_token()` does this).

### 1. Create the OSS bucket (once)
```
POST https://developer.api.autodesk.com/oss/v2/buckets
Headers: Authorization: Bearer <TOKEN>; Content-Type: application/json; x-ads-region: US
Body:  {"bucketKey": "leaf-web-demo-ibzfsm0zj8sgcjm4", "policyKey": "transient"}
# 200 created, or 409 if it already exists (fine)
```

### 2. Create the extract Activity (once)
POST body = output of `python -c "import sys;sys.path.insert(0,'da');import client,json;print(json.dumps(client.extract_activity_spec()))"`
```
POST https://developer.api.autodesk.com/da/us-east/v3/activities
Headers: Authorization: Bearer <TOKEN>; Content-Type: application/json
Body:  { "id":"LeafExtract", "engine":"Autodesk.AutoCAD+26_0",
         "commandLine":["$(engine.path)\\accoreconsole.exe /i \"$(args[HostDwg].path)\" /s \"$(settings[script].path)\""],
         "parameters":{ "HostDwg":{"verb":"get","required":true,"localName":"input.dwg"},
                        "Result":{"verb":"put","required":true,"localName":"result.txt"} },
         "settings":{ "script":{ "value":"<the CRLF LISP block from da/lisp.py>" } },
         "description":"Leaf headless DWG intake extraction (LISP families dump)." }
```

### 3. Alias the Activity to `prod` (once)
```
POST https://developer.api.autodesk.com/da/us-east/v3/activities/LeafExtract/aliases
Body:  {"id":"prod","version":1}
```

### 4. (per run — the client does this) Upload DWG, submit WorkItem, download result
The client (`da/client.py`) does steps 4a–4e automatically on the live path:
- **4a upload** — OSS Direct-to-S3, 3 calls:
  `GET  /oss/v2/buckets/<bucket>/objects/<key>/signeds3upload` → `{uploadKey, urls[0]}`
  `PUT  urls[0]` (raw DWG bytes, no auth header)
  `POST /oss/v2/buckets/<bucket>/objects/<key>/signeds3upload` body `{"uploadKey":"…"}`
- **4b submit** — `POST /da/us-east/v3/workitems` with the body `da_extract.py --dry-run` prints
  (activityId `<nickname>.LeafExtract+prod`; HostDwg verb get, Result verb put, both OSS REST
  URLs with a Bearer header DA resolves natively).
- **4c poll** — `GET /da/us-east/v3/workitems/{id}` every 2 s until status ∉ {pending,inprogress}.
- **4d download** — OSS signed download of the `Result` object:
  `GET /oss/v2/buckets/<bucket>/objects/<key>/signeds3download` → `{url}`, then GET the bytes.
- **4e parse** — `intake_parse.parse_text()` → Intake JSON (§1), byte-identical to the golden sample.

---

## Tools (§2) on DA
- `kind:"script"` tools → one `LeafTool_<engine_op>` **script Activity** each (same shape as
  `LeafExtract` but with an extra `Params` input for the `data:` JSON of the run's params, and
  a `Result` output `result.json`). The LISP must write `result.json` as either a full §3
  envelope or just `{result, overlay?}`; the client wraps it to the full §3 envelope.
- `kind:"appbundle"` tools → upload a compiled AppBundle first:
  `POST /da/us-east/v3/appbundles {"id":"…","engine":"Autodesk.AutoCAD+26_0"}` → `uploadParameters`
  → multipart `POST` the zip to `uploadParameters.endpointURL` → alias → reference it in the
  Activity's `appbundles:["<nickname>.<id>+prod"]`. (Not needed for the MVP script tools.)

## References (docs used, 2026-07)
- DA v3 overview: https://aps.autodesk.com/en/docs/design-automation/v3/developers_guide/overview/
- Execute WorkItem tutorial: https://get-started.aps.autodesk.com/tutorials/design-automation/execute-workitem/
- AutoCAD tutorial tasks (bucket→appbundle→activity→workitem): https://aps.autodesk.com/en/docs/design-automation/v3/tutorials/autocad/
- POST appbundles/activities/workitems: https://aps.autodesk.com/en/docs/design-automation/v3/reference/http/workitems-POST/
- OSS Direct-to-S3 migration: https://aps.autodesk.com/blog/data-management-oss-object-storage-service-migrating-direct-s3-approach
- App-managed bucket + signed S3 upload: https://aps.autodesk.com/en/docs/data/v2/tutorials/app-managed-bucket/
