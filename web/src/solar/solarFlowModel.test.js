import { describe, expect, it } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import ampacityDeclaration from '../../../server/solar_tools/solar_nec_ampacity_correction.json'
import voltageDeclaration from '../../../server/solar_tools/solar_nec_ac_voltage_drop.json'
import conduitDeclaration from '../../../server/solar_tools/solar_nec_conduit_fill.json'
import ocpdDeclaration from '../../../server/solar_tools/solar_nec_feeder_ocpd.json'
import conversionDeclaration from '../../../server/solar_tools/solar_trackers_to_panel_groups.json'
import { FLOW_PANELS } from './solarWorkspacePanels.js'
import {
  MAX_FLOW_STEPS, admittedOverlays, solarFlowPrefill, solarFlowReadyMap, solarFlowReasonCodes, solarFlowRecordRun,
  solarFlowRunOutcome, solarFlowRunStatus, solarFlowRunsFor, solarFlowState, solarFlowStepId, solarFlowSteps,
} from './solarFlowModel.js'
import {
  DEFAULT_SOLAR_FLOW, MAX_CATALOG_ROWS, SOLAR_FLOWS, SOLAR_FLOW_MATURITY_NOTES, SOLAR_FLOW_UNAVAILABLE_REASONS,
  solarFlowId, solarFlowOptionLabel, solarFlowOptions, solarFlowSelect,
} from './solarFlowModel.js'

const W1 = [
  ['solar-settings', 10], ['solar-size-strings', 20], ['solar-panel-groups', 30],
  ['solar-solve-proposal', 40], ['solar-commit-solve', 50], ['solar-correct-string', 60],
  ['solar-assign-equipment', 70], ['solar-homeruns', 80], ['solar-schedule', 90],
]
const READY = Object.freeze({ entitled: true, implemented: true, engine_ready: true, input_ready: true, refusal_reasons: [] })

it('FL14 closed params admit only their own selection overlay keys without mutation', () => {
  const overlays = { target_handle: 'AB', handle: 'AB' }
  const params = conversionDeclaration.record.params
  const before = structuredClone({ params, overlays })
  expect(admittedOverlays(params, overlays)).toEqual({})
  expect(admittedOverlays({ additionalProperties: false, properties: { handle: { type: 'string' } } }, overlays))
    .toEqual({ handle: 'AB' })
  const inherited = Object.assign(Object.create({ handle: {} }), { target_handle: {} })
  expect(admittedOverlays({ additionalProperties: false, properties: inherited }, overlays)).toEqual({})
  const nullProperties = Object.assign(Object.create(null), { handle: {} })
  expect(admittedOverlays({ additionalProperties: false, properties: nullProperties }, overlays)).toEqual({ handle: 'AB' })
  expect({ params, overlays }).toEqual(before)
})

it('FL15 open params keep overlay identity and unusable closed schemas admit nothing', () => {
  const overlays = { target_handle: 'AB', handle: 'AB' }
  for (const params of [{ properties: {} }, { additionalProperties: true }, { additionalProperties: 'false' }]) {
    expect(admittedOverlays(params, overlays)).toBe(overlays)
    expect(admittedOverlays(params, null)).toBeNull()
  }
  for (const params of [null, [], { additionalProperties: false, properties: null },
    { additionalProperties: false, properties: [] }]) {
    expect(admittedOverlays(params, overlays)).toEqual({})
  }
  for (const value of [null, undefined, [], 'AB']) {
    expect(admittedOverlays({ additionalProperties: false, properties: { handle: {} } }, value)).toEqual({})
  }
})

function blocked(...codes) {
  return { entitled: true, implemented: true, engine_ready: true, input_ready: false, refusal_reasons: codes }
}

function row(name, order, { wave = 1, availability = READY, solar = true } = {}) {
  return {
    name,
    label: name,
    availability,
    params: { type: 'object', properties: {} },
    ...(solar ? {
      solar: {
        schema: 'leaf.solar-tool-view.v1', name, family: 'stringing', wave, order,
        entitlement: 'run_write', interaction: { mode: 'form' },
      },
    } : {}),
  }
}

function steps(readyNames, blockedAvailability = blocked('valid_settings_required')) {
  return W1.map(([name, order]) => row(name, order, { availability: readyNames.includes(name) ? READY : blockedAvailability }))
}


const LIVE_TABLE = [
  ['stringing', [
    ['solar-settings', 1, 10, 'run_write'], ['solar-size-strings', 1, 20, 'run_write'],
    ['solar-panel-groups', 1, 30, 'run_write'], ['solar-commit-solve', 1, 50, 'run_write'],
    ['solar-correct-string', 1, 60, 'run_write'], ['solar-assign-equipment', 1, 70, 'run_write'],
    ['solar-homeruns', 1, 80, 'run_write'], ['solar-schedule', 1, 90, 'run_write'],
    ['solar-string-add', 2, 70, 'run_write'], ['solar-string-flip', 2, 72, 'run_write'],
    ['solar-string-swap', 2, 74, 'run_write'], ['solar-string-rebuild', 2, 76, 'run_read'],
    ['solar-string-data', 2, 78, 'run_read'], ['solar-string-delete', 2, 80, 'run_write'],
    ['solar-string-conductors', 2, 85, 'run_write'], ['solar-solve-proposal', 1, 40, 'solve'],
  ]],
  ['placement', [
    ['solar-panels-from-drawing', 1, 15, 'run_write'], ['solar-select-by-zone', 2, 10, 'run_read'],
    ['solar-electrical-zones', 2, 20, 'run_write'], ['solar-panel-add', 2, 30, 'run_write'],
    ['solar-panel-remove', 2, 40, 'run_write'], ['solar-panel-group-delete', 2, 50, 'run_write'],
    ['solar-autofill-plan', 2, 58, 'run_read'], ['solar-autofill', 2, 60, 'run_write'],
  ]],
  ['settings', [['solar-unit-sync', 2, 10, 'run_write']]],
  ['equipment', [
    ['solar-nec-ampacity-correction', 3, 10, 'run_read'], ['solar-nec-ac-voltage-drop', 3, 20, 'run_read'],
    ['solar-nec-conduit-fill', 3, 30, 'run_read'], ['solar-nec-feeder-ocpd', 3, 40, 'run_read'],
  ]],
]
function liveRow(name, family, wave, order, entitlement) {
  return {
    name, availability: READY, params: { type: 'object', properties: {} },
    solar: { schema: 'leaf.solar-tool-view.v1', name, family, wave, order, entitlement, interaction: { mode: 'form' } },
  }
}
const LIVE = LIVE_TABLE.map(([family_id, rows]) => ({
  family_id, capabilities: rows.map(([name, wave, order, entitlement]) => liveRow(name, family_id, wave, order, entitlement)),
}))
const ROOFTOP_LIVE = [
  'solar-settings', 'solar-panels-from-drawing', 'solar-size-strings', 'solar-panel-groups',
  'solar-solve-proposal', 'solar-commit-solve', 'solar-correct-string', 'solar-assign-equipment',
  'solar-string-conductors', 'solar-homeruns', 'solar-schedule',
]

function frozenFlow(id, stages) {
  return Object.freeze({ id, label: 'Probe flow', maturity: 'preview', stages: Object.freeze(stages.map(([id, label, capabilities]) =>
    Object.freeze({ id, label, capabilities: Object.freeze(capabilities) }))) })
}
const PROBE = Object.freeze([SOLAR_FLOWS[0], frozenFlow('probe', [
  ['a', 'Stage A', ['solar-x', 'solar-y']], ['b', 'Stage B', ['solar-z']],
])])
const probeRow = (name) => liveRow(name, 'equipment', 3, 10, 'run_read')
const OPTION_LABELS = [
  'Rooftop', 'Ground Mount Electrical (unavailable)', 'Ground Mount Physical (preview, unavailable)',
  'SolarEdge PDF Import (preview, unavailable)', 'PVcase Parity (tutorial, unavailable)',
]

const GROUND_ELECTRICAL_STAGES = [
  ['conversion', ['solar-trackers-to-panel-groups']],
  ['stringing', [
    'solar-settings', 'solar-size-strings', 'solar-string-add', 'solar-string-multi-add',
    'solar-string-midpoint', 'solar-string-flip', 'solar-string-swap', 'solar-string-delete',
    'solar-string-rebuild', 'solar-correct-string',
  ]],
  ['equipment', ['solar-assign-equipment', 'solar-string-conductors', 'solar-central-inverter-add']],
  ['feeders', ['solar-feeders']],
  ['calculations', [
    'solar-nec-ampacity-correction', 'solar-nec-ac-voltage-drop',
    'solar-nec-conduit-fill', 'solar-nec-feeder-ocpd',
  ]],
  ['outputs', ['solar-string-data', 'solar-electrical-schedules', 'solar-cable-export']],
]
const GROUND_ELECTRICAL_NAMES = GROUND_ELECTRICAL_STAGES.flatMap(([, names]) => names)
// The two tools Rooftop never lists carry their declaration waves, so one catalog proves both flows.
const GROUND_ONLY_WAVES = { 'solar-central-inverter-add': 2, 'solar-feeders': 3 }
const GROUND_ELECTRICAL_CATALOG = [{
  family_id: 'stringing',
  capabilities: GROUND_ELECTRICAL_NAMES.map((name, index) => row(name, index + 1, { wave: GROUND_ONLY_WAVES[name] ?? 1 })),
}]
function groundWithout(...omitted) {
  return [{
    ...GROUND_ELECTRICAL_CATALOG[0],
    capabilities: GROUND_ELECTRICAL_CATALOG[0].capabilities.filter((capability) => !omitted.includes(capability.name)),
  }]
}
const GROUND_WITHOUT_FEEDERS = groundWithout('solar-feeders')
function groundWithInvalidEquipment(...omitted) {
  return [{
    ...GROUND_ELECTRICAL_CATALOG[0],
    capabilities: groundWithout(...omitted)[0].capabilities
      .map((capability) => capability.name === 'solar-assign-equipment'
        ? { ...capability, solar: { ...capability.solar, wave: 9 } } : capability),
  }]
}
const GROUND_LIVE = [
  'solar-trackers-to-panel-groups',
  'solar-settings', 'solar-size-strings', 'solar-string-add', 'solar-string-flip', 'solar-string-swap',
  'solar-string-delete', 'solar-string-rebuild', 'solar-correct-string',
  'solar-assign-equipment', 'solar-string-conductors', 'solar-feeders',
  'solar-nec-ampacity-correction', 'solar-nec-ac-voltage-drop', 'solar-nec-conduit-fill', 'solar-nec-feeder-ocpd',
  'solar-string-data',
]

const F2_A = liveRow('solar-solaredge-accept', 'imports', 4, 30, 'run_write')
const F2_R = liveRow('solar-solaredge-tracking-read', 'imports', 4, 31, 'run_read')
const F2_SE = [{ family_id: 'imports', capabilities: [F2_A, F2_R] }]
const F2_HSE = { 'solaredge-import': ['solaredge-import'] }
const F2_HGP = { 'ground-physical': ['landxml', 'terrain', 'tracker-rows'] }
const F2_S = ['Upload the SolarEdge PDF', 'Inspect counts and matching', 'Accept tracking', 'Review accepted tracking']
const F2_G = ['Terrain', 'Native frame layout', 'Grade pads and native piles', 'Terrain and frame shade', 'Terrain and shade CSV']
const F2_P = ['Geometry conversion', 'Solve on the shared model', 'Exports']
const GP5_ROWS = [probeRow('solar-physical-shade'), probeRow('solar-physical-export')]
const GP5_CATALOG = [{ capabilities: GP5_ROWS }]
const gp5Select = (catalog = GP5_CATALOG, host = FLOW_PANELS) => solarFlowSelect(catalog, 'ground-physical', undefined, host)

it('GP5-02 no host admits none of the Ground Physical stages', () => {
  f2Refused(gp5Select(GP5_CATALOG, {}), F2_G)
})

it('GP5-03 the old host supplies only Terrain', () => {
  f2Refused(gp5Select(GP5_CATALOG, F2_HGP), F2_G.slice(1))
})

it('GP5-04 full host without tools leaves both physical reads missing', () => {
  f2Refused(gp5Select([]), F2_G.slice(3))
})

it('GP5-05 every physical catalog binding must be present and valid', () => {
  for (const index of [0, 1]) {
    f2Refused(gp5Select([{ capabilities: GP5_ROWS.filter((_, position) => position !== index) }]), [F2_G[index + 3]])
    const invalid = GP5_ROWS.map((entry, position) => position === index ? { ...entry, solar: null } : entry)
    f2Refused(gp5Select([{ capabilities: invalid }]), [F2_G[index + 3]])
  }
})

it('GP5-06 panel claims must belong to the registered flow and host', () => {
  for (const [panel, missing] of [['civil', F2_G.slice(1, 3)], ['physical-read', F2_G.slice(3)]]) {
    const panels = FLOW_PANELS['ground-physical'].filter((name) => name !== panel)
    f2Refused(gp5Select(GP5_CATALOG, { 'ground-physical': panels, rooftop: [panel] }), missing)
  }
  const stage = { id: 'a', label: 'Unknown panel', kind: 'workspace-panel-catalog', panels: ['unregistered'], capabilities: [GP5_ROWS[0].name] }
  f2Refused(f2Isolated(stage, { 'ground-physical': ['unregistered'] }, 'ground-physical', GP5_CATALOG), ['Unknown panel'])
})

it('GP5-07 complete admission retains two original rows in binding order', () => {
  const selected = gp5Select([{ capabilities: [{ ...GP5_ROWS[0], solar: null }, GP5_ROWS[1], ...GP5_ROWS, { ...GP5_ROWS[0] }] }])
  expect(selected).toMatchObject({ available: true, missing: [], reason: null })
  expect(selected.steps).toHaveLength(2)
  selected.steps.forEach((entry, index) => expect(entry).toBe(GP5_ROWS[index]))
  expect(solarFlowOptionLabel(solarFlowOptions(GP5_CATALOG, undefined, FLOW_PANELS)[2])).toBe('Ground Mount Physical (preview)')
})

it('GP5-08 blocked execution still satisfies catalog admission', () => {
  const rows = GP5_ROWS.map((entry) => ({ ...entry, availability: blocked('not_current_head') }))
  const selected = gp5Select([{ capabilities: rows }])
  expect(selected.available).toBe(true)
  expect(solarFlowState({ steps: selected.steps }).items.map((item) => item.status)).toEqual(['blocked', 'blocked'])
})

it('GP5-09 legacy panels ignore capabilities while combined stages require valid lists', () => {
  expect(f2Isolated(f2Panel({ capabilities: ['missing'] }), F2_HSE, 'solaredge-import', []).steps).toEqual([])
  expect(f2Isolated(f2Panel({ capabilities: ['missing'] }), F2_HSE, 'solaredge-import', []).available).toBe(true)
  f2Refused(f2Isolated(f2Panel({ kind: 'unknown', capabilities: [F2_A.name] })), ['Panel stage'])
  for (const capabilities of [undefined, [], null, 'solar-physical-shade', [''], [null], Array(1), [GP5_ROWS[0].name, 'missing']]) {
    f2Refused(f2Isolated({ ...f2Panel(), kind: 'workspace-panel-catalog', capabilities }), ['Panel stage'])
  }
  for (const panels of [undefined, [], null, 'solaredge-import', [''], [null], Array(1)]) {
    f2Refused(f2Isolated({ ...f2Panel(), kind: 'workspace-panel-catalog', panels, capabilities: [F2_A.name] }), ['Panel stage'])
  }
  const stage = { ...f2Panel(), kind: 'workspace-panel-catalog', capabilities: [F2_A.name, F2_R.name] }
  const admitted = f2Isolated(stage)
  expect(admitted).toMatchObject({ available: true, missing: [] })
  expect(admitted.steps).toHaveLength(2)
  expect(admitted.steps[0]).toBe(F2_A)
  expect(admitted.steps[1]).toBe(F2_R)
  f2Refused(f2Isolated(stage, F2_HSE, 'solaredge-import', [{ capabilities: Array(4096).fill(null) }, ...F2_SE]), ['Panel stage'])
})

it('GP5-22 a combined stage requires every capability rather than any one valid row', () => {
  const stage = { ...f2Panel(), kind: 'workspace-panel-catalog', capabilities: [F2_A.name, F2_R.name] }
  for (const valid of [F2_A, F2_R]) {
    f2Refused(f2Isolated(stage, F2_HSE, 'solaredge-import', [{ capabilities: [valid] }]), ['Panel stage'])
  }
  const admitted = f2Isolated(stage, F2_HSE, 'solaredge-import', [{ capabilities: [F2_R, F2_A] }])
  expect(admitted).toMatchObject({ available: true, missing: [] })
  expect(admitted.steps).toHaveLength(2)
  expect(admitted.steps[0]).toBe(F2_A)
  expect(admitted.steps[1]).toBe(F2_R)
})

it('GP5-18 other flow contracts remain the same for a given host', () => {
  expect(solarFlowSelect(LIVE, 'rooftop', undefined, FLOW_PANELS).steps.map((entry) => entry.name)).toEqual(ROOFTOP_LIVE)
  expect(solarFlowSelect(F2_SE, 'solaredge-import', undefined, FLOW_PANELS).steps).toEqual([F2_A, F2_R])
  expect(solarFlowSelect(GROUND_ELECTRICAL_CATALOG, 'ground-electrical', undefined, FLOW_PANELS).steps.map((entry) => entry.name)).toEqual(GROUND_ELECTRICAL_NAMES)
  f2Refused(solarFlowSelect([...GP5_CATALOG, ...F2_SE, ...GROUND_ELECTRICAL_CATALOG], 'pvcase-tutorial', undefined, FLOW_PANELS), F2_P)
})

it('GP5-19 Ground Physical bindings name their producer-chain evidence', () => {
  const hasEvidence = (file) => {
    const source = readFileSync(resolve(process.cwd(), '..', 'server', 'tests', file), 'utf8')
    const names = SOLAR_FLOWS[2].stages.flatMap((stage) => stage.capabilities)
    return source.includes('def test_ground_physical_admission_producer_chain')
      && ['solar-physical-shade', 'solar-physical-export'].every((name) => source.includes(name))
      && names.length === 2 && names.every((name) => ['solar-physical-shade', 'solar-physical-export'].includes(name))
  }
  expect(hasEvidence('test_solar_ground_physical_admission.py')).toBe(true)
  expect(() => hasEvidence('gp5_producer_evidence_file_does_not_exist.py')).toThrow()
})
const f2Select = (catalog = F2_SE, host = F2_HSE) => solarFlowSelect(catalog, 'solaredge-import', undefined, host)
function f2Panel(overrides = {}) {
  return { id: 'panel', label: 'Panel stage', kind: 'workspace-panel', panels: ['solaredge-import'], capabilities: [], ...overrides }
}
function f2Isolated(stage, host = F2_HSE, id = 'solaredge-import', catalog = F2_SE) {
  return solarFlowSelect(catalog, id, [{ id, label: 'Isolated flow', maturity: 'preview', stages: [stage] }], host)
}
function f2Refused(selection, missing) {
  expect(selection).toMatchObject({ available: false, steps: [], missing, reasonKey: 'stages_missing',
    reason: 'Some stages in this flow are not available yet.' })
}

describe('F2 workspace panel admission', () => {
  it('F2 01 frozen registry agrees with the untouched container', () => {
    expect(FLOW_PANELS).toEqual({
      rooftop: ['combiner-intake'], 'ground-electrical': [],
      'ground-physical': ['landxml', 'terrain', 'tracker-rows', 'civil', 'physical-read'],
      'solaredge-import': ['solaredge-import'], 'pvcase-tutorial': [],
    })
    expect(Object.isFrozen(FLOW_PANELS)).toBe(true)
    for (const panels of Object.values(FLOW_PANELS)) expect(Object.isFrozen(panels)).toBe(true)
    const source = readFileSync(resolve(process.cwd(), 'src/solar/SolarWorkspaceTools.jsx'), 'utf8')
    const executable = source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '')
    expect(executable).toMatch(new RegExp('import\\s+\\{\\s*FLOW_PANELS\\s*\\}\\s+from\\s+[\\x22\\x27]\\./solarWorkspacePanels\\.js[\\x22\\x27]'))
    expect(executable).toMatch(/FLOW_PANELS\[flow\]\?\.includes/)
    expect(executable).not.toMatch(/(?:const|let|var)\s+FLOW_PANELS\s*=/)
    expect(executable).not.toMatch(new RegExp('(?:rooftop|ground-electrical|ground-physical|solaredge-import|pvcase-tutorial)[\\x22\\x27]?\\s*:\\s*(?:Object\\.freeze\\s*\\(\\s*)?\\['))
  })

  it('F2 02 shipped stages and copy match the frozen contract', () => {
    const se = SOLAR_FLOWS[3]
    expect(se).toMatchObject({ id: 'solaredge-import', label: 'SolarEdge PDF Import', maturity: 'preview' })
    expect(se.stages).toEqual([
      { id: 'upload', label: F2_S[0], kind: 'workspace-panel', panels: ['solaredge-import'], capabilities: [] },
      { id: 'inspect', label: F2_S[1], kind: 'workspace-panel', panels: ['solaredge-import'], capabilities: [] },
      { id: 'tracking', label: F2_S[2], capabilities: [F2_A.name] },
      { id: 'review', label: F2_S[3], capabilities: [F2_R.name] },
    ])
    expect(SOLAR_FLOWS[2].stages).toEqual([
      { id: 'terrain', label: F2_G[0], kind: 'workspace-panel', panels: ['landxml', 'terrain'], capabilities: [] },
      { id: 'layout', label: F2_G[1], kind: 'workspace-panel', panels: ['civil'], capabilities: [] },
      { id: 'civil', label: F2_G[2], kind: 'workspace-panel', panels: ['civil'], capabilities: [] },
      { id: 'analysis', label: F2_G[3], kind: 'workspace-panel-catalog', panels: ['physical-read'], capabilities: ['solar-physical-shade'] },
      { id: 'outputs', label: F2_G[4], kind: 'workspace-panel-catalog', panels: ['physical-read'], capabilities: ['solar-physical-export'] },
    ])
    expect(SOLAR_FLOWS[0].stages).toBeNull()
    expect(SOLAR_FLOWS[1].stages.map(({ id, capabilities }) => [id, capabilities])).toEqual(GROUND_ELECTRICAL_STAGES)
    expect(SOLAR_FLOWS[4].stages).toEqual([
      { id: 'conversion', label: F2_P[0], capabilities: [] },
      { id: 'solve', label: F2_P[1], capabilities: [] },
      { id: 'outputs', label: F2_P[2], capabilities: [] },
    ])
    for (const flow of SOLAR_FLOWS) {
      expect(Object.isFrozen(flow)).toBe(true)
      if (flow.stages === null) continue
      expect(Object.isFrozen(flow.stages)).toBe(true)
      for (const stage of flow.stages) {
        expect(Object.isFrozen(stage)).toBe(true)
        expect(Object.isFrozen(stage.capabilities)).toBe(true)
        if (stage.panels) expect(Object.isFrozen(stage.panels)).toBe(true)
      }
    }
    expect(SOLAR_FLOW_UNAVAILABLE_REASONS.stages_missing).toBe('Some stages in this flow are not available yet.')
    expect(SOLAR_FLOW_UNAVAILABLE_REASONS.rooftop_steps_missing).toBe('This catalog offers no Rooftop steps for this drawing yet.')
    expect(SOLAR_FLOW_MATURITY_NOTES.preview).toBe('Preview flow: its results are not production Solar design yet.')
    expect(SOLAR_FLOW_MATURITY_NOTES.tutorial).toBe('Tutorial flow: conversion and solve are not available in this workspace yet.')
  })

  it('F2 03 SolarEdge admits two real catalog rows', () => {
    const selected = f2Select()
    expect(selected).toEqual({ flow: 'solaredge-import', label: 'SolarEdge PDF Import', maturity: 'preview',
      available: true, missing: [], reason: null, reasonKey: null, steps: [F2_A, F2_R] })
    expect(selected.steps).toHaveLength(2)
  })

  it('F2 04 no host map admits no SolarEdge panels', () => {
    f2Refused(solarFlowSelect(F2_SE, 'solaredge-import'), F2_S.slice(0, 2))
    f2Refused(f2Select(F2_SE, {}), F2_S.slice(0, 2))
  })

  it('F2 05 invalid or wrong-flow host entries refuse panels', () => {
    for (const host of [{}, { 'solaredge-import': [] }, { 'solaredge-import': 'solaredge-import' },
      { 'solaredge-import': {} }, { 'solaredge-import': null }, { 'ground-physical': ['solaredge-import'] }]) {
      f2Refused(f2Select(F2_SE, host), F2_S.slice(0, 2))
    }
  })

  it('F2 06 host claims cannot admit unregistered panels', () => {
    f2Refused(f2Isolated(f2Panel({ panels: ['not-registered'] }), { 'solaredge-import': ['not-registered'] }), ['Panel stage'])
  })

  it('F2 07 panel stages require a nonempty valid panel list', () => {
    for (const panels of [undefined, [], 'solaredge-import', null, [1], [''], Array(1), ['solaredge-import', null]]) {
      f2Refused(f2Isolated(f2Panel({ panels })), ['Panel stage'])
    }
    const omitted = f2Panel()
    delete omitted.panels
    f2Refused(f2Isolated(omitted), ['Panel stage'])
  })

  it('F2 08 unknown kinds never fall back to catalog admission', () => {
    for (const kind of ['unknown', 'catalog', null, '']) {
      f2Refused(f2Isolated(f2Panel({ kind, capabilities: [F2_A.name] })), ['Panel stage'])
    }
    expect(f2Isolated({ id: 'catalog', label: 'Catalog', kind: undefined, capabilities: [F2_A.name] }).steps[0]).toBe(F2_A)
  })

  it('F2 09 every required panel must belong to both lists', () => {
    const terrain = f2Panel({ panels: ['landxml', 'terrain'] })
    for (const host of [{ 'ground-physical': ['landxml'] }, { 'ground-physical': ['terrain'] },
      { 'ground-physical': ['landxml'], 'solaredge-import': ['terrain'] }]) {
      f2Refused(f2Isolated(terrain, host, 'ground-physical'), ['Panel stage'])
    }
    expect(f2Isolated(terrain, F2_HGP, 'ground-physical')).toMatchObject({ available: true, steps: [], missing: [], reason: null, reasonKey: null })
    f2Refused(f2Isolated({ ...terrain, panels: [...terrain.panels, 'not-registered'] },
      { 'ground-physical': [...F2_HGP['ground-physical'], 'not-registered'] }, 'ground-physical'), ['Panel stage'])
  })

  it('F2 10 missing accept leaves tracking missing', () => {
    f2Refused(f2Select([{ capabilities: [F2_R] }]), ['Accept tracking'])
  })

  it('F2 11 invalid tracking read leaves review missing', () => {
    f2Refused(f2Select([{ capabilities: [F2_A, { ...F2_R, solar: { ...F2_R.solar, wave: 9 } }] }]), ['Review accepted tracking'])
  })

  it('F2 12 panels do not replace absent catalog bindings', () => {
    f2Refused(f2Select([]), F2_S.slice(2))
  })

  it('F2 13 admission does not claim execution readiness', () => {
    const catalog = [{ capabilities: [F2_A, F2_R].map((entry) => ({ ...entry, availability: blocked('not_current_head') })) }]
    const selected = f2Select(catalog)
    expect(selected.available).toBe(true)
    expect(selected.steps).toEqual(catalog[0].capabilities)
    const state = solarFlowState({ steps: selected.steps })
    expect(state.items.map((item) => item.status)).toEqual(['blocked', 'blocked'])
    expect(state.items.every((item) => !item.enabled)).toBe(true)
  })

  it('F2 14 catalog identity and binding order survive panels', () => {
    const catalog = [{ capabilities: [{ ...F2_A, solar: null }, F2_R, F2_A, { ...F2_A }, { ...F2_R }] }]
    const selected = f2Select(catalog)
    expect(selected.steps).toEqual([F2_A, F2_R])
    expect(selected.steps[0]).toBe(F2_A)
    expect(selected.steps[1]).toBe(F2_R)
    expect(selected.steps).toHaveLength(2)
  })

  it('F2 15 a satisfied panel-only flow has no synthetic steps', () => {
    const selected = f2Isolated(f2Panel({ capabilities: [F2_A.name] }), F2_HSE, 'solaredge-import', [])
    expect(selected).toMatchObject({ available: true, steps: [], missing: [], reason: null, reasonKey: null })
    expect(f2Isolated(f2Panel({ capabilities: [F2_A.name] }))).toMatchObject({ available: true, steps: [], missing: [], reason: null, reasonKey: null })
  })

  it('F2 16 options use the supplied host map', () => {
    const withHost = solarFlowOptions(F2_SE, undefined, F2_HSE)[3]
    const withoutHost = solarFlowOptions(F2_SE)[3]
    expect(solarFlowOptionLabel(withHost)).toBe('SolarEdge PDF Import (preview)')
    expect(solarFlowOptionLabel(withoutHost)).toBe('SolarEdge PDF Import (preview, unavailable)')
    expect(Object.keys(withHost)).toEqual(['id', 'label', 'maturity', 'available'])
    expect(withHost).toEqual({ id: 'solaredge-import', label: 'SolarEdge PDF Import', maturity: 'preview', available: true })
    expect(withoutHost).toEqual({ ...withHost, available: false })
  })

  it('F2 17 Ground Physical credits only terrain and layout', () => {
    f2Refused(solarFlowSelect(GROUND_ELECTRICAL_CATALOG, 'ground-physical'), F2_G)
    f2Refused(solarFlowSelect(GROUND_ELECTRICAL_CATALOG, 'ground-physical', undefined, F2_HGP), F2_G.slice(1))
  })

  it('F2 18 legacy catalog selection and bounds stay fixed', () => {
    const host = { ...F2_HSE, ...F2_HGP }
    expect(solarFlowSelect(GROUND_ELECTRICAL_CATALOG, 'ground-electrical', undefined, host).steps).toHaveLength(22)
    const rooftop = solarFlowSelect(LIVE, 'rooftop', undefined, host)
    expect(rooftop.steps).toHaveLength(11)
    expect(rooftop.steps.map((entry) => entry.name)).toEqual(ROOFTOP_LIVE)
    solarFlowSteps(LIVE).forEach((entry, index) => expect(rooftop.steps[index]).toBe(entry))
    const x = probeRow('solar-x'), y = probeRow('solar-y'), z = probeRow('solar-z')
    const selected = solarFlowSelect([{ capabilities: [{ ...x, solar: null }, z, x, { ...x }, y] }], 'probe', PROBE, host)
    expect(selected.steps).toEqual([x, y, z])
    expect(selected.steps[0]).toBe(x)
    expect(selected.steps[1]).toBe(y)
    expect(selected.steps[2]).toBe(z)
    for (const [count, missing] of [[4096, ['Stage A', 'Stage B']], [4095, ['Stage B']]]) {
      const filler = Array.from({ length: count }, () => null)
      f2Refused(solarFlowSelect([{ capabilities: filler }, { capabilities: [x, z] }], 'probe', PROBE, host), missing)
    }
    const names = Array.from({ length: 70 }, (_, index) => `solar-m${String(index).padStart(3, '0')}`)
    const many = [SOLAR_FLOWS[0], frozenFlow('many', [
      ['a', 'Stage A', names.slice(0, 32)], ['b', 'Stage B', names.slice(32, 64)], ['c', 'Stage C', names.slice(64)],
    ])]
    const capped = solarFlowSelect([{ capabilities: names.map(probeRow) }], 'many', many, host)
    expect(capped.available).toBe(true)
    expect(capped.steps).toHaveLength(64)
    expect(capped.steps[63].name).toBe('solar-m063')
  })

  it('F2 19 PVcase remains unavailable with any registered host map', () => {
    f2Refused(solarFlowSelect([...GROUND_ELECTRICAL_CATALOG, ...LIVE, ...F2_SE], 'pvcase-tutorial', undefined, FLOW_PANELS), F2_P)
    expect(solarFlowSelect(F2_SE, 'pvcase-tutorial', undefined, FLOW_PANELS).maturity).toBe('tutorial')
  })

  it('F2 SE1 SolarEdge bindings name their producer-chain evidence', () => {
    const source = readFileSync(resolve('../server/tests', 'test_solar_solaredge_flow.py'), 'utf8')
    expect(source.trim().length).toBeGreaterThan(0)
    expect(source).toMatch(new RegExp('^def test_solaredge_flow_accept_run\\(', 'm'))
    expect(source).toMatch(new RegExp('^def test_solaredge_flow_tracking_read_run\\(', 'm'))
    for (const name of [F2_A.name, F2_R.name]) {
      expect(source.includes(`"${name}"`) || source.includes(`'${name}'`)).toBe(true)
    }
    expect(SOLAR_FLOWS[3].stages.flatMap((stage) => stage.capabilities)).toEqual([F2_A.name, F2_R.name])
  })
})

describe('W20-03 Ground Electrical admission', () => {
  it('W20-03 GE8 every bound Ground tool has converted-chain evidence', () => {
    const evidence = [
      'test_solar_ground_admission.py', 'test_solar_tool_trackers_to_panel_groups.py',
      'test_solar_ground_equipment.py', 'test_solar_tool_central_inverter_add.py',
      'test_solar_tool_solar_feeders_ground.py',
    ].map((file) => readFileSync(resolve('../server/tests', file), 'utf8'))
    for (const text of evidence) expect(text.trim().length).toBeGreaterThan(0)
    const hasEvidence = (name) => {
      const nec = name.startsWith('solar-nec-')
      if (!name.startsWith('solar-')) return false
      const id = name.slice(nec ? 'solar-nec-'.length : 'solar-'.length).replace(/-/g, '_')
        .replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
      const tuple = nec
        ? new RegExp(String.raw`\(\s*["']${id}["']\s*,\s*["'][A-Z0-9]+_SHA["']`)
        : new RegExp(String.raw`\(\s*["']${id}["']\s*,\s*["'][0-9a-f]{64}["']`)
      return evidence.some((text) => text.includes(`"${name}"`) || text.includes(`'${name}'`) || tuple.test(text))
    }
    const names = SOLAR_FLOWS.find((flow) => flow.id === 'ground-electrical').stages
      .flatMap((stage) => stage.capabilities)
    for (const name of names) expect(hasEvidence(name), name).toBe(true)
    expect(hasEvidence('solar-not-admitted')).toBe(false)
    expect(hasEvidence('solar-settings-extra')).toBe(false)
  })

  it('W20-03 GE1 every bound capability has a matching server declaration', () => {
    const directory = resolve('../server/solar_tools')
    const declarations = readdirSync(directory).filter((file) => file.endsWith('.json'))
      .map((file) => JSON.parse(readFileSync(resolve(directory, file), 'utf8')))
    const names = SOLAR_FLOWS.find((flow) => flow.id === 'ground-electrical').stages
      .flatMap((stage) => stage.capabilities)
    for (const name of names) expect(declarations.some((declaration) => declaration.name === name)).toBe(true)
  })

  it('W20-03 GE2 stages bind exactly the admitted tools once in order', () => {
    const stages = SOLAR_FLOWS.find((flow) => flow.id === 'ground-electrical').stages
    expect(stages.map(({ id, capabilities }) => [id, capabilities])).toEqual(GROUND_ELECTRICAL_STAGES)
    const names = stages.flatMap((stage) => stage.capabilities)
    expect(new Set(names).size).toBe(names.length)
  })

  it('W20-03 GE3 a complete bound catalog opens the flow in stage order', () => {
    const selected = solarFlowSelect(GROUND_ELECTRICAL_CATALOG, 'ground-electrical')
    expect(selected).toMatchObject({ available: true, reasonKey: null, reason: null, missing: [] })
    expect(selected.steps.map((step) => step.name)).toEqual(GROUND_ELECTRICAL_NAMES)
    expect(selected.steps).toHaveLength(22)
  })

  it('W20-03 GE4 Feeders and routes needs a valid solar-feeders row', () => {
    expect(solarFlowSelect(GROUND_WITHOUT_FEEDERS, 'ground-electrical')).toMatchObject({
      available: false, steps: [], reasonKey: 'stages_missing', missing: ['Feeders and routes'],
    })
    const invalid = [{
      ...GROUND_WITHOUT_FEEDERS[0],
      capabilities: [...GROUND_WITHOUT_FEEDERS[0].capabilities, row('solar-feeders', 99, { wave: 9 })],
    }]
    expect(solarFlowSelect(invalid, 'ground-electrical')).toEqual(solarFlowSelect(GROUND_WITHOUT_FEEDERS, 'ground-electrical'))
  })

  it('W20-03 GE5 invalid assignment with conductors and the central inverter absent leaves Equipment missing', () => {
    expect(solarFlowSelect(groundWithInvalidEquipment('solar-string-conductors', 'solar-central-inverter-add'), 'ground-electrical'))
      .toMatchObject({ available: false, steps: [], reasonKey: 'stages_missing', missing: ['Equipment'] })
  })

  it('W20-03 GE6 valid conductors alone, or a valid central inverter alone, satisfy Equipment', () => {
    for (const [omitted, kept] of [
      ['solar-central-inverter-add', 'solar-string-conductors'], ['solar-string-conductors', 'solar-central-inverter-add'],
    ]) {
      const selected = solarFlowSelect(groundWithInvalidEquipment(omitted), 'ground-electrical')
      expect(selected).toMatchObject({ available: true, reasonKey: null, reason: null, missing: [] })
      const names = selected.steps.map((step) => step.name)
      expect(names).toEqual(GROUND_ELECTRICAL_NAMES.filter((name) => name !== omitted && name !== 'solar-assign-equipment'))
      expect(names).toContain(kept)
    }
  })

  it('W20-03 GE7 Rooftop retains selection and row identity on the same catalogs', () => {
    const rooftop = GROUND_ELECTRICAL_NAMES.filter((name) => !(name in GROUND_ONLY_WAVES))
    const withoutConductors = rooftop.filter((name) => name !== 'solar-string-conductors')
    const withoutAssignment = withoutConductors.filter((name) => name !== 'solar-assign-equipment')
    for (const [catalog, names] of [
      [GROUND_ELECTRICAL_CATALOG, [...withoutConductors, 'solar-string-conductors']],
      [GROUND_WITHOUT_FEEDERS, [...withoutConductors, 'solar-string-conductors']],
      [groundWithInvalidEquipment('solar-string-conductors', 'solar-central-inverter-add'), withoutAssignment],
      [groundWithInvalidEquipment('solar-central-inverter-add'), [...withoutAssignment, 'solar-string-conductors']],
      [LIVE, ROOFTOP_LIVE],
    ]) {
      const selected = solarFlowSelect(catalog, 'rooftop')
      expect(selected).toMatchObject({ available: true, reasonKey: null, reason: null, missing: [] })
      expect(selected.steps.map((step) => step.name)).toEqual(names)
      expect(selected.steps.map((step) => step.name)).not.toContain('solar-central-inverter-add')
      expect(selected.steps.map((step) => step.name)).not.toContain('solar-feeders')
      solarFlowSteps(catalog).forEach((step, index) => expect(selected.steps[index]).toBe(step))
    }
  })

  it('G2c GE9 the live catalog with conversion and feeders opens the flow and its option', () => {
    const complete = [...LIVE, { family_id: 'routing', capabilities: [
      liveRow('solar-trackers-to-panel-groups', 'stringing', 3, 5, 'run_write'),
      liveRow('solar-feeders', 'routing', 3, 60, 'run_write'),
    ] }]
    const selected = solarFlowSelect(complete, 'ground-electrical')
    expect(selected).toMatchObject({ available: true, reasonKey: null, reason: null, missing: [] })
    expect(selected.steps.map((step) => step.name)).toEqual(GROUND_LIVE)
    expect(solarFlowOptions(complete).map(solarFlowOptionLabel))
      .toEqual(['Rooftop', 'Ground Mount Electrical', ...OPTION_LABELS.slice(2)])
    expect(solarFlowSelect(complete, 'rooftop').steps.map((step) => step.name)).toEqual(ROOFTOP_LIVE)
    const noFeeders = [complete[0], ...complete.slice(1, -1), { ...complete.at(-1), capabilities: [complete.at(-1).capabilities[0]] }]
    expect(solarFlowSelect(noFeeders, 'ground-electrical')).toMatchObject({ available: false, missing: ['Feeders and routes'] })
  })
})

describe('Solar flow selection', () => {
  it('FL1 freezes the exact flow table and its bounds', () => {
    expect(DEFAULT_SOLAR_FLOW).toBe('rooftop')
    expect(SOLAR_FLOWS.map((flow) => [flow.id, flow.label, flow.maturity])).toEqual([
      ['rooftop', 'Rooftop', 'production'], ['ground-electrical', 'Ground Mount Electrical', 'production'],
      ['ground-physical', 'Ground Mount Physical', 'preview'], ['solaredge-import', 'SolarEdge PDF Import', 'preview'],
      ['pvcase-tutorial', 'PVcase Parity', 'tutorial'],
    ])
    expect(SOLAR_FLOWS[0].stages).toBeNull()
    expect(SOLAR_FLOWS.slice(1).map((flow) => flow.stages.map(({ id, label }) => [id, label]))).toEqual([
      [['conversion', 'Tracker conversion'], ['stringing', 'Sizing and stringing'], ['equipment', 'Equipment'],
        ['feeders', 'Feeders and routes'], ['calculations', 'NEC calculations'], ['outputs', 'Schedules and exports']],
      [['terrain', 'Terrain'], ['layout', 'Native frame layout'], ['civil', 'Grade pads and native piles'],
        ['analysis', 'Terrain and frame shade'], ['outputs', 'Terrain and shade CSV']],
      [['upload', 'Upload the SolarEdge PDF'], ['inspect', 'Inspect counts and matching'],
        ['tracking', 'Accept tracking'], ['review', 'Review accepted tracking']],
      [['conversion', 'Geometry conversion'], ['solve', 'Solve on the shared model'], ['outputs', 'Exports']],
    ])
    expect(Object.isFrozen(SOLAR_FLOWS)).toBe(true)
    expect(SOLAR_FLOWS.length).toBeLessThanOrEqual(8)
    expect(new Set(SOLAR_FLOWS.map((flow) => flow.id)).size).toBe(SOLAR_FLOWS.length)
    for (const flow of SOLAR_FLOWS) {
      expect(Object.isFrozen(flow)).toBe(true)
      expect(flow.id).toMatch(/^[a-z][a-z0-9-]{0,31}$/)
      expect(typeof flow.label).toBe('string')
      expect(flow.label.length).toBeGreaterThan(0)
      expect(flow.label.length).toBeLessThanOrEqual(64)
      expect(['production', 'preview', 'tutorial']).toContain(flow.maturity)
      if (flow.stages === null) continue
      expect(Object.isFrozen(flow.stages)).toBe(true)
      expect(flow.stages.length).toBeLessThanOrEqual(8)
      expect(new Set(flow.stages.map((stage) => stage.label)).size).toBe(flow.stages.length)
      const names = flow.stages.flatMap((stage) => stage.capabilities)
      expect(new Set(names).size).toBe(names.length)
      for (const stage of flow.stages) {
        expect(Object.isFrozen(stage)).toBe(true)
        expect(Object.isFrozen(stage.capabilities)).toBe(true)
        if (stage.kind === 'workspace-panel' || stage.kind === 'workspace-panel-catalog') {
          expect(Object.isFrozen(stage.panels)).toBe(true)
          expect(stage.panels.length).toBeGreaterThan(0)
          expect(stage.panels.length).toBeLessThanOrEqual(32)
          if (stage.kind === 'workspace-panel') expect(stage.capabilities).toEqual([])
          else expect(stage.capabilities.length).toBeGreaterThan(0)
        }
        expect(stage.id).toMatch(/^[a-z][a-z0-9-]{0,31}$/)
        expect(typeof stage.label).toBe('string')
        expect(stage.label.length).toBeGreaterThan(0)
        expect(stage.label.length).toBeLessThanOrEqual(64)
        expect(stage.capabilities.length).toBeLessThanOrEqual(32)
        for (const name of stage.capabilities) expect(name).toMatch(/^[a-z][a-z0-9-]{0,63}$/)
      }
    }
  })

  it('FL2 names the conversion tool and the four real NEC declarations in registry order', () => {
    const names = SOLAR_FLOWS.flatMap((flow) => (flow.stages ?? []).flatMap((stage) => stage.capabilities))
    // W20-03 pins the full admitted Ground Electrical list in stage order.
    expect(names).toEqual([
      'solar-trackers-to-panel-groups',
      'solar-settings', 'solar-size-strings', 'solar-string-add', 'solar-string-multi-add',
      'solar-string-midpoint', 'solar-string-flip', 'solar-string-swap', 'solar-string-delete',
      'solar-string-rebuild', 'solar-correct-string',
      'solar-assign-equipment', 'solar-string-conductors', 'solar-central-inverter-add',
      'solar-feeders',
      'solar-nec-ampacity-correction', 'solar-nec-ac-voltage-drop',
      'solar-nec-conduit-fill', 'solar-nec-feeder-ocpd',
      'solar-string-data', 'solar-electrical-schedules', 'solar-cable-export',
      'solar-physical-shade', 'solar-physical-export',
      'solar-solaredge-accept', 'solar-solaredge-tracking-read',
    ])
    expect(SOLAR_FLOWS[1].stages[0].capabilities).toEqual(['solar-trackers-to-panel-groups'])
    for (const [index, declaration] of [conversionDeclaration, ampacityDeclaration, voltageDeclaration, conduitDeclaration,
      ocpdDeclaration].entries()) {
      expect(declaration.name).toBe([names[0], ...names.slice(15, 19)][index])
      expect(declaration.wave).toBe(3)
    }
  })

  it('FL3 Rooftop keeps live ordering and catalog row identity', () => {
    const selected = solarFlowSelect(LIVE, 'rooftop')
    expect(selected).toMatchObject({ available: true, reasonKey: null, reason: null, missing: [] })
    expect(selected.steps.map((step) => step.name)).toEqual(ROOFTOP_LIVE)
    expect(selected.steps[8].name).toBe('solar-string-conductors')
    solarFlowSteps(LIVE).forEach((step, index) => expect(selected.steps[index]).toBe(step))
  })

  it('FL4 Ground Mount Electrical omits the present NEC stage from missing stages', () => {
    expect(solarFlowSelect(LIVE, 'ground-electrical')).toEqual({
      flow: 'ground-electrical', label: 'Ground Mount Electrical', maturity: 'production', available: false, steps: [],
      reasonKey: 'stages_missing', reason: SOLAR_FLOW_UNAVAILABLE_REASONS.stages_missing,
      // W20-03: LIVE satisfies stringing, equipment, calculations and outputs.
      missing: ['Tracker conversion', 'Feeders and routes'],
    })
  })

  it('FL4b a catalog carrying the conversion tool leaves Tracker conversion off the missing stages', () => {
    const withConversion = LIVE.map((family) => family.family_id !== 'stringing' ? family : {
      ...family,
      capabilities: [...family.capabilities, liveRow('solar-trackers-to-panel-groups', 'stringing', 3, 5, 'run_write')],
    })
    expect(solarFlowSelect(withConversion, 'ground-electrical')).toEqual({
      flow: 'ground-electrical', label: 'Ground Mount Electrical', maturity: 'production', available: false, steps: [],
      reasonKey: 'stages_missing', reason: SOLAR_FLOW_UNAVAILABLE_REASONS.stages_missing,
      // W20-03: conversion satisfies the only other missing stage in LIVE.
      missing: ['Feeders and routes'],
    })
    expect(solarFlowOptions(withConversion).map(solarFlowOptionLabel)).toEqual(OPTION_LABELS)
    const invalidRow = { ...liveRow('solar-trackers-to-panel-groups', 'stringing', 3, 5, 'run_write') }
    invalidRow.solar = { ...invalidRow.solar, wave: 9 }
    const withInvalid = LIVE.map((family) => family.family_id !== 'stringing' ? family
      : { ...family, capabilities: [...family.capabilities, invalidRow] })
    expect(solarFlowSelect(withInvalid, 'ground-electrical').missing[0]).toBe('Tracker conversion')
  })

  it('FL5 the other flows and live options explain availability and maturity', () => {
    for (const [id, missing] of [
      ['ground-physical', F2_G],
      ['solaredge-import', ['Upload the SolarEdge PDF', 'Inspect counts and matching', 'Accept tracking', 'Review accepted tracking']],
      ['pvcase-tutorial', ['Geometry conversion', 'Solve on the shared model', 'Exports']],
    ]) expect(solarFlowSelect(LIVE, id)).toMatchObject({ available: false, steps: [], reasonKey: 'stages_missing', missing })
    expect(solarFlowOptions(LIVE).map((option) => option.available)).toEqual([true, false, false, false, false])
    expect(solarFlowOptions(LIVE).map(solarFlowOptionLabel)).toEqual(OPTION_LABELS)
  })

  it('FL6 injected flows take table order and first valid row identity', () => {
    const x = probeRow('solar-x'), y1 = { ...probeRow('solar-y'), label: 'first' }
    const y2 = { ...probeRow('solar-y'), label: 'duplicate' }, z = probeRow('solar-z')
    const catalog = [{ capabilities: [z, y1, y2] }, { capabilities: [x, probeRow('solar-q')] }]
    const selected = solarFlowSelect(catalog, 'probe', PROBE)
    expect(selected).toMatchObject({ available: true, reasonKey: null, reason: null, missing: [] })
    expect(selected.steps.map((step) => step.name)).toEqual(['solar-x', 'solar-y', 'solar-z'])
    expect(selected.steps[0]).toBe(x)
    expect(selected.steps[1]).toBe(y1)
    expect(selected.steps[2]).toBe(z)
    expect(solarFlowOptions(catalog, PROBE)).toEqual([
      { id: 'rooftop', label: 'Rooftop', maturity: 'production', available: false },
      { id: 'probe', label: 'Probe flow', maturity: 'preview', available: true },
    ])
  })

  it('FL7 invalid absent and nameless rows never yield a partial rail', () => {
    const z = probeRow('solar-z')
    for (const bad of [{ ...z, solar: { ...z.solar, wave: 9 } }, { ...z, solar: null }, { ...z, name: null }]) {
      expect(solarFlowSelect([{ capabilities: [probeRow('solar-x'), bad] }], 'probe', PROBE))
        .toMatchObject({ available: false, steps: [], missing: ['Stage B'], reasonKey: 'stages_missing' })
    }
  })

  it('FL8 empty Rooftop catalogs and frozen reason maps are explicit', () => {
    for (const catalog of [[], null]) expect(solarFlowSelect(catalog, 'rooftop')).toEqual({
      flow: 'rooftop', label: 'Rooftop', maturity: 'production', available: false, steps: [],
      reasonKey: 'rooftop_steps_missing', reason: 'This catalog offers no Rooftop steps for this drawing yet.', missing: [],
    })
    expect(Object.isFrozen(SOLAR_FLOW_UNAVAILABLE_REASONS)).toBe(true)
    expect(Object.keys(SOLAR_FLOW_UNAVAILABLE_REASONS).sort()).toEqual(['rooftop_steps_missing', 'stages_missing'])
    expect(Object.isFrozen(SOLAR_FLOW_MATURITY_NOTES)).toBe(true)
    expect(Object.keys(SOLAR_FLOW_MATURITY_NOTES).sort()).toEqual(['preview', 'tutorial'])
  })

  it('FL9 bounds both returned steps and all catalog rows visited', () => {
    const names = Array.from({ length: 70 }, (_, index) => `solar-m${String(index).padStart(3, '0')}`)
    const many = Object.freeze([SOLAR_FLOWS[0], frozenFlow('many', [
      ['a', 'Stage A', names.slice(0, 32)], ['b', 'Stage B', names.slice(32, 64)], ['c', 'Stage C', names.slice(64)],
    ])])
    const selected = solarFlowSelect([{ capabilities: names.map(probeRow) }], 'many', many)
    expect(selected.available).toBe(true)
    expect(selected.steps).toHaveLength(64)
    expect(selected.steps[63].name).toBe('solar-m063')
    expect(MAX_CATALOG_ROWS).toBe(4096)
    for (const [count, missing] of [[4096, ['Stage A', 'Stage B']], [4095, ['Stage B']]]) {
      const filler = Array.from({ length: count }, (_, index) => ({ name: `plain-${index}` }))
      expect(solarFlowSelect([{ capabilities: filler }, { capabilities: [probeRow('solar-x'), probeRow('solar-z')] }], 'probe', PROBE))
        .toMatchObject({ available: false, steps: [], missing })
    }
  })

  it('FL10 flow ids reject unknown oversized and inherited names', () => {
    for (const flow of SOLAR_FLOWS) expect(solarFlowId(flow.id)).toBe(flow.id)
    for (const value of ['', 'Rooftop', null, undefined, 7, 'x'.repeat(65), '__proto__', 'constructor', 'probe']) {
      expect(solarFlowId(value)).toBe('rooftop')
    }
    expect(solarFlowId('probe', PROBE)).toBe('probe')
    const maxId = 'x'.repeat(32)
    const oversizedId = 'x'.repeat(33)
    const bounded = Object.freeze([
      SOLAR_FLOWS[0], frozenFlow(maxId, []), frozenFlow(oversizedId, []),
    ])
    expect(solarFlowId(maxId, bounded)).toBe(maxId)
    expect(solarFlowId(oversizedId, bounded)).toBe(DEFAULT_SOLAR_FLOW)
  })
})


describe('solarFlowSteps', () => {
  it('CF11 admits conductors before homeruns while excluding other wave-two rows', () => {
    const conductor = row('solar-string-conductors', 85, { wave: 2 })
    const result = solarFlowSteps([{ capabilities: [
      ...W1.map(([name, order]) => row(name, order)), conductor,
      row('solar-string-add', 86, { wave: 2 }),
      row('solar-string-conductors', 1, { wave: 1 }),
    ] }])
    expect(result.map((item) => item.name)).toEqual([
      ...W1.slice(0, 7).map(([name]) => name), 'solar-string-conductors',
      ...W1.slice(7).map(([name]) => name),
    ])
    expect(result).toHaveLength(10)
    expect(result[7]).toBe(conductor)
    expect(conductor.solar.order).toBe(85)
    expect(conductor.solar.wave).toBe(2)
  })

  it('M1 keeps the nine W1 rows in (order, name) order, first wins, bounded', () => {
    const shuffled = [...W1].reverse().map(([name, order]) => row(name, order))
    const families = [
      { id: 'stringing', capabilities: [shuffled[0], row('solar-wave-two', 5, { wave: 2 }), row('solar-plain', 1, { solar: false })] },
      { id: 'design', capabilities: [...shuffled.slice(1), { ...row('solar-settings', 99), label: 'duplicate' }] },
      null,
      { id: 'empty' },
    ]
    const result = solarFlowSteps(families)
    expect(result.map((step) => step.name)).toEqual(W1.map(([name]) => name))
    expect(result.find((step) => step.name === 'solar-settings').label).toBe('solar-settings')
    expect(solarFlowSteps(null)).toEqual([])
    expect(solarFlowSteps('families')).toEqual([])
  })

  it('M1 breaks an order tie by name and truncates at MAX_FLOW_STEPS', () => {
    expect(solarFlowSteps([{ capabilities: [row('solar-b', 10), row('solar-a', 10)] }]).map((step) => step.name))
      .toEqual(['solar-a', 'solar-b'])
    const many = Array.from({ length: MAX_FLOW_STEPS + 6 }, (_, index) => row(`solar-${String(index).padStart(3, '0')}`, index))
    const result = solarFlowSteps([{ capabilities: many }])
    expect(MAX_FLOW_STEPS).toBe(64)
    expect(result).toHaveLength(64)
    expect(result[63].name).toBe('solar-063')
  })
})

describe('solarFlowReasonCodes', () => {
  it('M2 unreadable availability is one code', () => {
    for (const availability of [null, undefined, [], 'ready', { refusal_reasons: 'x' }, { entitled: true }]) {
      expect(solarFlowReasonCodes(availability)).toEqual(['capability_availability_unavailable'])
    }
  })

  it('M2 keeps only well-formed codes, at most eight', () => {
    expect(solarFlowReasonCodes({ refusal_reasons: ['valid_settings_required', 'BAD code', 'x'.repeat(65)] }))
      .toEqual(['valid_settings_required'])
    expect(solarFlowReasonCodes({ refusal_reasons: ['a'.repeat(64), 7, null, '9lead', 'under_score'] }))
      .toEqual(['a'.repeat(64), 'under_score'])
    const many = Array.from({ length: 20 }, (_, index) => `code_${index}`)
    expect(solarFlowReasonCodes({ refusal_reasons: many })).toEqual(many.slice(0, 8))
  })
})

describe('solarFlowState', () => {
  it('M3 only settings ready resumes at settings and nothing skips', () => {
    const { items, resumeIndex } = solarFlowState({ steps: steps(['solar-settings']) })
    expect(resumeIndex).toBe(0)
    expect(items).toHaveLength(9)
    expect(items[0].status).toBe('ready')
    expect(items[1].status).toBe('blocked')
    expect(items[1].codes).toEqual(['valid_settings_required'])
    expect(items.some((item) => item.skip)).toBe(false)
  })

  it('M4 resumes at the last ready step and marks a ready step past a gap as a skip', () => {
    const ready = ['solar-settings', 'solar-size-strings', 'solar-panel-groups', 'solar-correct-string']
    const { items, resumeIndex } = solarFlowState({ steps: steps(ready) })
    expect(resumeIndex).toBe(5)
    expect(items[5].name).toBe('solar-correct-string')
    expect(items[5].skip).toBe(true)
    expect(items[2].skip).toBe(false)
    expect(items[3].ready).toBe(false)
  })

  it('M3 no ready step means no resume marker', () => {
    expect(solarFlowState({ steps: steps([]) }).resumeIndex).toBe(-1)
    expect(solarFlowState({}).items).toEqual([])
  })

  it('M5 a step ready before and blocked by the graph now needs a rerun', () => {
    const previousReady = solarFlowReadyMap(steps(W1.map(([name]) => name)))
    expect(previousReady['solar-assign-equipment']).toBe(true)
    const graph = solarFlowState({ steps: steps(['solar-settings'], blocked('valid_strings_required')), previousReady })
    expect(graph.items[6].name).toBe('solar-assign-equipment')
    expect(graph.items[6].invalidated).toBe(true)
    const plan = solarFlowState({ steps: steps(['solar-settings'], blocked('entitlement_required')), previousReady })
    expect(plan.items[6].invalidated).toBe(false)
    const never = solarFlowState({ steps: steps(['solar-settings'], blocked('valid_strings_required')), previousReady: {} })
    expect(never.items[6].invalidated).toBe(false)
    const mixed = solarFlowState({ steps: steps([], blocked('drawing_context_required', 'valid_strings_required')), previousReady })
    expect(mixed.items[6].invalidated).toBe(true)
    expect(graph.items[0].invalidated).toBe(false)
  })

  it('CORR1 a graphless drawing starts from an enabled Solar settings step when the opener accepts it', () => {
    const seed = { entitled: true, implemented: true, engine_ready: true, input_ready: false, refusal_reasons: ['graph_seed_required'] }
    const graphless = W1.map(([name, order]) => row(name, order, {
      availability: name === 'solar-settings' ? seed : blocked('valid_settings_required'),
    }))
    const opener = (name, availability) => name === 'solar-settings'
      && Array.isArray(availability?.refusal_reasons) && availability.refusal_reasons[0] === 'graph_seed_required'
    const open = solarFlowState({ steps: graphless, openSettingsForm: opener })
    expect(open.resumeIndex).toBe(0)
    expect(open.items[0].enabled).toBe(true)
    expect(open.items[0].status).toBe('ready')
    expect(open.items[0].codes).toEqual([])
    expect(open.items.slice(1).every((item) => !item.enabled && item.status === 'blocked')).toBe(true)
    const today = solarFlowState({ steps: graphless })
    expect(today.resumeIndex).toBe(-1)
    expect(today.items[0].enabled).toBe(false)
    expect(today.items[0].status).toBe('blocked')
    expect(today.items[0].codes).toEqual(['graph_seed_required'])
    const refused = solarFlowState({ steps: graphless, openSettingsForm: () => false })
    expect(refused.items[0].enabled).toBe(false)
    const other = solarFlowState({ steps: graphless, openSettingsForm: () => true })
    expect(other.items[1].enabled).toBe(false)
  })

  it('CORR1 a transient or context refusal on a previously ready step is not an invalidation', () => {
    const previousReady = solarFlowReadyMap(steps(W1.map(([name]) => name)))
    for (const code of ['not_current_head', 'invalid_drawing_context', 'entitlement_policy_unavailable', 'persisted_graph_unavailable']) {
      const state = solarFlowState({ steps: steps(['solar-settings'], blocked(code)), previousReady })
      expect(state.items[6].codes).toEqual([code])
      expect(state.items[6].invalidated).toBe(false)
    }
  })

  it('M6 pending beats failed beats ready beats blocked', () => {
    const base = steps(['solar-settings', 'solar-size-strings'])
    const pending = solarFlowState({
      steps: base, pendingTool: 'solar-size-strings', runs: { 'solar-size-strings': { ok: false } },
    })
    expect(pending.items[1].status).toBe('pending')
    const failed = solarFlowState({ steps: base, runs: { 'solar-size-strings': { ok: false }, 'solar-panel-groups': { ok: false } } })
    expect(failed.items[1].status).toBe('failed')
    expect(failed.items[2].status).toBe('failed')
    const finished = solarFlowState({ steps: base, runs: { 'solar-size-strings': { ok: true } } })
    expect(finished.items[1].status).toBe('ready')
    expect(finished.items[2].status).toBe('blocked')
  })
})

describe('solarFlowPrefill', () => {
  const withRev = { params: { properties: { expected_rev: { type: 'integer', default: 1 } } } }

  it('M7 prefills an integer expected_rev from a real revision only', () => {
    expect(solarFlowPrefill(withRev, 7)).toEqual({ expected_rev: 7 })
    expect(solarFlowPrefill(withRev, 0)).toEqual({ expected_rev: 0 })
    for (const rev of [-1, 1.5, '7', null, undefined, Number.NaN]) expect(solarFlowPrefill(withRev, rev)).toEqual({})
    expect(solarFlowPrefill({ params: { properties: {} } }, 7)).toEqual({})
    expect(solarFlowPrefill({ params: { properties: { expected_rev: { type: 'string' } } } }, 7)).toEqual({})
    expect(solarFlowPrefill(null, 7)).toEqual({})
  })
})

describe('run memory', () => {
  it('G1A-5 preserves first well-formed own-property precedence', () => {
    const error = { reason_code: 'CLOUD_AUTH_MISSING', error_code: 'FORBIDDEN' }
    expect(solarFlowRunOutcome({ ok: false, reason_code: 'UNLISTED_REASON', error })).toEqual({ ok: false, code: 'UNLISTED_REASON' })
    expect(solarFlowRunOutcome({ ok: false, reason_code: 'bad code!', error })).toEqual({ ok: false, code: 'CLOUD_AUTH_MISSING' })
    expect(solarFlowRunOutcome({ ok: false, error: { ...error, reason_code: 'bad code!' } })).toEqual({ ok: false, code: 'FORBIDDEN' })
    expect(solarFlowRunOutcome({ ok: true, reason_code: 'TIMEOUT', error })).toEqual({ ok: true, code: null })
    for (const reason_code of ['A'.repeat(65), '9_LEAD', '', 7]) {
      expect(solarFlowRunOutcome({ error: { reason_code, error_code: 'FORBIDDEN' } })).toEqual({ ok: false, code: 'FORBIDDEN' })
    }
    expect(solarFlowRunOutcome({ error: { reason_code: 'A'.repeat(64) } })).toEqual({ ok: false, code: 'A'.repeat(64) })
    expect(solarFlowRunOutcome(Object.create({ reason_code: 'TIMEOUT' }))).toEqual({ ok: false, code: null })
    expect(solarFlowRunOutcome({ error: Object.create(error) })).toEqual({ ok: false, code: null })
    const array = []; array.reason_code = 'TIMEOUT'
    expect(solarFlowRunOutcome({ error: array })).toEqual({ ok: false, code: null })
    expect(solarFlowRunOutcome(Object.assign(Object.create(null), { error: Object.assign(Object.create(null), error) })))
      .toEqual({ ok: false, code: 'CLOUD_AUTH_MISSING' })
    expect(solarFlowRunOutcome({ error_code: 'TIMEOUT', message: 'TIMEOUT', classification: 'TIMEOUT', next_action: 'TIMEOUT', retryable: true }))
      .toEqual({ ok: false, code: null })
  })

  it('G1A-6 stores nested cloud refusals with drawing isolation and unchanged statuses', () => {
    const memory = solarFlowRecordRun(null, { tool: 'solar-size-strings', drawingId: 'd1' },
      { ok: false, error: { reason_code: 'CLOUD_AUTH_MISSING', error_code: 'FORBIDDEN' } })
    const runs = solarFlowRunsFor(memory, 'd1')
    expect(runs['solar-size-strings']).toEqual({ ok: false, code: 'CLOUD_AUTH_MISSING' })
    expect(solarFlowRunsFor(memory, 'd2')).toEqual({})
    expect(solarFlowRunStatus('solar-size-strings', null, runs)).toBe('failed')
    expect(solarFlowRunStatus('solar-size-strings', 'solar-size-strings', runs)).toBe('pending')
    const moved = solarFlowRecordRun(memory, { tool: 'solar-size-strings', drawingId: 'd2' }, { ok: true })
    expect(solarFlowRunsFor(moved, 'd1')).toEqual({})
    expect(solarFlowRunStatus('solar-size-strings', null, solarFlowRunsFor(moved, 'd2'))).toBe('finished')
  })

  it('G1A-12 an ok envelope or an earlier well-formed code reads no later nested value', () => {
    const trap = () => { throw new Error('a later nested value was read') }
    const nested = Object.defineProperty({ error_code: 'BAD_PARAMS' }, 'reason_code', { get: trap, enumerable: true })
    expect(solarFlowRunOutcome({ ok: true, reason_code: 'STALE_GRAPH_REVISION', error: nested })).toEqual({ ok: true, code: null })
    expect(solarFlowRunOutcome({ ok: false, reason_code: 'STALE_GRAPH_REVISION', error: nested }))
      .toEqual({ ok: false, code: 'STALE_GRAPH_REVISION' })
    const later = Object.defineProperty({ reason_code: 'CLOUD_AUTH_MISSING' }, 'error_code', { get: trap, enumerable: true })
    expect(solarFlowRunOutcome({ ok: false, error: later })).toEqual({ ok: false, code: 'CLOUD_AUTH_MISSING' })
    const okFirst = Object.defineProperty({ ok: true }, 'error', { get: trap, enumerable: true })
    expect(solarFlowRunOutcome(okFirst)).toEqual({ ok: true, code: null })
  })

  it('reduces an envelope to ok and the first well-formed code', () => {
    expect(solarFlowRunOutcome({ ok: true, reason_code: 'ignored' })).toEqual({ ok: true, code: null })
    expect(solarFlowRunOutcome({ ok: false, reason_code: 'invalid_seed_parent' })).toEqual({ ok: false, code: 'invalid_seed_parent' })
    expect(solarFlowRunOutcome({ ok: false, reason_code: 'Bad Code', error: { error_code: 'stale_rev' } }))
      .toEqual({ ok: false, code: 'stale_rev' })
    expect(solarFlowRunOutcome({ ok: false, error: { error_code: 'x'.repeat(65) } })).toEqual({ ok: false, code: null })
    expect(solarFlowRunOutcome(null)).toEqual({ ok: false, code: null })
  })

  it('CORR1 an uppercase server failure code survives and a malformed one is null', () => {
    expect(solarFlowRunOutcome({ ok: false, reason_code: 'INVALID_SEED_PARENT' })).toEqual({ ok: false, code: 'INVALID_SEED_PARENT' })
    expect(solarFlowRunOutcome({ ok: false, error: { error_code: 'STALE_GRAPH_REVISION' } }))
      .toEqual({ ok: false, code: 'STALE_GRAPH_REVISION' })
    expect(solarFlowRunOutcome({ ok: false, reason_code: 'A'.repeat(64) })).toEqual({ ok: false, code: 'A'.repeat(64) })
    expect(solarFlowRunOutcome({ ok: false, reason_code: 'A'.repeat(65) })).toEqual({ ok: false, code: null })
    expect(solarFlowRunOutcome({ ok: false, reason_code: 'bad-code' })).toEqual({ ok: false, code: null })
    expect(solarFlowRunOutcome({ ok: false, reason_code: '9_LEAD' })).toEqual({ ok: false, code: null })
  })

  it('records per drawing and hands back only the open drawing runs', () => {
    const first = solarFlowRecordRun(null, { tool: 'solar-homeruns', drawingId: 'd1' }, { ok: false, reason_code: 'stale_rev' })
    const second = solarFlowRecordRun(first, { tool: 'solar-schedule', drawingId: 'd1' }, { ok: true })
    expect(solarFlowRunsFor(second, 'd1')).toEqual({
      'solar-homeruns': { ok: false, code: 'stale_rev' }, 'solar-schedule': { ok: true, code: null },
    })
    expect(solarFlowRunsFor(second, 'd2')).toEqual({})
    const moved = solarFlowRecordRun(second, { tool: 'solar-schedule', drawingId: 'd2' }, { ok: true })
    expect(solarFlowRunsFor(moved, 'd2')).toEqual({ 'solar-schedule': { ok: true, code: null } })
    expect(solarFlowRunStatus('solar-homeruns', null, solarFlowRunsFor(second, 'd1'))).toBe('failed')
    expect(solarFlowRunStatus('solar-schedule', null, solarFlowRunsFor(second, 'd1'))).toBe('finished')
    expect(solarFlowRunStatus('solar-schedule', 'solar-schedule', {})).toBe('pending')
    expect(solarFlowRunStatus('constructor', null, {})).toBe(null)
  })

  it('step ids are DOM safe', () => {
    expect(solarFlowStepId('solar-settings')).toBe('solar-step-solar-settings')
    expect(solarFlowStepId('a b"c')).toBe('solar-step-a-b-c')
  })
})
