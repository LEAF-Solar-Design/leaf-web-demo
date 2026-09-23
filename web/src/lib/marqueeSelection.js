export function marqueeMode(start, end) {
  return end.x >= start.x ? 'window' : 'crossing'
}

export function worldRect(a, b) {
  if (!a || !b || !Number.isFinite(a.x) || !Number.isFinite(a.y)
    || !Number.isFinite(b.x) || !Number.isFinite(b.y)) return null
  return { minX: Math.min(a.x, b.x), minY: Math.min(a.y, b.y), maxX: Math.max(a.x, b.x), maxY: Math.max(a.y, b.y) }
}

// Clip the segment's parameter interval against both rectangle slabs.
// Unlike bounding-box overlap, this preserves the relation between x and y.
function intersects(a, b, rect) {
  let lo = 0, hi = 1
  for (let axis = 0; axis < 2; axis++) {
    const min = axis === 0 ? rect.minX : rect.minY
    const max = axis === 0 ? rect.maxX : rect.maxY
    const delta = b[axis] - a[axis]
    if (delta === 0) {
      if (a[axis] < min || a[axis] > max) return false
    } else {
      const t1 = (min - a[axis]) / delta
      const t2 = (max - a[axis]) / delta
      lo = Math.max(lo, Math.min(t1, t2))
      hi = Math.min(hi, Math.max(t1, t2))
      if (lo > hi) return false
    }
  }
  return true
}

export function marqueeHandles(polylines, rect, mode, isVisible = () => true) {
  const handles = []
  const seen = new Set()
  if (!rect || !(rect.maxX > rect.minX) || !(rect.maxY > rect.minY)) return Object.freeze(handles)
  for (const pl of polylines) {
    if (typeof pl?.handle !== 'string' || !pl.pts?.length || !isVisible(pl)) continue
    let valid = true, allInside = true, touches = false
    for (let i = 0; i < pl.pts.length; i++) {
      const p = pl.pts[i]
      if (!p || !Number.isFinite(p[0]) || !Number.isFinite(p[1])) { valid = false; break }
      for (let coordinate = 2; coordinate < p.length; coordinate++) {
        if (!Number.isFinite(p[coordinate])) { valid = false; break }
      }
      if (!valid) break
      const inside = p[0] >= rect.minX && p[0] <= rect.maxX && p[1] >= rect.minY && p[1] <= rect.maxY
      allInside = allInside && inside
      touches = touches || inside
      if (mode === 'crossing' && !touches && i > 0) touches = intersects(pl.pts[i - 1], p, rect)
    }
    if (!valid) continue
    if (mode === 'crossing' && !touches && pl.closed) touches = intersects(pl.pts[pl.pts.length - 1], pl.pts[0], rect)
    if ((allInside || (mode === 'crossing' && touches)) && !seen.has(pl.handle)) {
      seen.add(pl.handle)
      handles.push(pl.handle)
    }
  }
  return Object.freeze(handles)
}
