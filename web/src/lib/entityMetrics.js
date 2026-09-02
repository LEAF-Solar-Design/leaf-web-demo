// Client-derived geometry metrics for the properties dock (W4c-V2).
//
// EXTRACTED from src/mock/geometry.js (polyArea verbatim), which now imports
// from here — dedupe, not duplication: the shoelace the mock engine trusts
// is the shoelace the dock renders. Pure functions over the intake §1
// polyline shape ({ layer, closed, pts: [[x, y, z], ...], handle }); no
// React, no fetch, no engine contact (the WASM engine-truth readout is a
// DIFFERENT, deliberately absent thing — see ACCEPTANCE engine ownership).
//
// Fail-closed formatting: every formatter answers '—' rather than NaN,
// Infinity, or "-0.00" — a poisoned vertex must never poison the dock.

/** Shoelace area (absolute), drawing-unit². Verbatim from the mock engine. */
export function polyArea(pts) {
  let a = 0
  for (let i = 0; i < pts.length; i++) {
    const [x1, y1] = pts[i]
    const [x2, y2] = pts[(i + 1) % pts.length]
    a += x1 * y2 - x2 * y1
  }
  return Math.abs(a) / 2
}

/**
 * Path length in drawing units: the open perimeter, plus the closing edge
 * when the polyline is closed. Two-point degenerate paths measure their one
 * edge; a single vertex measures 0.
 */
export function polyLength(pts, closed = false) {
  if (!Array.isArray(pts) || pts.length < 2) return 0
  let length = 0
  const last = closed ? pts.length : pts.length - 1
  for (let i = 0; i < last; i++) {
    const [x1, y1] = pts[i]
    const [x2, y2] = pts[(i + 1) % pts.length]
    length += Math.hypot(x2 - x1, y2 - y1)
  }
  return length
}

/**
 * The dock's Geometry section for one selected intake entity, or null when
 * the entity has nothing honest to say (unresolved selection). Area only
 * for CLOSED polylines — an open path has no enclosed area and gets none.
 */
export function entityGeometry(entity, kind) {
  if (!entity) return null
  if (kind === 'polyline') {
    const pts = Array.isArray(entity.pts) ? entity.pts : []
    return {
      vertices: pts.length,
      closed: !!entity.closed,
      length: polyLength(pts, !!entity.closed),
      area: entity.closed && pts.length >= 3 ? polyArea(pts) : null,
    }
  }
  if (kind === 'insert') {
    return {
      position: Array.isArray(entity.pt) ? entity.pt.slice(0, 2) : null,
      rotation: Number.isFinite(entity.rot) ? entity.rot : null,
      scale: Array.isArray(entity.scale) ? entity.scale : null,
    }
  }
  if (kind === '3dface') {
    const corners = [entity.p1, entity.p2, entity.p3, entity.p4].filter(Array.isArray)
    return { corners: corners.length }
  }
  return null
}

/** Fixed-precision drawing-unit formatter: finite → '1234.56', else '—',
 *  and negative zero never survives the rounding. */
export function formatUnits(value, digits = 2) {
  if (!Number.isFinite(value)) return '—'
  const fixed = value.toFixed(digits)
  // Anything that rounds to zero renders unsigned: "-0.00" is a lie about
  // a real negative quantity and noise about a rounding artifact.
  return Number(fixed) === 0 ? (0).toFixed(digits) : fixed
}
