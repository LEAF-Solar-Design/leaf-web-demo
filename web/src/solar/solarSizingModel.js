import { pyStrip } from './solarSettingsModel.js'
import { decodeGroundFrameSlots } from './solarGroundSlots.js'
import { createSolarDrawingBinding, sameSolarDrawingBinding, solarDrawingBindingKey } from './solarDrawingBinding.js'

export const SOLAR_SIZING_REASONS = Object.freeze({
  sizing_graph_unavailable: 'String sizing is unavailable for this drawing.',
  sizing_project_scope: 'This form supports standalone drawings only.',
  sizing_zip_required: 'Save a project ZIP code in Solar settings before sizing strings.',
  sizing_panels_required: 'Place panels before sizing strings.',
  sizing_zones_invalid: 'Zones must cover every panel exactly once.',
  sizing_zone_models_required: 'Every zone needs a module and inverter model before sizing by zone.',
  sizing_zone_models_invalid: 'Zone module and inverter model names must be at most 256 characters of valid text.',
  sizing_fields_invalid: 'Check the marked sizing fields before submitting.',
  sizing_too_many_targets: 'Size at most 4096 targets at once.',
  sizing_manual_length_required: 'Save a whole-number string length from 1 to 4096 in Solar settings.',
})

export const MODULE_PARAMETER_KEYS = Object.freeze([
  'V_oc_ref', 'I_sc_ref', 'V_mp_ref', 'I_mp_ref', 'alpha_sc', 'beta_oc', 'STC', 'gamma_r', 'T_NOCT', 'N_s',
])

export const PROJECT_SIZING_CLOUD_REASONS = Object.freeze({
  zones_unsupported: 'Project string sizing does not support zones. Choose global cloud sizing or the saved global length.',
})

export function projectSizingCloudReason(reason) {
  switch (reason) {
    case 'zones_unsupported': return PROJECT_SIZING_CLOUD_REASONS.zones_unsupported
    default: return null
  }
}

const plain = (value) => value !== null && typeof value === 'object' &&
  [Object.prototype, null].includes(Object.getPrototypeOf(value))
const fail = (reason, invalid = []) => ({ ok: false, reason, invalid })
// Unicode White_Space used by the server's Text pattern under Rust regex.
const TEXT_WHITESPACE = /[\u0009-\u000d\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]/u
const nonblank = (value) => Array.from(value).some((char) => !TEXT_WHITESPACE.test(char))
const hasLoneSurrogate = (value) => Array.from(value).some((char) =>
  char.length === 1 && char.charCodeAt(0) >= 0xd800 && char.charCodeAt(0) <= 0xdfff)
const text = (value) => typeof value === 'string' && Array.from(value).length >= 1 &&
  Array.from(value).length <= 256 && nonblank(value) &&
  !hasLoneSurrogate(value)

const bounded = (value, min, max) => typeof value === 'number' && Number.isFinite(value) && value >= min && value <= max
const real = (value) => bounded(value, -1e7, 1e7)
const note = (value) => typeof value === 'string' && Array.from(value).length <= 4096 && !hasLoneSurrogate(value)
const shape = (value, required, optional = {}) => plain(value) &&
  Object.keys(value).every((key) => Object.hasOwn(required, key) || Object.hasOwn(optional, key)) &&
  Object.entries(required).every(([key, valid]) => Object.hasOwn(value, key) && valid(value[key])) &&
  Object.entries(optional).every(([key, valid]) => !Object.hasOwn(value, key) || value[key] === null || valid(value[key]))
const simulationResult = (value) => shape(value, {
  Conditions: note,
  max_module_voltage: real,
  string_design_voltage: (number) => bounded(number, 1, 100000) && Number.isInteger(number),
  string_length: (number) => bounded(number, 1, 4096),
}, {
  safety_factor: real, 'Cell Temperature': real, 'POA Irradiance': real, long_note: note, short_note: note,
})

// JSON.parse cannot tell 81.0 from 81, so an integral float in an int field reads valid here while the server's strict int refuses it.
export function sizingResponseValid(response) {
  return shape(response, {
    cells: (value) => Number.isInteger(value) && bounded(value, 0, 10000),
    voc: (value) => bounded(value, 0, 2000) && value > 0,
    isc: real, pmp: real, vmp: real, imp: real, bpmp: real, bvoc: real, alpha_sc: real,
    min_temp: (value) => bounded(value, -100, 100),
    simulation_results: (value) => shape(value, { standard: simulationResult }, {
      conservative: simulationResult, day: simulationResult, nsrdb: simulationResult,
      ashrae_1: simulationResult, ashrae_2: simulationResult,
    }),
  }, { weather_mode: note, mintemp: real })
}

export function sizingGraph(view, drawingVersion) {
  const unavailable = { ok: false, reason: 'sizing_graph_unavailable' }
  if (!plain(view) || view.version !== drawingVersion) return unavailable
  const graph = view.intake?.solar_design_graph
  return sizingGraphFields(graph)
}

function sizingGraphFields(graph) {
  const unavailable = { ok: false, reason: 'sizing_graph_unavailable' }
  if (!plain(graph) || !Number.isInteger(graph.rev) || graph.rev < 0 || graph.rev > 1000000 ||
      typeof graph.settings?.id !== 'string' || Array.from(graph.settings.id).length < 1 ||
      Array.from(graph.settings.id).length > 100 || typeof graph.project?.zip_code !== 'string' ||
      !Array.isArray(graph.panels) || !graph.panels.every((panel) => typeof panel?.id === 'string') ||
      !Array.isArray(graph.electrical_zones) || !graph.electrical_zones.every((zone) =>
        typeof zone?.id === 'string' && Array.isArray(zone.panel_refs) &&
        zone.panel_refs.every((id) => typeof id === 'string') &&
        (zone.module_model === undefined || typeof zone.module_model === 'string') &&
        (zone.inverter_model_a === undefined || typeof zone.inverter_model_a === 'string'))) return unavailable
  // A graph without frames (or without a slot block) takes the unchanged path: no slot ids.
  if (graph.frames !== undefined && !Array.isArray(graph.frames)) return unavailable
  const decoded = decodeGroundFrameSlots(graph.frames ?? [])
  if (decoded === null) return unavailable
  const slotIds = decoded.panelIds
  const records = graph.settings.extra?.string_sizing?.records
  const power = Object.fromEntries(plain(records) ? Object.entries(records).map(([id, record]) => {
    const watts = record?.response?.pmp
    return [id, record?.adapter_version === '2.0.0' && sizingResponseValid(record?.response) && typeof watts === 'number' &&
      Number.isFinite(watts) && watts > 0 && watts <= 1000000 ? watts : null]
  }) : [])
  return { ok: true, rev: graph.rev, settingsId: graph.settings.id,
    savedLength: graph.settings.panels_in_sequence,
    zip: Array.from(pyStrip(graph.project.zip_code)).slice(0, 5).join(''),
    panels: graph.panels, slotIds, zones: graph.electrical_zones.map((zone) => ({ ...zone,
      module_model: zone.module_model ?? '', inverter_model_a: zone.inverter_model_a ?? '',
    })), power }
}

export function sizingTargets(graph, mode) {
  if (!graph?.ok) return fail('sizing_graph_unavailable')
  if (graph.panels.length === 0 && graph.slotIds.length === 0) return fail('sizing_panels_required')
  if (mode === 'global' || mode === 'manual-global') return { ok: true, targets: [graph.settingsId] }
  if (mode !== 'zones') return fail('sizing_fields_invalid', ['mode'])
  const panels = new Set(graph.panels.map((panel) => panel.id))
  for (const id of graph.slotIds) panels.add(id)
  const covered = new Set()
  for (const zone of graph.zones) {
    if (!zone.panel_refs.length) return fail('sizing_zones_invalid')
    for (const id of zone.panel_refs) {
      if (!panels.has(id) || covered.has(id)) return fail('sizing_zones_invalid')
      covered.add(id)
    }
  }
  if (covered.size !== panels.size) return fail('sizing_zones_invalid')
  if (graph.zones.some((zone) => !nonblank(zone.module_model) || !nonblank(zone.inverter_model_a))) {
    return fail('sizing_zone_models_required')
  }
  if (graph.zones.some((zone) => !text(zone.module_model) || !text(zone.inverter_model_a))) {
    return fail('sizing_zone_models_invalid')
  }
  if (graph.zones.length > 4096) return fail('sizing_too_many_targets')
  return { ok: true, targets: graph.zones.map((zone) => zone.id) }
}

export function buildSizingParams({ graph, mode, draft = {} }) {
  if (!graph?.ok) return fail('sizing_graph_unavailable')
  if (mode === 'manual-global') {
    const targets = sizingTargets(graph, mode)
    if (!targets.ok) return targets
    if (!Number.isInteger(graph.savedLength) || graph.savedLength < 1 || graph.savedLength > 4096) {
      return fail('sizing_manual_length_required')
    }
    return { ok: true, params: { expected_rev: graph.rev, mode: 'manual-global', confirm: true } }
  }
  if (!graph.zip) return fail('sizing_zip_required')
  const targets = sizingTargets(graph, mode)
  if (!targets.ok) return targets
  const invalid = []
  const acceptedLength = mode === 'global' && Object.hasOwn(draft, 'accepted_string_length')
  if (acceptedLength && (!Number.isInteger(draft.accepted_string_length)
      || draft.accepted_string_length < 1 || draft.accepted_string_length > 4096)) invalid.push('accepted_string_length')
  const requireText = (key) => { if (!text(draft[key])) invalid.push(key) }
  const requireBoolean = (key) => { if (typeof draft[key] !== 'boolean') invalid.push(key) }
  if (mode === 'global') ['module_name', 'full_inverter_name'].forEach(requireText)
  ;['bifacial_coefficient', 'surface_tilt', 'surface_azimuth', 'albedo', 'max_voltage', 'thermal_model_type'].forEach(requireText)
  ;['bifacial', 'open_circuit_rise'].forEach(requireBoolean)
  if (!['fixed_tilt', 'single_axis'].includes(draft.racking_type)) invalid.push('racking_type')
  if (draft.racking_type === 'single_axis') {
    ;['axis_tilt', 'axis_azimuth', 'max_angle', 'gcr'].forEach(requireText)
    requireBoolean('backtrack')
  }
  if (typeof draft.grant_ref !== 'string' || !/^[a-zA-Z0-9_-]{1,64}(?![\s\S])/.test(draft.grant_ref)) invalid.push('grant_ref')
  const moduleParameters = {}
  if (draft.use_module_parameters) {
    for (const key of MODULE_PARAMETER_KEYS) {
      const value = draft[key]
      const numeric = typeof value === 'string' && /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?(?![\s\S])/.test(value)
        ? Number(value) : NaN
      if (!Number.isFinite(numeric) || (key === 'N_s'
        ? !Number.isInteger(numeric) || numeric < 1 || numeric > 10000
        : numeric < -1e7 || numeric > 1e7)) invalid.push(key)
      else moduleParameters[key] = numeric
    }
  }
  if (invalid.length) return fail('sizing_fields_invalid', invalid)
  const racking = { racking_type: draft.racking_type, surface_tilt: draft.surface_tilt,
    surface_azimuth: draft.surface_azimuth, albedo: draft.albedo }
  if (draft.racking_type === 'single_axis') {
    for (const key of ['axis_tilt', 'axis_azimuth', 'max_angle', 'gcr', 'backtrack']) racking[key] = draft[key]
  }
  const requests = Object.fromEntries(targets.targets.map((id, index) => [id, {
    module_name: mode === 'zones' ? graph.zones[index].module_model : draft.module_name,
    full_inverter_name: mode === 'zones' ? graph.zones[index].inverter_model_a : draft.full_inverter_name,
    bifacial: draft.bifacial, bifacial_coefficient: draft.bifacial_coefficient, racking_params: { ...racking },
    max_voltage: draft.max_voltage, thermal_model_type: draft.thermal_model_type,
    open_circuit_rise: draft.open_circuit_rise, zip_code: graph.zip,
    ...(draft.use_module_parameters ? { module_parameters: { ...moduleParameters } } : {}),
  }]))
  return { ok: true, params: { expected_rev: graph.rev, mode, requests, grant_ref: draft.grant_ref, confirm: true,
    ...(acceptedLength ? { accepted_string_length: draft.accepted_string_length } : {}) } }
}

export const PROJECT_SIZING_CONDUCTOR_REASONS = Object.freeze({
  binding_invalid: 'Open a project drawing version before changing Solar sizing or conductors.',
  context_unreadable: 'This project drawing version could not be read. Reload it and try again.',
  not_current_head: 'Select the current project drawing version before making this change.',
  version_digest_unavailable: 'This project drawing version has no usable intake digest. Reload it and try again.',
  graph_unreadable: 'The Solar design in this project drawing could not be read.',
  graphless: 'Start a Solar design in Solar settings before sizing strings or choosing conductors.',
  scope_stale: 'The drawing or session changed. Review the current inputs before submitting.',
  catalog_digest_required: 'Refresh the Solar tools before preparing this change.',
  idempotency_key_required: 'This request could not be prepared. Try again.',
  preview_required: 'Checking whether this change can be applied to this project drawing.',
  preview_refused: 'This change cannot be applied. Review the refusal and try again.',
  submit_unavailable: 'Project Solar submission is not available in this view.',
  run_in_progress: 'A Solar run is in progress. Wait for it to finish.',
  request_invalid: 'This request could not be prepared. Review the inputs and try again.',
  manual_only: 'Project string sizing supports the saved global length only.',
  project_adapter_unavailable: 'Conductor changes are not available for project drawings on this server.',
  checkout_required: 'The project checkout must be available before this new change can run.',
  access_denied: 'Your current session cannot make this project drawing change.',
  service_unavailable: 'Project Solar is unavailable. Try again when the service is available.',
  request_timeout: 'The request timed out. Its submission status is uncertain; retry the same request.',
  request_cancelled: 'The request stopped locally. Check its status before retrying.',
})

export function projectSizingConductorReason(reason) {
  switch (reason) {
    case 'binding_invalid': return PROJECT_SIZING_CONDUCTOR_REASONS.binding_invalid
    case 'context_unreadable': return PROJECT_SIZING_CONDUCTOR_REASONS.context_unreadable
    case 'not_current_head': return PROJECT_SIZING_CONDUCTOR_REASONS.not_current_head
    case 'version_digest_unavailable': return PROJECT_SIZING_CONDUCTOR_REASONS.version_digest_unavailable
    case 'graph_unreadable': return PROJECT_SIZING_CONDUCTOR_REASONS.graph_unreadable
    case 'graphless': return PROJECT_SIZING_CONDUCTOR_REASONS.graphless
    case 'scope_stale': return PROJECT_SIZING_CONDUCTOR_REASONS.scope_stale
    case 'catalog_digest_required': return PROJECT_SIZING_CONDUCTOR_REASONS.catalog_digest_required
    case 'idempotency_key_required': return PROJECT_SIZING_CONDUCTOR_REASONS.idempotency_key_required
    case 'preview_required': return PROJECT_SIZING_CONDUCTOR_REASONS.preview_required
    case 'preview_refused': return PROJECT_SIZING_CONDUCTOR_REASONS.preview_refused
    case 'submit_unavailable': return PROJECT_SIZING_CONDUCTOR_REASONS.submit_unavailable
    case 'run_in_progress': return PROJECT_SIZING_CONDUCTOR_REASONS.run_in_progress
    case 'request_invalid': return PROJECT_SIZING_CONDUCTOR_REASONS.request_invalid
    case 'manual_only': return PROJECT_SIZING_CONDUCTOR_REASONS.manual_only
    case 'project_adapter_unavailable': return PROJECT_SIZING_CONDUCTOR_REASONS.project_adapter_unavailable
    case 'checkout_required': return PROJECT_SIZING_CONDUCTOR_REASONS.checkout_required
    case 'access_denied': return PROJECT_SIZING_CONDUCTOR_REASONS.access_denied
    case 'service_unavailable': return PROJECT_SIZING_CONDUCTOR_REASONS.service_unavailable
    case 'request_timeout': return PROJECT_SIZING_CONDUCTOR_REASONS.request_timeout
    case 'request_cancelled': return PROJECT_SIZING_CONDUCTOR_REASONS.request_cancelled
    default: return null
  }
}

const PROJECT_HEX = new RegExp('^[0-9a-f]{64}(?![\\s\\S])')
const PROJECT_DIGEST = new RegExp('^sha256:[0-9a-f]{64}(?![\\s\\S])')

export function createProjectSizingConductorBinding(value) {
  const exact = createSolarDrawingBinding(value)
  // R7 checks descriptors and UUID syntax; exact length also rejects a final newline accepted by a $ anchor.
  if (!['organization_id', 'project_id', 'drawing_id', 'input_version_id'].every((key) => exact[key].length === 36)) {
    throw new TypeError('Invalid Solar binding')
  }
  return exact
}

// Canonical context validation is shared by the two forms, without a legacy version envelope.
export function projectSizingConductorContext({ binding, contextView }) {
  const refuse = (reason) => ({ mode: 'refused', reason })
  let exact
  try { exact = createProjectSizingConductorBinding(binding) } catch { return refuse('binding_invalid') }
  const context = contextView?.context
  if (!plain(contextView) || !plain(context) || !plain(contextView.intake)) return refuse('context_unreadable')
  let returned, head
  try {
    returned = createProjectSizingConductorBinding({ kind: 'project', organization_id: context.organization_id,
      project_id: context.project_id, drawing_id: context.drawing_id, input_version_id: context.input_version_id })
    head = createProjectSizingConductorBinding({ ...exact, input_version_id: context.head_version_id })
  } catch { return refuse('context_unreadable') }
  if (!sameSolarDrawingBinding(exact, returned) || typeof context.is_head !== 'boolean'
      || context.is_head !== sameSolarDrawingBinding(exact, head)) return refuse('context_unreadable')
  if (!context.is_head) return refuse('not_current_head')
  if (typeof context.intake_sha256 !== 'string' || !PROJECT_HEX.test(context.intake_sha256)) {
    return refuse('version_digest_unavailable')
  }
  const intake = contextView.intake
  const hasGraph = Object.hasOwn(intake, 'solar_design_graph')
  const hasDigest = Object.hasOwn(intake, 'solar_design_graph_sha256')
  if (!hasGraph && !hasDigest) return refuse('graphless')
  if (!hasGraph || !hasDigest || !plain(intake.solar_design_graph)
      || typeof intake.solar_design_graph_sha256 !== 'string' || !PROJECT_HEX.test(intake.solar_design_graph_sha256)) {
    return refuse('graph_unreadable')
  }
  return { mode: 'edit', binding: exact, intakeSha256: context.intake_sha256,
    graphSha256: intake.solar_design_graph_sha256, sourceGraph: intake.solar_design_graph }
}

// Copy first: freezing a successful state must never freeze the caller's graph.
export function freezeProjectSizingConductorState(value, seen = new Set()) {
  if (value === null || typeof value !== 'object') return value
  if (seen.has(value)) throw new TypeError('Invalid Solar graph')
  seen.add(value)
  const copy = Array.isArray(value) ? value.map((item) => freezeProjectSizingConductorState(item, seen))
    : Object.fromEntries(Object.entries(value).map(([key, item]) => [key, freezeProjectSizingConductorState(item, seen)]))
  seen.delete(value)
  return Object.freeze(copy)
}

export function deriveProjectSizingState(args) {
  const context = projectSizingConductorContext(args)
  if (context.mode === 'refused') return context
  const graph = sizingGraphFields(context.sourceGraph)
  if (!graph.ok) return { mode: 'refused', reason: 'graph_unreadable' }
  try {
    return freezeProjectSizingConductorState({ mode: 'edit', binding: context.binding,
      intakeSha256: context.intakeSha256, graphSha256: context.graphSha256, rev: graph.rev,
      graph, savedZip: context.sourceGraph.project.zip_code })
  } catch { return { mode: 'refused', reason: 'graph_unreadable' } }
}

export function projectSizingConductorRequestScope({ binding, state, catalogDigest, idempotencyKey }) {
  let exact
  try { exact = createProjectSizingConductorBinding(binding) } catch { return { ok: false, reason: 'binding_invalid' } }
  if (state?.mode === 'refused') return { ok: false, reason: state.reason }
  if (state?.mode !== 'edit') return { ok: false, reason: 'request_invalid' }
  if (!sameSolarDrawingBinding(exact, state.binding)
      || solarDrawingBindingKey(exact) !== solarDrawingBindingKey(state.binding)) return { ok: false, reason: 'scope_stale' }
  if (typeof catalogDigest !== 'string' || !PROJECT_DIGEST.test(catalogDigest)) return { ok: false, reason: 'catalog_digest_required' }
  if (typeof idempotencyKey !== 'string' || !idempotencyKey.trim()
      || new TextEncoder().encode(idempotencyKey).byteLength > 256) return { ok: false, reason: 'idempotency_key_required' }
  return { ok: true, binding: exact }
}

export function buildProjectSizingRequest({ binding, state, mode = 'manual-global', draft, catalogDigest, idempotencyKey }) {
  const scope = projectSizingConductorRequestScope({ binding, state, catalogDigest, idempotencyKey })
  if (!scope.ok) return scope
  if (mode !== 'manual-global' && mode !== 'global') return { ok: false, reason: 'zones_unsupported' }
  const built = buildSizingParams({ graph: state.graph, mode, draft })
  if (!built.ok) return built
  return { ok: true, request: { tool: 'solar-size-strings',
    params: { drawing_id: scope.binding.drawing_id, ...built.params }, catalogDigest, idempotencyKey } }
}

export function exactProjectSizingConductorPreview(outcome, tool, catalogDigest) {
  const rows = Array.isArray(outcome?.data?.families) ? outcome.data.families.flatMap((family) =>
    Array.isArray(family?.capabilities) ? family.capabilities.filter((row) => row?.name === tool) : []) : []
  const accepted = outcome?.ok === true && typeof catalogDigest === 'string' && PROJECT_DIGEST.test(catalogDigest)
    && rows.length > 0 && rows.every((row) => {
    const availability = row.availability
    return row.catalog_digest === catalogDigest && availability?.admission_checked === true
      && availability.admission?.status_code === 200 && availability.admission.error === null
      && availability.entitled === true && availability.engine_ready === true && availability.implemented === true
      && availability.input_ready === true && availability.runnable === true
      && Array.isArray(availability.refusal_reasons) && availability.refusal_reasons.length === 0
  })
  return { accepted, admission: rows.find((row) => row.availability?.admission?.error)?.availability.admission
    ?? rows[0]?.availability?.admission ?? null }
}
