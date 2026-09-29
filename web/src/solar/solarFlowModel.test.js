import { describe, expect, it } from 'vitest'
import {
  MAX_FLOW_STEPS, solarFlowPrefill, solarFlowReadyMap, solarFlowReasonCodes, solarFlowRecordRun,
  solarFlowRunOutcome, solarFlowRunStatus, solarFlowRunsFor, solarFlowState, solarFlowStepId, solarFlowSteps,
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
