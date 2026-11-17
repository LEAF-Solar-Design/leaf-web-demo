import { describe, expect, it } from 'vitest'
import { parseReconciliationNumber, reconciliationRows, reconciliationRowsFromValues, savedReconciliationValues, validReconciliationResponse, reconciliationCsv } from './projectReconciliation.js'

const id = (kind, n) => `leaf:${kind}:00000000-0000-4000-8000-${String(n).padStart(12, '0')}`
function graph() {
  const bytes = new Uint8Array(16)
  bytes[6] = 0x40; bytes[8] = 0x80; bytes[15] = 2
  return { rev: 7, panels: [{ id: id('panel', 1) }], frames: [
    { id: id('frame', 1), panel_refs: [id('panel', 1)], module_power_watts: 400.1 },
    { id: id('frame', 2), module_power_watts: 399.9, ground_slots: {
      codec: 'leaf.solar-ground-slots.v1', count: 1, panel_ids: btoa(String.fromCharCode(...bytes)),
      centres: btoa(String.fromCharCode(...new Uint8Array(16))), angle: 0,
      panel: { rev: 0, provenance: {}, validity: {}, extra: {} },
    } },
  ], strings: [{ id: id('string', 1), module_count: 2, ordered_panel_refs: [id('panel', 1), id('panel', 2)] }],
  inverters: [{ id: id('inverter', 1), equipment_type: 'string_inverter' },
    { id: id('inverter', 2), equipment_type: 'central_inverter' },
    { id: id('inverter', 3), equipment_type: 'combiner_box' }], settings: { optimizer_ratio: 1 } }
}

describe('saved engineering reconciliation', () => {
  it.each([1, 2])('rejects cross-string ownership of panel %s and recovers after repair', (panel) => {
    const saved = graph(), drafts = ['2', '0', '2', '1', '0.8']
    const baseline = reconciliationRows(saved, drafts)
    saved.strings.push({ id: id('string', 2), module_count: 1, ordered_panel_refs: [id('panel', panel)] })
    const before = structuredClone(saved)
    const rows = reconciliationRows(saved, drafts)
    expect(rows[3]).toMatchObject({ studio: '', oracle: '1', delta: '', verdict: 'UNREADABLE' })
    expect(rows[3].reason).toMatch(/belongs to more than one string/)
    for (const index of [0, 1, 2, 4]) expect(rows[index]).toEqual(baseline[index])
    expect(saved).toEqual(before)
    saved.strings.pop()
    expect(reconciliationRows(saved, drafts)).toEqual(baseline)
    expect(drafts).toEqual(['2', '0', '2', '1', '0.8'])
  })
  it('keeps exact rows and canonical CSV across mixed Roof/Ground row permutations without mutation', () => {
    const saved = graph()
    saved.panels.push({ id: id('panel', 3) })
    saved.frames[0].panel_refs.push(id('panel', 3))
    saved.strings = [1, 2, 3].map(n => ({ id: id('string', n), module_count: 1, ordered_panel_refs: [id('panel', n)] }))
    const drafts = ['03', '0', '2', '3', '1.2001']
    const expected = reconciliationRows(saved, drafts), csv = reconciliationCsv(expected)
    const permutations = rows => rows.length ? rows.flatMap((row, index) =>
      permutations(rows.filter((_, i) => i !== index)).map(rest => [row, ...rest])) : [[]]
    const original = structuredClone(saved)
    for (const frames of permutations(saved.frames)) for (const panels of permutations(saved.panels)) {
      for (const strings of permutations(saved.strings)) for (const inverters of permutations(saved.inverters)) {
        const candidate = { ...saved, frames, panels, strings, inverters }, before = structuredClone(candidate)
        const rows = reconciliationRows(candidate, drafts)
        expect(rows).toEqual(expected)
        expect(reconciliationCsv(rows)).toBe(csv)
        expect(candidate).toEqual(before)
      }
    }
    expect(saved).toEqual(original)
    expect(expected.map(row => row.studio)).toEqual(['3', '', '2', '3', '1.2001'])
  })
  it('reuses derived totals with the same exact row semantics and optional blank DC', () => {
    const saved = graph(), drafts = ['2', '0', '2', '1', '']
    expect(reconciliationRowsFromValues(savedReconciliationValues(saved), drafts)).toEqual(reconciliationRows(saved, drafts))
    expect(reconciliationRows(saved, drafts)[4]).toMatchObject({ studio: '0.8', oracle: '', delta: '', verdict: 'UNREADABLE' })
  })
  it('unions ordinary and decoded Ground panels, counts saved strings and actual inverter devices', () => {
    const rows = reconciliationRows(graph(), ['2', '2', '2', '1', '0.8'])
    expect(rows.map(({ metric, studio, delta, verdict }) => [metric, studio, delta, verdict])).toEqual([
      ['Panels', '2', '0', 'PASS'], ['Optimizers', '', '', 'UNREADABLE'],
      ['Inverters', '2', '0', 'PASS'], ['Strings', '1', '0', 'PASS'], ['kW DC', '0.8', '0', 'PASS'],
    ])
    expect(rows[1].reason).toMatch(/no explicit measured optimizer/)
  })
  it('keeps tiny nonzero differences and negative deltas exact', () => {
    const rows = reconciliationRows(graph(), ['3', '0', '2', '1', '0.800000000000000001'])
    expect(rows[0]).toMatchObject({ delta: '-1', verdict: 'FAIL' })
    expect(rows[4]).toMatchObject({ delta: '-0.000000000000000001', verdict: 'FAIL' })
    const saved = graph()
    saved.frames[0].module_power_watts = 1e-7
    saved.frames[1].module_power_watts = 1e-7
    expect(reconciliationRows(saved, [null, null, null, null, '0.0000000002'])[4].verdict).toBe('PASS')
  })
  it.each(['', ' ', '1e3', 'Infinity', 'NaN', 'true', '-1', '+1', '.1', '1.', '1'.repeat(40), '0.0000000000000000001', true, 0])('rejects unsupported report value %s', (value) => {
    expect(parseReconciliationNumber(value)).toBeNull()
  })
  it('accepts zero but bounds counts to safe integers and forbids fractional counts', () => {
    expect(parseReconciliationNumber('0', true)).toBe(0n)
    expect(parseReconciliationNumber('9007199254740991', true)).not.toBeNull()
    expect(parseReconciliationNumber('9007199254740992', true)).toBeNull()
    expect(parseReconciliationNumber('1.0', true)).toBeNull()
  })
  it.each([
    (g) => g.panels.push(g.panels[0]),
    (g) => { g.panels[0].id = 'invalid' },
    (g) => g.panels.push({ id: id('panel', 2) }),
    (g) => { g.frames[1].ground_slots.panel_ids = 'bad' },
  ])('refuses invalid panel evidence and resulting DC', (mutate) => {
    const saved = graph(); mutate(saved)
    const values = savedReconciliationValues(saved)
    expect(values[0].value).toBeNull()
    expect(values[3].value).toBeNull()
    expect(values[4].value).toBeNull()
  })
  it.each([
    (g) => { g.frames[0].panel_refs = [] },
    (g) => { g.frames[0].panel_refs.push(id('panel', 1)) },
    (g) => { g.frames[0].panel_refs.push(id('panel', 99)) },
    (g) => { g.frames[0].module_power_watts = 0 },
    (g) => { delete g.frames[0].module_power_watts },
    (g) => { g.frames[0].module_power_watts = Infinity },
    (g) => { g.frames[0].module_power_watts = 1e100 },
    (g) => { g.frames[0].module_power_watts = '400' },
    (g) => { g.frames[0].module_power_watts = 1e-30 },
    (g) => { g.frames[1].id = g.frames[0].id },
  ])('refuses incomplete or ambiguous DC evidence', (mutate) => {
    const saved = graph(); mutate(saved)
    expect(savedReconciliationValues(saved)[4].value).toBeNull()
  })
  it('refuses saved strings with panel references outside the ordinary and Ground panel union', () => {
    const saved = graph()
    saved.strings[0].ordered_panel_refs[1] = id('panel', 99)
    expect(reconciliationRows(saved, ['2', '0', '2', '1', '0.8'])[3])
      .toMatchObject({ studio: '', delta: '', verdict: 'UNREADABLE' })
  })
  it('refuses inconsistent strings and missing, unknown or duplicate device types/IDs', () => {
    for (const mutate of [g => { g.strings[0].module_count = 3 }, g => g.strings.push(g.strings[0])]) {
      const saved = graph(); mutate(saved)
      expect(savedReconciliationValues(saved)[3].value).toBeNull()
    }
    for (const mutate of [g => { delete g.inverters[0].equipment_type },
      g => { g.inverters[0].equipment_type = 'optimizer' }, g => g.inverters.push(g.inverters[0])]) {
      const saved = graph(); mutate(saved)
      expect(savedReconciliationValues(saved)[2].value).toBeNull()
    }
  })
  it('allows empty counts but never fabricates DC evidence', () => {
    const rows = reconciliationRows({ frames: [], panels: [], strings: [], inverters: [] }, ['0', '0', '0', '0', '0'])
    expect(rows.map(row => row.studio)).toEqual(['0', '', '0', '0', ''])
    expect(rows.map(row => row.verdict)).toEqual(['PASS', 'UNREADABLE', 'PASS', 'PASS', 'UNREADABLE'])
  })
  it('requires a successful read, full identity, revision, graph arrays and digest', () => {
    const binding = { kind: 'project', ...Object.fromEntries(['organization_id', 'project_id', 'drawing_id', 'input_version_id']
      .map((key, index) => [key, `00000000-0000-4000-8000-00000000000${index}`])) }
    const response = () => ({ ok: true, data: { context: { ...binding }, intake: { solar_design_graph: graph(), solar_design_graph_sha256: 'a'.repeat(64) } } })
    expect(validReconciliationResponse(response(), binding)).toBe(true)
    for (const key of ['organization_id', 'project_id', 'drawing_id', 'input_version_id']) {
      const value = response(); value.data.context[key] = 'wrong'
      expect(validReconciliationResponse(value, binding)).toBe(false)
    }
    for (const mutate of [r => { r.ok = false }, r => { r.data.intake.solar_design_graph.rev = -1 },
      r => { delete r.data.intake.solar_design_graph.panels }, r => { r.data.intake.solar_design_graph_sha256 = 'bad' }]) {
      const value = response(); mutate(value)
      expect(validReconciliationResponse(value, binding)).toBe(false)
    }
  })
  it('exports exact ordered rows including unavailable optimizer evidence', () => {
    expect(reconciliationCsv(reconciliationRows(graph(), ['2', '0', '1', '1', '0.800000000000000001']))).toBe(
      'metric,studio,oracle,delta,verdict\r\n"panels","2","2","0","PASS"\r\n' +
      '"optimizers","","0","","UNREADABLE"\r\n"inverters","2","1","1","FAIL"\r\n' +
      '"strings","1","1","0","PASS"\r\n"kw_dc","0.8","0.800000000000000001","-0.000000000000000001","FAIL"\r\n')
  })
  it('preserves valid displayed report spelling in CSV while comparing its exact value', () => {
    const rows = reconciliationRows(graph(), ['02', '0', '2', '1', '0.800'])
    expect(rows[0]).toMatchObject({ oracle: '02', delta: '0', verdict: 'PASS' })
    expect(rows[4]).toMatchObject({ oracle: '0.800', delta: '0', verdict: 'PASS' })
    expect(reconciliationCsv(rows)).toContain('"kw_dc","0.8","0.800","0","PASS"')
  })
})
