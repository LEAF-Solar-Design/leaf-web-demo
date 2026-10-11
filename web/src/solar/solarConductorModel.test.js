import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { deriveProjectConductorState, buildProjectConductorRequest } from './solarConductorModel.js'
import declaration from '../../../server/solar_tools/solar_string_conductors.json'
import { buildConductorParams, conductorGaugeOptions, conductorRows, CONDUCTOR_REASONS } from './solarConductorModel.js'

const strings = [
  { id: 'T1', circuit_tag: 'A1', wire_gauge: '' },
  { id: 'T2', circuit_tag: 'A2', wire_gauge: '8 AWG' },
]
const envelope = (items = strings, rev = 7) => ({ version: 3, intake: { solar_design_graph: { rev, strings: items } } })
const options = conductorGaugeOptions(declaration.record)
const rows = conductorRows(envelope(), 3).rows


const r9Binding = { kind: 'project', organization_id: '11111111-1111-1111-1111-111111111111',
  project_id: '22222222-2222-2222-2222-222222222222', drawing_id: '33333333-3333-3333-3333-333333333333',
  input_version_id: '44444444-4444-4444-4444-444444444444' }
const r9Digest = 'sha256:' + 'd'.repeat(64)
const r9Context = (graph) => ({ context: { ...r9Binding, head_version_id: r9Binding.input_version_id,
  is_head: true, intake_sha256: 'a'.repeat(64) }, intake: { solar_design_graph: graph,
  solar_design_graph_sha256: 'b'.repeat(64) } })
const r9Oracle = JSON.parse(readFileSync(`${process.cwd()}/src/solar/__fixtures__/solarSizingConductorStandalone.main-f139ceba.json`, 'utf8'))

describe('conductor model', () => {

  it('R9-03 conductor state freezes graph order rows without freezing source or implicit selection', () => {
    const contextView = r9Context({ rev: 7, strings })
    const state = deriveProjectConductorState({ binding: r9Binding, contextView })
    expect(state).toEqual({ mode: 'edit', binding: r9Binding, intakeSha256: 'a'.repeat(64),
      graphSha256: 'b'.repeat(64), rev: 7, rows })
    expect(Object.isFrozen(state)).toBe(true)
    expect(Object.isFrozen(state.rows)).toBe(true)
    expect(state.rows.every(Object.isFrozen)).toBe(true)
    expect(Object.isFrozen(contextView.intake.solar_design_graph.strings)).toBe(false)
    expect(state).not.toHaveProperty('selected')
    for (const invalid of [[strings[0], strings[0]], [{ ...strings[0], wire_gauge: null }]]) {
      expect(deriveProjectConductorState({ binding: r9Binding, contextView: r9Context({ rev: 7, strings: invalid }) }).reason).toBe('graph_unreadable')
    }
  })

  it('R9-15 reverse empty and lowercase standalone conductor results equal the stored oracle', () => {
    const base = { rev: 7, rows, selected: new Set(['T2', 'T1']), gauge: '10 AWG', options }
    for (const [name, changes] of [['conductors', {}], ['conductorsEmptySelection', { selected: new Set() }],
      ['conductorsLowercaseGauge', { gauge: '10 awg' }]]) {
      expect(JSON.stringify(buildConductorParams({ ...base, ...changes }))).toBe(r9Oracle.captures[name].json)
    }
  })

  it('R9-16 canonical conductors add only D and preserve operation revision and graph ordered assignments', () => {
    const state = deriveProjectConductorState({ binding: r9Binding, contextView: r9Context({ rev: 7, strings }) })
    const built = buildProjectConductorRequest({ binding: r9Binding, state, selected: new Set(['T2', 'T1']),
      gauge: '10 AWG', options, catalogDigest: r9Digest, idempotencyKey: 'key' })
    expect(built).toEqual({ ok: true, request: { tool: 'solar-string-conductors',
      params: { drawing_id: r9Binding.drawing_id, operation: 'set-conductors', expected_rev: 7, assignments: [
        { string_ref: 'T1', wire_gauge: '10 AWG' }, { string_ref: 'T2', wire_gauge: '10 AWG' },
      ] }, catalogDigest: r9Digest, idempotencyKey: 'key' } })
  })

  it('R9-17 missing malformed and duplicate declaration enums cannot produce a candidate', async () => {
    const state = deriveProjectConductorState({ binding: r9Binding, contextView: r9Context({ rev: 7, strings }) })
    for (const invalid of [undefined, null, [], [''], [' '], ['10 AWG', '10 AWG'], ['10 AWG', 1]]) {
      expect(buildProjectConductorRequest({ binding: r9Binding, state, selected: new Set(['T1']),
        gauge: '10 AWG', options: invalid, catalogDigest: r9Digest, idempotencyKey: 'key' }).reason).toBe('request_invalid')
    }
    // Tool and digest provenance is a form boundary; the model accepts only declaration-derived options.
    expect(options).toHaveLength(21)
    expect(options).not.toContain('10 awg')
    const { createElement } = await import('react')
    const { render, cleanup, act, screen } = await import('@testing-library/react')
    const { default: SolarConductorForm } = await import('./SolarConductorForm.jsx')
    const { createSolarProjectClient } = await import('./solarProjectClient.js')
    const { vi } = await import('vitest')
    const client = createSolarProjectClient({ fetchImpl: vi.fn() })
    client.setScope(r9Binding, 'epoch-1')
    const readContext = vi.fn(async () => ({ ok: true, status: 200, data: r9Context({ rev: 7, strings }) }))
    const catalog = vi.fn()
    const factory = vi.fn(() => 'key')
    try {
      for (const row of [{ ...declaration.record, name: 'other', catalog_digest: r9Digest },
        { ...declaration.record, catalog_digest: 'sha256:' + 'e'.repeat(64) },
        { ...declaration.record, catalog_digest: r9Digest, params: {} }]) {
        render(createElement(SolarConductorForm, { row, solarBinding: r9Binding, authorityKey: 'epoch-1',
          projectClient: { ...client, readContext, catalog }, catalogDigest: r9Digest,
          newProjectIntentKey: factory, onProjectSubmit: vi.fn() }))
        await act(async () => {})
        expect(screen.getByText('This request could not be prepared. Review the inputs and try again.')).toBeTruthy()
        expect(screen.getByRole('button', { name: 'Apply to selected strings' }).disabled).toBe(true)
        expect(catalog).not.toHaveBeenCalled()
        expect(factory).not.toHaveBeenCalled()
        cleanup()
      }
    } finally { cleanup(); client.dispose() }
  })
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
