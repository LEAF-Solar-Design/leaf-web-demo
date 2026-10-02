// The guided Solar step rail as pure data. Every fact here derives from the
// server's per-step availability (and the rail's own run outcomes), never from
// a client-side "step completed" flag: resume, skips and invalidation follow
// the persisted graph the server gates each step on. Bounded throughout: at
// most MAX_FLOW_STEPS steps, MAX_REASON_CODES codes per step, and no regex
// runs on a string longer than MAX_REASON_CODE_LENGTH.
import { solarView } from './solarView.js'

export const MAX_FLOW_STEPS = 64
export const MAX_CATALOG_ROWS = 4096
export const DEFAULT_SOLAR_FLOW = 'rooftop'
// Shipped table bounds: at most 8 flows and 8 stages per flow; unique flow ids.
// Flow/stage ids match /^[a-z][a-z0-9-]{0,31}$/; labels have 1..64 characters
// and stage labels are unique within each flow. Maturity is production, preview
// or tutorial. Capability names match /^[a-z][a-z0-9-]{0,63}$/; at most 32 per
// stage and no duplicate capability name within a flow.
function flowStage(id, label, capabilities = []) {
  return Object.freeze({ id, label, capabilities: Object.freeze(capabilities) })
}

function flowEntry(id, label, maturity, stages) {
  return Object.freeze({ id, label, maturity, stages: stages === null ? null : Object.freeze(stages) })
}

// A Ground Electrical tool is bound only when a server test runs it to a pinned result on a graph produced by the conversion builtin:
// test_solar_ground_admission.py, test_solar_tool_trackers_to_panel_groups.py, test_solar_ground_equipment.py,
// test_solar_tool_central_inverter_add.py, test_solar_tool_solar_feeders_ground.py.
export const SOLAR_FLOWS = Object.freeze([
  flowEntry('rooftop', 'Rooftop', 'production', null),
  flowEntry('ground-electrical', 'Ground Mount Electrical', 'production', [
    flowStage('conversion', 'Tracker conversion', ['solar-trackers-to-panel-groups']),
    flowStage('stringing', 'Sizing and stringing', [
      'solar-settings', 'solar-size-strings', 'solar-string-add', 'solar-string-multi-add',
      'solar-string-midpoint', 'solar-string-flip', 'solar-string-swap', 'solar-string-delete',
      'solar-string-rebuild', 'solar-correct-string',
    ]),
    flowStage('equipment', 'Equipment', [
      'solar-assign-equipment', 'solar-string-conductors', 'solar-central-inverter-add',
    ]),
    flowStage('feeders', 'Feeders and routes', ['solar-feeders']),
    flowStage('calculations', 'NEC calculations', [
      'solar-nec-ampacity-correction', 'solar-nec-ac-voltage-drop',
      'solar-nec-conduit-fill', 'solar-nec-feeder-ocpd',
    ]),
    flowStage('outputs', 'Schedules and exports', [
      'solar-string-data', 'solar-electrical-schedules', 'solar-cable-export',
    ]),
  ]),
  flowEntry('ground-physical', 'Ground Mount Physical', 'preview', [
    flowStage('terrain', 'Terrain'), flowStage('layout', 'Tracker layout'),
    flowStage('civil', 'Civil and piles'), flowStage('analysis', 'Shade and terrain analysis'),
    flowStage('outputs', 'Exports'),
  ]),
  flowEntry('solaredge-import', 'SolarEdge PDF Import', 'production', [
    flowStage('upload', 'Upload the SolarEdge PDF'), flowStage('inspect', 'Inspect counts and matching'),
    flowStage('tracking', 'Accept tracking'), flowStage('outputs', 'Schedules and exports'),
  ]),
  flowEntry('pvcase-tutorial', 'PVcase Parity', 'tutorial', [
    flowStage('conversion', 'Geometry conversion'), flowStage('solve', 'Solve on the shared model'),
    flowStage('outputs', 'Exports'),
  ]),
])

export const SOLAR_FLOW_UNAVAILABLE_REASONS = Object.freeze({
  stages_missing: 'This flow needs Solar tools this catalog does not offer yet, so it has no steps to run.',
  rooftop_steps_missing: 'This catalog offers no Rooftop steps for this drawing yet.',
})
export const SOLAR_FLOW_MATURITY_NOTES = Object.freeze({
  preview: 'Preview flow: its results are not production Solar design yet.',
  tutorial: 'Tutorial flow: it shows the supported conversion boundaries and never runs PVcase itself.',
})

export function solarFlowId(value, flows = SOLAR_FLOWS) {
  return typeof value === 'string' && value.length <= 32 && flows.some((flow) => flow.id === value)
    ? value : DEFAULT_SOLAR_FLOW
}

export function solarFlowSelect(families, flowId, flows = SOLAR_FLOWS) {
  const id = solarFlowId(flowId, flows)
  const flow = flows.find((entry) => entry.id === id)
  let steps
  let missing = []
  let reasonKey = null
  if (flow.stages === null) {
    steps = solarFlowSteps(families)
    if (steps.length === 0) reasonKey = 'rooftop_steps_missing'
  } else {
    const index = new Map()
    let visited = 0
    scan: for (const family of Array.isArray(families) ? families : []) {
      const rows = Array.isArray(family?.capabilities) ? family.capabilities : []
      for (const row of rows) {
        if (visited >= MAX_CATALOG_ROWS) break scan
        visited += 1
        if (!row || typeof row !== 'object' || typeof row.name !== 'string' || row.name.length === 0) continue
        if (!index.has(row.name) && solarView(row).state === 'valid') index.set(row.name, row)
      }
    }
    const stageRows = flow.stages.map((stage) => stage.capabilities.filter((name) => index.has(name)).map((name) => index.get(name)))
    missing = flow.stages.filter((stage, position) => stageRows[position].length === 0).map((stage) => stage.label)
    if (missing.length > 0) {
      reasonKey = 'stages_missing'
      steps = []
    } else steps = stageRows.flat().slice(0, MAX_FLOW_STEPS)
  }
  return {
    flow: id, label: flow.label, maturity: flow.maturity, available: reasonKey === null,
    steps, reasonKey, reason: reasonKey === null ? null : SOLAR_FLOW_UNAVAILABLE_REASONS[reasonKey], missing,
  }
}

export function solarFlowOptions(families, flows = SOLAR_FLOWS) {
  return flows.map(({ id, label, maturity }) => ({
    id, label, maturity, available: solarFlowSelect(families, id, flows).available,
  }))
}

export function solarFlowOptionLabel(option) {
  const parts = []
  if (option.maturity !== 'production') parts.push(option.maturity)
  if (!option.available) parts.push('unavailable')
  return parts.length === 0 ? option.label : `${option.label} (${parts.join(', ')})`
}

const MAX_REASON_CODES = 8
const MAX_REASON_CODE_LENGTH = 64
// Charset only; the length bound is the explicit check beside each use.
// Availability refusal codes are the server's lowercase vocabulary.
const REFUSAL_CODE_PATTERN = /^[a-z][a-z0-9_]*$/
// Run failure codes keep the server's own case (INVALID_SEED_PARENT, STALE_GRAPH_REVISION).
const REASON_CODE_PATTERN = /^[A-Za-z][A-Za-z0-9_]*$/
const AVAILABILITY_UNAVAILABLE = 'capability_availability_unavailable'
const READY_KEYS = ['entitled', 'implemented', 'engine_ready', 'input_ready']
const SETTINGS_STEP = 'solar-settings'
// Codes about the session, the plan, the engine or a transient context rather
// than the drawing's design graph. A step that stops being ready for one of
// these alone was not invalidated by an upstream change, so it never reads
// "Needs rerun".
const SESSION_CODES = new Set([
  'entitlement_required',
  'broker_adapter_unavailable',
  'capability_availability_unavailable',
  'drawing_context_required',
  'not_current_head',
  'invalid_drawing_context',
  'entitlement_policy_unavailable',
  'persisted_graph_unavailable',
])
const NO_RUNS = Object.freeze({})

function plainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && [Object.prototype, null].includes(Object.getPrototypeOf(value))
}

/** Selection overlays may supply only keys admitted by a closed params schema. */
export function admittedOverlays(params, overlays) {
  try {
    if (!plainObject(params)) return {}
    if (params.additionalProperties !== false) return overlays
    if (!plainObject(params.properties) || !plainObject(overlays)) return {}
    return Object.fromEntries(Object.entries(overlays)
      .filter(([key]) => Object.prototype.hasOwnProperty.call(params.properties, key)))
  } catch {
    return {}
  }
}

function ownValue(record, key) {
  return plainObject(record) && Object.prototype.hasOwnProperty.call(record, key) ? record[key] : undefined
}

function boundedCode(code) {
  return typeof code === 'string' && code.length > 0 && code.length <= MAX_REASON_CODE_LENGTH
}

function validRefusalCode(code) {
  return boundedCode(code) && REFUSAL_CODE_PATTERN.test(code)
}

function validReasonCode(code) {
  return boundedCode(code) && REASON_CODE_PATTERN.test(code)
}

// The Solar settings step on a drawing with no design yet: the opener App gives
// the ribbon (canOpenSolarSettingsForm) says its typed form can start one.
function settingsOpenable(row, openSettingsForm) {
  return row.name === SETTINGS_STEP && typeof openSettingsForm === 'function'
    && openSettingsForm(row.name, row.availability) === true
}

function stepReady(availability) {
  return plainObject(availability) && READY_KEYS.every((key) => availability[key] === true)
}

function compareSteps(a, b) {
  if (a.order !== b.order) return a.order - b.order
  return a.row.name < b.row.name ? -1 : a.row.name > b.row.name ? 1 : 0
}

/** The W1 rows of a family list in (order, name) order, de-duplicated by name (first wins), at most MAX_FLOW_STEPS. */
export function solarFlowSteps(families) {
  const seen = new Set()
  const picked = []
  for (const family of Array.isArray(families) ? families : []) {
    const rows = family && typeof family === 'object' && Array.isArray(family.capabilities) ? family.capabilities : []
    for (const row of rows) {
      if (!row || typeof row !== 'object' || typeof row.name !== 'string' || row.name.length === 0) continue
      if (seen.has(row.name)) continue
      const result = solarView(row)
      if (result.state !== 'valid' || (result.view.wave !== 1 && row.name !== 'solar-string-conductors')) continue
      seen.add(row.name)
      picked.push({ row, order: row.name === 'solar-string-conductors' ? 75 : result.view.order })
    }
  }
  return picked.sort(compareSteps).slice(0, MAX_FLOW_STEPS).map(({ row }) => row)
}

/** The well-formed refusal codes of one availability record, at most MAX_REASON_CODES; unreadable availability is one code. */
export function solarFlowReasonCodes(availability) {
  if (!plainObject(availability) || !Array.isArray(availability.refusal_reasons)) return [AVAILABILITY_UNAVAILABLE]
  const codes = []
  for (const code of availability.refusal_reasons) {
    if (codes.length >= MAX_REASON_CODES) break
    if (validRefusalCode(code) && !codes.includes(code)) codes.push(code)
  }
  return codes
}

/** A run envelope reduced to what the rail shows: ok, and the first well-formed reason code or null. */
export function solarFlowRunOutcome(envelope) {
  const ok = plainObject(envelope) && envelope.ok === true
  const candidates = [ownValue(envelope, 'reason_code'), ownValue(ownValue(envelope, 'error'), 'error_code')]
  return { ok, code: ok ? null : (candidates.find(validReasonCode) ?? null) }
}

/** The rail's run memory after one settled run; a run for another drawing starts a fresh memory. */
export function solarFlowRecordRun(previous, run, envelope) {
  const drawingId = typeof run?.drawingId === 'string' ? run.drawingId : null
  const tool = typeof run?.tool === 'string' ? run.tool : null
  const base = plainObject(previous) && previous.drawingId === drawingId && plainObject(previous.runs) ? previous.runs : {}
  if (tool === null) return { drawingId, runs: base }
  return { drawingId, runs: { ...base, [tool]: solarFlowRunOutcome(envelope) } }
}

/** The run outcomes that belong to the open drawing; any other drawing's are dropped. */
export function solarFlowRunsFor(memory, drawingId) {
  return plainObject(memory) && memory.drawingId === (drawingId ?? null) && plainObject(memory.runs) ? memory.runs : NO_RUNS
}

/** 'pending', 'failed', 'finished' or null for one step, from the rail's run memory alone. */
export function solarFlowRunStatus(name, pendingTool, runs) {
  if (typeof name !== 'string') return null
  if (name === pendingTool) return 'pending'
  const run = ownValue(runs, name)
  if (plainObject(run) && run.ok === false) return 'failed'
  if (plainObject(run) && run.ok === true) return 'finished'
  return null
}

/** Each step's readiness now, keyed by name. */
export function solarFlowReadyMap(steps) {
  const ready = Object.create(null)
  for (const row of Array.isArray(steps) ? steps.slice(0, MAX_FLOW_STEPS) : []) {
    if (row && typeof row.name === 'string') ready[row.name] = stepReady(row.availability)
  }
  return ready
}

/**
 * The rail's items and the step to resume. resumeIndex is the last ready step by
 * order (-1 when none is ready). A ready step after a step that is not ready is
 * a skip. A step that was ready before on this drawing and is not now, for a
 * reason about the design graph, is invalidated and needs a rerun.
 * openSettingsForm is App's typed-form opener (undefined outside its scope):
 * when it accepts the Solar settings step, that step is enabled and can be the
 * resume step even though the strict rule says not ready. No other step changes.
 */
export function solarFlowState({ steps, previousReady, pendingTool, runs, openSettingsForm } = {}) {
  const rows = (Array.isArray(steps) ? steps : []).slice(0, MAX_FLOW_STEPS)
    .filter((row) => row && typeof row === 'object' && typeof row.name === 'string')
  let firstBlocked = -1
  let resumeIndex = -1
  const items = rows.map((row, index) => {
    const ready = stepReady(row.availability)
    const enabled = ready || settingsOpenable(row, openSettingsForm)
    const codes = enabled ? [] : solarFlowReasonCodes(row.availability)
    if (enabled) resumeIndex = index
    if (!ready && firstBlocked === -1) firstBlocked = index
    const wasReady = previousReady != null && typeof previousReady === 'object'
      && Object.prototype.hasOwnProperty.call(previousReady, row.name) && previousReady[row.name] === true
    const runStatus = solarFlowRunStatus(row.name, pendingTool, runs)
    const status = runStatus === 'pending' || runStatus === 'failed' ? runStatus : enabled ? 'ready' : 'blocked'
    return {
      name: row.name,
      label: typeof row.label === 'string' && row.label.length > 0 ? row.label : row.name,
      row,
      ready,
      enabled,
      status,
      codes,
      skip: ready && firstBlocked !== -1 && firstBlocked < index,
      invalidated: !enabled && wasReady && codes.some((code) => !SESSION_CODES.has(code)),
    }
  })
  return { items, resumeIndex }
}

/** expected_rev prefilled from the drawing's graph revision, only when the step takes an integer one and rev is a real revision. */
export function solarFlowPrefill(row, rev) {
  const property = ownValue(ownValue(ownValue(row, 'params'), 'properties'), 'expected_rev')
  if (!plainObject(property) || property.type !== 'integer') return {}
  if (!Number.isSafeInteger(rev) || rev < 0) return {}
  return { expected_rev: rev }
}

/** The DOM id of a step's rail button; the step editor returns focus to it. */
export function solarFlowStepId(name) {
  const safe = String(name).slice(0, MAX_REASON_CODE_LENGTH).replace(/[^A-Za-z0-9_-]/g, '-')
  return `solar-step-${safe}`
}
