export const PRESET_TOOL = 'solar-design-presets'

export const SOLAR_PRESET_REASONS = Object.freeze({
  preset_declaration_unsupported: 'This preset form cannot read the tool parameters, so it stays closed.',
  revision_invalid: 'Enter the graph revision as a whole number from 0 to 2147483647.',
  subcommand_invalid: 'Choose Create, Swap or Delete.',
  name_invalid: 'Enter a preset name of 1 to 128 characters with no control characters.',
  prefix_invalid: 'Enter a preset prefix: one letter, or P and two digits.',
  settings_invalid: 'Check the marked preset settings before submitting.',
  settings_all_default: 'Enter the preset settings first. Every field still holds its starting value.',
  settings_required: 'This drawing has no preset settings yet, so supply them for its first preset.',
  drawing_settings_unfit: 'A drawing setting is longer than a preset can store. Supply the settings, or shorten it in Solar settings.',
  listing_unreadable: 'The preset list for this drawing could not be read.',
})

export const SOLAR_PRESET_NOTES = Object.freeze({
  supply_sync: "Supplied settings become the drawing's current preset settings, and the drawing's string, homerun and panel group layer names follow them.",
  layer_blank: 'An empty string, homerun or panel group layer here blanks that layer name on the drawing.',
})

export const PRESET_LABELS = Object.freeze({
  InstallationDesign: 'Installation design',
  MaxPanelGap: 'Max panel gap',
  AlignmentTolerance: 'Alignment tolerance',
  ExtractPanelsFromRackBlocks: 'Extract panels from rack blocks',
  ModuleLayer: 'Module layer',
  PanelGroupLayer: 'Panel group layer',
  PanelGroupLabelHeight: 'Panel group label height',
  PanelGroupOutlineWidth: 'Panel group outline width',
  StringLayer: 'String layer',
  StringWidth: 'String width',
  TagHeight: 'Tag height',
  LabelPlacement: 'Label placement',
  TagFirst: 'Tag first part',
  TagSecond: 'Tag second part',
  TagThird: 'Tag third part',
  TagDelim1: 'Tag delimiter 1',
  TagDelim2: 'Tag delimiter 2',
  NumPanelsInSequence: 'Panels in sequence',
  NumMppt: 'MPPT count',
  StringsPerMppt: 'Strings per MPPT',
  UseCombinerBox: 'Use combiner box',
  CombinerBoxSize: 'Combiner box size',
  CombinerBoxConnections: 'Combiner box connections',
  SolarEdgeContinuousNumbering: 'SolarEdge continuous numbering',
  SolarEdgeMatchPdfColors: 'SolarEdge match PDF colors',
  OptimizerBlock: 'Optimizer block',
  UseL2Collectors: 'Use L2 collectors',
  L1CollectorsPerL2: 'L1 collectors per L2',
  L2NumMppt: 'L2 MPPT count',
  L2StringsPerMppt: 'L2 strings per MPPT',
  InverterSelection: 'Inverter selection',
  ModuleSelection: 'Module selection',
  Vmp: 'Vmp',
  Imp: 'Imp',
  Pmp: 'Pmp',
  Voc: 'Voc',
  BVoc: 'BVoc',
  MinTemp: 'Minimum temperature',
  WireSize: 'Wire size',
  CableMaterial: 'Cable material',
  NecCableType: 'NEC cable type',
  MaxConductorTemp: 'Max conductor temperature',
  ConduitType: 'Conduit type',
  CableInstallationMethod: 'Cable installation method',
  AmbientTemperature: 'Ambient temperature',
  NoGroupedConductors: 'Number of grouped conductors',
  DistanceAboveRoof: 'Distance above roof',
  CableTray: 'Cable tray',
  VdRunMax: 'Max run voltage drop',
  SafetyFactor: 'Safety factor',
  DeratingFactor: 'Derating factor',
  HomeRunLayer: 'Homerun layer',
  InverterBlock: 'Inverter block',
  InverterBlockSize: 'Inverter block size',
  TagColor: 'Tag color',
  UseInverterColor: 'Use inverter color',
  UseOptimizers: 'Use optimizers',
  OptimizerRatio: 'Optimizer ratio',
  L2InverterSelection: 'L2 inverter selection',
  NumPanels: 'Panel count',
})

const plain = (value) => value !== null && typeof value === 'object'
  && (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null)
const own = (value, key) => value != null && Object.hasOwn(value, key) ? value[key] : undefined
const length = (value) => Array.from(value).length
const wholePattern = /^[+-]?[0-9]+$/
const numberPattern = /^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$/
// JavaScript's end anchor also matches before a final newline. Require the full match.
const matches = (pattern, value) => typeof value === 'string' && pattern.exec(value)?.[0] === value

function validText(value) {
  if (typeof value !== 'string') return false
  for (const character of value) {
    const point = character.codePointAt(0)
    if (point >= 0xd800 && point <= 0xdfff) return false
  }
  return true
}

function rightKind(field, value) {
  if (field.kind === 'text') return validText(value)
  if (field.kind === 'boolean') return typeof value === 'boolean'
  return field.kind === 'integer' ? Number.isSafeInteger(value) : typeof value === 'number' && Number.isFinite(value)
}

function withinBounds(field, value) {
  return field.kind === 'text' ? length(value) <= field.maxLength
    : field.kind === 'boolean' || (value >= field.min && value <= field.max)
}

export function presetFormSpec(params) {
  const unsupported = () => ({ ok: false, reason: 'preset_declaration_unsupported' })
  if (!plain(params) || own(params, 'type') !== 'object' || !plain(own(params, 'properties'))) return unsupported()
  const properties = own(params, 'properties')
  const keys = ['expected_rev', 'subcommand', 'name', 'prefix', 'current_settings']
  if (keys.some((key) => !Object.hasOwn(properties, key))
    || Object.keys(properties).some((key) => !keys.includes(key) && key !== 'drawing_id')) return unsupported()
  const rev = own(properties, 'expected_rev')
  const action = own(properties, 'subcommand')
  const name = own(properties, 'name')
  const prefix = own(properties, 'prefix')
  const settings = own(properties, 'current_settings')
  if (![rev, action, name, prefix, settings].every(plain)) return unsupported()
  const min = own(rev, 'minimum'), max = own(rev, 'maximum')
  const commands = own(action, 'enum')
  const nameMaxLength = own(name, 'maxLength')
  if (own(rev, 'type') !== 'integer' || !Number.isInteger(min) || !Number.isInteger(max)
    || min < 0 || min > max || max > 2147483647
    || own(action, 'type') !== 'string' || !Array.isArray(commands) || commands.length !== 3
    || !['Create', 'Swap', 'Delete'].every((value, index) => own(commands, index) === value)
    || own(name, 'type') !== 'string' || !Number.isInteger(nameMaxLength) || nameMaxLength < 1 || nameMaxLength > 128
    || own(prefix, 'type') !== 'string' || own(settings, 'type') !== 'object'
    || own(settings, 'additionalProperties') !== false || !plain(own(settings, 'properties'))) return unsupported()
  const fieldProperties = own(settings, 'properties')
  const fieldKeys = Object.keys(fieldProperties)
  const required = own(settings, 'required')
  if (fieldKeys.length < 1 || fieldKeys.length > 128
    || fieldKeys.some((key) => !matches(/^[A-Za-z][A-Za-z0-9]{0,63}$/, key))
    || !Array.isArray(required) || required.length !== fieldKeys.length || new Set(required).size !== fieldKeys.length
    || fieldKeys.some((key) => !required.includes(key))) return unsupported()
  const fields = []
  for (const key of fieldKeys) {
    const property = own(fieldProperties, key)
    if (!plain(property)) return unsupported()
    const type = own(property, 'type')
    const field = { key, label: own(PRESET_LABELS, key) ?? key, kind: type === 'string' ? 'text' : type,
      min: null, max: null, maxLength: null, fallback: null }
    if (type === 'string') {
      field.maxLength = own(property, 'maxLength')
      if (!Number.isInteger(field.maxLength) || field.maxLength < 0 || field.maxLength > 4096) return unsupported()
      field.fallback = ''
    } else if (type === 'boolean') {
      field.fallback = false
    } else if (type === 'integer' || type === 'number') {
      field.min = own(property, 'minimum')
      field.max = own(property, 'maximum')
      const boundValid = type === 'integer' ? Number.isSafeInteger : Number.isFinite
      if (!boundValid(field.min) || !boundValid(field.max) || field.min > field.max) return unsupported()
      field.fallback = field.min <= 0 && field.max >= 0 ? 0 : field.min
    } else return unsupported()
    if (Object.hasOwn(property, 'default')) {
      const value = own(property, 'default')
      if (!rightKind(field, value) || !withinBounds(field, value)) return unsupported()
      field.fallback = Object.is(value, -0) ? 0 : value
    }
    fields.push(Object.freeze(field))
  }
  return Object.freeze({ ok: true, subcommands: Object.freeze([...commands]), revision: Object.freeze({ min, max }),
    nameMaxLength, fields: Object.freeze(fields) })
}

export function presetDefaults(spec) {
  return Object.fromEntries(spec.fields.map((field) => [field.key, field.fallback]))
}

export function presetDrafts(spec, values) {
  return Object.fromEntries(spec.fields.map((field) => {
    const value = own(values, field.key)
    const starting = rightKind(field, value) ? value : field.fallback
    return [field.key, field.kind === 'integer' || field.kind === 'number' ? String(starting) : starting]
  }))
}

export function parsePresetField(field, draft) {
  const fail = (problem) => ({ ok: false, problem })
  if (field.kind === 'text') {
    if (!validText(draft)) return fail('invalid_text')
    return length(draft) > field.maxLength ? fail('too_long') : { ok: true, value: draft }
  }
  if (field.kind === 'boolean') return typeof draft === 'boolean' ? { ok: true, value: draft } : fail('invalid_text')
  if (typeof draft !== 'string') return fail('not_a_number')
  const text = draft.trim()
  if (!text) return fail('required')
  if (field.kind === 'integer' && !matches(wholePattern, text)) {
    return fail(matches(numberPattern, text) ? 'not_whole' : 'not_a_number')
  }
  if (field.kind === 'number' && !matches(numberPattern, text)) return fail('not_a_number')
  const value = Number(text)
  if (!rightKind(field, value) || !withinBounds(field, value)) return fail('out_of_range')
  return { ok: true, value: Object.is(value, -0) ? 0 : value }
}

export function fieldMessage(field, problem, draft) {
  switch (problem) {
    case 'required': return 'Enter a value.'
    case 'not_a_number': return 'Enter a number.'
    case 'not_whole': return 'Enter a whole number.'
    case 'out_of_range': return `Enter a value from ${field.min} to ${field.max}.`
    case 'too_long': return `This value has ${length(draft)} characters, and a preset stores at most ${field.maxLength}.`
    case 'invalid_text': return 'This value has a character a preset cannot store.'
    default: return ''
  }
}

export function presetName(draft, maxLength = 128) {
  if (typeof draft !== 'string') return { ok: false }
  const value = draft.trim()
  return validText(value) && !/[\u0000-\u001f\u007f\u0085]/.test(value) && length(value) >= 1 && length(value) <= maxLength
    ? { ok: true, value } : { ok: false }
}

export function presetPrefix(draft) {
  return matches(/^(?:[A-Za-z]|[Pp][0-9]{2})$/, draft) ? { ok: true, value: draft } : { ok: false }
}

export function presetListing(listing, spec) {
  const fail = () => ({ ok: false })
  const keys = ['schema', 'active_prefix', 'current_settings', 'rows', 'list_rows']
  if (!spec?.ok || !plain(listing) || Object.keys(listing).length !== keys.length
    || keys.some((key) => !Object.hasOwn(listing, key)) || own(listing, 'schema') !== 'leaf.solar-design-presets.v1') return fail()
  const rows = own(listing, 'rows'), listRows = own(listing, 'list_rows')
  if (!Array.isArray(rows) || rows.length > 256 || !Array.isArray(listRows) || listRows.length > 65) return fail()
  const indexed = new Map(), prefixes = new Set()
  let count = null
  for (const row of listRows) {
    if (!plain(row) || own(row, 'type') !== 'report' || typeof own(row, 'name') !== 'string') return fail()
    const name = own(row, 'name'), value = own(row, 'value')
    if (name === 'profiles') {
      if (count !== null || !Number.isInteger(value) || value < 0 || value > 64) return fail()
      count = value
      continue
    }
    if (!matches(/^profile-[1-9][0-9]*$/, name) || typeof value !== 'string') return fail()
    const index = Number(name.substring(8))
    const space = value.indexOf(' ')
    const prefix = value.substring(0, space), storedName = value.substring(space + 1)
    if (space < 0 || !matches(/^(?:[A-Z]|P[0-9]{2})$/, prefix) || !validText(storedName)
      || /[\u0000-\u001f\u007f]/.test(storedName) || length(storedName) < 1 || length(storedName) > 128
      || prefixes.has(prefix) || indexed.has(index)) return fail()
    prefixes.add(prefix)
    indexed.set(index, { prefix, name: storedName })
  }
  const active = own(listing, 'active_prefix')
  if (count === null || count !== indexed.size || (count === 0 ? active !== null : !prefixes.has(active))) return fail()
  const presets = []
  for (let index = 1; index <= count; index += 1) {
    const preset = indexed.get(index)
    if (!preset) return fail()
    presets.push(Object.freeze({ ...preset, active: preset.prefix === active }))
  }
  const settings = own(listing, 'current_settings')
  let current = null
  const unfit = []
  if (settings !== null) {
    if (!plain(settings) || Object.keys(settings).length !== spec.fields.length) return fail()
    const entries = []
    for (const field of spec.fields) {
      if (!Object.hasOwn(settings, field.key)) return fail()
      const value = own(settings, field.key)
      if (!rightKind(field, value) || (field.kind === 'text' && length(value) > 4096)) return fail()
      entries.push([field.key, value])
      if (!withinBounds(field, value)) unfit.push(field.key)
    }
    current = Object.fromEntries(entries)
  }
  return Object.freeze({ ok: true, active, presets: Object.freeze(presets), current, unfit: Object.freeze(unfit) })
}

export function buildPresetParams(spec, form) {
  const fail = (reason, invalid = []) => ({ ok: false, reason, invalid: Object.freeze(invalid) })
  if (!spec?.ok) return fail('preset_declaration_unsupported')
  const revision = typeof form.revision === 'string' ? form.revision.trim() : ''
  const rev = Number(revision)
  if (!matches(/^[0-9]+$/, revision) || !Number.isSafeInteger(rev) || rev < spec.revision.min || rev > spec.revision.max) return fail('revision_invalid')
  if (!spec.subcommands.includes(form.subcommand)) return fail('subcommand_invalid')
  const identity = form.subcommand === 'Create' ? presetName(form.name, spec.nameMaxLength) : presetPrefix(form.prefix)
  if (!identity.ok) return fail(form.subcommand === 'Create' ? 'name_invalid' : 'prefix_invalid')
  let settings
  if (form.mode === 'drawing') {
    if (form.listing?.ok && form.listing.current === null) return fail('settings_required')
    if (form.listing?.ok && form.listing.unfit.length > 0) return fail('drawing_settings_unfit')
  } else if (form.mode === 'supply') {
    const invalid = [], entries = []
    for (const field of spec.fields) {
      const parsed = parsePresetField(field, own(form.drafts, field.key))
      if (!parsed.ok) invalid.push(field.key)
      else entries.push([field.key, parsed.value])
    }
    if (invalid.length) return fail('settings_invalid', invalid)
    settings = Object.fromEntries(entries)
    if (spec.fields.every((field) => Object.is(settings[field.key], field.fallback))) return fail('settings_all_default')
  } else return fail('settings_invalid')
  const params = { expected_rev: rev, subcommand: form.subcommand,
    [form.subcommand === 'Create' ? 'name' : 'prefix']: identity.value }
  if (form.mode === 'supply') params.current_settings = settings
  return { ok: true, params }
}
