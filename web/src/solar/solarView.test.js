import { describe, expect, it } from 'vitest'
import { SOLAR_VIEW_SCHEMA, solarFormKeys, solarView } from './solarView.js'

const row = {
  name: 'solar-extra',
  solar: {
    schema: SOLAR_VIEW_SCHEMA, name: 'solar-extra', family: 'stringing',
    wave: 1, order: 10, entitlement: 'run_write', interaction: { mode: 'form' },
  },
}

describe('solar view contract', () => {
  it('solarView reads a valid leaf.solar-tool-view.v1 block', () => {
    expect(SOLAR_VIEW_SCHEMA).toBe('leaf.solar-tool-view.v1')
    expect(solarView(row)).toEqual({ state: 'valid', view: row.solar })
    for (const entitlement of ['run_read', 'run_write', 'solve']) {
      for (const mode of ['form', 'none', 'pick']) {
        const solar = { ...row.solar, entitlement, interaction: { mode }, wave: 5, order: 9999 }
        expect(solarView({ ...row, solar })).toEqual({ state: 'valid', view: solar })
      }
    }
    expect(solarView({ ...row, solar: { ...row.solar, order: 0 } }).state).toBe('valid')
  })

  it('solarView reports absent for a missing or null solar key', () => {
    for (const tool of [undefined, null, {}, { name: row.name }, { ...row, solar: null }]) {
      expect(solarView(tool)).toEqual({ state: 'absent' })
    }
  })

  it('solarView reports invalid for a wrong schema, name or entitlement', () => {
    for (const change of [
      { schema: 'x' }, { name: 'another-tool' }, { entitlement: 'build' }, { family: 1 },
      { wave: 0 }, { wave: 6 }, { wave: 1.5 }, { wave: '1' },
      { order: -1 }, { order: 10000 }, { order: 0.5 },
      { interaction: null }, { interaction: [] }, { interaction: { mode: 'other' } },
    ]) {
      expect(solarView({ ...row, solar: { ...row.solar, ...change } })).toEqual({ state: 'invalid' })
    }
    for (const solar of [[], true, 'solar', new Date(0), Object.assign(Object.create({ inherited: true }), row.solar)]) {
      expect(solarView({ ...row, solar })).toEqual({ state: 'invalid' })
    }
  })

  it('solarFormKeys drops pick-bound keys and drawing_id on a write tool', () => {
    const write = {
      ...row, capabilities: ['drawing.write'],
      params: { properties: { drawing_id: {}, expected_rev: {}, cancel: {}, changes: {}, initialize: {} } },
    }
    expect(solarFormKeys(write, row.solar)).toEqual(['expected_rev', 'cancel', 'changes', 'initialize'])
    expect(solarFormKeys({ ...write, capabilities: ['drawing.read'] }, row.solar))
      .toEqual(['drawing_id', 'expected_rev', 'cancel', 'changes', 'initialize'])
    const pick = { ...row.solar, interaction: { mode: 'pick', pick: [
      { kind: 'entity', key: 'panels' }, { kind: 'point', keys: ['x', 'y'] },
    ] } }
    expect(solarFormKeys({ params: { properties: { panels: {}, x: {}, y: {}, spacing: {} } } }, pick)).toEqual(['spacing'])
  })
})
