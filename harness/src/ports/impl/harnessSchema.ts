// STRANGLER SHIM (mushy-code extraction, 2026-08-06): this module moved to the
// vendored mushy-code library. The path and every export stay stable for all
// in-repo importers; the implementation lives at the re-exported location and
// is synced by scripts/sync-mushy-code.py (pin: harness/src/vendor/VENDOR-PIN.json).
//
// magpie AD4b (2026-09-24): assertHarnessCatalog is wrapped, not re-exported.
// The vendored bytes are HELD at mushy-code 52e8a36 (a declared overlay) and
// require the 4-value harness_confirmations status CHECK exactly. AD4b-2 widens
// that CHECK to admit 'consumed' in a LATER release, and this image must still
// start (and stay a valid rollback target) once it has. So the exact widened
// CHECK also satisfies the held requirement. Safe because this image never
// writes status 'consumed' and treats a consumed row as already decided. Any
// other CHECK text still fails closed. Pure; never mutates its input.
import {
  assertHarnessCatalog as assertPinnedHarnessCatalog,
  type HarnessCatalog,
  type HarnessConstraint,
} from "../../vendor/mushy-author/ports/impl/harnessSchema.js";

export type {
  HarnessCatalog,
  HarnessColumn,
  HarnessConstraint,
  HarnessIndex,
} from "../../vendor/mushy-author/ports/impl/harnessSchema.js";

export const HELD_CONFIRMATION_STATUS_CHECK =
  "CHECK ((status = ANY (ARRAY['pending'::text, 'approved'::text, 'denied'::text, 'expired'::text])))";
export const CONSUMED_CONFIRMATION_STATUS_CHECK =
  "CHECK ((status = ANY (ARRAY['pending'::text, 'approved'::text, 'denied'::text, 'expired'::text, 'consumed'::text])))";

function normalizedDefinition(value: string): string {
  return value.toLowerCase().replace(/\s+/g, " ").trim();
}

/** Returns a copy of `catalog` in which an exact widened confirmation status CHECK
 *  also stands in for the held 4-value CHECK. Linear in the constraint count. */
export function withWidenedConfirmationStatusAccepted(catalog: HarnessCatalog): HarnessCatalog {
  const widened = normalizedDefinition(CONSUMED_CONFIRMATION_STATUS_CHECK);
  const extra: HarnessConstraint[] = catalog.constraints
    .filter(
      (constraint) =>
        constraint.table_name === "harness_confirmations" &&
        normalizedDefinition(constraint.definition) === widened,
    )
    .map((constraint) => ({ ...constraint, definition: HELD_CONFIRMATION_STATUS_CHECK }));
  if (extra.length === 0) return catalog;
  return { ...catalog, constraints: [...catalog.constraints, ...extra] };
}

export function assertHarnessCatalog(catalog: HarnessCatalog): void {
  assertPinnedHarnessCatalog(withWidenedConfirmationStatusAccepted(catalog));
}
