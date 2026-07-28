# Instant execution wire contract v1

This directory defines the transport boundary for a tool that is admitted to
the `instant` execution class. Each document uses JSON Schema draft 2020-12 and
has a stable `$id`. Producers and consumers must validate messages before they
cross the boundary.

## Documents

| Document | Schema | Purpose |
| --- | --- | --- |
| Session assignment | `schemas/session-assignment.v1.schema.json` | Binds one executor endpoint and opaque signed lease to one tenant session and catalog snapshot. This object is harness-only. |
| Effective catalog input | `schemas/catalog-entry.v1.schema.json` | The byte-level inputs used to calculate an effective catalog digest. |
| Code load | `schemas/code-load.v1.schema.json` | Loads a content-addressed artifact without source credentials. |
| Invocation | `schemas/invocation.v1.schema.json` | Requests one deterministic tool invocation. |
| Response | `schemas/response.v1.schema.json` | Returns one terminal result or a typed error. |
| Error | `schemas/error.v1.schema.json` | Defines typed, fail-closed errors. |
| Cancellation | `schemas/cancellation.v1.schema.json` | Requests or reports cancellation. |
| Readiness | `schemas/readiness.v1.schema.json` | Advertises whether an executor may accept instant work. |
| Usage record | `schemas/usage-record.v1.schema.json` | Records one immutable usage outcome. |

All messages carry `contract: "leaf.instant-execution/v1"`. Required object
members use `additionalProperties: false`. The schemas are plain JSON Schema,
so Python implementations can use `jsonschema.Draft202012Validator` and
TypeScript implementations can use an AJV draft-2020 validator.

## Effective catalog digest

The effective catalog digest is `sha256` over the canonical UTF-8 JSON form of
the complete `catalog-entry` object. Canonical JSON means lexicographically
sorted object keys, no insignificant whitespace, JSON number syntax, and UTF-8
encoding. Implementations must reject a catalog entry when the separately
issued digest does not equal that value.

The digest input includes `execution_class`, `runtime`, `limits`, and
`artifact_digest`. It also includes the tool capability identity, parameter
schema digest, code digest, entrypoint, and catalog commit. A catalog refresh
that changes any of these fields creates a new digest. Consumers must never
substitute a newer catalog, artifact, code bundle, or execution class for an
invocation that carries a prior digest.

## Invocation and security rules

An invocation binds `invocation_id`, `tenant_id`, `session_id`, `assignment_id`,
`binding_epoch`, `lease_id`, the effective catalog digest, immutable
`code_digest`, immutable `artifact_digest`, one capability and tool identity,
parameters, drawing context reference, and an absolute deadline. The signed
lease token stays in authenticated RPC metadata and is never passed into user
code. Drawing context is only an opaque, versioned reference. It contains no
drawing bytes, filesystem path, presigned URL, or credential.

This boundary carries no AWS, Autodesk, Redis, PostgreSQL, broker, Claude, or
tenant credential. It carries only `tenant_id` and `session_id` as identity
bindings. The schemas block unexpected top-level fields. The local validator
also recursively rejects credential-like field names in an invocation,
including inside `params`. Receivers must apply the same guard before logging,
queueing, or forwarding an invocation.

An executor checks, in order: contract version, session binding and expiry,
effective catalog digest, execution class, capability entitlement, code and
artifact digests, drawing reference, deadline, and readiness. A failed check
does not execute the tool.

## Fail-closed behavior

`instant` means the request must remain instant or fail. It never changes into
batch. If Redis or the control plane is unavailable, stale, inconsistent, or
unreachable, the receiver returns `REDIS_UNAVAILABLE` or
`CONTROL_PLANE_UNAVAILABLE` with `execution_disposition: "not_started"`. It
must not enqueue a batch job, choose another execution class, use a stale
assignment, or issue a success response. `degraded` readiness likewise does
not accept new instant invocations.

`execution_disposition` is authoritative for retry safety:

| Value | Meaning |
| --- | --- |
| `not_started` | The receiver proved that user code did not start. A caller can retry with the same invocation id after recovery. |
| `unknown` | A timeout or transport split may have started work. The caller must query by invocation id before retrying. |
| `completed` | The invocation reached a terminal result. Replaying the same id returns that result. |

## Compatibility and idempotency

Version 1 accepts only the exact `leaf.instant-execution/v1` contract value.
Additive v1 fields require a new compatible schema revision and may not alter
the meaning of an existing required field. Changed validation, removed fields,
or changed enum semantics require a new contract major version.

`invocation_id` is a stable UUID for one logical request. The tuple
`(tenant_id, session_id, invocation_id)` is the idempotency key. A repeat with
byte-identical immutable fields and canonical-equivalent parameters returns the
stored terminal response or the current in-flight status. A repeat with the
same key but a different code digest, artifact digest, catalog digest,
capability, drawing reference, limits, deadline, or parameters fails with
`INVOCATION_CONFLICT` and starts nothing.

`cancellation_id` is idempotent per `(tenant_id, session_id, invocation_id)`.
`usage_id` is deterministic as `usage:<invocation_id>:<attempt>`. Usage is
written once per attempt and is immutable. Delivery may be at-least-once, so
the sink deduplicates by `usage_id`; it must not bill or count a duplicate.

## Fixture checks

Run from the repository root:

```powershell
python executor/contracts/validate_contracts.py
```

The script validates every fixture in `fixtures/manifest.json` with the named
JSON Schema and applies the invocation secret-field guard. It exits nonzero if
a valid fixture fails, an invalid fixture passes, a schema is malformed, or the
fixture manifest is incomplete.
