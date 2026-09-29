import { describe, expect, it } from 'vitest'
import declaration from '../../../server/solar_tools/solar_string_conductors.json'
import { buildConductorParams, conductorGaugeOptions, conductorRows, CONDUCTOR_REASONS } from './solarConductorModel.js'

const strings = [
  { id: 'T1', circuit_tag: 'A1', wire_gauge: '' },
  { id: 'T2', circuit_tag: 'A2', wire_gauge: '8 AWG' },
]
const envelope = (items = strings, rev = 7) => ({ version: 3, intake: { solar_design_graph: { rev, strings: items } } })
const options = conductorGaugeOptions(declaration.record)
const rows = conductorRows(envelope(), 3).rows

describe('conductor model', () => {
  it('CF3 assignments follow graph order even for reverse selection', () => {
    expect(buildConductorParams({ rev: 7, rows, selected: new Set(['T2', 'T1']), gauge: '10 AWG', options }))
      .toEqual({ ok: true, params: { operation: 'set-conductors', expected_rev: 7, assignments: [
        { string_ref: 'T1', wire_gauge: '10 AWG' }, { string_ref: 'T2', wire_gauge: '10 AWG' },
      ] } })
  })

  it('CF9 rejects invalid graph envelopes without partial rows', () => {
    const unavailable = { ok: false, reason: 'conductor_graph_unavailable' }
    const invalid = [null, [], { ...envelope(), version: 4 }, { version: 3, intake: { solar_design_graph: { rev: 7 } } },
      envelope([strings[0], strings[0]]), envelope(Array.from({ length: 4097 }, (_, i) => ({ ...strings[0], id: String(i) }))),
      ...[-1, 1000001, 1.5, '7', Number.MAX_SAFE_INTEGER + 1].map((rev) => envelope(strings, rev)),
      ...[null, [], { ...strings[0], id: '' }, { ...strings[0], id: 'a'.repeat(129) },
        { ...strings[0], circuit_tag: 'a'.repeat(4097) }, { ...strings[0], wire_gauge: null }].map((item) => envelope([item])),
    ]
    for (const value of invalid) expect(conductorRows(value, 3)).toEqual(unavailable)
    expect(conductorRows(envelope(), 3)).toEqual({ ok: true, rev: 7, rows: [
      { id: 'T1', tag: 'A1', gauge: '' }, { id: 'T2', tag: 'A2', gauge: '8 AWG' },
    ] })
    expect(conductorRows(envelope([], 0), 3).ok).toBe(true)
    expect(conductorRows(envelope(strings, 1000000), 3).ok).toBe(true)
  })

  it('CF10 derives a frozen gauge list only from a valid declaration enum', () => {
    expect(options).toEqual(declaration.record.params.properties.assignments.items.properties.wire_gauge.enum)
    expect(options).toHaveLength(21)
    expect(Object.isFrozen(options)).toBe(true)
    for (const value of [undefined, null, [], new Array(1), '10 AWG', ['10 AWG', '10 AWG'], ['10 AWG', 10]]) {
      expect(conductorGaugeOptions({ params: { properties: { assignments: { items: { properties: { wire_gauge: { enum: value } } } } } } })).toBeNull()
    }
    expect(conductorGaugeOptions({})).toBeNull()
  })

  it('CF4 validates selection, gauge, membership and selection bounds', () => {
    const base = { rev: 7, rows, selected: new Set(['T1']), gauge: '10 AWG', options }
    for (const [overrides, reason] of [
      [{ selected: new Set() }, 'conductor_selection_required'],
      [{ gauge: '' }, 'conductor_gauge_required'],
      [{ gauge: '10 awg' }, 'conductor_gauge_required'],
      [{ selected: new Set(['missing']) }, 'conductor_graph_unavailable'],
      [{ options: null }, 'conductor_graph_unavailable'],
      [{ selected: new Set(Array.from({ length: 4097 }, (_, i) => String(i))) }, 'conductor_selection_too_large'],
    ]) expect(buildConductorParams({ ...base, ...overrides })).toEqual({ ok: false, reason })
    expect(Object.isFrozen(CONDUCTOR_REASONS)).toBe(true)
    expect(Object.values(CONDUCTOR_REASONS).every((sentence) => sentence.length >= 12)).toBe(true)
  })
})
