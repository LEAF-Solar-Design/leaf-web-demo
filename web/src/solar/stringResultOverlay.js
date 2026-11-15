// string-panels emits drawing-space pts, not the saved graph's metre-space route.
export const STRING_PALETTE = Object.freeze(['#f87171', '#fb923c', '#facc15', '#a3e635', '#34d399', '#22d3ee', '#60a5fa', '#a78bfa', '#e879f9'])
const finite = n => typeof n === 'number' && Number.isFinite(n)
const dataOf = value => value?.result ?? value
const pathOf = pts => Array.isArray(pts) && pts.length > 0 && pts.every(p =>
  Array.isArray(p) && p.length >= 2 && p.every(finite)) ? pts.map(p => p.slice(0, 2)) : null
const empty = refusal => ({ polylines: [], panelFills: [], refusal })
const colorOf = id => {
  let hash = 0
  for (const c of String(id)) hash = (hash * 31 + c.charCodeAt(0)) >>> 0
  return STRING_PALETTE[hash % STRING_PALETTE.length]
}

export function stringResultSummary(result) {
  try {
    const data = dataOf(result)
    const stats = data?.stats
    const electrical = data?.electrical
    return {
      strings: finite(stats?.string_count) ? stats.string_count : undefined,
      panels: finite(stats?.panel_count) ? stats.panel_count : undefined,
      modules: finite(stats?.modules_stringed) ? stats.modules_stringed : undefined,
      maxVoc: finite(electrical?.worst_string_voc) ? electrical.worst_string_voc : undefined,
      limit: finite(data?.module?.max_system_voltage) ? data.module.max_system_voltage : undefined,
      pass: typeof electrical?.pass === 'boolean' ? electrical.pass : undefined,
    }
  } catch { return {} }
}

export function totalChipText(result) {
  const s = stringResultSummary(result)
  const parts = []
  if (s.panels !== undefined) parts.push(`${s.panels.toLocaleString('en-US')} panels`)
  if (s.strings !== undefined) parts.push(`${s.strings.toLocaleString('en-US')} strings`)
  if (s.maxVoc !== undefined) parts.push(`${s.maxVoc.toLocaleString('en-US')} V max`)
  return parts.length ? parts.join(' · ') : null
}

export function completionText(result) {
  const s = stringResultSummary(result)
  const parts = []
  if (s.strings !== undefined) parts.push(`${s.strings.toLocaleString('en-US')} strings`)
  // A passing aggregate check proves zero over limit; a failing aggregate does
  // not tell us how many strings failed.
  if (s.limit !== undefined && s.pass === true) parts.push(`0 over ${s.limit} V`)
  return parts.length ? parts.join(' · ') : totalChipText(result)
}

export function scopeOutline(value, state) {
  try {
    if (!['running', 'done', 'complete'].includes(state)) return null
    const data = dataOf(value)
    let pts = pathOf(data?.scope?.pts ?? data?.scope_outline ?? value?.scope?.pts)
    if (!pts) {
      const paths = data?.strings ?? data?.intake?.polylines ?? data?.polylines
      if (!Array.isArray(paths) || paths.length > 4096) return null
      let count = 0
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
      for (const path of paths) {
        if (!Array.isArray(path?.pts)) return null
        count += path.pts.length
        if (count > 100000) return null
        const points = pathOf(path.pts)
        if (!points) return null
        for (const [x, y] of points) {
          minX = Math.min(minX, x); minY = Math.min(minY, y)
          maxX = Math.max(maxX, x); maxY = Math.max(maxY, y)
        }
      }
      if (!count) return null
      pts = [[minX, minY], [maxX, minY], [maxX, maxY], [minX, maxY]]
    }
    if (!pts) return null
    const closed = [...pts]
    if (pts[0][0] !== pts.at(-1)[0] || pts[0][1] !== pts.at(-1)[1]) closed.push([...pts[0]])
    return { pts: closed, color: 'var(--accent)', strokeWidth: 2, glow: state === 'running' ? 8 : 0, kind: 'scope' }
  } catch { return null }
}

export function stringResultOverlay(result, { panels = [], metersPerUnit = 0.0254 } = {}) {
  try {
    const data = dataOf(result)
    if (!data || !Array.isArray(data.strings) || data.strings.length === 0) return empty(null)
    if (!finite(metersPerUnit) || metersPerUnit <= 0) return empty('invalid_scale')
    if (data.strings.length > 4096 || panels.length > 100000) return empty('overlay_limit')
    let points = 0
    const polylines = []
    const assignments = new Map()
    for (const string of data.strings) {
      if (!string || typeof string.id !== 'string' || !Array.isArray(string.pts)) return empty('invalid_graph')
      points += string.pts.length
      if (points > 100000) return empty('overlay_limit')
      const pts = pathOf(string.pts)
      if (!pts) return empty('invalid_graph')
      const color = colorOf(string.id)
      polylines.push({ id: string.id, pts, color, strokeWidth: 2 })
      for (const pt of pts) assignments.set(`${pt[0]},${pt[1]}`, color)
    }
    const panelFills = []
    for (const panel of panels) {
      if (!panel?.closed) continue
      points += panel.pts?.length || 0
      if (points > 100000) return empty('overlay_limit')
      const pts = pathOf(panel.pts)
      if (!pts) return empty('invalid_graph')
      const centre = pts.reduce((a, p) => [a[0] + p[0] / pts.length, a[1] + p[1] / pts.length], [0, 0])
      // The solver rounds centroids in inches, then converts back to drawing units.
      const scale = metersPerUnit / 0.0254
      const rounded = centre.map(n => +(n * scale).toFixed(3) / scale)
      const color = assignments.get(`${centre[0]},${centre[1]}`) ?? assignments.get(`${rounded[0]},${rounded[1]}`)
      panelFills.push({ pts, color: color ?? '#3f3f46', fillOpacity: color ? 0.35 : 0.1, strokeWidth: 1 })
    }
    if (polylines.length + panelFills.length > 4096) return empty('overlay_limit')
    return { polylines, panelFills, refusal: null }
  } catch { return empty('invalid_graph') }
}
