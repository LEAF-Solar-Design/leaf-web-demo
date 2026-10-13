import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { createRunSubmissionRequest } from '../runIntent.js'
import r9Declaration from '../../../server/solar_tools/solar_string_conductors.json'
import { deriveProjectSizingState, buildProjectSizingRequest, PROJECT_SIZING_CLOUD_REASONS,
  projectSizingCloudReason } from './solarSizingModel.js'
import { conductorRows, conductorGaugeOptions, buildConductorParams, deriveProjectConductorState } from './solarConductorModel.js'
import { buildSizingParams, MODULE_PARAMETER_KEYS, sizingGraph, sizingResponseValid, sizingTargets, SOLAR_SIZING_REASONS } from './solarSizingModel.js'

const MIN = { cells: 81, voc: 52.58, isc: 13.9965, pmp: 595.5, vmp: 44.64, imp: 13.33,
  bpmp: -1.4875, bvoc: -0.13145, alpha_sc: .007, min_temp: -2.7,
  simulation_results: { standard: { Conditions: 'P99.5 Voc', max_module_voltage: 51.26,
    string_design_voltage: 1500, string_length: 28 } } }

const raw = () => ({ rev: 7, settings: { id: 'S', extra: {} }, project: { zip_code: '44224-1234' },
  panels: [{ id: 'P1' }, { id: 'P2' }], electrical_zones: [
    { id: 'ZA', panel_refs: ['P1'], module_model: 'Module A', inverter_model_a: 'Inverter A' },
    { id: 'ZB', panel_refs: ['P2'], module_model: 'Module B', inverter_model_a: 'Inverter B' },
  ] })
const read = (graph = raw()) => sizingGraph({ version: 3, intake: { solar_design_graph: graph } }, 3)
const draft = () => ({ module_name: 'Module', full_inverter_name: 'Inverter', bifacial: false,
  bifacial_coefficient: '.7', racking_type: 'fixed_tilt', surface_tilt: '5', surface_azimuth: '180', albedo: '.25',
  max_voltage: '1500', thermal_model_type: 'close mount glass glass', open_circuit_rise: false, grant_ref: 'grant_1' })
const build = (changes = {}, graph = read(), mode = 'global') => buildSizingParams({ graph, mode, draft: { ...draft(), ...changes } })

const r9Binding = { kind: 'project', organization_id: '11111111-1111-1111-1111-111111111111',
  project_id: '22222222-2222-2222-2222-222222222222', drawing_id: '33333333-3333-3333-3333-333333333333',
  input_version_id: '44444444-4444-4444-4444-444444444444' }
const r9Digest = 'sha256:' + 'd'.repeat(64)
const r9Context = (graph) => ({ context: { ...r9Binding, head_version_id: r9Binding.input_version_id,
  is_head: true, intake_sha256: 'a'.repeat(64) }, intake: { solar_design_graph: graph,
  solar_design_graph_sha256: 'b'.repeat(64) } })
const r9Oracle = JSON.parse(readFileSync(`${process.cwd()}/src/solar/__fixtures__/solarSizingConductorStandalone.main-f139ceba.json`, 'utf8'))
const r9Manual = () => ({ ...raw(), settings: { ...raw().settings, panels_in_sequence: 3 }, project: { zip_code: '' }, electrical_zones: [] })

describe('solarSizingModel', () => {

  it('R9-01 canonical context cannot pass either legacy version envelope', () => {
    const context = r9Context(raw())
    expect(sizingGraph(context, null)).toEqual({ ok: false, reason: 'sizing_graph_unavailable' })
    expect(conductorRows(context, null)).toEqual({ ok: false, reason: 'conductor_graph_unavailable' })
  })

  it('R9-02 sizing state copies and deeply freezes exact canonical identities and graph fields', () => {
    const source = r9Context(raw())
    const before = JSON.stringify(source)
    const state = deriveProjectSizingState({ binding: r9Binding, contextView: source })
    expect(state).toEqual({ mode: 'edit', binding: r9Binding, intakeSha256: 'a'.repeat(64),
      graphSha256: 'b'.repeat(64), rev: 7, graph: read(), savedZip: '44224-1234' })
    const frozen = (value) => {
      if (value && typeof value === 'object') {
        expect(Object.isFrozen(value)).toBe(true)
        Object.values(value).forEach(frozen)
      }
    }
    frozen(state)
    expect(Object.isFrozen(source.intake.solar_design_graph.panels)).toBe(false)
    expect(JSON.stringify(source)).toBe(before)
    source.intake.solar_design_graph.panels[0].id = 'changed'
    expect(state.graph.panels[0].id).toBe('P1')
  })

  it('R9-04 binding shape and data descriptors reject incomplete extra accessor and noncanonical IDs', () => {
    const accessor = { ...r9Binding }
    Object.defineProperty(accessor, 'drawing_id', { get() { throw new Error('must not read') }, enumerable: true })
    for (const binding of [null, { ...r9Binding, extra: true }, { ...r9Binding, kind: undefined },
      Object.fromEntries(Object.entries(r9Binding).filter(([key]) => key !== 'drawing_id')), accessor,
      { ...r9Binding, drawing_id: 'ABCDEFAB-1111-1111-1111-111111111111' }, { ...r9Binding, drawing_id: 'd1' },
      { ...r9Binding, drawing_id: r9Binding.drawing_id + '\n' }]) {
      expect(deriveProjectSizingState({ binding, contextView: r9Context(raw()) })).toEqual({ mode: 'refused', reason: 'binding_invalid' })
      expect(deriveProjectConductorState({ binding, contextView: r9Context(raw()) }).reason).toBe('binding_invalid')
    }
  })

  it('R9-06 context identity head UUID and strict head agreement are checked before graph fields', () => {
    const variants = [null, [], { ...r9Context(raw()), intake: null }]
    for (const key of ['organization_id', 'project_id', 'drawing_id', 'input_version_id']) {
      const context = r9Context(raw())
      context.context[key] = '66666666-6666-6666-6666-666666666666'
      variants.push(context)
    }
    for (const changes of [{ head_version_id: 'bad' }, { is_head: 1 }, { is_head: false },
      { is_head: true, head_version_id: '66666666-6666-6666-6666-666666666666' }]) {
      const context = r9Context(raw()); Object.assign(context.context, changes); variants.push(context)
    }
    for (const contextView of variants) {
      expect(deriveProjectSizingState({ binding: r9Binding, contextView }).reason).toBe('context_unreadable')
    }
  })

  it('R9-07 historical contexts refuse without retargeting their binding', () => {
    const contextView = r9Context(raw())
    contextView.context.head_version_id = '66666666-6666-6666-6666-666666666666'
    contextView.context.is_head = false
    const state = deriveProjectSizingState({ binding: r9Binding, contextView })
    expect(state).toEqual({ mode: 'refused', reason: 'not_current_head' })
    expect(buildProjectSizingRequest({ binding: r9Binding, state }).reason).toBe('not_current_head')
  })

  it('R9-08 intake digests require exactly lowercase 64 hex characters', () => {
    for (const digest of [undefined, null, '', 'A'.repeat(64), 'a'.repeat(63), 'a'.repeat(64) + '\n']) {
      const contextView = r9Context(raw()); contextView.context.intake_sha256 = digest
      expect(deriveProjectSizingState({ binding: r9Binding, contextView }).reason).toBe('version_digest_unavailable')
    }
  })

  it('R9-09 absent companions are graphless and partial null or malformed companions refuse', () => {
    for (const [intake, reason] of [[{}, 'graphless'], [{ solar_design_graph: raw() }, 'graph_unreadable'],
      [{ solar_design_graph_sha256: 'b'.repeat(64) }, 'graph_unreadable'],
      [{ solar_design_graph: null, solar_design_graph_sha256: 'b'.repeat(64) }, 'graph_unreadable'],
      [{ solar_design_graph: raw(), solar_design_graph_sha256: 'B'.repeat(64) }, 'graph_unreadable'],
      [{ solar_design_graph: { ...raw(), rev: -1 }, solar_design_graph_sha256: 'b'.repeat(64) }, 'graph_unreadable']]) {
      const contextView = { ...r9Context(raw()), intake }
      expect(deriveProjectSizingState({ binding: r9Binding, contextView })).toEqual({ mode: 'refused', reason })
    }
  })

  it('R9-10 manual standalone success no panels and invalid length equal the immutable oracle', () => {
    const graph = r9Manual()
    for (const [name, changes] of [['manual', {}], ['manualNoPanels', { panels: [] }],
      ['manualInvalidLength', { settings: { ...graph.settings, panels_in_sequence: '3' } }]]) {
      expect(JSON.stringify(buildSizingParams({ graph: read({ ...graph, ...changes }), mode: 'manual-global' }))).toBe(r9Oracle.captures[name].json)
    }
  })

  it('R9-11 canonical manual sizing adds only drawing ID without ZIP or zone coverage', () => {
    const state = deriveProjectSizingState({ binding: r9Binding, contextView: r9Context(r9Manual()) })
    expect(buildProjectSizingRequest({ binding: r9Binding, state, catalogDigest: r9Digest, idempotencyKey: 'key' }))
      .toEqual({ ok: true, request: { tool: 'solar-size-strings', params: {
        drawing_id: r9Binding.drawing_id, expected_rev: 7, mode: 'manual-global', confirm: true,
      }, catalogDigest: r9Digest, idempotencyKey: 'key' } })
  })

  it('R9-12 compact slot panels support manual sizing and malformed slots refuse', () => {
    const graph = r9Manual()
    graph.panels = []
    graph.frames = [{ ground_slots: { codec: 'leaf.solar-ground-slots.v1', count: 1,
      panel_ids: 'pOJc3Wd1QD2eDCdUAGmTCA==', centres: 'A'.repeat(22) + '==', angle: 0,
      panel: { rev: 0, provenance: {}, validity: {}, extra: {} } } }]
    const state = deriveProjectSizingState({ binding: r9Binding, contextView: r9Context(graph) })
    expect(buildProjectSizingRequest({ binding: r9Binding, state, catalogDigest: r9Digest, idempotencyKey: 'key' }).ok).toBe(true)
    graph.frames[0].ground_slots.count = 2
    expect(deriveProjectSizingState({ binding: r9Binding, contextView: r9Context(graph) }).reason).toBe('graph_unreadable')
  })

  it('R9-13 project global builds the cloud request and zones refuse', () => {
    const contextView = r9Context(raw()), input = draft()
    const state = deriveProjectSizingState({ binding: r9Binding, contextView })
    const args = { binding: r9Binding, state, mode: 'global', draft: input,
      catalogDigest: r9Digest, idempotencyKey: 'cloud-key' }
    const before = JSON.stringify({ contextView, state, input, binding: r9Binding })
    expect(buildProjectSizingRequest(args)).toEqual({ ok: true, request: { tool: 'solar-size-strings',
      params: { drawing_id: r9Binding.drawing_id, ...buildSizingParams({ graph: state.graph, mode: 'global', draft: input }).params },
      catalogDigest: r9Digest, idempotencyKey: 'cloud-key' } })
    expect(buildProjectSizingRequest({ ...args, mode: 'zones' })).toEqual({ ok: false, reason: 'zones_unsupported' })
    for (const catalogDigest of [undefined, '', r9Digest + '\n']) {
      expect(buildProjectSizingRequest({ ...args, catalogDigest }).reason).toBe('catalog_digest_required')
    }
    for (const idempotencyKey of [undefined, '', '🌞'.repeat(65)]) {
      expect(buildProjectSizingRequest({ ...args, idempotencyKey }).reason).toBe('idempotency_key_required')
    }
    expect(buildProjectSizingRequest({ ...args, binding: { ...r9Binding, drawing_id: 'bad' } }).reason).toBe('binding_invalid')
    expect(buildProjectSizingRequest({ ...args, state: { ...state, binding: { ...r9Binding,
      input_version_id: '55555555-5555-5555-5555-555555555555' } } }).reason).toBe('scope_stale')
    expect(buildProjectSizingRequest({ ...args, draft: { ...input, grant_ref: '' } }).invalid).toContain('grant_ref')
    expect(JSON.stringify({ contextView, state, input, binding: r9Binding })).toBe(before)
  })

  it('R14D-07 test_project_zones_remain_refused', () => {
    const state = deriveProjectSizingState({ binding: r9Binding, contextView: r9Context(raw()) })
    const args = { binding: r9Binding, state, mode: 'zones', draft: draft(), catalogDigest: r9Digest, idempotencyKey: 'key' }
    expect(buildSizingParams({ graph: state.graph, mode: 'zones', draft: draft() }).ok).toBe(true)
    for (const mode of ['zones', 'other', null]) {
      expect(buildProjectSizingRequest({ ...args, mode })).toEqual({ ok: false, reason: 'zones_unsupported' })
    }
    expect(buildProjectSizingRequest({ ...args, binding: null })).toEqual({ ok: false, reason: 'binding_invalid' })
    expect(buildProjectSizingRequest({ ...args, state: null }).reason).toBe('request_invalid')
    expect(buildProjectSizingRequest({ ...args, catalogDigest: '' }).reason).toBe('catalog_digest_required')
    expect(buildProjectSizingRequest({ ...args, idempotencyKey: '' }).reason).toBe('idempotency_key_required')
    expect(Object.isFrozen(PROJECT_SIZING_CLOUD_REASONS)).toBe(true)
    expect(projectSizingCloudReason('zones_unsupported')).toBe('Project string sizing does not support zones. Choose global cloud sizing or the saved global length.')
    expect(projectSizingCloudReason('unknown')).toBeNull()
  })

  it('R9-18 malformed digest or UTF8 key refuses before preparation', () => {
    const state = deriveProjectSizingState({ binding: r9Binding, contextView: r9Context(r9Manual()) })
    for (const catalogDigest of [undefined, null, '', 'd'.repeat(64), 'sha256:' + 'D'.repeat(64), r9Digest + '\n']) {
      expect(buildProjectSizingRequest({ binding: r9Binding, state, catalogDigest, idempotencyKey: 'key' }).reason).toBe('catalog_digest_required')
    }
    for (const idempotencyKey of [undefined, null, '', ' ', '🌞'.repeat(65)]) {
      expect(buildProjectSizingRequest({ binding: r9Binding, state, catalogDigest: r9Digest, idempotencyKey }).reason).toBe('idempotency_key_required')
    }
    expect(buildProjectSizingRequest({ binding: r9Binding, state, catalogDigest: r9Digest, idempotencyKey: '🌞'.repeat(64) }).ok).toBe(true)
  })

  it('R9-44 all ten standalone captures retain exact serialized strings byte lengths and hashes', () => {
    const manual = buildSizingParams({ graph: read(r9Manual()), mode: 'manual-global' })
    const conductorGraph = conductorRows({ version: 3, intake: { solar_design_graph: { rev: 7, strings: [
      { id: 'T1', circuit_tag: 'A1', wire_gauge: '' }, { id: 'T2', circuit_tag: 'A2', wire_gauge: '8 AWG' },
    ] } } }, 3)
    const base = { rev: 7, rows: conductorGraph.rows, selected: new Set(['T2', 'T1']),
      gauge: '10 AWG', options: conductorGaugeOptions(r9Declaration.record) }
    const conductors = buildConductorParams(base)
    const captures = { manual,
      manualNoPanels: buildSizingParams({ graph: read({ ...r9Manual(), panels: [] }), mode: 'manual-global' }),
      manualInvalidLength: buildSizingParams({ graph: read({ ...r9Manual(), settings: { ...r9Manual().settings, panels_in_sequence: '3' } }), mode: 'manual-global' }),
      conductors, conductorsEmptySelection: buildConductorParams({ ...base, selected: new Set() }),
      conductorsLowercaseGauge: buildConductorParams({ ...base, gauge: '10 awg' }),
      manualWire: createRunSubmissionRequest('solar-size-strings', manual.params, 'd1', { dwgVersion: 3 }),
      conductorWire: createRunSubmissionRequest('solar-string-conductors', conductors.params, 'd1', { dwgVersion: 3 }),
      globalCloudSizing: buildSizingParams({ graph: read(), mode: 'global', draft: draft() }),
      zoneCloudSizing: buildSizingParams({ graph: read(), mode: 'zones', draft: draft() }),
    }
    expect(Object.keys(captures)).toEqual(Object.keys(r9Oracle.captures))
    for (const [name, value] of Object.entries(captures)) {
      const serialized = JSON.stringify(value)
      expect(serialized).toBe(r9Oracle.captures[name].json)
      expect(Buffer.byteLength(serialized, 'utf8')).toBe(r9Oracle.captures[name].bytes)
      expect(createHash('sha256').update(serialized).digest('hex')).toBe(r9Oracle.captures[name].sha256)
    }
  })
  it('W21C2-manual-saved-length', () => {
    const source = raw()
    source.settings.panels_in_sequence = 3
    source.project.zip_code = ''
    const graph = read(source)
    const before = JSON.stringify(source)
    expect(graph.savedLength).toBe(3)
    expect(buildSizingParams({ graph, mode: 'manual-global' })).toEqual({ ok: true,
      params: { expected_rev: 7, mode: 'manual-global', confirm: true } })
    expect(JSON.stringify(source)).toBe(before)
    expect(graph.power).toEqual({})
  })

  it('W21C2-manual-length-validation', () => {
    for (const length of [undefined, null, true, false, '3', 0, -1, 1.5, NaN, Infinity, -Infinity, 4097]) {
      const source = raw()
      source.settings.panels_in_sequence = length
      expect(buildSizingParams({ graph: read(source), mode: 'manual-global' })).toEqual({ ok: false,
        reason: 'sizing_manual_length_required', invalid: [] })
    }
    for (const length of [1, 4096]) {
      const source = raw()
      source.settings.panels_in_sequence = length
      expect(buildSizingParams({ graph: read(source), mode: 'manual-global' }).ok).toBe(true)
    }
  })

  it('W21C2-manual-targets', () => {
    const source = raw()
    source.settings.panels_in_sequence = 3
    source.project.zip_code = ''
    source.electrical_zones = []
    expect(sizingTargets(read(source), 'manual-global')).toEqual({ ok: true, targets: ['S'] })
    source.panels = []
    expect(buildSizingParams({ graph: read(source), mode: 'manual-global' })).toEqual({ ok: false,
      reason: 'sizing_panels_required', invalid: [] })
    source.frames = [{ ground_slots: { codec: 'leaf.solar-ground-slots.v1', count: 1,
      panel_ids: 'pOJc3Wd1QD2eDCdUAGmTCA==', centres: 'A'.repeat(22) + '==', angle: 0,
      panel: { rev: 0, provenance: {}, validity: {}, extra: {} } } }]
    expect(buildSizingParams({ graph: read(source), mode: 'manual-global' })).toEqual({ ok: true,
      params: { expected_rev: 7, mode: 'manual-global', confirm: true } })
    expect(buildSizingParams({ graph: null, mode: 'manual-global' }).reason).toBe('sizing_graph_unavailable')
  })

  it('keeps the reason vocabulary frozen and complete', () => {
    expect(Object.isFrozen(SOLAR_SIZING_REASONS)).toBe(true)
    expect(Object.keys(SOLAR_SIZING_REASONS)).toEqual(['sizing_graph_unavailable', 'sizing_project_scope',
      'sizing_zip_required', 'sizing_panels_required', 'sizing_zones_invalid', 'sizing_zone_models_required',
      'sizing_zone_models_invalid', 'sizing_fields_invalid', 'sizing_too_many_targets', 'sizing_manual_length_required'])
  })

  it('accepts only current plain views and bounded graph revisions', () => {
    const view = { version: 3, intake: { solar_design_graph: raw() } }
    for (const value of [null, [], { ...view, version: 2 }, Object.assign(new Date(), view)]) {
      expect(sizingGraph(value, 3)).toEqual({ ok: false, reason: 'sizing_graph_unavailable' })
    }
    for (const rev of [-1, 1.1, 1000001, NaN, Infinity, '7']) expect(read({ ...raw(), rev }).ok).toBe(false)
    for (const rev of [0, 1000000]) expect(read({ ...raw(), rev }).ok).toBe(true)
  })

  it('validates graph collection shapes and treats absent zone models as blank', () => {
    for (const changes of [{ settings: { id: '' } }, { project: {} }, { panels: [{}] }, { panels: null },
      { electrical_zones: [{ id: 'ZA', panel_refs: [1] }] }, { electrical_zones: [{ id: 'ZA', panel_refs: [], module_model: null }] }]) {
      expect(read({ ...raw(), ...changes }).ok).toBe(false)
    }
    const graph = raw(); delete graph.electrical_zones[0].module_model
    expect(read(graph).zones[0].module_model).toBe('')
    expect(sizingTargets(read(graph), 'zones').reason).toBe('sizing_zone_models_required')
  })

  it('strips Python whitespace and takes five ZIP code points', () => {
    const graph = raw(); graph.project.zip_code = '\u001c 🌞12345 \u0085'
    expect(read(graph).zip).toBe('🌞1234')
    graph.project.zip_code = '\u001c\u0085'
    expect(build({}, read(graph)).reason).toBe('sizing_zip_required')
  })

  it('confirms only current adapter power within the numeric bounds', () => {
    const graph = raw()
    const values = [595.5, 1000000, 0, -1, true, '595', NaN, Infinity, 1000001]
    graph.settings.extra.string_sizing = { records: Object.fromEntries(values.map((pmp, index) =>
      [String(index), { adapter_version: '2.0.0', response: { ...MIN, pmp } }])) }
    expect(Object.values(read(graph).power)).toEqual([595.5, 1000000, null, null, null, null, null, null, null])
  })

  it('rejects outside panels, duplicates, empty zones and missing coverage', () => {
    for (const refs of [['P3'], ['P1'], [], ['P2', 'P2']]) {
      const graph = raw(); graph.electrical_zones[1].panel_refs = refs
      expect(sizingTargets(read(graph), 'zones').reason).toBe('sizing_zones_invalid')
      expect(sizingTargets(read(graph), 'global')).toEqual({ ok: true, targets: ['S'] })
    }
    const graph = raw(); graph.electrical_zones.pop()
    expect(sizingTargets(read(graph), 'zones').reason).toBe('sizing_zones_invalid')
  })

  it('limits zone targets to 4096', () => {
    const graph = raw()
    graph.panels = Array.from({ length: 4097 }, (_, index) => ({ id: `P${index}` }))
    graph.electrical_zones = graph.panels.map(({ id }, index) => ({ id: `Z${index}`, panel_refs: [id],
      module_model: 'Module', inverter_model_a: 'Inverter' }))
    expect(sizingTargets(read(graph), 'zones').reason).toBe('sizing_too_many_targets')
    graph.panels.pop(); graph.electrical_zones.pop()
    expect(sizingTargets(read(graph), 'zones').targets).toHaveLength(4096)
  })

  it('validates Unicode Text without changing typed values', () => {
    for (const value of ['', ' \u3000\u0085', '\t\n\u000b\u000c\r', '\ud800', '\udfff', '🌞'.repeat(257)]) {
      expect(build({ module_name: value }).invalid).toContain('module_name')
    }
    for (const value of ['🌞'.repeat(256), '  Module  ', '\u200b', '\u001c\u0085']) {
      expect(build({ module_name: value }).params.requests.S.module_name).toBe(value)
    }
    expect(build({ module_name: '', full_inverter_name: '' }, read(), 'zones').ok).toBe(true)
  })

  it('validates zone Text and preserves valid models verbatim', () => {
    for (const key of ['module_model', 'inverter_model_a']) {
      for (const [value, reason] of [['', 'sizing_zone_models_required'], [' \u3000\u0085', 'sizing_zone_models_required'],
        ['A'.repeat(257), 'sizing_zone_models_invalid'], ['\ud800', 'sizing_zone_models_invalid']]) {
        const graph = raw(); graph.electrical_zones[0][key] = value
        expect(build({}, read(graph), 'zones').reason).toBe(reason)
      }
      for (const value of ['\u001c', 'A'.repeat(256), '  Model  ']) {
        const graph = raw(); graph.electrical_zones[0][key] = value
        const requestKey = key === 'module_model' ? 'module_name' : 'full_inverter_name'
        expect(build({}, read(graph), 'zones').params.requests.ZA[requestKey]).toBe(value)
      }
    }
  })

  it('validates complete sizing responses with strict nested fields', () => {
    const standard = (changes) => ({ ...MIN, simulation_results: {
      standard: { ...MIN.simulation_results.standard, ...changes },
    } })
    const without = (key) => Object.fromEntries(Object.entries(MIN).filter(([name]) => name !== key))
    const cases = [
      [MIN, true], [{ pmp: 595.5 }, false], [without('cells'), false],
      [{ ...MIN, extra: null }, false], [{ ...MIN, cells: true }, false], [{ ...MIN, cells: 81.5 }, false],
      [{ ...MIN, voc: 0 }, false], [{ ...MIN, isc: '1' }, false],
      [{ ...MIN, simulation_results: {} }, false], [{ ...MIN, simulation_results: { standard: null } }, false],
      [{ ...MIN, simulation_results: { ...MIN.simulation_results, nsrdb: {} } }, false],
      [standard({ string_design_voltage: 1500.5 }), false], [standard({ cell_temperature: null }), false],
      [{ ...MIN, weather_mode: 'A'.repeat(4097) }, false], [[], false],
      [{ ...MIN, mintemp: null }, true], [{ ...MIN, weather_mode: null }, true],
      [{ ...MIN, simulation_results: { ...MIN.simulation_results, conservative: null } }, true],
      [standard({ 'Cell Temperature': null }), true], [standard({ string_design_voltage: 1500.0 }), true],
      [{ ...MIN, weather_mode: '🌞'.repeat(4096) }, true], [standard({ string_length: 1.5 }), true],
      [standard({ 'POA Irradiance': Infinity }), false], [standard({ safety_factor: NaN }), false],
      [standard({ extra: null }), false], [{ ...MIN, mintemp: undefined }, false],
      [Object.assign(new Date(), MIN), false],
    ]
    for (const [response, expected] of cases) expect(sizingResponseValid(response)).toBe(expected)
  })

  it('refuses lone surrogates in response notes', () => {
    for (const key of ['weather_mode', 'Conditions', 'long_note', 'short_note']) {
      for (const value of ['\ud800', 'a\udfff', 'x\ud800y']) {
        const response = key === 'weather_mode' ? { ...MIN, weather_mode: value } : {
          ...MIN, simulation_results: { standard: { ...MIN.simulation_results.standard, [key]: value } },
        }
        const graph = raw()
        graph.settings.extra.string_sizing = { records: { S: { adapter_version: '2.0.0', response } } }
        expect(sizingResponseValid(response)).toBe(false)
        expect(read(graph).power.S).toBe(null)
      }
    }
    const response = { ...MIN, weather_mode: '\u{1F31E}' }
    const graph = raw()
    graph.settings.extra.string_sizing = { records: { S: { adapter_version: '2.0.0', response } } }
    expect(sizingResponseValid(response)).toBe(true)
    expect(read(graph).power.S).toBe(595.5)
  })

  it('requires explicit booleans and an entire valid grant', () => {
    for (const value of [undefined, '', 'false', 0, null]) expect(build({ bifacial: value }).invalid).toContain('bifacial')
    for (const value of ['', 'g'.repeat(65), 'grant\n', 'grant space', '🌞']) {
      expect(build({ grant_ref: value }).invalid).toContain('grant_ref')
    }
    expect(build({ grant_ref: 'g'.repeat(64), bifacial: true }).ok).toBe(true)
  })

  it('parses whole decimal module parameters with finite bounds', () => {
    const parameters = Object.fromEntries(MODULE_PARAMETER_KEYS.map((key) => [key, key === 'N_s' ? '72' : '1']))
    const input = { use_module_parameters: true, ...parameters }
    for (const value of ['', '0x20', '1oops', 'Infinity', 'NaN', '1e309', '10000001', '1\n', ' 1']) {
      expect(build({ ...input, STC: value }).invalid).toContain('STC')
    }
    for (const value of ['-1e7', '1e7', '.5', '+1.2e2']) {
      expect(build({ ...input, STC: value }).params.requests.S.module_parameters.STC).toBe(Number(value))
    }
    for (const value of ['0', '10001', '72.5']) expect(build({ ...input, N_s: value }).invalid).toContain('N_s')
    expect(build({ ...input, N_s: '1' }).params.requests.S.module_parameters.N_s).toBe(1)
    expect(build({ ...input, N_s: '10000' }).params.requests.S.module_parameters.N_s).toBe(10000)
    expect(build({ ...input, use_module_parameters: false, STC: 'bad' }).params.requests.S).not.toHaveProperty('module_parameters')
  })

  it('preserves parameter and target order without mutating inputs', () => {
    const graph = read(), input = draft()
    const before = JSON.stringify({ graph, input })
    const result = buildSizingParams({ graph, mode: 'zones', draft: input })
    expect(Object.keys(result.params)).toEqual(['expected_rev', 'mode', 'requests', 'grant_ref', 'confirm'])
    expect(Object.keys(result.params.requests)).toEqual(['ZA', 'ZB'])
    expect(JSON.stringify({ graph, input })).toBe(before)
  })
})
