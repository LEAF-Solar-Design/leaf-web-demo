// The guided tour's result readout.
//
// PURE: no React, no DOM. Given the tour step and the job controller's latest
// result envelope, returns the numbers the engine just computed as a short
// list of { label, value } rows, or null when the envelope does not belong to
// this step (a stale result from the previous beat, a failed run, or a step
// with no tool). Nothing here invents a number: every value is read from the
// envelope, and a field that is missing or not finite is dropped.

const MAX_LAYER_ROWS = 4

function finite(value) {
  const n = typeof value === 'number' ? value : Number.NaN
  return Number.isFinite(n) ? n : null
}

function fmt(n, digits = 0) {
  return n.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

function countRows(result) {
  const rows = []
  const counts = result?.counts && typeof result.counts === 'object' ? result.counts : {}
  const layers = Object.entries(counts)
    .filter(([, v]) => finite(v) !== null)
    .sort((a, b) => b[1] - a[1])
  for (const [layer, n] of layers.slice(0, MAX_LAYER_ROWS)) {
    rows.push({ label: `Layer ${String(layer).slice(0, 40)}`, value: fmt(n) })
  }
  if (layers.length > MAX_LAYER_ROWS) rows.push({ label: 'Other layers', value: String(layers.length - MAX_LAYER_ROWS) })
  const total = finite(result?.total)
  if (total !== null) rows.push({ label: 'Total entities', value: fmt(total) })
  return rows
}

function edgeRows(result) {
  const near = finite(result?.panels_near_edge)
  const total = finite(result?.panels_total)
  const dist = finite(result?.distance_in)
  if (near === null) return []
  const label = dist === null ? 'Panels near the edge' : `Panels within ${fmt(dist)} in of the edge`
  return [{ label, value: total === null ? fmt(near) : `${fmt(near)} of ${fmt(total)}` }]
}

function areaRows(result) {
  const rows = []
  const total = finite(result?.total_area_sqft)
  const panels = finite(result?.panels)
  const max = finite(result?.max_area_sqft)
  if (total !== null) rows.push({ label: 'Total panel area', value: `${fmt(total, 1)} sq ft` })
  if (panels !== null) rows.push({ label: 'Panels measured', value: fmt(panels) })
  if (max !== null) rows.push({ label: 'Largest module (marked)', value: `${fmt(max, 1)} sq ft` })
  return rows
}

function deleteRows(result) {
  const rows = []
  const removed = typeof result?.removed === 'string' ? result.removed.slice(0, 32) : null
  const before = finite(result?.panels_before)
  const after = finite(result?.panels_after)
  if (removed) rows.push({ label: 'Deleted panel', value: removed })
  if (before !== null && after !== null) rows.push({ label: 'Panels', value: `${fmt(before)} before, ${fmt(after)} after` })
  return rows
}

const BY_TOOL = {
  'count-by-layer': countRows,
  'highlight-panels-near-edge': edgeRows,
  'measure-panel-area': areaRows,
  'delete-marked-panel': deleteRows,
}

/**
 * Rows for the tour card, or null. Fails closed: anything malformed returns null
 * so the card simply shows no readout.
 */
export function summarizeTourResult(step, envelope) {
  try {
    if (!step?.tool || !envelope || envelope.ok !== true) return null
    if (envelope.tool !== step.tool) return null
    const build = BY_TOOL[step.tool]
    if (!build) return null
    const rows = build(envelope.result || {})
    return rows.length ? rows : null
  } catch {
    return null
  }
}

export default summarizeTourResult
