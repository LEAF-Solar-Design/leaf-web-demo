// Mock version chain (CONTRACT-ADDENDUM §11, demo side) — a PURE in-memory
// APPEND-ONLY chain for drawing_id 'demo'. No React, no import.meta, no JSON
// import: `node` must be able to import this module headless.
//
// v1 is the base intake seated at load; every `drawing.write` run (delete-
// marked-panel) appends a NEW version computed from the CURRENT HEAD — never
// recomputed from the base — so a second delete produces v3 rather than
// silently replacing v2 (which resurrected the first deleted panel and made
// the receipt and the header disagree). undo()/redo() walk the chain
// deterministically; a write after an undo truncates the redo tail first.
// list() returns the LIVE shape VersionHistory.jsx consumes
// ({head, latest, versions:[{v, tool, note, created, sha256}]}).

export const DRAWING_ID = 'demo'

// Deterministic content digest stand-in (FNV-1a, 64 hex chars by mixing four
// lanes) so history rows carry a stable provenance string without node:crypto.
function digest(str) {
  const s = String(str)
  let out = ''
  for (let lane = 0; lane < 4; lane++) {
    let h = 0x811c9dc5 ^ (lane * 0x9e3779b9)
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i)
      h = Math.imul(h, 0x01000193) >>> 0
    }
    out += ('00000000' + h.toString(16)).slice(-8)
  }
  return (out + out).slice(0, 64)
}

function fingerprint(intake) {
  const pls = intake?.polylines || []
  return digest(`${pls.length}|${pls.map((p) => p.handle).join(',')}`)
}

// Shallow clone of the intake with a replaced polyline list — the previous
// intake object (layers, meta, ...) is never mutated.
function withPolylines(intake, polylines) {
  return { ...intake, polylines }
}

const chain = {
  intakes: [],    // index 0 = v1, index n-1 = vn
  removed: null,  // handle removed by the most recent write
  head: 1,
  latest: 1,
  versions: [],   // [{v, parent, tool, note, created, sha256}]
}

function nowIso() {
  return new Date().toISOString()
}

/** Reset the chain and seat `intake` as v1 (the base). Idempotent per intake. */
export function seedBase(intake) {
  chain.intakes = intake ? [intake] : []
  chain.removed = null
  chain.head = 1
  chain.latest = 1
  chain.versions = intake
    ? [{
        v: 1,
        parent: null,
        tool: 'base',
        note: 'Original drawing',
        created: nowIso(),
        sha256: fingerprint(intake),
      }]
    : []
  return chain.intakes[0] || null
}

/** Drop the whole chain (mode change / re-running the demo clean). */
export function reset() {
  seedBase(null)
}

export function isSeeded() {
  return chain.intakes.length > 0
}

/** The intake currently at head (the one a write is computed against). */
function headOrNull() {
  return chain.intakes[chain.head - 1] || null
}

/** The handle applyDelete() would remove by default (last polyline AT HEAD). */
export function defaultHandle() {
  const pls = headOrNull()?.polylines || []
  return pls.length ? pls[pls.length - 1].handle : null
}

/**
 * Append a new version = CURRENT HEAD minus the polyline whose handle matches
 * (default: last at head). Any redo tail above head is discarded first, so the
 * chain stays a single append-only line of history.
 * Returns {drawing_id, version, parent, removed, intake}.
 */
export function applyDelete(handle) {
  const cur = headOrNull()
  if (!cur) throw new Error('mockVersions: seedBase(intake) first')
  const pls = cur.polylines || []
  const target = handle != null && pls.some((p) => p.handle === handle)
    ? handle
    : defaultHandle()
  if (target == null) throw new Error('mockVersions: nothing to delete')
  const next = withPolylines(cur, pls.filter((p) => p.handle !== target))

  // Truncate the redo tail (a write after an undo forks from head), then append.
  chain.intakes = chain.intakes.slice(0, chain.head)
  chain.versions = chain.versions.slice(0, chain.head)
  chain.intakes.push(next)
  chain.head = chain.latest = chain.intakes.length
  chain.removed = target
  chain.versions.push({
    v: chain.head,
    parent: chain.head - 1,
    tool: 'delete-marked-panel',
    note: `Deleted panel ${target}`,
    created: nowIso(),
    sha256: fingerprint(next),
  })

  return {
    drawing_id: DRAWING_ID,
    version: chain.head,
    parent: chain.head - 1,
    removed: target,
    intake: next,
  }
}

/** The intake at a version ('head' | 1 | 2 | ...). */
export function intakeAt(version) {
  const v = version === 'head' || version == null ? chain.head : Number(version)
  const it = chain.intakes[v - 1]
  if (!it) throw new Error(`mockVersions: version ${v} does not exist`)
  return it
}

/** The intake at head. */
export function headIntake() {
  return intakeAt('head')
}

/** LIVE /intake shape: {intake, version, head, latest}. */
export function view(version) {
  const v = version === 'head' || version == null ? chain.head : Number(version)
  return {
    drawing_id: DRAWING_ID,
    intake: intakeAt(v),
    version: v,
    head: chain.head,
    latest: chain.latest,
  }
}

/** Step head back one version. Returns the LIVE undo shape. */
export function undo() {
  if (chain.head > 1) chain.head -= 1
  return view('head')
}

/** Step head forward one version. Returns the LIVE redo shape. */
export function redo() {
  if (chain.head < chain.latest) chain.head += 1
  return view('head')
}

/** LIVE /versions shape consumed by VersionHistory.jsx. */
export function list() {
  return {
    drawing_id: DRAWING_ID,
    head: chain.head,
    latest: chain.latest,
    checkout: null,
    versions: chain.versions.map((r) => ({ ...r })),
  }
}
