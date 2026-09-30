import { validObjectBounds } from '../lib/drawingObjectIndex.js'

export const clamp = (value, min, max) => Math.max(min, Math.min(max, value))

export function overviewBounds(records) {
  let result = null
  for (const record of records || []) {
    const b = record?.bounds
    if (!validObjectBounds(b)) continue
    result = result ? { minX: Math.min(result.minX, b.minX), minY: Math.min(result.minY, b.minY),
      maxX: Math.max(result.maxX, b.maxX), maxY: Math.max(result.maxY, b.maxY) } : { ...b }
  }
  return result
}

/** Drawing Y points up; SVG Y points down. The unused space is letterboxed. */
export function overviewMap(bounds, width = 180, height = 112, padding = 8) {
  if (!validObjectBounds(bounds) || ![width, height, padding].every(Number.isFinite)
    || padding < 0 || width <= padding * 2 || height <= padding * 2) return null
  const b = { ...bounds }
  for (const axis of ['X', 'Y']) if (b[`min${axis}`] === b[`max${axis}`]) {
    const pad = Math.max(0.5, Math.abs(b[`min${axis}`]) * Number.EPSILON)
    b[`min${axis}`] -= pad; b[`max${axis}`] += pad
  }
  const dx = b.maxX - b.minX, dy = b.maxY - b.minY
  if (![dx, dy].every((value) => Number.isFinite(value) && value > 0)) return null
  const scale = Math.min((width - padding * 2) / dx, (height - padding * 2) / dy)
  if (!Number.isFinite(scale) || scale <= 0) return null
  const w = dx * scale, h = dy * scale
  return { bounds: b, scale, x: (width - w) / 2, y: (height - h) / 2, width: w, height: h }
}

export function toOverviewPoint(map, point) {
  if (!map || ![point?.x, point?.y].every(Number.isFinite)) return null
  const x = map.x + (point.x - map.bounds.minX) * map.scale
  const y = map.y + (map.bounds.maxY - point.y) * map.scale
  return [x, y].every(Number.isFinite) ? { x, y } : null
}

export function fromOverviewPoint(map, point) {
  if (!map || ![point?.x, point?.y].every(Number.isFinite)) return null
  return { x: map.bounds.minX + (clamp(point.x, map.x, map.x + map.width) - map.x) / map.scale,
    y: map.bounds.maxY - (clamp(point.y, map.y, map.y + map.height) - map.y) / map.scale }
}

/** Keep a small edge marker even when the entire camera is off the drawing. */
export function overviewRect(map, bounds, minimum = 2) {
  if (!map || !validObjectBounds(bounds) || !Number.isFinite(minimum) || minimum < 0) return null
  const top = toOverviewPoint(map, { x: bounds.minX, y: bounds.maxY })
  const bottom = toOverviewPoint(map, { x: bounds.maxX, y: bounds.minY })
  if (!top || !bottom) return null
  const x = clamp(top.x, map.x, map.x + map.width), y = clamp(top.y, map.y, map.y + map.height)
  const width = Math.min(map.width, Math.max(minimum, clamp(bottom.x, map.x, map.x + map.width) - x))
  const height = Math.min(map.height, Math.max(minimum, clamp(bottom.y, map.y, map.y + map.height) - y))
  return { x: Math.min(x, map.x + map.width - width), y: Math.min(y, map.y + map.height - height), width, height }
}

/** Union records in an 8 by 8 spatial grid, never an SVG element per entity. */
export function aggregateOverview(records, map) {
  if (!map) return []
  const cells = new Map()
  for (const record of records || []) {
    const b = record?.bounds
    if (!validObjectBounds(b)) continue
    const point = toOverviewPoint(map, { x: b.minX / 2 + b.maxX / 2, y: b.minY / 2 + b.maxY / 2 })
    if (!point) continue
    const col = clamp(Math.floor((point.x - map.x) / map.width * 8), 0, 7)
    const row = clamp(Math.floor((point.y - map.y) / map.height * 8), 0, 7)
    const key = row * 8 + col
    cells.set(key, overviewBounds([{ bounds: cells.get(key) }, record]))
  }
  return Array.from(cells, ([key, bounds]) => ({ key, ...overviewRect(map, bounds) }))
}
