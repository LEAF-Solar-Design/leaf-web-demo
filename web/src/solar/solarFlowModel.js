// The guided Solar step rail as pure data. Every fact here derives from the
// server's per-step availability (and the rail's own run outcomes), never from
// a client-side "step completed" flag: resume, skips and invalidation follow
// the persisted graph the server gates each step on. Bounded throughout: at
// most MAX_FLOW_STEPS steps, MAX_REASON_CODES codes per step, and no regex
// runs on a string longer than MAX_REASON_CODE_LENGTH.
import { solarView } from './solarView.js'
import { FLOW_PANELS } from './solarWorkspacePanels.js'
import { createSolarDrawingBinding, sameSolarDrawingBinding, solarCheckoutBindingKey, solarDrawingBindingKey } from './solarDrawingBinding.js'
import { deriveProjectSettingsState } from './solarSettingsModel.js'

export const PROJECT_EQUIPMENT_TOOL = 'solar-assign-equipment'
export const PROJECT_SOLAR_FLOW_REASONS = Object.freeze({
  binding_invalid: 'Open a project drawing version before assigning equipment.',
  context_unreadable: 'This project drawing version could not be read. Reload it and try again.',
  graph_unreadable: 'The Solar design stored with this drawing cannot be read, so equipment cannot be changed.',
  graphless: 'Start a Solar design in settings before assigning equipment.',
  not_current_head: 'Equipment changes require the newest drawing version. Open the head version first.',
  version_digest_unavailable: 'This drawing version has no recorded content digest. Reload it before assigning equipment.',
  scope_stale: 'The drawing or session changed. Review the current equipment inputs before submitting.',
  catalog_digest_required: 'Refresh the Solar tools before assigning equipment.',
  idempotency_key_required: 'This equipment request could not be prepared. Try again.',
  request_invalid: 'This equipment request could not be prepared. Review the inputs and try again.',
  invalid_value: 'Correct the marked equipment input before reviewing this request.',
  preview_required: 'Checking whether this equipment request can be applied to this drawing.',
  preview_refused: 'This equipment request cannot be applied. Review the refusal and try again.',
  submit_unavailable: 'Project equipment submission is not available in this view.',
  tool_unsupported: 'This tool has no project form in this view.',
  run_in_progress: 'A run is in progress. Wait for it to finish.',
  catalog_pending: 'Checking Solar steps for the selected project drawing version.',
  review_required: 'Review the inputs to check whether this step can run.',
})

// Static members keep the sentence map readable by the honesty scanner.
export function projectSolarFlowReason(reason) {
  switch (reason) {
    case 'binding_invalid': return PROJECT_SOLAR_FLOW_REASONS.binding_invalid
    case 'context_unreadable': return PROJECT_SOLAR_FLOW_REASONS.context_unreadable
    case 'graph_unreadable': return PROJECT_SOLAR_FLOW_REASONS.graph_unreadable
    case 'graphless': return PROJECT_SOLAR_FLOW_REASONS.graphless
    case 'not_current_head': return PROJECT_SOLAR_FLOW_REASONS.not_current_head
    case 'version_digest_unavailable': return PROJECT_SOLAR_FLOW_REASONS.version_digest_unavailable
    case 'scope_stale': return PROJECT_SOLAR_FLOW_REASONS.scope_stale
    case 'catalog_digest_required': return PROJECT_SOLAR_FLOW_REASONS.catalog_digest_required
    case 'idempotency_key_required': return PROJECT_SOLAR_FLOW_REASONS.idempotency_key_required
    case 'request_invalid': return PROJECT_SOLAR_FLOW_REASONS.request_invalid
    case 'invalid_value': return PROJECT_SOLAR_FLOW_REASONS.invalid_value
    case 'preview_required': return PROJECT_SOLAR_FLOW_REASONS.preview_required
    case 'preview_refused': return PROJECT_SOLAR_FLOW_REASONS.preview_refused
    case 'submit_unavailable': return PROJECT_SOLAR_FLOW_REASONS.submit_unavailable
    case 'tool_unsupported': return PROJECT_SOLAR_FLOW_REASONS.tool_unsupported
    case 'run_in_progress': return PROJECT_SOLAR_FLOW_REASONS.run_in_progress
    case 'catalog_pending': return PROJECT_SOLAR_FLOW_REASONS.catalog_pending
    case 'review_required': return PROJECT_SOLAR_FLOW_REASONS.review_required
    default: return null
  }
}

const PROJECT_FLOW_DIGEST = new RegExp('^sha256:[0-9a-f]{64}$')
const PROJECT_FLOW_CODE = new RegExp('^[A-Za-z][A-Za-z0-9_]{0,127}$')
const PROJECT_FLOW_TOOL = new RegExp('^[a-z0-9][a-z0-9-]{0,127}$')

export function deriveProjectEquipmentState({ binding, contextView }) {
  let state
  try { state = deriveProjectSettingsState({ binding, contextView }) }
  catch { return { mode: 'refused', reason: 'context_unreadable' } }
  if (state.mode === 'refused') return state
  if (state.mode === 'initialize') return { mode: 'refused', reason: 'graphless' }
  if (!Number.isSafeInteger(state.rev) || state.rev < 0) return { mode: 'refused', reason: 'graph_unreadable' }
  return { mode: 'edit', binding: state.binding, intakeSha256: state.intakeSha256, rev: state.rev }
}

// Descriptor reads reject accessors, sparse arrays, cycles and non-JSON values without invoking them.
function equipmentJson(value, seen = new Set(), depth = 0) {
  if (depth > 128) throw new TypeError('Invalid equipment values')
  if (value === null || typeof value === 'boolean' || typeof value === 'string') return value
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if ((!Array.isArray(value) && !plainObject(value)) || seen.has(value)) throw new TypeError('Invalid equipment values')
  seen.add(value)
  const keys = Reflect.ownKeys(value)
  const result = Array.isArray(value) ? [] : {}
  if (Array.isArray(value)) {
    if (keys.length !== value.length + 1) throw new TypeError('Invalid equipment array')
    for (let index = 0; index < value.length; index += 1) {
      const descriptor = Object.getOwnPropertyDescriptor(value, String(index))
      if (!descriptor || !Object.hasOwn(descriptor, 'value')) throw new TypeError('Invalid equipment array')
      result.push(equipmentJson(descriptor.value, seen, depth + 1))
    }
  } else {
    for (const key of keys) {
      const descriptor = Object.getOwnPropertyDescriptor(value, key)
      if (typeof key !== 'string' || !descriptor.enumerable || !Object.hasOwn(descriptor, 'value')) {
        throw new TypeError('Invalid equipment object')
      }
      Object.defineProperty(result, key, { enumerable: true, value: equipmentJson(descriptor.value, seen, depth + 1) })
    }
  }
  seen.delete(value)
  return Object.freeze(result)
}

export function buildProjectEquipmentRequest({ binding, state, values = {}, catalogDigest, idempotencyKey }) {
  const fail = (reason) => ({ ok: false, reason })
  let exact
  try { exact = createSolarDrawingBinding(binding) } catch { return fail('binding_invalid') }
  if (state?.mode === 'refused') return fail(state.reason)
  if (!sameSolarDrawingBinding(exact, state?.binding)) return fail('scope_stale')
  if (state?.mode !== 'edit' || !Number.isSafeInteger(state.rev) || state.rev < 0) return fail('request_invalid')
  if (typeof catalogDigest !== 'string' || !PROJECT_FLOW_DIGEST.test(catalogDigest)) return fail('catalog_digest_required')
  if (typeof idempotencyKey !== 'string' || !idempotencyKey.trim()
      || new TextEncoder().encode(idempotencyKey).byteLength > 256) return fail('idempotency_key_required')
  let copied
  try {
    if (!plainObject(values) || Reflect.ownKeys(values).some((key) => !['equipment', 'assignments', 'cancel', 'preview'].includes(key))) {
      return fail('request_invalid')
    }
    copied = equipmentJson(values)
  } catch { return fail('request_invalid') }
  return { ok: true, request: { tool: PROJECT_EQUIPMENT_TOOL,
    params: { drawing_id: exact.drawing_id, expected_rev: state.rev, ...copied }, catalogDigest, idempotencyKey } }
}

function projectStepReady(row) {
  const view = row?.availability
  return typeof row?.catalog_digest === 'string' && PROJECT_FLOW_DIGEST.test(row.catalog_digest)
    && view?.admission_checked === true && view.admission?.status_code === 200 && view.admission.error === null
    && ['entitled', 'engine_ready', 'implemented', 'input_ready', 'runnable'].every((key) => view[key] === true)
    && Array.isArray(view.refusal_reasons) && view.refusal_reasons.length === 0
}

export function exactProjectEquipmentPreview(outcome, catalogDigest) {
  if (!outcome?.ok || !Array.isArray(outcome.data?.families)) return { accepted: false, admission: null }
  const rows = outcome.data.families.flatMap((family) => Array.isArray(family?.capabilities) ? family.capabilities : [])
    .filter((row) => row?.name === PROJECT_EQUIPMENT_TOOL)
  const accepted = (row) => row.catalog_digest === catalogDigest && projectStepReady(row)
  return { accepted: rows.length > 0 && rows.every(accepted),
    admission: rows.find((row) => !accepted(row))?.availability?.admission ?? null }
}

export function projectSolarFlowScope(binding, authorityKey) {
  try {
    const exact = createSolarDrawingBinding(binding)
    if (typeof authorityKey !== 'string' || !authorityKey.trim()
        || new TextEncoder().encode(authorityKey).byteLength > 256) return null
    return { artifactKey: JSON.stringify([solarCheckoutBindingKey(exact), authorityKey]), versionKey: solarDrawingBindingKey(exact) }
  } catch { return null }
}

export function recordProjectSolarFlowRun(previous, run, settlement) {
  try {
    if (!plainObject(settlement) || Reflect.ownKeys(settlement).some((key) => !['terminal', 'ok', 'reason_code'].includes(key))) return previous
    settlement = equipmentJson(settlement)
  } catch { return previous }
  // Each association field is read once: an accessor that answers differently on a second read
  // can never key the run under one scope and store another binding beside it.
  let binding
  let authorityKey
  let tool
  try {
    binding = createSolarDrawingBinding(run?.binding)
    authorityKey = run?.authorityKey
    tool = run?.tool
  } catch { return previous }
  const scope = projectSolarFlowScope(binding, authorityKey)
  if (!scope || typeof tool !== 'string' || !PROJECT_FLOW_TOOL.test(tool)
      || !plainObject(settlement) || settlement.terminal !== true || typeof settlement.ok !== 'boolean'
      || (Object.hasOwn(settlement, 'reason_code') && (typeof settlement.reason_code !== 'string'
        || !PROJECT_FLOW_CODE.test(settlement.reason_code)))) return previous
  const base = plainObject(previous) && previous.artifactKey === scope.artifactKey && plainObject(previous.runs) ? previous.runs : {}
  return { artifactKey: scope.artifactKey, runs: { ...base, [tool]: {
    binding, authorityKey,
    ok: settlement.ok, code: settlement.ok ? null : settlement.reason_code ?? null,
  } } }
}

export function projectSolarFlowRunsFor(memory, binding, authorityKey) {
  const scope = projectSolarFlowScope(binding, authorityKey)
  return scope && plainObject(memory) && memory.artifactKey === scope.artifactKey && plainObject(memory.runs) ? memory.runs : NO_RUNS
}

const PROJECT_GRAPH_CODES = new Set([
  'GRAPH_NOT_EMBEDDED', 'INVALID_SEED_PARENT', 'GRAPH_DIGEST_MISMATCH', 'STALE_GRAPH_REVISION',
  'SIZING_CONFIRMATION_REQUIRED', 'UNRESOLVED_UNITS', 'INVALID_GRAPH_SCHEMA',
  'EQUIPMENT_INPUT_CAPACITY_EXCEEDED', 'EQUIPMENT_UNASSIGNED', 'EQUIPMENT_EMPTY_STRING',
  'EQUIPMENT_VOLTAGE_UNKNOWN', 'EQUIPMENT_POWER_UNKNOWN', 'EQUIPMENT_VOLTAGE_EXCEEDED', 'EQUIPMENT_POWER_EXCEEDED',
  'valid_settings_required', 'sizing_confirmation_required', 'unresolved_units', 'equipment_required',
])

export function deriveProjectSolarFlowState({ steps, current, previousReady, pending, runs, binding, authorityKey, openSettingsForm } = {}) {
  if (current !== true || !projectSolarFlowScope(binding, authorityKey)) return { items: [], resumeIndex: -1 }
  let firstBlocked = -1
  let resumeIndex = -1
  const all = Array.isArray(steps) ? steps : []
  // A catalog past the bound is refused whole: cutting it before rows are grouped by name could
  // hide a later duplicate whose admission contradicts an accepted one.
  if (all.length > MAX_CATALOG_ROWS) return { items: [], resumeIndex: -1 }
  const rows = all.filter((row) => row && typeof row.name === 'string')
  const grouped = new Map()
  for (const row of rows) {
    if (!grouped.has(row.name)) grouped.set(row.name, [])
    grouped.get(row.name).push(row)
  }
  const items = [...grouped.values()].slice(0, MAX_FLOW_STEPS).map((matching, index) => {
      const row = matching[0]
      const ready = matching.every((entry) => entry.catalog_digest === row.catalog_digest && projectStepReady(entry))
    const reviewable = PROJECT_SOLAR_REVIEW_TOOLS.includes(row.name) && matching.every((entry) => {
        const availability = entry.availability
        return typeof entry.catalog_digest === 'string' && PROJECT_FLOW_DIGEST.test(entry.catalog_digest)
          && entry.catalog_digest === row.catalog_digest && availability?.implemented === true && availability.engine_ready === true
          && availability.admission_checked === false && availability.admission === null
          && Array.isArray(availability.refusal_reasons) && availability.refusal_reasons.length === 1
          && availability.refusal_reasons[0] === 'project_request_required'
      })
      const enabled = ready || reviewable
      const codes = []
      let graphRefusal = false
      for (const entry of matching) {
        const availability = entry.availability
        // Distinct codes are collected before the bound, so a repeated code never uses up the
        // slots and a later distinct code, a graph code among them, is still seen.
        const rowCodes = []
        if (Array.isArray(availability?.refusal_reasons)) {
          for (const code of availability.refusal_reasons) {
            if (rowCodes.length >= MAX_REASON_CODES) break
            if (typeof code === 'string' && PROJECT_FLOW_CODE.test(code) && !rowCodes.includes(code)) rowCodes.push(code)
          }
        }
        const nestedCode = availability?.admission?.error?.reason_code
        if (typeof nestedCode === 'string' && PROJECT_FLOW_CODE.test(nestedCode) && !rowCodes.includes(nestedCode)) rowCodes.push(nestedCode)
        for (const code of rowCodes) if (codes.length < MAX_REASON_CODES && !codes.includes(code)) codes.push(code)
        graphRefusal ||= availability?.admission_checked === true
          && ((availability.admission?.status_code === 409 && PROJECT_GRAPH_CODES.has(nestedCode))
            || (availability.admission?.status_code === 200 && availability.admission.error === null
              && rowCodes.some((code) => PROJECT_GRAPH_CODES.has(code))))
      }
      const runOutcome = ownValue(runs, row.name) ?? null
      const sameRun = runOutcome?.authorityKey === authorityKey && sameSolarDrawingBinding(runOutcome?.binding, binding)
      const pendingHere = pending?.tool === row.name && pending.authorityKey === authorityKey && sameSolarDrawingBinding(pending.binding, binding)
      const status = pendingHere ? 'pending' : sameRun && runOutcome.ok === false ? 'failed'
        : ready ? 'ready' : enabled ? 'review' : 'blocked'
      if (enabled) resumeIndex = index
      if (!ready && firstBlocked === -1) firstBlocked = index
      return { name: row.name, label: typeof row.label === 'string' && row.label ? row.label : row.name,
        row, ready, enabled, status, codes: enabled ? [] : codes, runOutcome,
        skip: ready && firstBlocked !== -1 && firstBlocked < index,
        invalidated: !enabled && previousReady?.[row.name] === true && graphRefusal }
    })
  return { items, resumeIndex }
}

export const PROJECT_SOLAR_REVIEW_TOOLS = Object.freeze([
  'solar-settings', 'solar-panels-from-drawing', 'solar-size-strings',
  'solar-string-add', 'solar-string-multi-add', 'solar-solve-proposal', 'solar-commit-solve', 'solar-correct-string',
  'solar-string-conductors', 'solar-assign-equipment',
])

export const PROJECT_SOLAR_ROOFTOP_STAGES = Object.freeze([
  flowStage('settings', 'Settings', ['solar-settings']),
  flowStage('panels', 'Panel import', ['solar-panels-from-drawing']),
  flowStage('sizing', 'Manual sizing', ['solar-size-strings']),
  flowStage('strings', 'Strings', ['solar-string-add', 'solar-string-multi-add',
    'solar-solve-proposal', 'solar-commit-solve', 'solar-correct-string']),
  flowStage('conductors', 'Conductors', ['solar-string-conductors']),
  flowStage('equipment', 'Equipment', ['solar-assign-equipment']),
  flowStage('routes', 'Routes', ['solar-homeruns', 'solar-feeders']),
  flowStage('outputs', 'Outputs', ['solar-schedule', 'solar-string-data', 'solar-electrical-schedules', 'solar-cable-export']),
])

export const PROJECT_SOLAR_RAIL_REASONS = Object.freeze({
  binding_invalid: 'Open a project drawing version to review Solar steps.',
  catalog_pending: 'Checking Solar steps for the selected project drawing version.',
  stage_missing: 'This Solar step is not available in the current project catalog.',
  stage_refused: 'This Solar step cannot run with the current project request.',
  catalog_unreadable: 'The project Solar catalog could not be read. Refresh the tools and try again.',
  form_unavailable: 'This tool has no project form in this view.',
  run_in_progress: 'A run is in progress. Wait for it to finish.',
  review_required: 'Review the inputs to check whether this step can run.',
})

export function projectSolarRailReason(code) {
  switch (code) {
    case 'binding_invalid': return PROJECT_SOLAR_RAIL_REASONS.binding_invalid
    case 'catalog_pending': return PROJECT_SOLAR_RAIL_REASONS.catalog_pending
    case 'stage_missing': return PROJECT_SOLAR_RAIL_REASONS.stage_missing
    case 'stage_refused': return PROJECT_SOLAR_RAIL_REASONS.stage_refused
    case 'catalog_unreadable': return PROJECT_SOLAR_RAIL_REASONS.catalog_unreadable
    case 'form_unavailable': return PROJECT_SOLAR_RAIL_REASONS.form_unavailable
    case 'run_in_progress': return PROJECT_SOLAR_RAIL_REASONS.run_in_progress
    case 'review_required': return PROJECT_SOLAR_RAIL_REASONS.review_required
    default: return null
  }
}

// Count every catalog entry before filtering. The extra read detects overflow without
// hiding a late duplicate behind standalone selection's first-row rule.
export function projectSolarFlowSelect(families, flowId) {
  const id = solarFlowId(flowId)
  const flow = SOLAR_FLOWS.find((entry) => entry.id === id)
  const stages = id === DEFAULT_SOLAR_FLOW ? PROJECT_SOLAR_ROOFTOP_STAGES : flow.stages
  const grouped = new Map()
  let visited = 0
  let unreadable = false
  try {
    scan: for (const family of Array.isArray(families) ? families : []) {
      for (const row of Array.isArray(family?.capabilities) ? family.capabilities : []) {
        visited += 1
        if (visited > MAX_CATALOG_ROWS) { unreadable = true; break scan }
        if (!row || typeof row.name !== 'string') continue
        if (!grouped.has(row.name)) grouped.set(row.name, [])
        grouped.get(row.name).push(row)
      }
    }
  } catch { unreadable = true }
  const positions = stages.flatMap((stage) => {
    const names = stage.capabilities.length ? stage.capabilities : [`project-stage-${stage.id}`]
    return names.map((name) => {
      const matching = unreadable ? [] : grouped.get(name) ?? []
      let usable = false
      try { usable = matching.length > 0 && matching.every((row) => solarView(row).state === 'valid') } catch {}
      const label = matching[0]?.label
      return { name, stage: stage.id, label: typeof label === 'string' && label ? label : names.length === 1 ? stage.label : name,
        row: matching[0] ?? null, rows: matching, usable,
        formUnavailable: stage.kind !== undefined || stage.capabilities.length === 0 }
    })
  }).slice(0, MAX_FLOW_STEPS)
  const missing = stages.filter((stage) => positions.some((position) => position.stage === stage.id
    && (!position.usable || position.formUnavailable))).map((stage) => stage.label)
  return { flow: id, label: flow.label, maturity: flow.maturity, positions,
    steps: unreadable ? [] : positions.flatMap((position) => position.rows),
    available: !unreadable && missing.length === 0, missing,
    reasonKey: unreadable ? 'catalog_unreadable' : null }
}

export function projectSolarFlowOptions(families) {
  return SOLAR_FLOWS.map(({ id, label, maturity }) => ({ id, label, maturity,
    available: projectSolarFlowSelect(families, id).available }))
}

// Display gaps are separate from capability rows. Scope and catalog failures never
// acquire run history, readiness or admission through a placeholder.
export function projectSolarFlowDisplay({ selection, current, binding, authorityKey, busy = false,
  previousReady, pending, runs } = {}) {
  const scope = projectSolarFlowScope(binding, authorityKey)
  const active = current === true && !!scope && !!selection && selection.reasonKey !== 'catalog_unreadable'
  const derived = deriveProjectSolarFlowState({ steps: active ? selection.steps : [], current: active,
    binding, authorityKey, previousReady, pending, runs })
  const byName = new Map(derived.items.map((item) => [item.name, item]))
  const usableNames = new Set((selection?.positions ?? [])
    .filter((position) => position.usable && !position.formUnavailable).map((position) => position.name))
  const resumeName = [...derived.items].reverse().find((item) => item.enabled && usableNames.has(item.name))?.name
  let gap = false
  let resumeIndex = -1
  const items = (selection?.positions ?? []).map((position, index) => {
    const source = active && position.usable && !position.formUnavailable ? byName.get(position.name) : null
    const item = source ? { ...source, label: position.label, skip: source.ready && gap } : {
      name: position.name, label: position.label, row: position.row, ready: false, enabled: false,
      status: 'blocked', codes: active ? byName.get(position.name)?.codes ?? [] : [],
      runOutcome: null, skip: false, invalidated: false,
    }
    item.reasonKey = !scope ? 'binding_invalid' : current !== true ? 'catalog_pending'
      : selection.reasonKey === 'catalog_unreadable' ? 'catalog_unreadable'
        : position.formUnavailable ? 'form_unavailable' : !position.row ? 'stage_missing'
          : !item.enabled ? 'stage_refused' : busy ? 'run_in_progress'
            : item.status === 'review' ? 'review_required' : null
    item.enabled = item.enabled && !busy && !!position.row
    if (item.enabled && item.name === resumeName) resumeIndex = index
    if (!item.ready) gap = true
    return item
  })
  return { items, resumeIndex }
}

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

function panelStage(id, label, panels) {
  return Object.freeze({ id, label, kind: 'workspace-panel', panels: Object.freeze(panels), capabilities: Object.freeze([]) })
}

function panelCatalogStage(id, label, panels, capabilities) {
  return Object.freeze({ id, label, kind: 'workspace-panel-catalog', panels: Object.freeze(panels), capabilities: Object.freeze(capabilities) })
}

function flowEntry(id, label, maturity, stages) {
  return Object.freeze({ id, label, maturity, stages: stages === null ? null : Object.freeze(stages) })
}

// Catalog bindings require a named producer-chain server test with pinned results.
// Panel bindings require a registered workspace implementation with integration coverage
// and explicit host admission; registry membership alone does not establish a mount.
// Tracking labels do not admit electrical outputs. Ground conversion evidence:
// test_solar_ground_admission.py, test_solar_tool_trackers_to_panel_groups.py, test_solar_ground_equipment.py,
// test_solar_tool_central_inverter_add.py, test_solar_tool_solar_feeders_ground.py.
// SolarEdge producer-chain evidence: test_solar_solaredge_flow.py,
// test_solaredge_flow_accept_run and test_solaredge_flow_tracking_read_run.
// Ground Physical producer-chain evidence: test_solar_ground_physical_admission.py,
// test_ground_physical_admission_producer_chain.
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
    panelStage('terrain', 'Terrain', ['landxml', 'terrain']),
    panelStage('layout', 'Native frame layout', ['civil']),
    panelStage('civil', 'Grade pads and native piles', ['civil']),
    panelCatalogStage('analysis', 'Terrain and frame shade', ['physical-read'], ['solar-physical-shade']),
    panelCatalogStage('outputs', 'Terrain and shade CSV', ['physical-read'], ['solar-physical-export']),
  ]),
  flowEntry('solaredge-import', 'SolarEdge PDF Import', 'preview', [
    panelStage('upload', 'Upload the SolarEdge PDF', ['solaredge-import']),
    panelStage('inspect', 'Inspect counts and matching', ['solaredge-import']),
    flowStage('tracking', 'Accept tracking', ['solar-solaredge-accept']),
    flowStage('review', 'Review accepted tracking', ['solar-solaredge-tracking-read']),
  ]),
  flowEntry('pvcase-tutorial', 'PVcase Parity', 'tutorial', [
    flowStage('conversion', 'Geometry conversion'), flowStage('solve', 'Solve on the shared model'),
    flowStage('outputs', 'Exports'),
  ]),
])

export const SOLAR_FLOW_UNAVAILABLE_REASONS = Object.freeze({
  stages_missing: 'Some stages in this flow are not available yet.',
  rooftop_steps_missing: 'This catalog offers no Rooftop steps for this drawing yet.',
})
export const SOLAR_FLOW_MATURITY_NOTES = Object.freeze({
  preview: 'Preview flow: its results are not production Solar design yet.',
  tutorial: 'Tutorial flow: conversion and solve are not available in this workspace yet.',
})

export function solarFlowId(value, flows = SOLAR_FLOWS) {
  return typeof value === 'string' && value.length <= 32 && flows.some((flow) => flow.id === value)
    ? value : DEFAULT_SOLAR_FLOW
}

export function solarFlowSelect(families, flowId, flows = SOLAR_FLOWS, workspacePanelsByFlow = {}) {
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
    const admitted = []
    const stageRows = flow.stages.map((stage) => {
      if (stage.kind === undefined) {
        const rows = stage.capabilities.filter((name) => index.has(name)).map((name) => index.get(name))
        admitted.push(rows.length > 0)
        return rows
      }
      const registered = FLOW_PANELS[id]
      const supplied = workspacePanelsByFlow?.[id]
      const panelsAdmitted = Array.isArray(stage.panels) && stage.panels.length > 0
        && Array.isArray(registered) && Array.isArray(supplied)
        && Array.from(stage.panels).every((panel) => typeof panel === 'string' && panel.length > 0
          && registered.includes(panel) && supplied.includes(panel))
      if (stage.kind === 'workspace-panel-catalog') {
        const capabilitiesAdmitted = Array.isArray(stage.capabilities) && stage.capabilities.length > 0
          && Array.from(stage.capabilities).every((name) => typeof name === 'string' && name.length > 0 && index.has(name))
        admitted.push(panelsAdmitted && capabilitiesAdmitted)
        return panelsAdmitted && capabilitiesAdmitted ? stage.capabilities.map((name) => index.get(name)) : []
      }
      admitted.push(stage.kind === 'workspace-panel' && panelsAdmitted)
      return []
    })
    missing = flow.stages.filter((stage, position) => !admitted[position]).map((stage) => stage.label)
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

export function solarFlowOptions(families, flows = SOLAR_FLOWS, workspacePanelsByFlow = {}) {
  return flows.map(({ id, label, maturity }) => ({
    id, label, maturity, available: solarFlowSelect(families, id, flows, workspacePanelsByFlow).available,
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
  if (ok) return { ok, code: null }
  // Candidates are read lazily in precedence order: the first well-formed code wins, and no later
  // nested value is read once an earlier one has decided the outcome.
  const readers = [() => ownValue(envelope, 'reason_code'), () => ownValue(ownValue(envelope, 'error'), 'reason_code'),
    () => ownValue(ownValue(envelope, 'error'), 'error_code')]
  for (const read of readers) {
    const code = read()
    if (validReasonCode(code)) return { ok, code }
  }
  return { ok, code: null }
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
