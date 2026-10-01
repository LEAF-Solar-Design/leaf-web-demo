// @vitest-environment node
import { readFileSync } from 'node:fs'
import { expect, it } from 'vitest'
import declaration from '../../../server/solar_tools/solar_design_presets.json'
import snapshot from '../../../docs/parity/evidence/ground/generate/profile-settings.json'
import { presetFormSpec, presetDefaults, presetDrafts, parsePresetField, fieldMessage, presetName,
  presetPrefix, presetListing, buildPresetParams, PRESET_LABELS, SOLAR_PRESET_REASONS, SOLAR_PRESET_NOTES } from './solarPresetModel.js'

const params = declaration.record.params
const keys = Object.keys(params.properties.current_settings.properties)
const SNAP = Object.fromEntries(keys.map((key) => [key, snapshot[key]]))
const spec = presetFormSpec(params)
const field = (key) => spec.fields.find((entry) => entry.key === key)
const row = (n, name, value) => ({ id: { entity_id: n }, type: 'report', quantity: 1, unit: 'each', name, value })
const LIST_TWO = { schema: 'leaf.solar-design-presets.v1', active_prefix: 'B', current_settings: { ...SNAP }, rows: [],
  list_rows: [row('report-profile-1', 'profile-1', 'A Alpha'), row('report-profile-2', 'profile-2', 'B Beta'), row('report-profiles', 'profiles', 2)] }
const LIST_EMPTY = { schema: LIST_TWO.schema, active_prefix: null, current_settings: null, rows: [], list_rows: [row('report-profiles', 'profiles', 0)] }
const LIST_257 = { schema: LIST_TWO.schema, active_prefix: 'A', current_settings: { ...SNAP, StringLayer: 'x'.repeat(257) }, rows: [],
  list_rows: [row('report-profile-1', 'profile-1', 'A Alpha'), row('report-profiles', 'profiles', 1)] }
const form = (changes = {}) => ({ revision: '0', subcommand: 'Create', name: '  Alpha ', prefix: 'A', mode: 'drawing', drafts: presetDrafts(spec, SNAP), listing: null, ...changes })
const build = (changes) => buildPresetParams(spec, form(changes))

it('PM1 derives ordered frozen fields and labels from the declaration', () => {
  expect(spec.ok).toBe(true)
  expect(spec.fields.map((entry) => entry.key)).toEqual(keys)
  expect(keys).toHaveLength(60)
  expect(Object.keys(PRESET_LABELS)).toEqual(keys)
  expect(new Set(Object.values(PRESET_LABELS)).size).toBe(60)
  for (const [kind, count] of Object.entries({ text: 31, number: 13, integer: 9, boolean: 7 })) {
    expect(spec.fields.filter((entry) => entry.kind === kind)).toHaveLength(count)
  }
  for (const entry of spec.fields) {
    expect(Object.isFrozen(entry)).toBe(true)
    expect(entry.label).toBe(PRESET_LABELS[entry.key])
    if (entry.kind === 'text') expect(entry.maxLength).toBe(256)
    if (entry.kind === 'integer' || entry.kind === 'number') {
      expect(entry.min).toBe(entry.kind === 'integer' ? 0 : -1000000)
      expect(entry.max).toBe(1000000)
    }
  }
  expect(['StringLayer', 'NumMppt', 'HomeRunLayer', 'SolarEdgeMatchPdfColors', 'BVoc', 'NumPanels'].map((key) => field(key).label))
    .toEqual(['String layer', 'MPPT count', 'Homerun layer', 'SolarEdge match PDF colors', 'BVoc', 'Panel count'])
  expect(spec.subcommands).toEqual(['Create', 'Swap', 'Delete'])
  expect(spec.revision).toEqual({ min: 0, max: 2147483647 })
  expect(spec.nameMaxLength).toBe(128)
  expect(Object.isFrozen(spec.fields)).toBe(true)
  expect(Object.isFrozen(PRESET_LABELS)).toBe(true)
})

it('PM2 derives defaults by kind and honors declared defaults and lower bounds', () => {
  const defaults = presetDefaults(spec)
  expect(Object.keys(defaults)).toEqual(keys)
  for (const entry of spec.fields) expect(defaults[entry.key]).toBe(entry.kind === 'text' ? '' : entry.kind === 'boolean' ? false : 0)
  const copy = structuredClone(params)
  copy.properties.current_settings.properties.NumMppt.default = 2
  copy.properties.current_settings.properties.CombinerBoxSize.minimum = 5
  expect(presetDefaults(presetFormSpec(copy))).toMatchObject({ NumMppt: 2, CombinerBoxSize: 5 })
})

it('PM3 fails closed on unsupported declarations', () => {
  const mutations = [
    (p) => { delete p.properties.current_settings },
    (p) => { p.properties.x = { type: 'string' } },
    (p) => { p.properties.subcommand.enum = ['Swap', 'Create', 'Delete'] },
    (p) => { p.properties.current_settings.additionalProperties = true },
    (p) => { p.properties.current_settings.properties.StringLayer.type = ['string', 'null'] },
    (p) => { delete p.properties.current_settings.properties.StringLayer.maxLength },
    (p) => { p.properties.current_settings.properties.NumMppt.minimum = 1.5 },
    (p) => { p.properties.current_settings.required = p.properties.current_settings.required.filter((key) => key !== 'NumPanels') },
    (p) => { p.properties.current_settings.required.push('Vmp') },
    (p) => {
      const properties = Object.fromEntries(Array.from({ length: 129 }, (_, index) => [`Field${index}`, { type: 'boolean' }]))
      p.properties.current_settings.properties = properties
      p.properties.current_settings.required = Object.keys(properties)
    },
    (p) => { p.properties.current_settings.properties['bad-key'] = { type: 'boolean' }; p.properties.current_settings.required.push('bad-key') },
    (p) => { p.properties.current_settings.properties.NumMppt.default = 'x' },
    (p) => { p.properties.expected_rev.maximum = 2147483648 },
  ]
  const cases = [null, [], ...mutations.map((mutate) => { const copy = structuredClone(params); mutate(copy); return copy })]
  for (const value of cases) expect(presetFormSpec(value)).toEqual({ ok: false, reason: 'preset_declaration_unsupported' })
})

it('PM4 parses whole numbers without accepting decimal or exponent syntax', () => {
  const f = field('NumMppt')
  for (const [draft, value] of [['12', 12], [' 7 ', 7], ['+3', 3], ['-0', 0]]) {
    expect(parsePresetField(f, draft)).toEqual({ ok: true, value })
    expect(Object.is(parsePresetField(f, draft).value, value)).toBe(true)
  }
  for (const [draft, problem] of [['', 'required'], ['abc', 'not_a_number'], ['2.5', 'not_whole'], ['1e3', 'not_whole'], ['-1', 'out_of_range'], ['1000001', 'out_of_range'], ['9007199254740993', 'out_of_range']]) expect(parsePresetField(f, draft)).toEqual({ ok: false, problem })
  expect(fieldMessage(f, 'not_whole')).toBe('Enter a whole number.')
  expect(fieldMessage(f, 'out_of_range')).toBe('Enter a value from 0 to 1000000.')
})

it('PM5 parses finite bounded decimal numbers', () => {
  const f = field('Vmp')
  for (const [draft, value] of [['44.64', 44.64], ['120', 120], ['.5', 0.5], ['1e3', 1000], ['-0', 0], ['-1000000', -1000000]]) {
    expect(parsePresetField(f, draft)).toEqual({ ok: true, value })
    expect(Object.is(parsePresetField(f, draft).value, value)).toBe(true)
  }
  for (const draft of ['1000000.5', '1e400', '-1000001']) expect(parsePresetField(f, draft)).toEqual({ ok: false, problem: 'out_of_range' })
  for (const draft of ['NaN', 'Infinity', '0x10', '1,5']) expect(parsePresetField(f, draft)).toEqual({ ok: false, problem: 'not_a_number' })
  expect(parsePresetField(f, '')).toEqual({ ok: false, problem: 'required' })
  expect(fieldMessage(f, 'out_of_range')).toBe('Enter a value from -1000000 to 1000000.')
})

it('PM6 preserves text and counts code points without truncation', () => {
  const f = field('StringLayer')
  for (const value of ['x'.repeat(256), '\u{1F600}'.repeat(256), '', '  a ']) expect(parsePresetField(f, value)).toEqual({ ok: true, value })
  expect(parsePresetField(f, '\ud800')).toEqual({ ok: false, problem: 'invalid_text' })
  expect(parsePresetField(f, 'x'.repeat(257))).toEqual({ ok: false, problem: 'too_long' })
  expect(fieldMessage(f, 'too_long', 'x'.repeat(257))).toBe('This value has 257 characters, and a preset stores at most 256.')
})

it('PM7 validates names and full prefixes', () => {
  for (const draft of ['Alpha', '  Alpha \t', '\ufeffAlpha']) expect(presetName(draft)).toEqual({ ok: true, value: 'Alpha' })
  expect(presetName(' ' + 'x'.repeat(128))).toEqual({ ok: true, value: 'x'.repeat(128) })
  for (const draft of ['', '   ', 'a\tb', 'a\u007fb', 'Alpha\u0085', 'A\ud800', 'x'.repeat(129), 7]) expect(presetName(draft)).toEqual({ ok: false })
  for (const value of ['A', 'z', 'P01', 'p99']) expect(presetPrefix(value)).toEqual({ ok: true, value })
  for (const value of ['AB', 'P1', 'P123', '', ' A', 'A\n', 7]) expect(presetPrefix(value)).toEqual({ ok: false })
})

it('PM8 reads measured lists and refuses malformed results', () => {
  expect(presetListing(LIST_257, spec)).toEqual({ ok: true, active: 'A', presets: [{ prefix: 'A', name: 'Alpha', active: true }], current: LIST_257.current_settings, unfit: ['StringLayer'] })
  expect(presetListing(LIST_257, spec).current.StringLayer).toHaveLength(257)
  expect(presetListing(LIST_EMPTY, spec)).toEqual({ ok: true, active: null, presets: [], current: null, unfit: [] })
  expect(presetListing(LIST_TWO, spec).presets).toEqual([{ prefix: 'A', name: 'Alpha', active: false }, { prefix: 'B', name: 'Beta', active: true }])
  expect(presetListing(LIST_TWO, spec).unfit).toEqual([])
  const mutations = [
    (p) => { delete p.rows }, (p) => { p.extra = true }, (p) => { p.schema = 'v2' },
    (p) => { p.list_rows[2].value = 3 }, (p) => { p.list_rows[0].value = 'AA Alpha' },
    (p) => { p.list_rows[1].name = 'profile-3' }, (p) => { p.active_prefix = 'Z' },
    (p) => { delete p.current_settings.NumPanels }, (p) => { p.current_settings.Vmp = '44' },
    (p) => { p.current_settings.StringLayer = 'x'.repeat(4097) },
    (p) => { p.list_rows = Array(66).fill(p.list_rows[0]) },
  ]
  for (const value of [null, { ...LIST_EMPTY, active_prefix: 'A' }, ...mutations.map((mutate) => { const copy = structuredClone(LIST_TWO); mutate(copy); return copy })]) expect(presetListing(value, spec)).toEqual({ ok: false })
})

it('PM9 omits current settings in drawing mode', () => {
  const result = build()
  expect(result).toEqual({ ok: true, params: { expected_rev: 0, subcommand: 'Create', name: 'Alpha' } })
  expect(Object.keys(result.params)).toEqual(['expected_rev', 'subcommand', 'name'])
  expect(Object.hasOwn(result.params, 'current_settings')).toBe(false)
})

it('PM10 supplies all settings in declaration order with their types', () => {
  const result = build({ mode: 'supply' })
  expect(result.params.current_settings).toEqual(SNAP)
  expect(Object.keys(result.params.current_settings)).toEqual(keys)
  expect(typeof result.params.current_settings.NumMppt).toBe('number')
  expect(result.params.current_settings.UseL2Collectors).toBe(true)
  expect(Object.keys(result.params)).toEqual(['expected_rev', 'subcommand', 'name', 'current_settings'])
  expect(build({ mode: 'supply', revision: '2', name: 'Gamma', listing: presetListing(LIST_TWO, spec) })).toEqual({ ok: true, params: { expected_rev: 2, subcommand: 'Create', name: 'Gamma', current_settings: SNAP } })
})

it('PM11 sends only the identity for the chosen action', () => {
  expect(build({ revision: '2', subcommand: 'Swap', prefix: 'a' }).params).toEqual({ expected_rev: 2, subcommand: 'Swap', prefix: 'a' })
  expect(build({ revision: '3', subcommand: 'Delete', prefix: 'B' }).params).toEqual({ expected_rev: 3, subcommand: 'Delete', prefix: 'B' })
  expect(build({ prefix: 'B' }).params).toEqual({ expected_rev: 0, subcommand: 'Create', name: 'Alpha' })
})

it('PM12 refuses invalid requests in contract order', () => {
  const refusal = (changes, reason, invalid = []) => expect(build(changes)).toEqual({ ok: false, reason, invalid })
  for (const revision of ['', '-1', '1.5', 'abc', '2147483648']) refusal({ revision }, 'revision_invalid')
  expect(build({ revision: '2147483647' }).params.expected_rev).toBe(2147483647)
  refusal({ revision: '', name: '' }, 'revision_invalid')
  refusal({ name: '' }, 'name_invalid')
  refusal({ subcommand: 'Swap', prefix: 'AB' }, 'prefix_invalid')
  refusal({ subcommand: 'List' }, 'subcommand_invalid')
  refusal({ mode: 'supply', drafts: presetDrafts(spec, presetDefaults(spec)) }, 'settings_all_default')
  refusal({ mode: 'supply', drafts: { ...presetDrafts(spec, SNAP), MaxPanelGap: 'x', NumMppt: '2.5' } }, 'settings_invalid', ['MaxPanelGap', 'NumMppt'])
  refusal({ listing: presetListing(LIST_EMPTY, spec) }, 'settings_required')
  for (const subcommand of ['Create', 'Swap']) refusal({ subcommand, listing: presetListing(LIST_257, spec) }, 'drawing_settings_unfit')
  refusal({ mode: 'supply', drafts: presetDrafts(spec, LIST_257.current_settings) }, 'settings_invalid', ['StringLayer'])
  expect(build({ mode: 'supply', drafts: presetDrafts(spec, { ...SNAP, StringLayer: 'x'.repeat(256) }) }).ok).toBe(true)
  refusal({ mode: 'other' }, 'settings_invalid')
})

it('PM13 exposes frozen reason sentences and display notes', () => {
  const sentences = {
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
  }
  expect(SOLAR_PRESET_REASONS).toEqual(sentences)
  expect(Object.keys(SOLAR_PRESET_REASONS)).toEqual(Object.keys(sentences))
  expect(SOLAR_PRESET_NOTES).toEqual({
    supply_sync: "Supplied settings become the drawing's current preset settings, and the drawing's string, homerun and panel group layer names follow them.",
    layer_blank: 'An empty string, homerun or panel group layer here blanks that layer name on the drawing.',
  })
  for (const map of [SOLAR_PRESET_REASONS, SOLAR_PRESET_NOTES]) {
    expect(Object.isFrozen(map)).toBe(true)
    for (const sentence of Object.values(map)) {
      expect(sentence.length).toBeGreaterThanOrEqual(12)
      expect(sentence).toMatch(/^[A-Za-z]/)
      for (const point of [0x2013, 0x2014]) expect(sentence).not.toContain(String.fromCharCode(point))
    }
  }
})

it('PM14 keeps transport and raw JSON entry out of the model and form', () => {
  for (const path of ['./solarPresetModel.js', './SolarPresetForm.jsx']) {
    const source = readFileSync(new URL(path, import.meta.url), 'utf8')
    for (const forbidden of ['api.js', 'fetch(', 'XMLHttpRequest', 'localStorage', 'JSON.parse']) expect(source).not.toContain(forbidden)
  }
})

it('PM15 caps the listing rows at 256', () => {
  expect(presetListing({ ...LIST_TWO, rows: Array(256).fill({}) }, spec).ok).toBe(true)
  expect(presetListing({ ...LIST_TWO, rows: Array(257).fill({}) }, spec)).toEqual({ ok: false })
})

it('PM16 bounds-checks declared defaults', () => {
  expect(presetFormSpec(structuredClone(params)).ok).toBe(true)
  const withDefault = (key, value) => {
    const copy = structuredClone(params)
    copy.properties.current_settings.properties[key].default = value
    return presetFormSpec(copy)
  }
  expect(withDefault('Vmp', 1000000).ok).toBe(true)
  expect(withDefault('NumMppt', 0).ok).toBe(true)
  expect(withDefault('Vmp', 1000001)).toEqual({ ok: false, reason: 'preset_declaration_unsupported' })
  expect(withDefault('NumMppt', -1)).toEqual({ ok: false, reason: 'preset_declaration_unsupported' })
})

it('PM17 pins every label to the declared settings keys', () => {
  const labels = {
    InstallationDesign: 'Installation design', MaxPanelGap: 'Max panel gap', AlignmentTolerance: 'Alignment tolerance',
    ExtractPanelsFromRackBlocks: 'Extract panels from rack blocks', ModuleLayer: 'Module layer', PanelGroupLayer: 'Panel group layer',
    PanelGroupLabelHeight: 'Panel group label height', PanelGroupOutlineWidth: 'Panel group outline width', StringLayer: 'String layer',
    StringWidth: 'String width', TagHeight: 'Tag height', LabelPlacement: 'Label placement', TagFirst: 'Tag first part',
    TagSecond: 'Tag second part', TagThird: 'Tag third part', TagDelim1: 'Tag delimiter 1', TagDelim2: 'Tag delimiter 2',
    NumPanelsInSequence: 'Panels in sequence', NumMppt: 'MPPT count', StringsPerMppt: 'Strings per MPPT', UseCombinerBox: 'Use combiner box',
    CombinerBoxSize: 'Combiner box size', CombinerBoxConnections: 'Combiner box connections',
    SolarEdgeContinuousNumbering: 'SolarEdge continuous numbering', SolarEdgeMatchPdfColors: 'SolarEdge match PDF colors',
    OptimizerBlock: 'Optimizer block', UseL2Collectors: 'Use L2 collectors', L1CollectorsPerL2: 'L1 collectors per L2',
    L2NumMppt: 'L2 MPPT count', L2StringsPerMppt: 'L2 strings per MPPT', InverterSelection: 'Inverter selection',
    ModuleSelection: 'Module selection', Vmp: 'Vmp', Imp: 'Imp', Pmp: 'Pmp', Voc: 'Voc', BVoc: 'BVoc', MinTemp: 'Minimum temperature',
    WireSize: 'Wire size', CableMaterial: 'Cable material', NecCableType: 'NEC cable type', MaxConductorTemp: 'Max conductor temperature',
    ConduitType: 'Conduit type', CableInstallationMethod: 'Cable installation method', AmbientTemperature: 'Ambient temperature',
    NoGroupedConductors: 'Number of grouped conductors', DistanceAboveRoof: 'Distance above roof', CableTray: 'Cable tray',
    VdRunMax: 'Max run voltage drop', SafetyFactor: 'Safety factor', DeratingFactor: 'Derating factor', HomeRunLayer: 'Homerun layer',
    InverterBlock: 'Inverter block', InverterBlockSize: 'Inverter block size', TagColor: 'Tag color', UseInverterColor: 'Use inverter color',
    UseOptimizers: 'Use optimizers', OptimizerRatio: 'Optimizer ratio', L2InverterSelection: 'L2 inverter selection', NumPanels: 'Panel count',
  }
  expect(Object.keys(labels)).toHaveLength(60)
  expect(PRESET_LABELS).toEqual(labels)
  const source = readFileSync(new URL('../../../server/solar_tools/solar_design_presets.json', import.meta.url), 'utf8')
  const declared = Object.keys(JSON.parse(source).record.params.properties.current_settings.properties)
  expect(Object.keys(PRESET_LABELS)).toEqual(declared)
  expect(Object.keys(labels)).toEqual(declared)
})

it('PM18 refuses two presets that share a prefix', () => {
  const listing = { ...LIST_TWO, active_prefix: 'A',
    list_rows: [row('report-profile-1', 'profile-1', 'A Alpha'), row('report-profile-2', 'profile-2', 'A Beta'), row('report-profiles', 'profiles', 2)] }
  expect(presetListing(listing, spec).ok).toBe(false)
  expect(presetListing({ ...listing,
    list_rows: [row('report-profile-1', 'profile-1', 'A Alpha'), row('report-profile-2', 'profile-2', 'B Beta'), row('report-profiles', 'profiles', 2)] }, spec).ok).toBe(true)
})
