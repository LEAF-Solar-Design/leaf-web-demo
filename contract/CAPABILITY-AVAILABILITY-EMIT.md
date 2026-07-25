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
2. `runtimeState` is measured, not asserted, and it is a statement about NOW:
   derive it from current dependency and worker health (the jobs worker and
   broker delivery machinery in `server/routers/jobs.py`), not from the last
   run's outcome. CONTRACT.md §10 scopes `degraded_mode` to APS fallback
   runs; a capability can be degraded (for example `read_only` fallback on
   worker loss) without any prior degraded run. Recent `degraded_mode: true`
   runs may serve as supporting evidence, never as the definition.
3. `evidence` carries real receipts. Initial wiring: attach the existing
   proof artifacts named in the port matrix (test-suite receipts as
   `contract_test`, live write receipt as `end_to_end`, broker ledger line as
   `observability`). Each receipt is content-addressed: `digest.value` =
   sha256 of the artifact served at `uri`.
4. `observedAt`/`expiresAt`: availability is a lease, not a fact. Emit SHORT
   leases; the console must treat an expired lease as `unavailable` and say so.
   The normative length is the TTL below, 15 seconds. (An earlier draft of this
   line said "minutes, not days", which contradicted the 15 s figure two
   paragraphs down. Seconds is correct.)

   **Normative TTL: `LEASE_TTL_SECONDS = 15`** (equivalently the website's
   `SERVER_AVAILABILITY_TTL_MS = 15000`). This line is the single source both
   sides cite. The window between `observedAt` and `expiresAt` must never exceed
   one TTL, and `observedAt` must not be stamped further than one TTL into the
   future (the clock-skew bound).

   Changing this number is a COORDINATED CONTRACT EVENT: the server emit and the
   console validator must change in the same release, or every emitted
   availability is rejected by the browser and every capability silently shows
   locked. Both sides assert against this line —
   `server/product_capability_availability.py` via
   `test_product_capability_availability.py`, so a server-side edit that drifts
   from this document fails the suite rather than shipping.

   **Known limit of that assertion:** it catches a one-sided SERVER change only.
   Editing this document and the Python constant together still passes while the
   website sits at a different `SERVER_AVAILABILITY_TTL_MS`. Closing that needs a
   cross-repo contract test, which does not exist yet. Treat a TTL change as a
   two-repo change and verify the website side by hand.
5. Transport: availability rides the authenticated platform-registry response
   path only (types.ts:27). It is never embedded in unauthenticated or public
   payloads.
6. Envelope: the carrying response obeys CONTRACT.md §10 like every other
   body (`error`, `degraded_mode` at top level).

## Naming interlock

Capability names in the emit follow ADOPTION.md §3 (ruling R-A). String
solving is ONE product capability, `drawing.solve.strings`
(`server/routers/capabilities_promotion.py:57`; the authenticated registry
emit builds its availability at `server/routers/jobs.py:332-355`). Current
state, named honestly: the promotion router is standalone and reflects only
the heuristic; the jobs.py emit carries no tool names. REQUIRED at
adoption: the emit lists both tool names under that one capability,
`autofill-string-targets` (heuristic) and `string-autofill-opt` (real
optimizer), as distinct tool names within one capability, and every run
result discloses which solver ran. No shared tool names, no precedence.
