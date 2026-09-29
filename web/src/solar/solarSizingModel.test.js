import { describe, expect, it } from 'vitest'
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

describe('solarSizingModel', () => {
  it('keeps the reason vocabulary frozen and complete', () => {
    expect(Object.isFrozen(SOLAR_SIZING_REASONS)).toBe(true)
    expect(Object.keys(SOLAR_SIZING_REASONS)).toEqual(['sizing_graph_unavailable', 'sizing_project_scope',
      'sizing_zip_required', 'sizing_panels_required', 'sizing_zones_invalid', 'sizing_zone_models_required',
      'sizing_zone_models_invalid', 'sizing_fields_invalid', 'sizing_too_many_targets'])
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
