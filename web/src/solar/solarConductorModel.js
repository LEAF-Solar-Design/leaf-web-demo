export const CONDUCTOR_REASONS = Object.freeze({
  conductor_graph_unavailable: 'Conductor choices are unavailable for this drawing.',
  conductor_selection_required: 'Select at least one string.',
  conductor_gauge_required: 'Choose a conductor for the selected strings.',
  conductor_selection_too_large: 'Select at most 4096 strings.',
  conductor_project_scope: 'This form supports standalone drawings only.',
})

function plainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && [Object.prototype, null].includes(Object.getPrototypeOf(value))
}

const unavailable = () => ({ ok: false, reason: 'conductor_graph_unavailable' })

export function conductorGaugeOptions(row) {
  const options = row?.params?.properties?.assignments?.items?.properties?.wire_gauge?.enum
  if (!Array.isArray(options) || options.length === 0
    || !Array.from(options).every((value) => typeof value === 'string') || new Set(options).size !== options.length) return null
  return Object.freeze([...options])
}

export function conductorRows(intakeView, drawingVersion) {
  if (!plainObject(intakeView) || intakeView.version !== drawingVersion || !plainObject(intakeView.intake)) return unavailable()
  const graph = intakeView.intake.solar_design_graph
  if (!plainObject(graph) || !Number.isSafeInteger(graph.rev) || graph.rev < 0 || graph.rev > 1000000
    || !Array.isArray(graph.strings) || graph.strings.length > 4096) return unavailable()
  const seen = new Set()
  const rows = []
  for (const item of graph.strings) {
    if (!plainObject(item) || typeof item.id !== 'string' || item.id.length < 1 || item.id.length > 128
      || seen.has(item.id) || typeof item.circuit_tag !== 'string' || item.circuit_tag.length > 4096
      || typeof item.wire_gauge !== 'string' || item.wire_gauge.length > 4096) return unavailable()
    seen.add(item.id)
    rows.push({ id: item.id, tag: item.circuit_tag, gauge: item.wire_gauge })
  }
  return { ok: true, rev: graph.rev, rows }
}

export function buildConductorParams({ rev, rows, selected, gauge, options }) {
  if (!Array.isArray(options) || !Array.isArray(rows) || !Number.isSafeInteger(rev) || rev < 0 || rev > 1000000) return unavailable()
  if (!(selected instanceof Set) || selected.size === 0) return { ok: false, reason: 'conductor_selection_required' }
  if (selected.size > 4096) return { ok: false, reason: 'conductor_selection_too_large' }
  const ids = new Set(rows.map((row) => row.id))
  if ([...selected].some((id) => typeof id !== 'string' || !ids.has(id))) return unavailable()
  if (!gauge || !options.includes(gauge)) return { ok: false, reason: 'conductor_gauge_required' }
  return { ok: true, params: {
    operation: 'set-conductors', expected_rev: rev,
    assignments: rows.filter((row) => selected.has(row.id)).map((row) => ({ string_ref: row.id, wire_gauge: gauge })),
  } }
}
