import { describe, expect, it, vi } from 'vitest'
import { parseReconciliationNumber, reconciliationRows, reconciliationRowsFromValues, savedReconciliationValues, validReconciliationResponse, reconciliationCsv,
  engineeringTotalsPath, createEngineeringTotalsRecord, parseEngineeringTotalsRecord, savedOptimizerMeasurement } from './projectReconciliation.js'

describe('private engineering totals records', () => {
  const binding = { kind: 'project', organization_id: '11111111-1111-1111-1111-111111111111',
    project_id: '22222222-2222-2222-2222-222222222222', drawing_id: '33333333-3333-3333-3333-333333333333',
    input_version_id: '44444444-4444-4444-4444-444444444444' }
  const hash = 'a'.repeat(64), drafts = ['0002', '0', '01', '1', '00.8000']
  const make = (values = drafts) => createEngineeringTotalsRecord(binding, hash, values)
  it('preserves entered strings, copies inputs and binds the exact private path', () => {
    const record = make(), content = JSON.stringify(record)
    expect(record).toEqual({ schema: 'leaf.engineering-reconciliation.v1',
      binding: { organization_id: binding.organization_id, project_id: binding.project_id,
        drawing_id: binding.drawing_id, input_version_id: binding.input_version_id },
      graph_sha256: hash, values: drafts, provenance: 'user_supplied_unverified' })
    expect(engineeringTotalsPath(binding)).toBe(`.leaf/engineering-reconciliation/${binding.drawing_id}/${binding.input_version_id}.json`)
    expect(parseEngineeringTotalsRecord(content, binding, hash)).toEqual(record)
    expect(record.values).not.toBe(drafts)
    expect(record.binding).not.toBe(binding)
    expect(make([...drafts.slice(0, 4), '']).values[4]).toBe('')
    expect(make([...drafts.slice(0, 4), '0']).values[4]).toBe('0')
  })
  it('requires dense five-string inputs with four valid counts and optional valid DC', () => {
    const sparse = [...drafts]; delete sparse[1]
    for (const values of [sparse, drafts.slice(1), [...drafts, '0'], [2, '0', '1', '1', '0'],
      ['', '0', '1', '1', '0'], ['1.0', '0', '1', '1', '0'], ['9007199254740992', '0', '1', '1', '0'],
      [...drafts.slice(0, 4), ' '], [...drafts.slice(0, 4), '-1'], [...drafts.slice(0, 4), '1e2']]) expect(make(values)).toBeNull()
    expect(createEngineeringTotalsRecord(binding, hash.toUpperCase(), drafts)).toBeNull()
    expect(engineeringTotalsPath({ ...binding, project_id: 'bad' })).toBeNull()
    expect(createEngineeringTotalsRecord({ ...binding, extra: true }, hash, drafts)).toBeNull()
  })
  it('accepts only the exact canonical writer bytes and matching owner and graph', () => {
    const record = make(), content = JSON.stringify(record)
    for (const invalid of ['null', '{}', content + '\n', JSON.stringify(record, null, 2),
      content.replace('{', '{"extra":true,'), content.replace('{', '{"schema":"other",'),
      content.replace('0002', '\\u0030002'), JSON.stringify({ ...record, provenance: 'verified' }),
      ' '.repeat(4097), 'é'.repeat(2049)]) expect(parseEngineeringTotalsRecord(invalid, binding, hash)).toBeNull()
    for (const key of ['organization_id', 'project_id', 'drawing_id', 'input_version_id']) {
      expect(parseEngineeringTotalsRecord(content, { ...binding, [key]: '55555555-5555-5555-5555-555555555555' }, hash)).toBeNull()
    }
    expect(parseEngineeringTotalsRecord(content, binding, 'b'.repeat(64))).toBeNull()
    expect(drafts).toEqual(['0002', '0', '01', '1', '00.8000'])
  })
})

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

describe('explicit saved optimizer measurements', () => {
  function measured(paired = false, bridge = false) {
    const refs = [1, 2, 3, 4].map(n => id('panel', n))
    const saved = { rev: 7, panels: refs.map((ref, index) => ({ id: ref,
      provenance: { source_handle: `A${index}` } })),
    frames: [1, 2].map((n, index) => ({ id: id('frame', n),
      panel_refs: refs.slice(index * 2, index * 2 + 2), module_power_watts: 400 })), strings: [], inverters: [] }
    const strings = bridge ? [{ index: 0, source: 'bridge', frame_ref: null, pdf_matrix: 0,
      partial: false, panel_refs: refs }] : saved.frames.map((frame, index) => ({ index, source: 'group',
      frame_ref: frame.id, pdf_matrix: 0, partial: false, panel_refs: frame.panel_refs }))
    const groups = paired ? [refs.slice(0, 2), refs.slice(2)] : refs.map(ref => [ref])
    const optimizers = groups.map((panel_refs, index) => ({ id: `pdf-optimizer-${index}`,
      vendor_label: '42', pdf_matrix: 0, panel_refs, panel_handles: panel_refs.map(ref => `A${refs.indexOf(ref)}`),
      string_index: bridge ? 0 : Math.floor(refs.indexOf(panel_refs[0]) / 2) }))
    const string_counts = strings.map(row => optimizers.filter(opt => opt.string_index === row.index).length)
    saved.extra = { solaredge_import: { schema: 'leaf.solar-project-solaredge-import.v1',
      source: { artifact_id: 'a'.repeat(64), content_sha256: 'b'.repeat(64) },
      report: { artifact_id: 'c'.repeat(64), content_sha256: 'd'.repeat(64), graph_sha256: 'e'.repeat(64) },
      frames: saved.frames.map((frame, pdf_grid) => ({ frame_ref: frame.id, pdf_matrix: 0, pdf_grid })),
      strings, unassigned_panel_refs: [],
      optimizer_measurement: { schema: 'leaf.solaredge-optimizer-measurement.v1', graph_rev: 7,
        complete: true, total_count: optimizers.length, string_counts, optimizers } } }
    return saved
  }
  it.each([[false, false, 4, [2, 2]], [true, false, 2, [1, 1]], [true, true, 2, [2]]])(
    'counts distinct markers with paired=%s bridge=%s', (paired, bridge, total, strings) => {
      const saved = measured(paired, bridge), before = structuredClone(saved)
      expect(savedOptimizerMeasurement(saved)).toEqual({ total_count: total, string_counts: strings })
      expect(reconciliationRows(saved, ['4', String(total), '0', '0', '1.6'])[1])
        .toMatchObject({ studio: String(total), delta: '0', verdict: 'PASS' })
      expect(saved).toEqual(before)
    })
  it.each([
    g => { g.rev++ },
    g => { delete g.extra.solaredge_import.optimizer_measurement },
    g => { delete g.extra.solaredge_import.report },
    g => { g.extra.solaredge_import.optimizer_measurement.complete = false },
    g => { g.extra.solaredge_import.optimizer_measurement.total_count++ },
    g => { g.extra.solaredge_import.optimizer_measurement.string_counts[0]++ },
    g => { g.extra.solaredge_import.optimizer_measurement.optimizers[1].id = 'pdf-optimizer-0' },
    g => { g.extra.solaredge_import.optimizer_measurement.optimizers[0].panel_refs[0] = id('panel', 99) },
    g => { g.extra.solaredge_import.optimizer_measurement.optimizers[0].string_index = 1 },
    g => { g.extra.solaredge_import.optimizer_measurement.optimizers[0].panel_handles[0] = 'BAD' },
    g => { g.extra.solaredge_import.strings[0].partial = true },
    g => { g.extra.solaredge_import.frames.pop() },
    g => { g.extra.solaredge_import.unassigned_panel_refs = [id('panel', 1)] },
  ])('keeps stale, partial, missing or mismatched measurements unreadable', mutate => {
    const saved = measured(); mutate(saved)
    expect(savedOptimizerMeasurement(saved)).toBeNull()
    expect(reconciliationRows(saved, ['4', '0', '0', '0', '1.6'])[1])
      .toMatchObject({ studio: '', delta: '', verdict: 'UNREADABLE' })
  })
  it('distinguishes explicit complete zero from missing measurement', () => {
    const saved = measured(), record = saved.extra.solaredge_import
    saved.panels = []; saved.frames = []
    record.frames = []; record.strings = []
    Object.assign(record.optimizer_measurement, { total_count: 0, string_counts: [], optimizers: [] })
    expect(reconciliationRows(saved, ['0', '0', '0', '0', ''])[1].verdict).toBe('PASS')
    delete record.optimizer_measurement
    expect(reconciliationRows(saved, ['0', '0', '0', '0', ''])[1].verdict).toBe('UNREADABLE')
  })
})

describe('saved engineering reconciliation', () => {
  it('equates explicit panels and genuine compact slots under an explicit typed ID mapping', () => {
    const explicit = { rev: 8, panels: [1, 2, 3, 4].map(n => ({ id: id('panel', n) })),
      frames: [
        { id: id('frame', 1), panel_refs: [id('panel', 1), id('panel', 2)], module_power_watts: 401 },
        { id: id('frame', 2), panel_refs: [id('panel', 3), id('panel', 4)], module_power_watts: 599 },
      ], strings: [
        { id: id('string', 1), module_count: 2, ordered_panel_refs: [id('panel', 1), id('panel', 3)] },
        { id: id('string', 2), module_count: 2, ordered_panel_refs: [id('panel', 2), id('panel', 4)] },
      ], inverters: graph().inverters }
    const mapping = new Map([
      ...[1, 2, 3, 4].map(n => [id('panel', n), id('panel', n + 10)]),
      ...[1, 2].map(n => [id('frame', n), id('frame', n + 10)]),
      ...[1, 2].map(n => [id('string', n), id('string', n + 10)]),
      ...[1, 2, 3].map(n => [id('inverter', n), id('inverter', n + 10)]),
    ])
    const compact = { rev: 8, panels: [], frames: explicit.frames.map(frame => {
      const ids = frame.panel_refs.flatMap(ref => mapping.get(ref).split(':')[2].replaceAll('-', '')
        .match(/../g).map(hex => parseInt(hex, 16)))
      const centres = new Uint8Array(32), view = new DataView(centres.buffer)
      view.setFloat64(0, 10, true); view.setFloat64(8, 20, true)
      view.setFloat64(16, 12, true); view.setFloat64(24, 20, true)
      return { id: mapping.get(frame.id), module_power_watts: frame.module_power_watts,
        ground_slots: { codec: 'leaf.solar-ground-slots.v1', count: 2,
          panel_ids: btoa(String.fromCharCode(...ids)), centres: btoa(String.fromCharCode(...centres)),
          angle: 0, panel: { rev: 0, provenance: {}, validity: {}, extra: {} } } }
    }), strings: explicit.strings.map(string => ({ ...string, id: mapping.get(string.id),
      ordered_panel_refs: string.ordered_panel_refs.map(ref => mapping.get(ref)) })),
    inverters: explicit.inverters.map(device => ({ ...device, id: mapping.get(device.id) })) }
    const drafts = ['4', '0', '2', '2', '2'], before = structuredClone([explicit, compact, drafts])
    // Four panels, two power-converting devices, two disjoint strings;
    // 2*401 + 2*599 = 2000 integer watts = 2 kW. Optimizers have no measurement.
    const expectedCsv = 'metric,studio,oracle,delta,verdict\r\n' +
      '"panels","4","4","0","PASS"\r\n"optimizers","","0","","UNREADABLE"\r\n' +
      '"inverters","2","2","0","PASS"\r\n"strings","2","2","0","PASS"\r\n' +
      '"kw_dc","2","2","0","PASS"\r\n'
    const explicitRows = reconciliationRows(explicit, drafts)
    expect(explicitRows.map(row => row.studio)).toEqual(['4', '', '2', '2', '2'])
    expect(explicitRows.map(row => row.verdict)).toEqual(['PASS', 'UNREADABLE', 'PASS', 'PASS', 'PASS'])
    expect(reconciliationCsv(explicitRows)).toBe(expectedCsv)
    expect(reconciliationRows(compact, drafts)).toEqual(explicitRows)
    expect(reconciliationCsv(reconciliationRows(compact, drafts))).toBe(expectedCsv)
    expect([explicit, compact, drafts]).toStrictEqual(before)
  })
  it('preserves all rows when a powered frame is split and isolates missing split-frame power', () => {
    const refs = [1, 2, 3].map(n => id('panel', n))
    const original = { rev: 1, panels: refs.map(id => ({ id })),
      frames: [{ id: id('frame', 1), panel_refs: [...refs], module_power_watts: 401 }],
      strings: [{ id: id('string', 1), module_count: 3, ordered_panel_refs: [...refs] }],
      inverters: graph().inverters }
    const split = structuredClone(original)
    split.frames = [
      { id: id('frame', 1), panel_refs: refs.slice(0, 1), module_power_watts: 401 },
      { id: id('frame', 2), panel_refs: refs.slice(1), module_power_watts: 401 },
    ]
    const drafts = ['3', '0', '2', '1', '1.203'], before = structuredClone([original, split, drafts])
    const baseline = reconciliationRows(original, drafts)
    expect(baseline.map(row => row.studio)).toEqual(['3', '', '2', '1', '1.203'])
    expect(baseline.map(row => row.verdict)).toEqual(['PASS', 'UNREADABLE', 'PASS', 'PASS', 'PASS'])
    expect(reconciliationRows(split, drafts)).toEqual(baseline)
    expect([original, split, drafts]).toStrictEqual(before)
    delete split.frames[1].module_power_watts
    const missingBefore = structuredClone(split), missing = reconciliationRows(split, drafts)
    expect(missing.slice(0, 4)).toEqual(baseline.slice(0, 4))
    expect(missing[4]).toMatchObject({ studio: '', oracle: '1.203', delta: '', verdict: 'UNREADABLE' })
    expect(missing[4].reason).toMatch(/positive supported module watts for every panel/)
    expect(split).toStrictEqual(missingBefore)
    split.frames[1].module_power_watts = 401
    expect(reconciliationRows(split, drafts)).toEqual(baseline)
    expect([original, split, drafts]).toStrictEqual(before)
  })
  it('keeps literal signed DC CSV canonical under decimal-comma and grouped locale formatters', () => {
    const saved = graph()
    saved.frames[0].module_power_watts = 1234567
    saved.frames[1].module_power_watts = 7654321
    // 1,234,567 + 7,654,321 = 8,888,888 W = 8888.888 kW exactly.
    const vectors = [
      ['8888.887999999999999999', '0.000000000000000001'],
      ['8888.888000000000000001', '-0.000000000000000001'],
    ]
    const before = structuredClone([saved, vectors]), spies = []
    try {
      spies.push(vi.spyOn(Number.prototype, 'toLocaleString').mockReturnValue('8.888,888'))
      spies.push(vi.spyOn(BigInt.prototype, 'toLocaleString').mockReturnValue('8.888.888'))
      spies.push(vi.spyOn(Intl, 'NumberFormat').mockImplementation(function () {
        return { format: () => '8.888,888' }
      }))
      expect((8888.888).toLocaleString()).toBe('8.888,888')
      expect((8888888n).toLocaleString()).toBe('8.888.888')
      expect(new Intl.NumberFormat().format(8888.888)).toBe('8.888,888')
      for (const [report, delta] of vectors) {
        const drafts = ['2', '0', '2', '1', report], draftBefore = [...drafts]
        const rows = reconciliationRows(saved, drafts)
        expect(rows[4]).toMatchObject({ studio: '8888.888', oracle: report, delta, verdict: 'FAIL' })
        expect(reconciliationCsv(rows)).toBe('metric,studio,oracle,delta,verdict\r\n' +
          '"panels","2","2","0","PASS"\r\n"optimizers","","0","","UNREADABLE"\r\n' +
          '"inverters","2","2","0","PASS"\r\n"strings","1","1","0","PASS"\r\n' +
          `"kw_dc","8888.888","${report}","${delta}","FAIL"\r\n`)
        expect(drafts).toStrictEqual(draftBefore)
      }
      expect([saved, vectors]).toStrictEqual(before)
    } finally { spies.reverse().forEach(spy => spy.mockRestore()) }
  })
  it('adds disjoint Roof/Ground components and preserves totals under consistent typed renaming', () => {
    const roof = { panels: [{ id: id('panel', 1) }],
      frames: [{ id: id('frame', 1), panel_refs: [id('panel', 1)], module_power_watts: 401 }],
      strings: [{ id: id('string', 1), module_count: 1, ordered_panel_refs: [id('panel', 1)] }],
      inverters: [{ id: id('inverter', 1), equipment_type: 'string_inverter' }] }
    const ground = { panels: [], frames: [{ ...graph().frames[1], module_power_watts: 599 }],
      strings: [{ id: id('string', 2), module_count: 1, ordered_panel_refs: [id('panel', 2)] }],
      inverters: [{ id: id('inverter', 2), equipment_type: 'central_inverter' },
        { id: id('inverter', 3), equipment_type: 'combiner_box' }] }
    const before = structuredClone([roof, ground])
    const combined = { rev: 7 }
    for (const key of ['panels', 'frames', 'strings', 'inverters']) combined[key] = [...roof[key], ...ground[key]]
    // Independent arithmetic: 1+1 panels/devices/strings; 401+599=1000 integer watts.
    for (const [saved, reports, totals] of [
      [roof, ['1', '0', '1', '1', '0.401'], ['1', '', '1', '1', '0.401']],
      [ground, ['1', '0', '1', '1', '0.599'], ['1', '', '1', '1', '0.599']],
      [combined, ['2', '0', '2', '2', '1'], ['2', '', '2', '2', '1']],
    ]) {
      const rows = reconciliationRows(saved, reports)
      expect(rows.map(row => row.studio)).toEqual(totals)
      expect(rows.map(row => row.verdict)).toEqual(['PASS', 'UNREADABLE', 'PASS', 'PASS', 'PASS'])
    }
    const drafts = ['02', '00', '02', '02', '1.000']
    const baseline = reconciliationRows(combined, drafts), csv = reconciliationCsv(baseline)
    const renamed = structuredClone(combined)
    const rename = value => value.replace(/(\d{12})$/, digits => String(Number(digits) + 100).padStart(12, '0'))
    for (const key of ['panels', 'frames', 'strings', 'inverters']) for (const entity of renamed[key]) {
      entity.id = rename(entity.id)
      for (const refs of ['panel_refs', 'ordered_panel_refs']) if (entity[refs]) entity[refs] = entity[refs].map(rename)
    }
    const hex = rename(id('panel', 2)).split(':')[2].replaceAll('-', '')
    renamed.frames[1].ground_slots.panel_ids = btoa(String.fromCharCode(...hex.match(/../g).map(pair => parseInt(pair, 16))))
    const renamedBefore = structuredClone(renamed)
    expect(reconciliationRows(renamed, drafts)).toEqual(baseline)
    expect(reconciliationCsv(reconciliationRows(renamed, drafts))).toBe(csv)
    renamed.strings[1].ordered_panel_refs[0] = id('panel', 999)
    const brokenBefore = structuredClone(renamed), broken = reconciliationRows(renamed, drafts)
    expect(broken[3]).toMatchObject({ studio: '', delta: '', verdict: 'UNREADABLE' })
    expect(broken[3].reason).toMatch(/ordered panel references disagree/)
    for (const index of [0, 1, 2, 4]) expect(broken[index]).toEqual(baseline[index])
    expect(renamed).toStrictEqual(brokenBefore)
    renamed.strings[1].ordered_panel_refs[0] = rename(id('panel', 2))
    expect(reconciliationCsv(reconciliationRows(renamed, drafts))).toBe(csv)
    expect(renamed).toStrictEqual(renamedBefore)
    expect([roof, ground]).toStrictEqual(before)
    expect(drafts).toEqual(['02', '00', '02', '02', '1.000'])
  })
  it.each([
    ['frames', [0, 3, 4]], ['panels', [0, 3, 4]], ['strings', [3]], ['inverters', [2]],
  ])('rejects sparse %s with named dependent metrics and recovers dense evidence', (collection, affected) => {
    for (const position of ['first', 'last', 'all']) {
      const saved = graph(), drafts = ['2', '0', '2', '1', '0.8']
      const dense = [...saved[collection]], baseline = reconciliationRows(saved, drafts)
      const removed = position === 'all' ? dense.map((_, index) => index)
        : [position === 'first' ? 0 : dense.length - 1]
      removed.forEach(index => { delete saved[collection][index] })
      const before = structuredClone(saved), rows = reconciliationRows(saved, drafts)
      rows.forEach((row, index) => {
        if (affected.includes(index)) {
          expect(row).toMatchObject({ metric: baseline[index].metric, studio: '', delta: '', verdict: 'UNREADABLE' })
          expect(row.reason).toBeTruthy()
        } else expect(row).toEqual(baseline[index])
      })
      expect(saved).toStrictEqual(before)
      removed.forEach(index => expect(Object.hasOwn(saved[collection], index)).toBe(false))
      saved[collection] = dense
      expect(reconciliationRows(saved, drafts)).toEqual(baseline)
      expect(drafts).toEqual(['2', '0', '2', '1', '0.8'])
    }
  })
  it('reverses literal count and DC deltas when two valid mixed graphs exchange roles', () => {
    const a = graph(), b = graph()
    b.panels.push({ id: id('panel', 3) })
    b.frames[0].panel_refs.push(id('panel', 3))
    b.strings = [
      { id: id('string', 1), module_count: 2, ordered_panel_refs: [id('panel', 1), id('panel', 2)] },
      { id: id('string', 2), module_count: 1, ordered_panel_refs: [id('panel', 3)] },
    ]
    // In integer tenths of watts: A=8000, B=12001; difference=4001 (0.4001 kW).
    const before = structuredClone([a, b])
    const totalsA = ['2', '0', '2', '1', '0.8'], totalsB = ['3', '0', '2', '2', '1.2001']
    for (const [saved, reports, totals, deltas] of [
      [a, totalsB, ['2', '', '2', '1', '0.8'], ['-1', '', '0', '-1', '-0.4001']],
      [b, totalsA, ['3', '', '2', '2', '1.2001'], ['1', '', '0', '1', '0.4001']],
    ]) {
      const rows = reconciliationRows(saved, reports)
      expect(rows.map(row => row.studio)).toEqual(totals)
      expect(rows.map(row => row.delta)).toEqual(deltas)
      expect(rows.map(row => row.verdict)).toEqual(['FAIL', 'UNREADABLE', 'PASS', 'FAIL', 'FAIL'])
    }
    for (const [saved, reports] of [[a, totalsA], [b, totalsB]]) {
      expect(reconciliationRows(saved, reports).map(row => row.verdict))
        .toEqual(['PASS', 'UNREADABLE', 'PASS', 'PASS', 'PASS'])
    }
    expect([a, b]).toStrictEqual(before)
    expect(totalsA).toEqual(['2', '0', '2', '1', '0.8'])
    expect(totalsB).toEqual(['3', '0', '2', '2', '1.2001'])
  })
  it('repairs a compact Ground/explicit Roof ID collision without changing devices or caller evidence', () => {
    const saved = graph(), drafts = ['02', '0', '2', '1', '0.800']
    const baseline = reconciliationRows(saved, drafts)
    saved.panels.push({ id: id('panel', 2) })
    const before = structuredClone(saved), rows = reconciliationRows(saved, drafts)
    for (const index of [0, 3, 4]) {
      expect(rows[index]).toMatchObject({ studio: '', oracle: drafts[index], delta: '', verdict: 'UNREADABLE' })
    }
    expect(rows[0].reason).toMatch(/Panel IDs or compact Ground slots/)
    expect(rows[3].reason).toMatch(/panel union is invalid/)
    expect(rows[4].reason).toMatch(/complete, unique frame coverage/)
    for (const index of [1, 2]) expect(rows[index]).toEqual(baseline[index])
    expect(saved).toStrictEqual(before)
    saved.panels.pop()
    expect(reconciliationRows(saved, drafts)).toEqual(baseline)
    expect(drafts).toEqual(['02', '0', '2', '1', '0.800'])
  })
  it.each([
    [-1000000, '999999999999999000'], [0, null], [1000000, null],
  ])('bounds aggregate DC with supported individual integer watts: offset %s', (offset, expected) => {
    const saved = { rev: 1, panels: [], frames: [], strings: [], inverters: graph().inverters }
    // 2000 * 500000000000000000 W = 10^21 W = 10^18 kW.
    // Each power and offset is exactly representable; unique coverage cannot cause rejection.
    let integerWatts = 0n
    for (let n = 1; n <= 2000; n++) {
      const panel = id('panel', n), power = 500000000000000000 + (n === 2000 ? offset : 0)
      integerWatts += BigInt(power)
      const frame = { id: id('frame', n), panel_refs: [panel], module_power_watts: power }
      expect(savedReconciliationValues({ panels: [{ id: panel }], frames: [frame], strings: [], inverters: [] })[4].value)
        .toBe(BigInt(power) * 10n ** 15n)
      saved.panels.push({ id: panel }); saved.frames.push(frame)
    }
    expect(integerWatts).toBe(10n ** 21n + BigInt(offset))
    const before = structuredClone(saved), drafts = ['2000', '0', '2', '0', '999999999999999000']
    const rows = reconciliationRows(saved, drafts)
    expect(rows.slice(0, 4).map(row => [row.studio, row.verdict]))
      .toEqual([['2000', 'PASS'], ['', 'UNREADABLE'], ['2', 'PASS'], ['0', 'PASS']])
    expect(rows[4]).toMatchObject(expected === null
      ? { studio: '', delta: '', verdict: 'UNREADABLE' }
      : { studio: expected, delta: '0', verdict: 'PASS' })
    if (expected === null) expect(rows[4].reason).toMatch(/supported module watts/)
    expect(saved).toStrictEqual(before)
  })
  it.each(['strings', 'DC'])('repairs compound foreign references and missing coverage independently, %s first', first => {
    const saved = graph(), drafts = ['2', '0', '2', '1', '0.8']
    const baseline = reconciliationRows(saved, drafts)
    saved.strings[0].ordered_panel_refs[1] = id('panel', 99)
    saved.frames[0].panel_refs = []
    const check = (unavailable) => {
      const before = structuredClone(saved), rows = reconciliationRows(saved, drafts)
      rows.forEach((row, index) => {
        if (unavailable.includes(index)) {
          expect(row).toMatchObject({ studio: '', delta: '', verdict: 'UNREADABLE' })
          expect(row.reason).toBeTruthy()
        } else expect(row).toEqual(baseline[index])
      })
      expect(saved).toStrictEqual(before)
      expect(drafts).toEqual(['2', '0', '2', '1', '0.8'])
    }
    check([3, 4])
    const repairStrings = () => { saved.strings[0].ordered_panel_refs[1] = id('panel', 2) }
    const repairDC = () => { saved.frames[0].panel_refs = [id('panel', 1)] }
    if (first === 'strings') { repairStrings(); check([4]); repairDC() }
    else { repairDC(); check([3]); repairStrings() }
    check([])
  })
  it.each(['first', 'middle', 'last', 'all', 'singleton'])('rejects %s sparse ordered references and recovers from dense evidence', position => {
    const saved = graph(), drafts = ['03', '0', '2', '1', '1.2001']
    saved.panels.push({ id: id('panel', 3) })
    saved.frames[0].panel_refs.push(id('panel', 3))
    const dense = position === 'singleton' ? [id('panel', 1)] : [id('panel', 1), id('panel', 2), id('panel', 3)]
    saved.strings[0].module_count = dense.length
    saved.strings[0].ordered_panel_refs = [...dense]
    const baseline = reconciliationRows(saved, drafts)
    const refs = saved.strings[0].ordered_panel_refs
    for (const index of position === 'all' ? [0, 1, 2] : [{ first: 0, middle: 1, last: 2, singleton: 0 }[position]]) delete refs[index]
    const before = structuredClone(saved), rows = reconciliationRows(saved, drafts)
    expect(refs.length).toBe(dense.length)
    expect(rows[3]).toMatchObject({ studio: '', oracle: '1', delta: '', verdict: 'UNREADABLE' })
    expect(rows[3].reason).toMatch(/ordered panel references disagree/)
    for (const index of [0, 1, 2, 4]) expect(rows[index]).toEqual(baseline[index])
    expect(saved).toStrictEqual(before)
    expect(drafts).toEqual(['03', '0', '2', '1', '1.2001'])
    saved.strings[0].ordered_panel_refs = [...dense]
    expect(reconciliationRows(saved, drafts)).toEqual(baseline)
  })
  it.each([
    [0, 'central_inverter', '2', '0'], [0, 'combiner_box', '1', '-1'],
    [1, 'string_inverter', '2', '0'], [1, 'combiner_box', '1', '-1'],
    [2, 'string_inverter', '3', '1'], [2, 'central_inverter', '3', '1'],
  ])('reclassifies device %s to %s without counting combiners or changing siblings', (index, type, total, delta) => {
    const saved = graph(), drafts = ['2', '0', '2', '1', '0.8']
    const baseline = reconciliationRows(saved, drafts), original = saved.inverters[index].equipment_type
    saved.inverters[index].equipment_type = type
    const before = structuredClone(saved), rows = reconciliationRows(saved, drafts)
    expect(rows[2]).toMatchObject({ studio: total, oracle: '2', delta, verdict: delta === '0' ? 'PASS' : 'FAIL' })
    for (const sibling of [0, 1, 3, 4]) expect(rows[sibling]).toEqual(baseline[sibling])
    expect(saved).toStrictEqual(before)
    expect(drafts).toEqual(['2', '0', '2', '1', '0.8'])
    saved.inverters[index].equipment_type = original
    expect(reconciliationRows(saved, drafts)).toEqual(baseline)
  })
  const kindCases = [
    ['panel collection', 'panel', [0, 3, 4], (g, kind) => { g.panels[0].id = id(kind, 1) }],
    ['Roof frame collection', 'frame', [4], (g, kind) => { g.frames[0].id = id(kind, 1) }],
    ['Ground frame collection', 'frame', [4], (g, kind) => { g.frames[1].id = id(kind, 2) }],
    ['string collection', 'string', [3], (g, kind) => { g.strings[0].id = id(kind, 1) }],
    ['inverter collection', 'inverter', [2], (g, kind) => { g.inverters[0].id = id(kind, 1) }],
    ['ordinary frame reference', 'panel', [4], (g, kind) => { g.frames[0].panel_refs[0] = id(kind, 1) }],
    ['ordinary string reference', 'panel', [3], (g, kind) => { g.strings[0].ordered_panel_refs[0] = id(kind, 1) }],
    ['compact Ground string reference', 'panel', [3], (g, kind) => { g.strings[0].ordered_panel_refs[1] = id(kind, 2) }],
  ].flatMap(([name, correct, affected, change]) => ['panel', 'frame', 'string', 'inverter']
    .filter(kind => kind !== correct).map(kind => [name, kind, correct, affected, change]))
  it.each(kindCases)('rejects %s with valid UUID but wrong %s kind and repairs it', (_name, kind, correct, affected, change) => {
    const saved = graph(), drafts = ['2', '0', '2', '1', '0.8']
    const baseline = reconciliationRows(saved, drafts)
    change(saved, kind)
    const before = structuredClone(saved), rows = reconciliationRows(saved, drafts)
    rows.forEach((row, index) => {
      if (affected.includes(index)) {
        expect(row).toMatchObject({ metric: baseline[index].metric, studio: '', delta: '', verdict: 'UNREADABLE' })
        expect(row.reason).toBeTruthy()
      } else expect(row).toEqual(baseline[index])
    })
    expect(saved).toEqual(before)
    expect(drafts).toEqual(['2', '0', '2', '1', '0.8'])
    change(saved, correct)
    expect(reconciliationRows(saved, drafts)).toEqual(baseline)
  })
  it('distinguishes no drawn strings from an empty string row and repairs positive evidence', () => {
    const saved = graph(), drafts = ['2', '0', '2', '0', '0.8']
    saved.strings = []
    const baseline = reconciliationRows(saved, drafts)
    expect(baseline[3]).toMatchObject({ studio: '0', delta: '0', verdict: 'PASS' })
    saved.strings.push({ id: id('string', 1), module_count: 0, ordered_panel_refs: [] })
    const before = structuredClone(saved), rows = reconciliationRows(saved, drafts)
    expect(rows[3]).toMatchObject({ studio: '', delta: '', verdict: 'UNREADABLE' })
    expect(rows[3].reason).toMatch(/positive module counts/)
    for (const index of [0, 1, 2, 4]) expect(rows[index]).toEqual(baseline[index])
    expect(saved).toEqual(before)
    saved.strings[0] = graph().strings[0]
    expect(reconciliationRows(saved, ['2', '0', '2', '1', '0.8'])[3])
      .toMatchObject({ studio: '1', delta: '0', verdict: 'PASS' })
  })
  it.each(['frame', 'string', 'inverter'])('requires panel kind in ordinary Ground references, not %s', kind => {
    const saved = graph(), drafts = ['2', '0', '2', '1', '0.8']
    delete saved.frames[1].ground_slots
    saved.frames[1].panel_refs = [id('panel', 2)]
    saved.panels.push({ id: id('panel', 2) })
    const baseline = reconciliationRows(saved, drafts)
    expect(baseline[4].verdict).toBe('PASS')
    saved.frames[1].panel_refs[0] = id(kind, 2)
    const before = structuredClone(saved), rows = reconciliationRows(saved, drafts)
    expect(rows.slice(0, 4)).toEqual(baseline.slice(0, 4))
    expect(rows[4]).toMatchObject({ metric: 'kW DC', studio: '', delta: '', verdict: 'UNREADABLE' })
    expect(saved).toEqual(before)
    saved.frames[1].panel_refs[0] = id('panel', 2)
    expect(reconciliationRows(saved, drafts)).toEqual(baseline)
  })
  // Literal decimal expectations come from integer watts (or femtowatts), not a floating sum or production helper.
  it.each([
    [400.125, 399.875, '0.8', '0.800000000000000001', '-0.000000000000000001', 'FAIL'],
    [1e-7, 3e-7, '0.0000000004', '0.0000000004', '0', 'PASS'],
    [1e-15, 1e-15, '0.000000000000000002', '0.000000000000000003', '-0.000000000000000001', 'FAIL'],
    [100000000000000000, 1000, '100000000000001', '100000000000000', '1', 'FAIL'],
    [999999999999999900, 1000, '1000000000000000.9', '1000000000000000', '0.9', 'FAIL'],
  ])('exports independent mixed Roof/Ground watts %s + %s exactly', (roof, ground, total, oracle, delta, verdict) => {
    const saved = graph()
    saved.frames[0].module_power_watts = roof
    saved.frames[1].module_power_watts = ground
    const before = structuredClone(saved)
    expect(reconciliationCsv(reconciliationRows(saved, ['2', '0', '2', '1', oracle]))).toBe(
      'metric,studio,oracle,delta,verdict\r\n"panels","2","2","0","PASS"\r\n' +
      '"optimizers","","0","","UNREADABLE"\r\n"inverters","2","2","0","PASS"\r\n' +
      '"strings","1","1","0","PASS"\r\n' +
      `"kw_dc","${total}","${oracle}","${delta}","${verdict}"\r\n`)
    expect(saved).toEqual(before)
  })
  it.each([1e-16, 1.0000000000000002e-15, 1e18, 1e21, undefined, null, 0])(
    'keeps independent counts when watt precision/range/coverage is unsupported: %s', power => {
      const saved = graph(), drafts = ['2', '0', '2', '1', '0.8']
      const baseline = reconciliationRows(saved, drafts)
      saved.frames[1].module_power_watts = power
      const before = structuredClone(saved), rows = reconciliationRows(saved, drafts)
      expect(rows.slice(0, 4)).toEqual(baseline.slice(0, 4))
      expect(rows[4]).toMatchObject({ studio: '', oracle: '0.8', delta: '', verdict: 'UNREADABLE' })
      expect(reconciliationCsv(rows)).toContain('"kw_dc","","0.8","","UNREADABLE"\r\n')
      expect(saved).toEqual(before)
      saved.frames[1].module_power_watts = 399.9
      expect(reconciliationRows(saved, drafts)).toEqual(baseline)
    })
  it.each([8, 64, 512])('bounds observed graph visits linearly for %s mixed frame pairs without mutation', size => {
    const saved = { rev: 1, panels: [], frames: [], strings: [], inverters: [] }
    for (let n = 1; n <= size; n++) {
      const roof = id('panel', n * 2 - 1), ground = id('panel', n * 2)
      const hex = ground.split(':')[2].replaceAll('-', '')
      const bytes = Uint8Array.from(hex.match(/../g), pair => parseInt(pair, 16))
      saved.panels.push({ id: roof })
      saved.frames.push({ id: id('frame', n * 2 - 1), panel_refs: [roof], module_power_watts: 401 },
        { id: id('frame', n * 2), module_power_watts: 599, ground_slots: {
          ...graph().frames[1].ground_slots, panel_ids: btoa(String.fromCharCode(...bytes)),
        } })
      saved.strings.push({ id: id('string', n), module_count: 2, ordered_panel_refs: [roof, ground] })
      saved.inverters.push({ id: id('inverter', n), equipment_type: 'string_inverter' })
    }
    const before = JSON.stringify(saved), visits = {}, entries = {}
    // Accessors observe public evidence reads, including nested references/compact columns.
    // Frozen containers and throwing setters reject writes without proxying production helpers.
    function observe(value, counter) {
      if (!value || typeof value !== 'object') return
      for (const key of Object.keys(value)) {
        const child = value[key]
        observe(child, counter)
        Object.defineProperty(value, key, { enumerable: true, get() { visits[counter]++; return child },
          set() { throw new Error('Evidence was mutated') } })
      }
      Object.freeze(value)
    }
    for (const key of ['panels', 'frames', 'strings', 'inverters']) {
      visits[key] = 0
      entries[key] = saved[key].length
      observe(saved[key], key)
    }
    Object.freeze(saved)
    const rows = reconciliationRows(saved, [String(size * 2), '0', String(size), String(size), String(size)])
    const observed = { ...visits }
    expect(rows.map(row => row.studio)).toEqual([String(size * 2), '', String(size), String(size), String(size)])
    expect(rows.map(row => row.delta)).toEqual(['0', '', '0', '0', '0'])
    for (const key of Object.keys(observed)) {
      expect(observed[key]).toBeGreaterThanOrEqual(entries[key])
      expect(observed[key]).toBeLessThanOrEqual(100 * entries[key])
    }
    expect(JSON.stringify(saved)).toBe(before)
    const csv = reconciliationCsv(rows)
    for (let repeat = 0; repeat < 3; repeat++) {
      expect(reconciliationCsv(reconciliationRows(saved,
        [String(size * 2), '0', String(size), String(size), String(size)]))).toBe(csv)
      expect(JSON.stringify(saved)).toBe(before)
    }
  })
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
