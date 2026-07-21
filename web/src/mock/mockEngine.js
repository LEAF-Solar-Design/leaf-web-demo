// Mock "engine" — dispatches on tool.engine_op and produces a Result
// envelope (CONTRACT §3) computed from the real intake data, so the demo
// is genuinely functional with no backend. Drawing units are inches
// (panels measure ~77 x 38.5 -> a standard ~78in x 39in module).

import { centroid, polyArea, bounds } from './geometry.js'

const SQIN_PER_SQFT = 144

function envelope(tool, result, overlay, t0) {
  return {
    ok: true,
    tool: tool.name,
    version: tool.version || '1.0.0',
    result,
    overlay: overlay || null,
    timing_ms: Math.round((typeof performance !== 'undefined' ? performance.now() : Date.now()) - t0),
    cost: null, // null until a real APS run (CONTRACT §3)
    error: null,
  }
}

const OPS = {
  count_by_layer(intake) {
    const counts = {}
    for (const L of intake.layers || []) counts[L] = 0
    for (const pl of intake.polylines) {
      counts[pl.layer] = (counts[pl.layer] || 0) + 1
    }
    // Drop layers with zero geometry for a tidy table, but keep at least all non-zero.
    const nonzero = {}
    for (const [k, v] of Object.entries(counts)) if (v > 0) nonzero[k] = v
    return { result: { counts: nonzero, total: intake.polylines.length }, overlay: null }
  },

  highlight_near_edge(intake, params) {
    const dist = Number(params?.distance_in ?? 60)
    const b = bounds(intake.polylines)
    const handles = []
    for (const pl of intake.polylines) {
      const [cx, cy] = centroid(pl.pts)
      const d = Math.min(cx - b.minX, b.maxX - cx, cy - b.minY, b.maxY - cy)
      if (d <= dist) handles.push(pl.handle)
    }
    return {
      result: {
        distance_in: dist,
        panels_near_edge: handles.length,
        panels_total: intake.polylines.length,
      },
      overlay: { highlight_handles: handles },
    }
  },

  measure_area(intake) {
    let total = 0, max = -Infinity, min = Infinity, maxHandle = null, maxCentroid = null
    for (const pl of intake.polylines) {
      const a = polyArea(pl.pts)
      total += a
      if (a > max) { max = a; maxHandle = pl.handle; maxCentroid = centroid(pl.pts) }
      if (a < min) min = a
    }
    const n = intake.polylines.length
    const toSqft = (sqin) => +(sqin / SQIN_PER_SQFT).toFixed(1)
    return {
      result: {
        panels: n,
        total_area_sqft: toSqft(total),
        avg_area_sqft: toSqft(total / n),
        min_area_sqft: toSqft(min),
        max_area_sqft: toSqft(max),
      },
      overlay: {
        highlight_handles: maxHandle ? [maxHandle] : [],
        markers: maxCentroid
          ? [{ pt: maxCentroid, label: `largest ${toSqft(max)} sqft` }]
          : [],
      },
    }
  },

  // The one `drawing.write` op in mock: removes the marked panel and reports a
  // §11 new_version stamp so the app can seat v2 and light Undo/Redo/History.
  // Pure w.r.t. the passed intake — the version chain itself lives in
  // mockVersions.js (which recomputes the same removal deterministically).
  delete_marked_panel(intake, params) {
    const pls = intake.polylines || []
    const asked = params?.handle || params?.target_handle || null
    const handle = asked && pls.some((p) => p.handle === asked)
      ? asked
      : (pls.length ? pls[pls.length - 1].handle : null)
    const remaining = pls.filter((p) => p.handle !== handle)
    return {
      result: {
        removed: handle,
        panels_before: pls.length,
        panels_after: remaining.length,
        new_version: { drawing_id: 'demo', version: 2, parent: 1 },
        intake: { ...intake, polylines: remaining },
      },
      overlay: { highlight_handles: handle ? [handle] : [], removed_handles: handle ? [handle] : [] },
    }
  },

  select_by_layer(intake, params) {
    const layer = params?.layer ?? 'Panels'
    const handles = intake.polylines.filter((p) => p.layer === layer).map((p) => p.handle)
    return {
      result: { layer, selected: handles.length },
      overlay: { highlight_handles: handles },
    }
  },
}

export function runMock(tool, params, intake) {
  const t0 = typeof performance !== 'undefined' ? performance.now() : Date.now()
  const op = OPS[tool.engine_op]
  if (!op) {
    return {
      ok: false, tool: tool.name, version: tool.version || '1.0.0',
      result: null, overlay: null, timing_ms: 0, cost: null,
      error: `mock engine has no op for '${tool.engine_op}'`,
    }
  }
  const { result, overlay } = op(intake, params || {})
  return envelope(tool, result, overlay, t0)
}
