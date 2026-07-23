# Agent ops runbook: gate-chain state in the compose stack

How an operator runs the conversational agent's gate chain (`server/agent_gate.py`,
design: `docs/AGENT-SPINE-DESIGN.md` §5) in the `docker compose` stack. Four
drills: kill switch, policy edit + validation, approval TTL/expiry, rate-state
reset. Every command below is runnable as written.

## Where state lives

`docker-compose.yml` pins every MUTABLE gate artifact under `/data/state` on the
`leaf-state` named volume, so all of it survives an app-container restart (and is
dropped only by `docker compose down -v`):

| Env (app service) | Path in container | What it is |
| --- | --- | --- |
| `LEAF_AGENT_KILL_FILE` | `/data/state/agent.disabled` | file-presence kill switch |
| `LEAF_AGENT_APPROVALS_DIR` | `/data/state/agent_approvals` | pending-approval records (one JSON file per confirmation id) |
| `LEAF_AGENT_GRANTS_FILE` | `/data/state/agent_session_grants.json` | confirm-once session grants |
| `LEAF_AGENT_RATE_FILE` | `/data/state/agent_rate_state.json` | per-tenant per-hour budget snapshot (the authority, not a cache) |
| `LEAF_AGENT_AUDIT` | `/data/state/agent_audit.jsonl` | append-only gate-decision audit |
| `LEAF_AGENT_LEDGER` | `/data/state/agent_ledger.jsonl` | append-only per-turn metering ledger |
| `LEAF_AGENT_TENANTS_FILE` | `/data/state/agent_tenants.json` | per-tenant kill flag + tighten-only overlay |

The one deliberate exception: `LEAF_AGENT_POLICY_FILE` points at
`/app/server/agent_policy.json`, the catalog baked into the image from the
git-versioned `server/agent_policy.json`. The policy is CONFIG, not state; it
changes via git + image rebuild (drill 2), never by hand inside the volume.

## Preconditions (all drills)

Run everything from the repo root (the directory holding `docker-compose.yml`).
The drills exercise the real gate consult (`POST /internal/agent/gate`), which
answers 401 unless `LEAF_APP_DISPATCH_SECRET` is set (fail closed, wire contract
§0), so boot the stack with the secret exported:

```bash
export LEAF_APP_DISPATCH_SECRET=ops-drill-secret
docker compose up -d --build --wait
```

(PowerShell: `$env:LEAF_APP_DISPATCH_SECRET = 'ops-drill-secret'` before
`docker compose up -d --build --wait`.)

The demo stack runs with auth off, so ANY `tenant_id` string resolves to the
full-access `demo` tier (`server/deps.py` `backedge_tenant` off-auth +
`entitlements.resolve_tier`). The gate's state is DURABLE and namespaced by
tenant: rate buckets are keyed by tenant, confirm-once grants by
tenant + session + action + tool. So the drills run under a FRESH tenant per
run, never `demo`: old `demo` stamps (real traffic or an earlier drill inside
the same hour) would otherwise eat the budget the drill expectations assume.
The `${BASHPID:-$$}-$RANDOM` suffix defuses collisions: subshells inherit
`$$` from the parent shell (only `BASHPID` tells them apart), and `$RANDOM`
covers two initializations in the same shell within the same second:

```bash
gate() { curl -s -X POST http://localhost:8130/internal/agent/gate \
  -H 'Content-Type: application/json' \
  -H "X-Dispatch-Secret: $LEAF_APP_DISPATCH_SECRET" \
  -d "$1"; echo; }
RUN_TAG="$(date +%s)-${BASHPID:-$$}-$RANDOM"
DT="ops-drill-$RUN_TAG"       # drill tenant: fresh rate/grant/approval namespace
SID="ops-session-$RUN_TAG"
RT="rate-drill-$RUN_TAG"      # separate tenant drill 4 exhausts on purpose
```

Sanity check before any drill (expect `"decision":"allow"`):

```bash
gate "{\"tenant_id\":\"$DT\",\"session_id\":\"$SID\",\"turn_id\":\"t0\",\"action\":\"read_platform_state\",\"args\":{\"what\":\"drill\"}}"
```

## Drill 1: kill switch

Semantics: the switch is FILE PRESENCE at `LEAF_AGENT_KILL_FILE`. If the file
exists, every gate call denies before any other check (even unknown actions
report the kill switch). The file's first line (up to 200 chars) is echoed as
the deny reason. There is deliberately NO API off-toggle at any privilege level
(design §5; `GET /api/agent/killswitch` is read-only): only someone with
filesystem access to the volume can lift it.

FIRST, the guard. The switch is live durable state: if it is already engaged,
someone meant it, and running the drill would overwrite their reason and later
LIFT a real emergency stop. Investigate instead; only proceed on
`clear-to-drill`. (`-e`, not `-f`, deliberately: `kill_switch_active()` is
`Path.exists()`, so ANY filesystem entry at the path — a stray directory
included — keeps the gate killed, and the guard must see exactly what the gate
sees.)

```bash
docker compose exec app sh -c 'if [ -e /data/state/agent.disabled ]; then echo "ALREADY ENGAGED - do NOT drill. Reason:"; cat /data/state/agent.disabled 2>/dev/null; exit 1; else echo clear-to-drill; fi'
```

Engage. The claim is ATOMIC: `set -C` (noclobber) makes the redirect an
`O_EXCL` create, so detecting a switch someone engaged after the guard and
claiming the path are one step, never a look-then-write race. The marker
carries `$RUN_TAG` so the lift below can prove the switch is still this
run's (the reason line is what tenants' deny reasons will carry):

```bash
docker compose exec app sh -c 'set -C; { printf "%s\n" "$0" > /data/state/agent.disabled; } 2>/dev/null && echo engaged || { echo "ALREADY ENGAGED - do NOT drill. Reason:"; cat /data/state/agent.disabled 2>/dev/null; exit 1; }' "ops drill: paused by operator $RUN_TAG"
```

Verify the gate denies with the kill-switch reason:

```bash
gate "{\"tenant_id\":\"$DT\",\"session_id\":\"$SID\",\"turn_id\":\"t1\",\"action\":\"read_platform_state\",\"args\":{\"what\":\"drill\"}}"
```

Expect `"decision":"deny"` and a `"reason"` of
`kill_switch_active: ops drill: paused by operator <your RUN_TAG>`.
The tenant-visible status route agrees:

```bash
curl -s http://localhost:8130/api/agent/killswitch
```

Expect `"active":true`. Now prove restart survival (the file lives on the
volume, not in the container):

```bash
docker compose restart app
docker compose exec app sh -c 'test -e /data/state/agent.disabled && echo still-engaged'
```

Re-run the `gate` call above: still `deny` / `kill_switch_active`. Lift it —
only if the switch still carries THIS run's marker (a real emergency stop
engaged meanwhile must never be lifted by a drill):

```bash
docker compose exec app sh -c '[ "$(cat /data/state/agent.disabled 2>/dev/null)" = "$0" ] && rm /data/state/agent.disabled && echo lifted || { echo "switch is not this run marker - NOT lifting"; exit 1; }' "ops drill: paused by operator $RUN_TAG"
```

Re-run the `gate` call: `"decision":"allow"`. Done.

(The `sh -c` wrapper is deliberate on every in-container path in this runbook:
a bare `/data/...` argument gets mangled by MSYS path conversion when the
operator drives compose from Git Bash on Windows.)

Related but different: the PER-TENANT kill flag (`agent_tenants.json`) has an
ops API (`POST /api/ops/agent/tenants/{tid}/disable|enable`) because its blast
radius is one tenant. Its guard follows the platform ops-surface rule
(`server/routers/ops.py`): with `LEAF_OPS_SECRET` set, every call must present
it in `X-Ops-Secret` (constant-time compare); unset under LIVE auth the surface
answers 503 (fail closed); unset in the auth-off demo compose the surface is
OPEN like every other demo route, and `:8130` is published to the host. Do not
treat the demo stack as a hardened boundary. The GLOBAL file switch stays
file-only by design at every privilege level.

## Drill 2: policy edit + validation

The catalog (`server/agent_policy.json`) is versioned config. Two rules:

1. An edit is a CODE CHANGE landing in THREE places, because the test suite
   deliberately pins the shipped catalog: the JSON itself, the hardcoded
   fail-safe mirror `_HARDCODED_DEFAULTS` in `server/agent_policy.py` (kept
   equivalent by `test_hardcoded_defaults_mirror_shipped_json`), and any
   pinning assertions the edit touches (e.g.
   `test_shipped_catalog_loads_with_v1_shape` asserts per-action policies,
   rate limits and TTL). A one-file edit WILL fail the suite; that is the
   design, not an accident. Ship via PR + image rebuild.
2. Validation is STRICT and fail-closed: a present-but-invalid file makes every
   gate call deny `policy_load_failed` rather than fall back. Unknown fields,
   quoted booleans, and loosening overlays are load errors.

Validate a CANDIDATE edit before touching the repo (this writes only to the
system temp dir; example tightening: `run_write_tool` to `always-confirm`).
From the repo root:

```bash
cd server
python - <<'EOF'
import json, tempfile, agent_policy
from pathlib import Path
raw = json.loads(Path("agent_policy.json").read_text(encoding="utf-8"))
raw["actions"]["run_write_tool"]["policy"] = "always-confirm"   # the candidate edit
cand = Path(tempfile.gettempdir()) / "agent_policy.candidate.json"
cand.write_text(json.dumps(raw, indent=2), encoding="utf-8")
pol = agent_policy.load_policy(cand)
print("candidate valid:", len(pol.actions), "actions;",
      "run_write_tool ->", pol.actions["run_write_tool"].policy)
EOF
cd ..
```

Expect `candidate valid: 9 actions; run_write_tool -> always-confirm`. An
invalid candidate raises `PolicyError` naming the offending field instead. The
shipped file's own validation plus the full pin/mirror discipline:

```bash
cd server
python -c "import agent_policy; p = agent_policy.load_policy(); print('valid:', len(p.actions), 'actions; ttl', p.approval_ttl_s, 's; limits', p.rate_limits)"
python -m pytest tests/test_agent_policy.py tests/test_agent_gate.py -q
cd ..
```

On the UNEDITED tree the first command prints
`valid: 9 actions; ttl 300 s; limits {'low': 120, 'medium': 60, 'high': 10}`
and both suites are green. When you land a real edit, these same two commands
are the acceptance gate AFTER the three-place change. Ship it:

```bash
docker compose build app
docker compose up -d --wait app
```

The gate re-reads the policy file on EVERY request (the revalidate step closes
the TOCTOU window), so the new catalog is live as soon as the container is.

Prove the fail-closed posture against the RUNNING stack, then restore (the
in-container copy is image-owned, so the restore returns it to the shipped
bytes; a container recreate would too):

```bash
docker compose exec app sh -c 'cp agent_policy.json /tmp/agent_policy.bak && printf "{broken" > agent_policy.json'
gate "{\"tenant_id\":\"$DT\",\"session_id\":\"$SID\",\"turn_id\":\"t2\",\"action\":\"read_platform_state\",\"args\":{\"what\":\"drill\"}}"
docker compose exec app sh -c 'cp /tmp/agent_policy.bak agent_policy.json'
```

The middle call must answer `"decision":"deny"` with a
`"reason"` starting `policy_load_failed:`. After the restore, the same call
allows again.

## Drill 3: approval TTL / expiry

Semantics: confirm-once and always-confirm actions file a durable pending record
under `LEAF_AGENT_APPROVALS_DIR`, bound to the exact
`{tenant, session, action, canonical-json(args)}` and to a TTL of
`approval_ttl_s` (300 s in the shipped catalog). Expiry auto-denies: a decision
racing expiry records `expired`, and a redemption after expiry denies
`approval_expired`. A granted record redeems exactly once (`consumed_at` stamp;
replays deny). Changing the TTL is a drill-2 policy edit of `approval_ttl_s`.
(The fresh `$DT`/`$SID` from the preconditions matter here: confirm-once grants
are keyed by tenant + session + action + tool, so identifiers reused from an
earlier drill run that GRANTED could answer `allow_via_session_grant` instead
of filing a new record, and prior `demo`-tenant rate stamps could deny this
proposal outright.)

File a pending approval (confirm-once action) and capture its confirmation id:

```bash
CID=$(gate "{\"tenant_id\":\"$DT\",\"session_id\":\"$SID\",\"turn_id\":\"t3\",\"action\":\"run_write_tool\",\"args\":{\"tool\":\"noop_write\",\"dwg\":\"drill\"}}" \
  | python -c "import json,sys; print(json.load(sys.stdin)['confirmation_id'])")
echo "$CID"
```

Inspect the durable record (note `expires_at` is `created_at` + 300 s):

```bash
docker compose exec app sh -c "cat /data/state/agent_approvals/$CID.json"
```

List everything currently pending (undecided + unexpired), from inside the app:

```bash
docker compose exec app python -c "import json, agent_gate; print(json.dumps(agent_gate.list_pending(), indent=2))"
```

Let the TTL lapse, then present the redemption the resume turn would make (same
tenant/session/action/args, `confirmation_id` inside args):

```bash
sleep 310
gate "{\"tenant_id\":\"$DT\",\"session_id\":\"$SID\",\"turn_id\":\"t4\",\"action\":\"run_write_tool\",\"args\":{\"tool\":\"noop_write\",\"dwg\":\"drill\",\"confirmation_id\":\"$CID\"}}"
```

Expect `"decision":"deny"`, `"reason":"approval_expired"`, and `list_pending`
now shows the record gone. Decided/expired record files stay on disk as the
durable trace beside the audit log; leave them (they are evidence, and
`list_pending` already filters them).

## Drill 4: rate-state reset

Semantics: budgets are per-tenant per-hour by category (`rate_limits` in the
catalog: low 120, medium 60, high 10). The snapshot file is the authority:
every check re-reads it, spends the unit, and rewrites it under an OS file
lock held on the sibling `agent_rate_state.json.lock`. A MISSING snapshot is a
genuinely empty budget; a present-but-corrupt one DENIES every call
(`rate_state_unreadable`) until reset. Never hand-edit it.

Exhaust the `medium` budget for this run's throwaway tenant (`run_read_tool` is
an auto-policy medium-category action, so this is 61 pure gate decisions; `$RT`
is fresh, so calls 1..60 allow and call 61 denies):

```bash
for i in $(seq 1 61); do
  gate "{\"tenant_id\":\"$RT\",\"session_id\":\"$SID\",\"turn_id\":\"r$i\",\"action\":\"run_read_tool\",\"args\":{\"tool\":\"probe\"}}"
done | tail -n 2
```

The last line must be a deny with `"reason":"rate_limit_exceeded: medium (60/60)"`.
Inspect the spent budget:

```bash
docker compose exec app sh -c 'cat /data/state/agent_rate_state.json; echo'
```

Reset. WARNING: this hands the full budget back to EVERY tenant (there is no
per-tenant reset tooling; Phase-2 gap); do it deliberately, typically after a
drill, a runaway-loop incident you have already stopped, or a
corrupt-snapshot deny. Two rules make the reset race-free:

* Delete the snapshot UNDER THE MODULE'S OWN LOCK. The gate's entire
  read-check-write is atomic inside `_snapshot_lock`
  (`server/agent_gate.py`), so a lock-held unlink serializes with every
  concurrent budget write: nothing can restore pre-reset buckets after the
  reset, and a request that queues behind it simply re-spends from the fresh
  empty budget (by design: a reset hands budget back). A bare `rm` has
  neither property. The `.lock` sibling itself is never deleted: lockers open
  it by name, and removing it would let a later locker lock a different
  inode, un-serializing the read-check-write.
* QUIESCE with the kill switch around it, ATOMICALLY CLAIMED and chained on
  the HOST. `set -C` (noclobber) makes the engage an `O_EXCL` create:
  claiming the path and detecting an emergency stop someone else engaged are
  one atomic step, never a look-then-write race. The three commands are one
  `&&` chain in the operator's own shell, so a failed claim stops the whole
  sequence (a container-side `exit 1` alone stops nothing an operator pastes
  next). The lift removes the switch only after proving the file still
  carries THIS run's marker: a real emergency stop engaged meanwhile is
  never overwritten or lifted. Scope honestly: engaging stops NEW calls at
  the kill check; a call already past the kill check can still spend one
  stamp on the fresh snapshot after the reset (the lock only guarantees old
  buckets never come back). Quiet stack: the verification below shows
  exactly one stamp. Live traffic: a straggler's stamp may sit beside it.

```bash
RESET_MARK="rate-state reset in progress $RUN_TAG"
docker compose exec app sh -c 'set -C; { printf "%s\n" "$0" > /data/state/agent.disabled; } 2>/dev/null && echo quiesced || { echo "kill switch ALREADY ENGAGED - resolve that first, no reset. Reason:"; cat /data/state/agent.disabled 2>/dev/null; exit 1; }' "$RESET_MARK" \
&& docker compose exec app python -c "
import agent_gate
p = agent_gate.rate_file()
with agent_gate._snapshot_lock(p):
    p.unlink(missing_ok=True)
print('rate snapshot reset under lock:', p)
" \
&& docker compose exec app sh -c '[ "$(cat /data/state/agent.disabled 2>/dev/null)" = "$0" ] && rm /data/state/agent.disabled && echo lifted || { echo "switch is not this reset marker - NOT lifting"; exit 1; }' "$RESET_MARK"
```

(`_snapshot_lock` is agent_gate's own lock helper; the runbook reuses it
inside the app container, so the command is always version-matched to the
code it serializes against. If the middle step fails, the chain stops with
the switch still engaged under this run's marker: investigate, then lift
with the last command alone — it refuses unless the marker is still this
run's.)

Verify recovery: the same call allows again and the fresh snapshot carries
the verification call's stamp (exactly one on the quiet drill stack):

```bash
gate "{\"tenant_id\":\"$RT\",\"session_id\":\"$SID\",\"turn_id\":\"r62\",\"action\":\"run_read_tool\",\"args\":{\"tool\":\"probe\"}}"
docker compose exec app sh -c 'cat /data/state/agent_rate_state.json; echo'
```

## Where to look afterwards

Every drill above leaves its trace in the two append-only files (audit = who
asked and what the gate decided, args projected through each action's allowlist
only; ledger = per-turn metering):

```bash
docker compose exec app sh -c 'tail -n 5 /data/state/agent_audit.jsonl'
docker compose exec app sh -c 'tail -n 5 /data/state/agent_ledger.jsonl 2>/dev/null || echo "(no turns metered yet: the gate writes the audit; only completed turns write the ledger)"'
```
