// Mock version chain (CONTRACT-ADDENDUM §11, demo side) — a PURE in-memory
// v1<->v2 chain for drawing_id 'demo'. No React, no import.meta, no JSON
// import: `node` must be able to import this module headless.
//
// v1 is the base intake seated at load; a `drawing.write` run (delete-marked-
// panel) creates v2 = base minus one polyline. undo()/redo() walk the chain
// deterministically; list() returns the LIVE shape VersionHistory.jsx consumes
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

// Shallow clone of the intake with a replaced polyline list — the base object
// (layers, meta, ...) is never mutated.
function withPolylines(intake, polylines) {
  return { ...intake, polylines }
}

const chain = {
  base: null,     // v1 intake
  v2: null,       // v2 intake (null until applyDelete)
  removed: null,  // handle removed by v1 -> v2
  head: 1,
  latest: 1,
  versions: [],   // [{v, parent, tool, note, created, sha256}]
}

function nowIso() {
  return new Date().toISOString()
}

/** Reset the chain and seat `intake` as v1 (the base). Idempotent per intake. */
export function seedBase(intake) {
  chain.base = intake || null
  chain.v2 = null
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
  return chain.base
}

/** Drop the whole chain (mode change / re-running the demo clean). */
export function reset() {
  seedBase(null)
}

export function isSeeded() {
  return chain.base != null
}

/** The handle applyDelete() would remove by default (the last polyline). */
export function defaultHandle() {
  const pls = chain.base?.polylines || []
  return pls.length ? pls[pls.length - 1].handle : null
}

/**
 * Create v2 = base minus the polyline whose handle matches (default: last).
 * Returns {version, parent, drawing_id, removed, intake}. Re-applying replaces
 * v2 (the demo only ever needs one write generation).
 */
export function applyDelete(handle) {
  if (!chain.base) throw new Error('mockVersions: seedBase(intake) first')
  const pls = chain.base.polylines || []
  const target = handle != null && pls.some((p) => p.handle === handle)
    ? handle
    : defaultHandle()
  if (target == null) throw new Error('mockVersions: nothing to delete')
  const next = pls.filter((p) => p.handle !== target)
  chain.v2 = withPolylines(chain.base, next)
  chain.removed = target
  chain.head = 2
  chain.latest = 2
  chain.versions = [
    chain.versions[0],
    {
      v: 2,
      parent: 1,
      tool: 'delete-marked-panel',
      note: `Deleted panel ${target}`,
      created: nowIso(),
      sha256: fingerprint(chain.v2),
    },
  ]
  return {
    drawing_id: DRAWING_ID,
    version: 2,
    parent: 1,
    removed: target,
    intake: chain.v2,
  }
}

/** The intake at a version ('head' | 1 | 2). */
export function intakeAt(version) {
  const v = version === 'head' || version == null ? chain.head : Number(version)
  if (v === 2) {
    if (!chain.v2) throw new Error('mockVersions: version 2 does not exist')
    return chain.v2
  }
  return chain.base
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

/** Step head back one version (v2 -> v1). Returns the LIVE undo shape. */
export function undo() {
  if (chain.head > 1) chain.head -= 1
  return view('head')
}

/** Step head forward one version (v1 -> v2). Returns the LIVE redo shape. */
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
