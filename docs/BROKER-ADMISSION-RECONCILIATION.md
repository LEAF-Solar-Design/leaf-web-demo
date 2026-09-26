# Broker admission reconciliation

Use this procedure only when a PostgreSQL broker admission remains in
`executing` because the broker stopped before it published a terminal result.
An executing admission might already represent paid APS work. Never delete it,
change it back to `leased`, or submit the run again.

## Access requirements

The operator needs both runtime secrets:

- `LEAF_BROKER_SECRET`
- `LEAF_BROKER_RECONCILE_SECRET`

Send them as `X-Broker-Secret` and `X-Broker-Reconcile-Secret`. Do not place
either value in a command transcript, ticket, evidence record, or request body.
The reconciliation secret is required even when normal local broker auth is
disabled.

## Inspect the admission

List unresolved executions:

```text
GET /broker/admin/admissions/executing
```

Read one admission and its immutable resolution history:

```text
GET /broker/admin/admissions/{event_key}?tenant_id={tenant_id}
```

Record its tenant, event key, request fingerprint, APS mode, reserved cost,
execution start time, age, slot deadline, and `slot_stuck` state. A held slot
continues to count against `APS_MAX_CONCURRENCY` after its deadline. Expiry is
an alarm, not permission to reuse unknown APS capacity. If the tenant or
fingerprint differs from the incident, stop. Do not resolve it.

## Verify APS before resolving

1. Find the matching broker request in the production logs using the tenant,
   execution time, event key, and request fingerprint.
2. Find the corresponding APS Design Automation WorkItem or the evidence that
   no WorkItem was accepted. Check the APS account, Activity, submission time,
   WorkItem identifier, terminal status, engine time, and reported cost.
3. Save durable evidence, such as an APS WorkItem URL or identifier plus the
   relevant CloudWatch log event IDs. Put the evidence location in
   `evidence_ref`.
4. If APS acceptance or terminal state remains uncertain, stop. Leave the
   admission in `executing`. Escalate for manual investigation. Never infer
   "no charge" from a missing application response.

## Allowed resolutions

`confirmed_failed_no_charge` is allowed only when APS evidence proves that no
paid WorkItem was accepted. The broker writes a terminal failure, a ledger row
with no cost, and an immutable audit record.

`verified_terminal` is allowed only when APS supplies a verified terminal
outcome. Provide the exact response envelope, HTTP status, and frozen nine-field
ledger entry, including measured engine time and cost when present.

The request must include:

- a stable operator identity;
- a reason of at least 16 characters;
- an APS evidence reference;
- the exact confirmation phrase
  `RESOLVE {tenant_id} {event_key} {resolution}`.

Example body for a proven no-charge failure:

```json
{
  "tenant_id": "tenant-id",
  "resolution": "confirmed_failed_no_charge",
  "operator_id": "operator@example.com",
  "reason": "APS search proves that no WorkItem was accepted",
  "evidence_ref": "aps-evidence://incident/reference",
  "confirmation": "RESOLVE tenant-id event-key confirmed_failed_no_charge"
}
```

Submit it to:

```text
POST /broker/admin/admissions/{event_key}/resolve
```

After the request succeeds, read the admission again. Confirm that it is
`terminal`, its reservation is zero, and `resolution_audit` contains the
operator, reason, evidence reference, prior `executing` state, and terminal
status. Confirm that its APS slot is `released`. Preserve that response with
the incident record.

The transaction publishes the terminal result, immutable ledger row, and audit
record together. A second resolution is rejected. No reconciliation endpoint
can execute a tool or clear an unknown run.

## Automatic reconciler

The broker admission reconciler is part of hosted APS execution recovery. It
settles only outcomes the evidence proves. Production scheduling is a separate
follow-up; importing the module starts no thread or loop. It requires the
`wd-broker-ledger-job-id` ledger schema and resolver support for `job_id`.

With `LEAF_BROKER_STORE=postgres`, each tick reads at most 100 executing
admissions. Admissions younger than 3,600 seconds are skipped without a status
request or write. An older non-live admission can be resolved as
`confirmed_failed_no_charge`: that path never submits paid APS work. Its
evidence reference is `non-live-admission:<event_key>`.

A live admission must have a job-bound event key of the form
`<job-id>:broker-run` or `<job-id>:broker-fallback`, where the job id matches
`[0-9a-f-]{36}`. Two eligible admissions with the same job id both alarm. The
reconciler first reads the broker's active WorkItem registry, then falls back
to its durable `ACTIVE_WORKITEMS_PATH` sidecar. The sidecar reader is read-only:
the last open or close record for each job wins, without a TTL filter. A missing
file provides no correlation; an unreadable, malformed, or larger-than-16-MiB
file cannot provide evidence, including any otherwise valid prefix.

Each tick makes at most 20 APS status requests, each with a 10-second timeout.
Candidates beyond the request budget remain executing for later ticks. These
requests share APS's 150-per-minute application limit with the live broker, so
choose the loop interval with the live polling load in mind.
Eligible live admissions use FIFO queue positions: new arrivals join behind
waiting admissions, and each status check moves its admission to the back.
Residual: this no-starvation order holds only across ticks in one process, so
the production arming follow-up (`priorities-s4-s6-012`) must use the loop form
(`run_forever`) in one process, because each `--once` invocation starts a fresh
queue and checks the oldest eligible admissions first.
Residual: `list_executing(100)` reads at most the 100 oldest executing
admissions, so an admission beyond the first 100 waits until older ones settle.

Only `failedDownload`, `failedInstructions`, `failedUpload`,
`failedLimitDataSize`, `failedLimitProcessingTime`, and `cancelled` permit an
automatic live resolution. The resolution is `verified_terminal`, with a
non-retryable `WORKITEM_FAILED` envelope, HTTP 502, and null result and ledger
costs. The ledger retains the job id. The evidence reference is
`aps-workitem:<workitem_id>:<status>`. This matches the live broker's failure
record; it does not infer that the failed WorkItem incurred no charge.

Every resolution calls the same validated resolver as the admin route, with
operator identity `reconciler`, a rule-specific reason, evidence reference, and
the exact tenant/event/resolution confirmation phrase. That transaction writes
the terminal result, ledger and audit, and releases the settled APS slot.
The reconciler never deletes, re-leases, resubmits, cancels, or writes a sidecar.
Legacy store mode returns `{"mode":"legacy","checked":0}` without work.

Set `LEAF_BROKER_RECONCILER_ALARM_ONLY=1` to turn every proposed resolution into
an `alarm_only_mode` alarm without a write. Status reads still occur. Other
alarm reasons retain their meaning:

Default correlation requires a strict sidecar read each tick: `sidecar_unreadable` blocks use of memory after a failed read, and `correlation_unproven` blocks a memory WorkItem that the sidecar does not confirm.

| Alarm reason | Required investigation |
| --- | --- |
| `event_key_not_job_bound` | Establish the admission's job identity. |
| `ambiguous_job_admissions` | Disambiguate the executing admissions for one job. |
| `sidecar_unreadable` | Restore or inspect the correlation evidence. |
| `no_workitem_correlation` | Find and verify the matching APS WorkItem. |
| `aps_succeeded_needs_operator` | Recover the tool adapter's actual successful result. |
| `aps_not_terminal` | Check pending, missing, or unknown APS status. |
| `aps_status_unreadable` | Investigate HTTP errors, timeouts, or invalid APS responses. |
| `resolve_rejected` | Inspect current admission state and resolver validation. |
| `alarm_only_mode` | Inspect the proposed resolution and the kill-switch setting. |

The manual procedure above remains the path for every alarm. In particular,
APS success is never resolved automatically: paid work finished, but only the
tool adapter can construct its actual result. An operator must recover that
result before using `verified_terminal`. Missing evidence is never permission
to mark live work as no-charge.

From the `server/` directory, run one tick or an interruptible loop:

```text
python -m broker_admission_reconciler --once
python -m broker_admission_reconciler --loop --interval-s 60
```

`--once` prints a summary JSON object and exits 0 after the tick completes,
including when individual admissions alarm. PostgreSQL summaries contain
`checked` (age-eligible rows examined, including budget-deferred rows),
`skipped_young`, `resolved`, and `alarmed`. Alarms go to stderr as one JSON line
per admission, prefixed `[leaf-broker-reconciler] ALARM `, with fields `event`,
`event_key`, `tenant_id`, `reason`, and `age_seconds`. The event name is
`broker_admission_reconcile_alarm`. Store/listing errors still propagate to the
caller; they are not successful ticks.

In-process callers can inject an APS status client, a `correlation(job_id)`
reader, and an `alarm(record)` sink into `reconcile_once`. `run_forever` accepts
an interval and a stop event and waits interruptibly between completed ticks.
