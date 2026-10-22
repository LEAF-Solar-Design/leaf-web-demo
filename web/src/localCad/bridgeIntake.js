const MAX_POINTS = 200000
const list = value => Array.isArray(value) ? value : []
const point = value => Array.isArray(value) && Number.isFinite(value[0]) && Number.isFinite(value[1])
  ? [value[0], value[1], Number.isFinite(value[2]) ? value[2] : 0] : null

// Budget includes the Viewer's subsequent sampling of bulges and round entities.
export function bridgeToIntake(payload = {}, options = {}) {
  const result = { source: 'bridge', documentId: `autocad:${payload.drawing ?? ''}`,
    dwg: payload.drawing, geometry: payload.geometry, bounds: payload.bounds, polylines: [], circles: [], arcs: [],
    inserts: [], faces3d: [], layers: [], points: 0, truncated: !!payload.truncated }
  const layers = new Set()
  const identity = row => ({ handle: String(row.handle ?? ''), layer: String(row.layer || '0') })
  const reserve = count => {
    if (result.points + count > MAX_POINTS) { result.truncated = true; return false }
    result.points += count
    return true
  }
  const append = (kind, row, cost) => {
    if (!reserve(cost)) return
    result[kind].push(row)
    layers.add(row.layer)
  }
  for (const row of list(payload.lines)) {
    if (!Array.isArray(row?.pts) || row.pts.length !== 2) continue
    const pts = row.pts.map(point)
    if (pts.every(Boolean)) append('polylines', { ...identity(row), pts, closed: false, strokeOnly: true }, 2)
  }
  for (const row of list(payload.polylines)) {
    if (!Array.isArray(row?.pts) || row.pts.length < 2) continue
    if (row.pts.length > MAX_POINTS - result.points) { result.truncated = true; continue }
    const pts = row.pts.map(point)
    if (!pts.every(Boolean)) continue
    const bulges = pts.map((_, i) => Number.isFinite(row.bulges?.[i]) ? row.bulges[i] : 0)
    // Each bulged segment samples at most 48 interior points.
    const cost = pts.length + bulges.reduce((n, b, i) => n + (b && (row.closed || i < pts.length - 1) ? 48 : 0), 0)
    append('polylines', { ...identity(row), pts, closed: !!row.closed, bulges,
      ...(row.closed !== true ? { strokeOnly: true } : {}) }, cost)
  }
  for (const kind of ['circles', 'arcs']) {
    for (const row of list(payload[kind])) {
      const c = point(row?.c)
      if (!c || !Number.isFinite(row.r) || row.r <= 0) continue
      if (kind === 'arcs' && (!Number.isFinite(row.start_deg) || !Number.isFinite(row.end_deg))) continue
      append(kind, { ...identity(row), c, r: row.r,
        ...(kind === 'arcs' ? { start_deg: row.start_deg, end_deg: row.end_deg } : {}) }, kind === 'circles' ? 48 : 49)
    }
  }
  for (const row of list(payload.inserts)) {
    const pt = point(row?.pt)
    if (pt && typeof row.name === 'string') append('inserts', { ...identity(row), name: row.name, pt, rot: row.rot_deg, scale: row.scale }, 1)
  }
  if (options.textGlyphs) {
    options.textGlyphs(list(payload.texts), { result, layers, reserve, identity, point, maxPoints: MAX_POINTS })
  } else result.textPending = list(payload.texts).length
  for (const layer of list(payload.layers)) if (typeof layer?.name === 'string') layers.add(layer.name)
  result.layers = [...layers]
  return result
}
