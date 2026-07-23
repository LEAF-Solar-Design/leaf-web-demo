# Runbook: broker tenant kill-switch + ledger-to-usage aggregation

Audience: the on-call operator. Scope: `server/broker.py` (CONTRACT-ADDENDUM
§8, FROZEN). Last verified: 2026-07-22 against the compose stack.

## What the switch is

A per-tenant, persisted disable flag inside the credential-holding broker
process. It is checked FIRST in the `/broker/run` preflight and on
`/broker/extract`, so a disabled tenant never reaches APS, never spends money,
and still gets one honest ledger line (`status: "TENANT_DISABLED"`) per
attempt. State lives in `broker_tenants.json` (env `BROKER_TENANTS`; on the
compose stack: `/data/state/broker_tenants.json` on the `leaf-state` volume).

Fail-closed guarantees you can rely on (all tested):

* A corrupt tenant record reads as DISABLED, never enabled.
* A present-but-unparseable `broker_tenants.json` refuses broker BOOT
  (`BrokerStateError`) instead of starting with every kill flag disarmed.
* A flag that is not a real boolean (`null`, `0`, `""`) reads as DISABLED.

## When to pull it

Runaway spend on one tenant, abusive/compromised tenant traffic, or a request
from the tenant themselves. For a broker-wide stop, stop the broker service:
the app degrades to `BROKER_UNREACHABLE` envelopes and NOTHING can reach APS,
because the broker is the only credential holder.

## Pull the switch (preferred: app-side ops proxy)

The ops surface proxies the broker and needs the internal ops secret
(`LEAF_OPS_SECRET`), never the broker secret:

```bash
curl -s -X POST -H "X-Ops-Secret: $LEAF_OPS_SECRET" \
  http://localhost:8130/api/ops/tenants/<TENANT_ID>/disable
```

Direct-to-broker alternative (from a host that can reach the internal network;
the broker publishes no host port — on compose, exec into the app container):

```bash
docker compose exec app python -c "
import os, requests
r = requests.post('http://broker:8140/broker/tenants/<TENANT_ID>/disable',
                  headers={'X-Broker-Secret': os.environ.get('LEAF_BROKER_SECRET','')})
print(r.status_code, r.text)"
```

`X-Broker-Secret` discipline (F4): required whenever `LEAF_BROKER_SECRET` is
set; constant-time compare; with live auth on and no secret configured the
broker answers 503 (fail-closed) — fix the env, don't work around it.

## Verify it took (do all three)

1. **Broker health** (open endpoint) lists the tenant:
   `GET http://broker:8140/broker/health` → `tenants_disabled` contains the id.
   Via ops read: `GET /api/ops/tenants` shows `disabled: true` on the row.
2. **A run is denied**: any `/broker/run` for the tenant returns the §10
   envelope `error_code: TENANT_DISABLED`, `retryable: false` (HTTP 403-class
   per `DEFAULT_HTTP_STATUS`).
3. **The ledger shows the denial**: the last line of `broker_ledger.jsonl`
   for that tenant has `"status":"TENANT_DISABLED"` —
   `docker compose exec broker tail -n 5 /data/state/broker_ledger.jsonl`.

## Re-enable

Same call with `/enable`; verify `tenants_disabled` no longer lists the id and
a mock run (`aps_live:false`) returns `ok:true`.

## Emergency file-level path (broker API unreachable, switch must stay on)

1. Edit the state file on the volume:
   `docker compose exec broker sh -c 'cat /data/state/broker_tenants.json'` —
   set `{"<TENANT_ID>": {"disabled": true}}` (valid JSON, real booleans).
2. Restart the broker service: `docker compose restart broker`.
3. If the broker refuses to boot with `BrokerStateError`: the file is corrupt.
   That is the switch failing CLOSED — repair the JSON (keep every existing
   record; do not truncate to `{}`, which would disarm every other tenant) and
   restart. Never delete the file to "fix" boot.

## Ledger → usage aggregation (how spend becomes numbers)

One pipeline, no cron, resolved at request time:

```
broker.py  --appends-->  broker_ledger.jsonl  (leaf.broker-ledger-line.v1,
                                               server/broker_ledger.schema.json)
                             |
        da/usage.py reads the SAME file (app-side read is safe: no credential)
                             |
   +-------------------------+---------------------------+
   |                         |                           |
 GET /api/usage         GET /api/ops/tenants        broker preflights
 (tenant self-view:     (operator view: per-tenant  (402 spend cap via
  today/total runs +     runs/usd_est joined with    spent_from_broker_ledger;
  usd_est + cap)         kill-switch state)          429 daily run quota)
```

Counting rules (frozen with the schema):

* Denial lines (`TENANT_DISABLED`, `QUOTA_EXCEEDED`) never count as spend or
  runs — a denied tenant cannot be billed into a deeper hole.
* The daily run quota counts only `aps_live: true` lines (APS_LIVE=0 runs are
  un-metered/free) in the current UTC day (`ts` bucket).
* `usd_est: null` lines add zero spend.
* Missing/empty ledger → zeros, never an error.

Config knobs: `LEAF_TENANT_CAP_USD` / `LEAF_USAGE_CAPS[_FILE]` (spend caps,
OFF unless configured), `LEAF_DAILY_RUN_QUOTA` + tier defaults (ON for metered
tiers on the live path), `LEAF_USAGE_LEDGER` > `BROKER_LEDGER` (app-side read
path; compose already shares `/data/state/broker_ledger.jsonl`).
