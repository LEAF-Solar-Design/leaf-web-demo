// Standardization slice 5a: the stage's Run ladder as exact sentences.
//
// The old ToolCast predicate was one boolean OR of six rungs; the PromptBox
// `disabledReason` prop carries the FIRST failing rung's sentence instead.
// These rows pin (1) every sentence byte for byte, (2) the rung order, which
// is the old predicate's evaluation order, (3) the all-clear null, and (4)
// the frozen map, because a reason map a consumer can mutate is not a
// contract (the honesty-ladder gate says the same thing statically).
import { describe, expect, it } from 'vitest'

import { STAGE_RUN_REASONS, STAGE_HELP_REASONS, stageHelpPaletteRow, stageRunDisabledReason } from './stageRunReasons.js'

const CLEAR = Object.freeze({
  sessionActive: true, hasDrawing: true, busy: false, jobRunning: false, routing: false, loading: false,
})

describe('STAGE_RUN_REASONS', () => {
  it('spells each rung as the exact sentence the title carries', () => {
    expect(STAGE_RUN_REASONS).toEqual({
      session: 'Sign in to run a request on this drawing.',
      drawing: 'Upload a DWG or DXF before running a request.',
      busy: 'A request is already running. Wait for it to finish.',
      job: 'A job is still running. Detach from it or wait for it to finish.',
      routing: 'Routing the request. Wait for the route decision.',
      loading: 'The drawing is still loading.',
    })
  })

  it('is frozen and every value clears the honesty-ladder floor', () => {
    expect(Object.isFrozen(STAGE_RUN_REASONS)).toBe(true)
    for (const value of Object.values(STAGE_RUN_REASONS)) {
      expect(typeof value).toBe('string')
      expect(value.length).toBeGreaterThanOrEqual(12)
      expect(value).toMatch(/^[A-Za-z]/)
      expect(value).not.toMatch(/\b(TODO|TBD|FIXME)\b|\?\?\?/i)
    }
  })
})

describe('stageRunDisabledReason', () => {
  it('returns null when every rung passes', () => {
    expect(stageRunDisabledReason(CLEAR)).toBeNull()
  })

  it('names the first failing rung in the old predicate’s order', () => {
    expect(stageRunDisabledReason({ ...CLEAR, sessionActive: false })).toBe(STAGE_RUN_REASONS.session)
    expect(stageRunDisabledReason({ ...CLEAR, hasDrawing: false })).toBe(STAGE_RUN_REASONS.drawing)
    expect(stageRunDisabledReason({ ...CLEAR, busy: true })).toBe(STAGE_RUN_REASONS.busy)
    expect(stageRunDisabledReason({ ...CLEAR, jobRunning: true })).toBe(STAGE_RUN_REASONS.job)
    expect(stageRunDisabledReason({ ...CLEAR, routing: true })).toBe(STAGE_RUN_REASONS.routing)
    expect(stageRunDisabledReason({ ...CLEAR, loading: true })).toBe(STAGE_RUN_REASONS.loading)
  })

  it('an earlier rung wins over a later one, exactly as the OR short-circuited', () => {
    expect(stageRunDisabledReason({ ...CLEAR, sessionActive: false, routing: true })).toBe(STAGE_RUN_REASONS.session)
    expect(stageRunDisabledReason({ ...CLEAR, hasDrawing: false, loading: true })).toBe(STAGE_RUN_REASONS.drawing)
    expect(stageRunDisabledReason({ ...CLEAR, busy: true, jobRunning: true })).toBe(STAGE_RUN_REASONS.busy)
  })

  it('is disabled (and not truthy-enabled) exactly when the old predicate was', () => {
    const old = (s) => s.sessionActive !== true || !s.hasDrawing || s.busy || s.jobRunning || s.routing || s.loading
    const keys = Object.keys(CLEAR)
    for (let mask = 0; mask < 2 ** keys.length; mask += 1) {
      const state = {}
      keys.forEach((key, i) => { state[key] = Boolean(mask & (1 << i)) })
      expect(stageRunDisabledReason(state) !== null).toBe(Boolean(old(state)))
    }
  })

  it('fails closed on a missing field: no argument at all reads as signed out', () => {
    expect(stageRunDisabledReason()).toBe(STAGE_RUN_REASONS.session)
    expect(stageRunDisabledReason({ hasDrawing: true })).toBe(STAGE_RUN_REASONS.session)
  })
})

// Standardization slice 13d: the stage's Help ladder. One static rung, since
// the stage mounts no ShortcutSheet and no Shift+? rung today.
describe('STAGE_HELP_REASONS', () => {
  it('is frozen and clears the honesty-ladder floor', () => {
    expect(Object.isFrozen(STAGE_HELP_REASONS)).toBe(true)
    for (const value of Object.values(STAGE_HELP_REASONS)) {
      expect(typeof value).toBe('string')
      expect(value.length).toBeGreaterThanOrEqual(12)
      expect(value).toMatch(/^[A-Za-z]/)
      expect(value).not.toMatch(/\b(TODO|TBD|FIXME)\b|\?\?\?/i)
    }
  })
})

describe('stageHelpPaletteRow', () => {
  it('carries the console registry record\'s own id, label, icon and cap — nothing re-typed', () => {
    const row = stageHelpPaletteRow()
    expect(row).toEqual({
      id: 'bar:shortcuts',
      label: 'Keyboard shortcuts',
      icon: '',
      kbd: 'Shift+?',
      disabled: true,
      reason: STAGE_HELP_REASONS.shortcuts,
      onSelect: expect.any(Function),
    })
  })

  it('declares disabled honestly: selecting it runs nothing (no ShortcutSheet on the stage)', () => {
    expect(() => stageHelpPaletteRow().onSelect()).not.toThrow()
  })
})
