// Per-application rail population (W4c-V1, operator directive: "the rails
// are floating, and populate with the tools needed to drive each application
// as they specifically call for").
//
// Pure functions over the catalog + the surface manifest — no React, no
// fetch — so the fold that drives BOTH the studio's left rail and the
// drafting ribbon is one tested decision, not two drifting copies.
// STUDIO-ONLY consumers: the old shell renders the unfolded rail
// byte-for-byte (callers guard on studioGround, never here).
import { PRODUCT_SURFACES } from '../site/productSurfaces.js'

const BY_ID = new Map(PRODUCT_SURFACES.map((surface) => [surface.id, surface]))

/**
 * The families a surface's rail and ribbon carry. `familyIds: null` means
 * the whole catalog (CAD); a non-empty fold filters AND orders by the list.
 * A DECLARED empty fold (`familyIds: []`, sheets) is honest, not a lag: it
 * returns no families on purpose, and the rail renders "No tools for this
 * surface yet." rather than fabricating a catalog the page never reads.
 * Unknown surface ids (no manifest record at all) fail open to the whole
 * catalog instead — a new tab must never boot with an empty tool rail
 * because this map lagged.
 */
export function familiesForSurface(families, surfaceId) {
  const surface = BY_ID.get(surfaceId)
  const fold = surface?.familyIds ?? null
  const list = Array.isArray(families) ? families : []
  if (!fold) return list
  const byId = new Map(list.map((family) => [family.family_id, family]))
  return fold.map((id) => byId.get(id)).filter(Boolean)
}

/**
 * The spine monogram for a family: two characters, mono-caps — the repo has
 * NO icon vocabulary (zero svg in src/) and the design grammar is mono-caps
 * micro-labels, so monograms fit where an invented icon set would not.
 * First letters of the first two words, else the first two letters.
 */
export function familyMonogram(label) {
  // Symbol-only words ("&") carry no identity — skip them so
  // "Selection & highlighting" reads SH, not S&.
  const words = String(label || '').trim().split(/\s+/).filter((w) => /^[a-z0-9]/i.test(w))
  if (words.length === 0) return '··'
  const raw = words.length >= 2 ? words[0][0] + words[1][0] : words[0].slice(0, 2)
  return raw.toUpperCase().padEnd(2, '·')
}
