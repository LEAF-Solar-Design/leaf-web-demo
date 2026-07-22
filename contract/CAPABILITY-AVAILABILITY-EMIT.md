# ServerCapabilityAvailability backend emit spec (wave 1 lane B2)

Status: SPEC (backend implementation lands in the registry-promotion lane).
Consumer shape is already frozen on the website side. Date: 2026-07-22.

## Consumer contract (frozen, do not re-decide)

leaf_website `lib/leaf-platform/types.ts` defines the shapes the console
consumes; `lib/leaf-platform/projection.ts` builds the shell projection.
The backend emit must conform to these, field for field:

- `ServerCapabilityAvailability` (types.ts:28-40):
  `contractVersion` = `leaf.platform.v1alpha1` (types.ts:2), `authority` =
  `leaf-platform-registry`, `productCapability`, `implementationState`
  (`implemented|planned`), `runtimeState` (`available|degraded|unavailable`),
  `state` (`shipping|connected_degraded|locked_planned|failed_retryable`,
  types.ts:5-9), `observedAt`, `expiresAt`, optional `reasonCode`, optional
  `fallback` (`mode: local|cached|read_only`, `provenanceRequired: true`),
  and `evidence`.
- `CapabilityEvidence` (types.ts:20-25): `kind`
  (`contract_test|security|end_to_end|observability|recovery`), `uri`,
  `verifiedAt`, `digest` (`sha256` + value).

## Emit rules

1. Source of truth is the capability registry
   (`server/capability_families.json` + the catalog of §9), not hand-lists.
   A capability that is not registry-promoted is emitted as
   `implementationState: "planned"`, `state: "locked_planned"`. Never
   fabricate availability for a dead or unproven backend (element-inventory
   §7: "dead backend: locked, never fabricated").
2. `runtimeState` is measured, not asserted: `degraded` iff the last run for
   that capability set `degraded_mode: true` (CONTRACT.md §10) inside the
   `expiresAt` window; `unavailable` on fail-closed dependency checks.
3. `evidence` carries real receipts. Initial wiring: attach the existing
   proof artifacts named in the port matrix (test-suite receipts as
   `contract_test`, live write receipt as `end_to_end`, broker ledger line as
   `observability`). Each receipt is content-addressed: `digest.value` =
   sha256 of the artifact served at `uri`.
4. `observedAt`/`expiresAt`: availability is a lease, not a fact. Emit short
   leases (minutes, not days); the console must treat an expired lease as
   `unavailable` and say so.
5. Transport: availability rides the authenticated platform-registry response
   path only (types.ts:27). It is never embedded in unauthenticated or public
   payloads.
6. Envelope: the carrying response obeys CONTRACT.md §10 like every other
   body (`error`, `degraded_mode` at top level).

## Naming interlock

Capability names in the emit follow ADOPTION.md §3 (ruling R-A): the
stringing family emits `autofill-string-targets` (heuristic) and
`string-autofill-opt` (real optimizer) as distinct capabilities, each with
its own evidence. No shared names, no precedence.
