import { decodeGroundFrameSlots } from './solarGroundSlots.js'

export const SOLAR_STRING_COMPOSER_REASONS = Object.freeze({
  scope: 'Open a saved standalone drawing before creating strings.',
  loading: 'Reading the current drawing.',
  unavailable: 'The current drawing could not be read. Reload it and try again.',
  malformed: 'The saved panel data cannot be read. Reload the drawing before creating strings.',
  representation: 'This saved panel layout is not supported by this string form.',
  noPanels: 'This drawing has no panels available for stringing.',
  empty: 'Choose at least one panel.',
  range: 'Enter whole slot numbers within this row.',
  missing: 'A selected panel is no longer available. Reload the drawing and choose again.',
  duplicate: 'A panel appears more than once. Remove the repeated selection.',
  assigned: 'A selected panel already belongs to a string. Choose unassigned panels.',
  singleLimit: 'Choose at most 4096 panels for one string.',
  multiLimit: 'Choose at most 900 panels for this run.',
  length: 'Enter a whole string length from 1 to 900.',
  notSized: 'Set a positive string length in Solar settings or complete string sizing first.',
  tooLong: 'The requested string length exceeds the saved limit.',
  infeasible: 'This panel count cannot form strings at the requested length. Change the count or length.',
  units: 'Resolve the drawing units before creating strings.',
  numberExhausted: 'The drawing has no available string numbers. Change the starting string number before trying again.',
  request: 'These string inputs cannot be submitted. Review the selection and try again.',
  stale: 'The drawing changed. Reload it and choose the panels again.',
  checking: 'Checking the current drawing before review.',
  pending: 'This step is running. Confirm or wait for it to finish.',
  failed: 'Strings were not created. Your inputs are kept.',
  finished: 'Strings created.',
  cancelled: 'The run was cancelled. Your inputs are kept.',
})

export const STRING_ADD_TOOL = 'solar-string-add'
export const STRING_MULTI_ADD_TOOL = 'solar-string-multi-add'
export const MAX_SINGLE_REFS = 4096
export const MAX_MULTI_REFS = 900
export const MAX_STRING_LENGTH = 900
export const MAX_GRAPH_REV = 2147483647
export const MAX_COMBO_PANELS = 90000

const REFUSAL = Object.freeze({
  scope: Object.freeze({ ok: false, code: 'scope', reason: SOLAR_STRING_COMPOSER_REASONS.scope }),
  unavailable: Object.freeze({ ok: false, code: 'unavailable', reason: SOLAR_STRING_COMPOSER_REASONS.unavailable }),
  malformed: Object.freeze({ ok: false, code: 'malformed', reason: SOLAR_STRING_COMPOSER_REASONS.malformed }),
  representation: Object.freeze({ ok: false, code: 'representation', reason: SOLAR_STRING_COMPOSER_REASONS.representation }),
  noPanels: Object.freeze({ ok: false, code: 'noPanels', reason: SOLAR_STRING_COMPOSER_REASONS.noPanels }),
  empty: Object.freeze({ ok: false, code: 'empty', reason: SOLAR_STRING_COMPOSER_REASONS.empty }),
  range: Object.freeze({ ok: false, code: 'range', reason: SOLAR_STRING_COMPOSER_REASONS.range }),
  missing: Object.freeze({ ok: false, code: 'missing', reason: SOLAR_STRING_COMPOSER_REASONS.missing }),
  duplicate: Object.freeze({ ok: false, code: 'duplicate', reason: SOLAR_STRING_COMPOSER_REASONS.duplicate }),
  assigned: Object.freeze({ ok: false, code: 'assigned', reason: SOLAR_STRING_COMPOSER_REASONS.assigned }),
  singleLimit: Object.freeze({ ok: false, code: 'singleLimit', reason: SOLAR_STRING_COMPOSER_REASONS.singleLimit }),
  multiLimit: Object.freeze({ ok: false, code: 'multiLimit', reason: SOLAR_STRING_COMPOSER_REASONS.multiLimit }),
  length: Object.freeze({ ok: false, code: 'length', reason: SOLAR_STRING_COMPOSER_REASONS.length }),
  notSized: Object.freeze({ ok: false, code: 'notSized', reason: SOLAR_STRING_COMPOSER_REASONS.notSized }),
  tooLong: Object.freeze({ ok: false, code: 'tooLong', reason: SOLAR_STRING_COMPOSER_REASONS.tooLong }),
  infeasible: Object.freeze({ ok: false, code: 'infeasible', reason: SOLAR_STRING_COMPOSER_REASONS.infeasible }),
  units: Object.freeze({ ok: false, code: 'units', reason: SOLAR_STRING_COMPOSER_REASONS.units }),
  numberExhausted: Object.freeze({ ok: false, code: 'numberExhausted', reason: SOLAR_STRING_COMPOSER_REASONS.numberExhausted }),
  request: Object.freeze({ ok: false, code: 'request', reason: SOLAR_STRING_COMPOSER_REASONS.request }),
  stale: Object.freeze({ ok: false, code: 'stale', reason: SOLAR_STRING_COMPOSER_REASONS.stale }),
  failed: Object.freeze({ ok: false, code: 'failed', reason: SOLAR_STRING_COMPOSER_REASONS.failed }),
})
const FRESH = Object.freeze({ ok: true })
const indexes = new WeakMap()
const plain = (value) => value !== null && typeof value === 'object' &&
  [Object.prototype, null].includes(Object.getPrototypeOf(value))
const positive = (value) => Number.isInteger(value) && value > 0

export function stringComboSequences(target, count) {
  if (!Number.isInteger(target) || target < 1 || target > MAX_STRING_LENGTH ||
      !Number.isInteger(count) || count < 0 || count > MAX_COMBO_PANELS) return null
  const spread = Math.ceil(count / target)
  for (let i = 0; i <= spread; i += 1) {
    if (i * target + (spread - i) * (target - 1) === count) return [target, target - 1, i, spread - i]
  }
  for (let i = 0; i <= spread; i += 1) {
    if (i * (target - 1) + (spread - i) * (target - 2) === count) return [target - 1, target - 2, i, spread - i]
  }
  return [0, 0, 0, 0]
}

export function stringComboSizes(target, count) {
  const sequence = stringComboSequences(target, count)
  if (sequence === null || (sequence[0] === 0 && sequence[2] === 0)) return null
  const [a, b, qa, qb] = sequence
  const sizes = []
  for (let i = 0; i < qa; i += 1) sizes.push(a)
  for (let i = 0; i < qb; i += 1) sizes.push(b)
  if (sizes.some((size) => size < 1 || size > target) ||
      sizes.reduce((sum, size) => sum + size, 0) !== count) return null
  return sizes
}

export function savedStringLimit(graph) {
  if (!plain(graph)) return 0
  let limit = 0
  const consider = (value) => { if (Number.isInteger(value)) limit = Math.max(limit, value) }
  if (plain(graph.settings)) consider(graph.settings.panels_in_sequence)
  if (Array.isArray(graph.electrical_zones)) {
    for (const zone of graph.electrical_zones) {
      if (plain(zone)) consider(zone.panels_in_sequence)
    }
  }
  return limit
}

export function stringComposerView({ envelope, drawingId, drawingVersion, projectId }) {
  if (projectId !== null || typeof drawingId !== 'string' || !drawingId.length ||
      !positive(drawingVersion)) return REFUSAL.scope
  if (!plain(envelope) || !positive(envelope.version)) return REFUSAL.unavailable
  if (envelope.version !== drawingVersion) return REFUSAL.stale
  if (!plain(envelope.intake)) return REFUSAL.unavailable
  const graph = envelope.intake.solar_design_graph
  if (graph === undefined || graph === null) return REFUSAL.noPanels
  if (!plain(graph)) return REFUSAL.malformed
  if (!Number.isInteger(graph.rev) || graph.rev < 0 || graph.rev > MAX_GRAPH_REV ||
      !plain(graph.settings) || !Array.isArray(graph.electrical_zones) ||
      !Array.isArray(graph.frames) || !Array.isArray(graph.panels) || !Array.isArray(graph.strings) ||
      !graph.strings.every((string) => plain(string) && Array.isArray(string.ordered_panel_refs) &&
        string.ordered_panel_refs.every((ref) => typeof ref === 'string'))) return REFUSAL.malformed
  const design = graph.project?.installation_design
  if (design !== 'Ground' && design !== 'Roof') return REFUSAL.representation
  const rows = []
  const panels = []
  if (design === 'Ground') {
    if (graph.panels.length) return REFUSAL.representation
    const decoded = decodeGroundFrameSlots(graph.frames)
    if (decoded === null) return REFUSAL.malformed
    if (!decoded.frames.length) return REFUSAL.noPanels
    for (const frame of decoded.frames) {
      if (typeof frame.frameId !== 'string' || !frame.frameId.length) return REFUSAL.malformed
      const name = graph.frames[frame.frameIndex].name
      rows.push(Object.freeze({ frameIndex: frame.frameIndex, frameId: frame.frameId,
        name: typeof name === 'string' ? name : '', slotCount: frame.slots.length,
        panelIds: Object.freeze(frame.slots.map((slot) => slot.panelId)) }))
    }
  } else {
    if (graph.frames.some((frame) => frame !== null && typeof frame === 'object' &&
        Object.hasOwn(frame, 'ground_slots'))) return REFUSAL.representation
    if (!graph.panels.every((panel) => plain(panel) && typeof panel.id === 'string' &&
        panel.id.length >= 1 && panel.id.length <= 128)) return REFUSAL.malformed
    if (!graph.panels.length) return REFUSAL.noPanels
    const frames = new Map()
    for (const frame of graph.frames) {
      if (plain(frame) && !frames.has(frame.id)) frames.set(frame.id, frame)
    }
    for (const panel of graph.panels) {
      const frameRef = typeof panel.frame_ref === 'string' ? panel.frame_ref : null
      const name = frames.get(frameRef)?.name
      panels.push(Object.freeze({ id: panel.id, frameRef, frameName: typeof name === 'string' ? name : '' }))
    }
  }
  const assigned = new Set(graph.strings.flatMap((string) => string.ordered_panel_refs))
  const view = Object.freeze({ ok: true, binding: Object.freeze({ drawingId, drawingVersion, rev: graph.rev }),
    design, savedLimit: savedStringLimit(graph), rows: Object.freeze(rows), panels: Object.freeze(panels),
    assignedCount: assigned.size })
  indexes.set(view, { rows: new Map(rows.map((row) => [row.frameIndex, row])),
    panels: new Set(panels.map((panel) => panel.id)), assigned })
  return view
}

export function composeStringRequest({ view, toolName, queue, stringLength }) {
  if (view?.ok !== true) return REFUSAL.unavailable
  if (toolName !== STRING_ADD_TOOL && toolName !== STRING_MULTI_ADD_TOOL) return REFUSAL.request
  const multi = toolName === STRING_MULTI_ADD_TOOL
  if (multi && (!Number.isInteger(stringLength) || stringLength < 1 ||
      stringLength > MAX_STRING_LENGTH)) return REFUSAL.length
  if (!Array.isArray(queue)) return REFUSAL.request
  if (!queue.length) return REFUSAL.empty
  const limit = multi ? MAX_MULTI_REFS : MAX_SINGLE_REFS
  const overLimit = multi ? REFUSAL.multiLimit : REFUSAL.singleLimit
  if (queue.length > limit) return overLimit
  let count = 0
  for (const entry of queue) {
    if (!plain(entry) || entry.kind !== (view.design === 'Ground' ? 'range' : 'panel')) return REFUSAL.request
    if (view.design === 'Ground') {
      if (!Number.isInteger(entry.frameIndex) || entry.frameIndex < 0 ||
          typeof entry.frameId !== 'string') return REFUSAL.request
      if (!positive(entry.fromSlot) || !positive(entry.toSlot)) return REFUSAL.range
      count += Math.abs(entry.toSlot - entry.fromSlot) + 1
    } else {
      if (typeof entry.panelId !== 'string') return REFUSAL.request
      count += 1
    }
  }
  if (count > limit) return overLimit
  const index = indexes.get(view)
  if (!index) return REFUSAL.unavailable
  for (const entry of queue) {
    if (view.design === 'Ground') {
      const row = index.rows.get(entry.frameIndex)
      if (!row || row.frameId !== entry.frameId) return REFUSAL.missing
      if (entry.fromSlot > row.slotCount || entry.toSlot > row.slotCount) return REFUSAL.range
    } else if (!index.panels.has(entry.panelId)) return REFUSAL.missing
  }
  const ids = []
  for (const entry of queue) {
    if (view.design === 'Ground') {
      const row = index.rows.get(entry.frameIndex)
      const direction = entry.fromSlot <= entry.toSlot ? 1 : -1
      for (let slot = entry.fromSlot; ; slot += direction) {
        ids.push(row.panelIds[slot - 1])
        if (slot === entry.toSlot) break
      }
    } else ids.push(entry.panelId)
  }
  if (new Set(ids).size !== ids.length) return REFUSAL.duplicate
  if (view.savedLimit < 1) return REFUSAL.notSized
  if ((multi ? stringLength : count) > view.savedLimit) return REFUSAL.tooLong
  if (ids.some((id) => index.assigned.has(id))) return REFUSAL.assigned
  const sizes = multi ? stringComboSizes(stringLength, count) : [count]
  if (sizes === null) return REFUSAL.infeasible
  const params = multi
    ? { operation: 'add-strings', expected_rev: view.binding.rev, string_length: stringLength,
      ordered_panel_refs: ids }
    : { operation: 'add-string', expected_rev: view.binding.rev, ordered_panel_refs: ids }
  return Object.freeze({ ok: true, params,
    preview: Object.freeze({ ids: ids.slice(), sizes: Object.freeze(sizes) }) })
}

export function checkStringFreshness({ view, drawingId, drawingVersion, envelope }) {
  if (view?.ok !== true) return REFUSAL.unavailable
  if (drawingId !== view.binding.drawingId || drawingVersion !== view.binding.drawingVersion) return REFUSAL.stale
  if (!plain(envelope) || !positive(envelope.version) || !plain(envelope.intake) ||
      !plain(envelope.intake.solar_design_graph)) return REFUSAL.unavailable
  if (envelope.version !== view.binding.drawingVersion ||
      envelope.intake.solar_design_graph.rev !== view.binding.rev) return REFUSAL.stale
  return FRESH
}

export function stringRunRefusal(code) {
  switch (code) {
    case 'INVALID_STRING_ADD_REQUEST':
    case 'INVALID_STRING_MULTI_ADD_REQUEST': return REFUSAL.request
    case 'DUPLICATE_PANEL_MEMBERSHIP': return REFUSAL.duplicate
    case 'STALE_GRAPH_REVISION': return REFUSAL.stale
    case 'UNRESOLVED_UNITS': return REFUSAL.units
    case 'STRING_LENGTH_NOT_SIZED': return REFUSAL.notSized
    case 'STRING_TOO_LONG': return REFUSAL.tooLong
    case 'MISSING_PANEL': return REFUSAL.missing
    case 'PANEL_ALREADY_ASSIGNED': return REFUSAL.assigned
    case 'MULTI_ADD_INFEASIBLE_COUNT': return REFUSAL.infeasible
    case 'STRING_NUMBER_EXHAUSTED': return REFUSAL.numberExhausted
    case 'panels_required': return REFUSAL.noPanels
    case 'GRAPH_LIMIT_EXCEEDED':
    case 'INVALID_JSON_OBJECT':
    case 'INVALID_JSON_STRING':
    case 'NONFINITE_NUMBER':
    case 'NUMBER_LIMIT_EXCEEDED':
    case 'INVALID_JSON_VALUE':
    case 'SCHEMA_LIMIT_EXCEEDED':
    case 'INVALID_GRAPH':
    case 'UNSUPPORTED_SCHEMA_VERSION':
    case 'UNKNOWN_UNITS':
    case 'INVALID_GRAPH_SCHEMA':
    case 'INVALID_REVISION_CHAIN':
    case 'DUPLICATE_APPLICATION_ID':
    case 'ID_KIND_MISMATCH':
    case 'FUTURE_ENTITY_REVISION':
    case 'FUTURE_SCHEDULE_REVISION':
    case 'INVALID_GROUND_SLOTS':
    case 'INSTALLATION_DESIGN_MISMATCH':
    case 'TRACKER_SLOT_MISMATCH':
    case 'DEGENERATE_TRACKER_AXIS':
    case 'DUPLICATE_TRACKER_SOURCE':
    case 'STRING_COUNT_MISMATCH':
    case 'PANEL_ASSIGNMENT_MISMATCH':
    case 'FRAME_MEMBERSHIP_MISMATCH':
    case 'MISSING_ELECTRICAL_ZONE':
    case 'INVALID_MATRIX_DIMENSIONS':
    case 'INVALID_MATRIX_PANEL':
    case 'MATRIX_CELL_MISMATCH':
    case 'MATRIX_SEQUENCE_MISMATCH':
    case 'FRAME_ASSIGNMENT_MISMATCH':
    case 'FRAME_SEQUENCE_MISMATCH':
    case 'MATRIX_INPUT_MISMATCH':
    case 'INVERTER_CAPACITY_EXCEEDED':
    case 'INVERTER_ASSIGNMENT_MISMATCH':
    case 'DUPLICATE_INVERTER_INPUT':
    case 'EQUIPMENT_TYPE_REQUIRED':
    case 'L2_MODE_REQUIRED':
    case 'DUPLICATE_EQUIPMENT_NUMBER':
    case 'L2_MIXED_INPUTS':
    case 'L2_CAPACITY_EXCEEDED':
    case 'L2_ASSIGNMENT_MISMATCH':
    case 'DUPLICATE_L2_INPUT':
    case 'INVALID_SCHEDULE_COLUMNS':
    case 'ROUTE_ENDPOINT_MISMATCH':
    case 'DUPLICATE_FEEDER':
    case 'ROUTE_PATHWAY_MISMATCH':
      return REFUSAL.malformed
    default: return REFUSAL.failed
  }
}
