// THE CONTAINMENT DIFF (standardization slice 9c, the change capsule's Check
// 3). Pure, allocation-bounded by the intake sizes themselves, no I/O.
//
// Mirrors server/routers/drawings.py `_entity_identity` / `_entity_map` /
// `_version_delta` exactly: the same three entity arrays (polylines, inserts,
// faces3d), the same handle-or-content-hash identity, so a client-computed
// containment verdict agrees with the server's own delta math rather than
// inventing a second one. web/src/mock/mockVersions.js runs the same
// algorithm again for the demo version chain; that module keeps its helpers
// private (no export), so this is an independent, byte-for-byte-equivalent
// implementation rather than a shared import — documented here so a future
// reader does not read the duplication as drift.
//
// THE HONEST LIMIT, carried over from the server comment: an entity with no
// stable `handle` falls back to a content hash. A content-hash identity can
// prove "unchanged" but never proves "this is the same entity as the one the
// user right-clicked", so `scopeDeltaToHandle` below refuses any touched
// entity with a null handle rather than guessing it is the target.

export const ENTITY_ARRAY_KINDS = Object.freeze(['polylines', 'inserts', 'faces3d'])

// Small FNV-1a mix, four lanes concatenated to 64 hex chars — the same
// dependency-free approach mockVersions.js uses (no node:crypto, so this
// module loads under plain `node --test` exactly like a browser bundle).
function digest(value) {
  const s = JSON.stringify(value)
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

function entityIdentity(kind, entity) {
  const handle = entity && typeof entity === 'object' ? entity.handle : null
  if (typeof handle === 'string' && handle) return { key: `${kind}:h:${handle}`, handle }
  return { key: `${kind}:c:${digest(entity)}`, handle: null }
}

function entityMap(intake) {
  const map = new Map()
  for (const kind of ENTITY_ARRAY_KINDS) {
    const rows = intake && intake[kind]
    if (!Array.isArray(rows)) continue
    for (const entity of rows) {
      if (entity && typeof entity === 'object') {
        const { key, handle } = entityIdentity(kind, entity)
        map.set(key, { entity, handle })
      }
    }
  }
  return map
}

/**
 * Every entity identity that differs between `baseIntake` and
 * `candidateIntake`: `{ touched: [{key, handle, change}] }`, `change` one of
 * 'added' | 'modified' | 'deleted'.
 *
 * Returns `null` when either intake is not a parseable object — "a delta
 * could not be computed" is a distinct, honest outcome from "computed, zero
 * changes", and callers must not conflate the two.
 */
export function computeEntityDelta(baseIntake, candidateIntake) {
  if (!baseIntake || typeof baseIntake !== 'object') return null
  if (!candidateIntake || typeof candidateIntake !== 'object') return null
  const baseMap = entityMap(baseIntake)
  const candidateMap = entityMap(candidateIntake)
  const touched = []
  for (const [key, { entity, handle }] of candidateMap) {
    const prior = baseMap.get(key)
    if (!prior) touched.push({ key, handle, change: 'added' })
    else if (JSON.stringify(prior.entity) !== JSON.stringify(entity)) touched.push({ key, handle, change: 'modified' })
  }
  for (const [key, { handle }] of baseMap) {
    if (!candidateMap.has(key)) touched.push({ key, handle, change: 'deleted' })
  }
  return { touched }
}

/**
 * Containment verdict for one target handle: `{scoped, touched, outside}`.
 * `scoped` is true only when EVERY touched entity carries the exact target
 * handle — a null handle (content-hash fallback) or any other handle lands
 * in `outside` and refuses. Zero touched entities is vacuously scoped: a
 * proposal that changes nothing cannot reach outside the element either.
 * `delta === null` (uncomputable) always refuses.
 */
export function scopeDeltaToHandle(delta, handle) {
  if (!delta || !Array.isArray(delta.touched)) return { scoped: false, touched: [], outside: [] }
  const outside = delta.touched.filter((t) => t.handle !== handle)
  return { scoped: outside.length === 0, touched: delta.touched, outside }
}
