import { pyStrip } from './solarSettingsModel.js'
import { decodeGroundFrameSlots } from './solarGroundSlots.js'

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
})

export const MODULE_PARAMETER_KEYS = Object.freeze([
  'V_oc_ref', 'I_sc_ref', 'V_mp_ref', 'I_mp_ref', 'alpha_sc', 'beta_oc', 'STC', 'gamma_r', 'T_NOCT', 'N_s',
])

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
    zip: Array.from(pyStrip(graph.project.zip_code)).slice(0, 5).join(''),
    panels: graph.panels, slotIds, zones: graph.electrical_zones.map((zone) => ({ ...zone,
      module_model: zone.module_model ?? '', inverter_model_a: zone.inverter_model_a ?? '',
    })), power }
}

export function sizingTargets(graph, mode) {
  if (!graph?.ok) return fail('sizing_graph_unavailable')
  if (graph.panels.length === 0 && graph.slotIds.length === 0) return fail('sizing_panels_required')
  if (mode === 'global') return { ok: true, targets: [graph.settingsId] }
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
  if (!graph.zip) return fail('sizing_zip_required')
  const targets = sizingTargets(graph, mode)
  if (!targets.ok) return targets
  const invalid = []
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
  return { ok: true, params: { expected_rev: graph.rev, mode, requests, grant_ref: draft.grant_ref, confirm: true } }
}
