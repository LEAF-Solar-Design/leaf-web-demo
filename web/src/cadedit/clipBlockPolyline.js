const MAX_CLIP_VERTICES = 512
export const MAX_CLIP_EDGE_CHECKS = 1_000_000

const sameXY = (a, b) => a[0] === b[0] && a[1] === b[1]
function cross(a, b, p) {
  const left = (b[0] - a[0]) * (p[1] - a[1])
  const right = (b[1] - a[1]) * (p[0] - a[0])
  const turn = left - right
  // Rotations can leave a residual when the products for collinear points cancel.
  const roundoff = 8 * Number.EPSILON * Math.max(Math.abs(left), Math.abs(right))
  return Number.isFinite(turn) && Math.abs(turn) <= roundoff ? 0 : turn
}

function reserveClipChecks(budget, cost) {
  if (cost > budget.remaining) return false
  budget.remaining -= cost
  return true
}

function distinctRing(points) {
  const ring = points.filter((point, index) => index === 0 || !sameXY(point, points[index - 1]))
  if (ring.length > 1 && sameXY(ring[0], ring.at(-1))) ring.pop()
  return ring
}

function convexWinding(ring) {
  if (ring.length < 3 || new Set(ring.map(p => `${p[0]},${p[1]}`)).size !== ring.length) return 0
  let winding = 0
  for (let index = 0; index < ring.length; index++) {
    const a = ring[index]
    const b = ring[(index + 1) % ring.length]
    for (const point of ring) {
      const side = cross(a, b, point)
      if (!Number.isFinite(side)) return 0
      if (side === 0) continue
      const sign = Math.sign(side)
      if (winding && sign !== winding) return 0
      winding = sign
    }
  }
  return winding
}

function interpolate(a, b, ratio) {
  if (ratio === 0) return a
  if (ratio === 1) return b
  return a.map((value, index) => value * (1 - ratio) + b[index] * ratio)
}

function clipRing(points, halfPlanes) {
  let ring = distinctRing(points)
  if (!convexWinding(ring)) return null
  for (const distance of halfPlanes) {
    const next = []
    for (let index = 0; index < ring.length; index++) {
      const point = ring[index]
      const previous = ring[(index + ring.length - 1) % ring.length]
      const before = distance(previous)
      const after = distance(point)
      if (!Number.isFinite(before) || !Number.isFinite(after)) return null
      if ((before < 0) !== (after < 0)) {
        const denominator = before - after
        if (!Number.isFinite(denominator)) return null
        next.push(interpolate(previous, point, before / denominator))
      }
      if (after >= 0) next.push(point)
    }
    ring = distinctRing(next)
    if (ring.length < 3) return []
  }
  // Edge-only and vertex-only contact has no visible fill.
  const turns = ring.map((point, i) => cross(ring[0], point, ring[(i + 1) % ring.length]))
  if (!turns.every(Number.isFinite)) return null
  return turns.some(turn => turn !== 0) ? [ring] : []
}

function clipSegments(points, halfPlanes) {
  const runs = []
  let run = null
  for (let index = 1; index < points.length; index++) {
    const a = points[index - 1]
    const b = points[index]
    let start = 0
    let end = 1
    for (const distance of halfPlanes) {
      const before = distance(a)
      const after = distance(b)
      if (!Number.isFinite(before) || !Number.isFinite(after)) return null
      if (before < 0 && after < 0) { start = 1; end = 0; break }
      if ((before < 0) === (after < 0)) continue
      const denominator = before - after
      if (!Number.isFinite(denominator)) return null
      const crossing = before / denominator
      if (before < 0) start = Math.max(start, crossing)
      else end = Math.min(end, crossing)
    }
    if (start >= end) { run = null; continue }
    const first = interpolate(a, b, start)
    const last = interpolate(a, b, end)
    if (sameXY(first, last)) continue
    if (!run || start > 0) {
      run = [first]
      runs.push(run)
    }
    run.push(last)
    if (end < 1) run = null
  }
  return runs
}

/** Prepare a convex kept window in the reference's block coordinates.
 * A null filter or clip result signals incomplete geometry. An empty result
 * means the geometry was fully clipped.
 */
export function prepareBlockClip(record, budget) {
  const points = record?.points
  if (record?.inverted !== false || !Array.isArray(points) || points.length > MAX_CLIP_VERTICES
    || !points.every(point => Array.isArray(point) && point.length === 2 && point.every(Number.isFinite))) return null
  const boundary = distinctRing(points)
  if (!reserveClipChecks(budget, boundary.length ** 2)) return null
  const winding = convexWinding(boundary)
  if (!winding) return null

  const halfPlanes = boundary.map((a, index) => {
    const b = boundary[(index + 1) % boundary.length]
    return point => winding * cross(a, b, point)
  })
  return polyline => {
    const size = polyline.pts.length
    const edges = boundary.length
    if (!reserveClipChecks(budget, size * edges)) return null
    if (halfPlanes.every(distance => polyline.pts.every(point => {
      const side = distance(point)
      return Number.isFinite(side) && side >= 0
    }))) return [polyline]

    const clippingCost = (size + edges) * edges + (polyline.closed ? size ** 2 : 0)
    if (!reserveClipChecks(budget, clippingCost)) return null
    const pieces = polyline.closed ? clipRing(polyline.pts, halfPlanes) : clipSegments(polyline.pts, halfPlanes)
    return pieces?.map(pts => ({ ...polyline, pts, ...(!polyline.closed ? { strokeOnly: true } : {}) })) ?? null
  }
}
