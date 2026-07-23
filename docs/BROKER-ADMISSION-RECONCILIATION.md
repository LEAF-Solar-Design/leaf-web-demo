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
