import { describe, expect, it } from 'vitest'
import ampacityDeclaration from '../../../server/solar_tools/solar_nec_ampacity_correction.json'
import voltageDeclaration from '../../../server/solar_tools/solar_nec_ac_voltage_drop.json'
import conduitDeclaration from '../../../server/solar_tools/solar_nec_conduit_fill.json'
import ocpdDeclaration from '../../../server/solar_tools/solar_nec_feeder_ocpd.json'
import {
  MAX_FLOW_STEPS, solarFlowPrefill, solarFlowReadyMap, solarFlowReasonCodes, solarFlowRecordRun,
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
  'SolarEdge PDF Import (unavailable)', 'PVcase Parity (tutorial, unavailable)',
]

describe('Solar flow selection', () => {
  it('FL1 freezes the exact flow table and its bounds', () => {
    expect(DEFAULT_SOLAR_FLOW).toBe('rooftop')
    expect(SOLAR_FLOWS.map((flow) => [flow.id, flow.label, flow.maturity])).toEqual([
      ['rooftop', 'Rooftop', 'production'], ['ground-electrical', 'Ground Mount Electrical', 'production'],
      ['ground-physical', 'Ground Mount Physical', 'preview'], ['solaredge-import', 'SolarEdge PDF Import', 'production'],
      ['pvcase-tutorial', 'PVcase Parity', 'tutorial'],
    ])
    expect(SOLAR_FLOWS[0].stages).toBeNull()
    expect(SOLAR_FLOWS.slice(1).map((flow) => flow.stages.map(({ id, label }) => [id, label]))).toEqual([
      [['conversion', 'Tracker conversion'], ['stringing', 'Sizing and stringing'], ['equipment', 'Equipment'],
        ['feeders', 'Feeders and routes'], ['calculations', 'NEC calculations'], ['outputs', 'Schedules and exports']],
      [['terrain', 'Terrain'], ['layout', 'Tracker layout'], ['civil', 'Civil and piles'],
        ['analysis', 'Shade and terrain analysis'], ['outputs', 'Exports']],
      [['upload', 'Upload the SolarEdge PDF'], ['inspect', 'Inspect counts and matching'],
        ['tracking', 'Accept tracking'], ['outputs', 'Schedules and exports']],
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
        expect(stage.id).toMatch(/^[a-z][a-z0-9-]{0,31}$/)
        expect(typeof stage.label).toBe('string')
        expect(stage.label.length).toBeGreaterThan(0)
        expect(stage.label.length).toBeLessThanOrEqual(64)
        expect(stage.capabilities.length).toBeLessThanOrEqual(32)
        for (const name of stage.capabilities) expect(name).toMatch(/^[a-z][a-z0-9-]{0,63}$/)
      }
    }
  })

  it('FL2 names only the four real NEC declarations in registry order', () => {
    const names = SOLAR_FLOWS.flatMap((flow) => (flow.stages ?? []).flatMap((stage) => stage.capabilities))
    expect(names).toEqual(['solar-nec-ampacity-correction', 'solar-nec-ac-voltage-drop', 'solar-nec-conduit-fill', 'solar-nec-feeder-ocpd'])
    for (const [index, declaration] of [ampacityDeclaration, voltageDeclaration, conduitDeclaration, ocpdDeclaration].entries()) {
      expect(declaration.name).toBe(names[index])
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
      missing: ['Tracker conversion', 'Sizing and stringing', 'Equipment', 'Feeders and routes', 'Schedules and exports'],
    })
  })

  it('FL5 the other flows and live options explain availability and maturity', () => {
    for (const [id, missing] of [
      ['ground-physical', ['Terrain', 'Tracker layout', 'Civil and piles', 'Shade and terrain analysis', 'Exports']],
      ['solaredge-import', ['Upload the SolarEdge PDF', 'Inspect counts and matching', 'Accept tracking', 'Schedules and exports']],
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
