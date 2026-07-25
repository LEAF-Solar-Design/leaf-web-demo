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
4. `observedAt`/`expiresAt`: availability is a lease, not a fact.

   **Normative TTL: `LEASE_TTL_SECONDS = 15`, matching the website's exported
   `SERVER_AVAILABILITY_TTL_MS = 15_000`.** Verified by content against
   `git show origin/main:lib/leaf-platform/projection.ts` on 2026-07-24, and
   asserted by `test_the_lease_ttl_matches_the_websites_constant_read_from_origin_main`,
   which reads that constant out of `origin/main` and fails on drift (it SKIPS with
   a stated reason if the sibling repo is unreachable, rather than passing
   vacuously).

   The console enforces, structurally: `expiresAt` in the future, `expiresAt` after
   `observedAt`, `expiresAt - observedAt` no longer than one TTL (so a supplied
   2099 expiry cannot extend trust), and `observedAt` no further than one TTL into
   the future (bounded clock-skew tolerance). It also validates every enum
   exhaustively, requires `fallback.provenanceRequired === true` when `fallback` is
   present, and requires `evidence` to be an array whose every member is a
   complete sha256-digested record.

   Changing the TTL is a COORDINATED CONTRACT EVENT: a one-sided change makes the
   browser reject every emitted availability, and every capability then shows
   locked with nothing reporting why.

   **CORRECTION OF A CORRECTION (2026-07-24).** A previous revision of this
   section announced that `SERVER_AVAILABILITY_TTL_MS` "does not exist anywhere in
   the website repo" and downgraded the TTL, window and skew rules to local
   invention. **That retraction was wrong**, and the original claim above was
   right. The mistake: I read the *stale local working tree* of leaf_website rather
   than `origin/main`. Squash merges rewrite SHAs, so the change was merged into
   `main` even though its branch commit `c5f9c39` is not an ancestor of it. Verify
   cross-repo claims by CONTENT against `origin/<branch>`, never against a local
   checkout and never by SHA ancestry.

   `test_the_website_still_enforces_the_rules_this_module_mirrors` now pins the
   PREMISE too: if the website ever drops one of these rules, that test fails here
   instead of this module's strictness quietly becoming arbitrary.

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
