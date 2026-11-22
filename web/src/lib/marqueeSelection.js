import { Vector3 } from 'three'
import { unprojectClientToPlane } from '../components/viewerMath.js'

export function marqueeMode(start, end) {
  return end.x >= start.x ? 'window' : 'crossing'
}

export function worldRect(a, b) {
  if (!a || !b || !Number.isFinite(a.x) || !Number.isFinite(a.y)
    || !Number.isFinite(b.x) || !Number.isFinite(b.y)) return null
  return { minX: Math.min(a.x, b.x), minY: Math.min(a.y, b.y), maxX: Math.max(a.x, b.x), maxY: Math.max(a.y, b.y) }
}

// An axis-aligned plan view needs just two unprojections per drag. In a
// rotated view, test the visible outline against the original client box.
export function marqueeMapping(camera, bounds, start, end, heightForPolyline = () => 0) {
  const rect = worldRect(start, end)
  if (!rect || !camera?.isOrthographicCamera || !bounds) return null
  const { left, top, width, height } = bounds
  if (![left, top, width, height].every(Number.isFinite) || width <= 0 || height <= 0) return null
  camera.updateWorldMatrix(true, false)
  const view = camera.matrixWorldInverse.elements
  if (!view.every(Number.isFinite) || !camera.projectionMatrix.elements.every(Number.isFinite)
    || camera.projectionMatrix.determinant() === 0 || Math.abs(view[10]) < 1e-12) return null

  if ([view[1], view[4], view[8], view[9]].every(value => Math.abs(value) < 1e-15)) {
    const a = unprojectClientToPlane(camera, bounds, start.x, start.y)
    const b = unprojectClientToPlane(camera, bounds, end.x, end.y)
    const flatRect = worldRect(a, b)
    return flatRect && { rect: flatRect }
  }

  // Allow roundoff from camera transforms at an inclusive pixel boundary.
  const tolerance = 16 * Number.EPSILON * Math.max(1, width, height, Math.abs(left), Math.abs(top))
  if (rect.maxX > rect.minX && rect.maxY > rect.minY) {
    rect.minX -= tolerance
    rect.maxX += tolerance
    rect.minY -= tolerance
    rect.maxY += tolerance
  }
  const vertex = new Vector3()
  function project(pl) {
    // A null height uses each bridge vertex's elevation.
    const z = heightForPolyline(pl)
    return point => {
      vertex.set(point[0], point[1], z ?? point[2] ?? 0).project(camera)
      return [left + (1 + vertex.x) * width / 2, top + (1 - vertex.y) * height / 2]
    }
  }
  return { rect, project }
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

export function marqueeHandles(polylines, rect, mode, isVisible = () => true, project) {
  const handles = []
  const groups = new Map()
  if (!rect || !(rect.maxX > rect.minX) || !(rect.maxY > rect.minY)) return Object.freeze(handles)
  for (const pl of polylines) {
    if (typeof pl?.handle !== 'string' || !pl.pts?.length || !isVisible(pl)) continue
    let valid = true, allInside = true, touches = false
    const mapPoint = project?.(pl)
    let first, previous
    for (let i = 0; i < pl.pts.length; i++) {
      let p = pl.pts[i]
      if (!p || !Number.isFinite(p[0]) || !Number.isFinite(p[1])) { valid = false; break }
      for (let coordinate = 2; coordinate < p.length; coordinate++) {
        if (!Number.isFinite(p[coordinate])) { valid = false; break }
      }
      if (!valid) break
      if (mapPoint) {
        p = mapPoint(p)
        if (!Number.isFinite(p[0]) || !Number.isFinite(p[1])) { valid = false; break }
      }
      if (i === 0) first = p
      const inside = p[0] >= rect.minX && p[0] <= rect.maxX && p[1] >= rect.minY && p[1] <= rect.maxY
      allInside = allInside && inside
      touches = touches || inside
      if (mode === 'crossing' && !touches && i > 0) touches = intersects(previous, p, rect)
      previous = p
    }
    if (!valid) continue
    if (mode === 'crossing' && !touches && pl.closed) touches = intersects(previous, first, rect)
    // A block can project into several polylines with one source handle.
    // Window selection contains the whole visible entity, not just one edge.
    const group = groups.get(pl.handle) || { allInside: true, touches: false }
    group.allInside = group.allInside && allInside
    group.touches = group.touches || touches
    groups.set(pl.handle, group)
  }
  for (const [handle, group] of groups) {
    if (group.allInside || (mode === 'crossing' && group.touches)) handles.push(handle)
  }
  return Object.freeze(handles)
}
