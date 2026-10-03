import { describe, expect, it, vi } from 'vitest'
import { createHash } from 'node:crypto'
import ground from './__fixtures__/stringComposerGround.json'
import rooftop from './__fixtures__/stringComposerRooftop.json'
import { decodeGroundFrameSlots, GROUND_SLOT_CODEC } from './solarGroundSlots.js'
import {
  SOLAR_STRING_COMPOSER_REASONS, STRING_ADD_TOOL, STRING_MULTI_ADD_TOOL,
  MAX_SINGLE_REFS, MAX_MULTI_REFS, MAX_STRING_LENGTH, MAX_GRAPH_REV, MAX_COMBO_PANELS,
  stringComboSequences, stringComboSizes, savedStringLimit, stringComposerView,
  composeStringRequest, checkStringFreshness, stringRunRefusal,
} from './solarStringComposerModel.js'

vi.mock('./solarGroundSlots.js', async (importOriginal) => {
  const real = await importOriginal()
  return { ...real, decodeGroundFrameSlots: vi.fn(real.decodeGroundFrameSlots) }
})

const envelope = (graph = ground, version = 7) => ({ intake: { solar_design_graph: graph }, version })
const makeView = (graph = ground) => stringComposerView({
  envelope: envelope(graph), drawingId: 'd1', drawingVersion: 7, projectId: null,
})
const changed = (base, edit) => { const graph = structuredClone(base); edit(graph); return graph }
const P = [
  'leaf:panel:a4e25cdd-6775-403d-9e0c-275400699308',
  'leaf:panel:670bc2b0-4640-458e-b12e-7d2f664a6766',
  'leaf:panel:a53ced07-0ab7-4dca-8ec1-c8f1982e1fc8',
  'leaf:panel:a89fade1-f331-4578-9d71-332417337b99',
  'leaf:panel:ce43e26e-a661-420b-824d-de3ff7a0be01',
]
const Q = rooftop.panels.slice(3).map((panel) => panel.id)
const range = (frameIndex = 0, fromSlot = 1, toSlot = 1) => ({
  kind: 'range', frameIndex, frameId: ground.frames[frameIndex]?.id ?? 'nope', fromSlot, toSlot,
})
const panel = (panelId) => ({ kind: 'panel', panelId })
const compose = (queue, graph = ground, multi = false, stringLength = 3) => composeStringRequest({
  view: makeView(graph), toolName: multi ? STRING_MULTI_ADD_TOOL : STRING_ADD_TOOL, queue, stringLength,
})
const refusal = (result, code) => {
  expect(result.ok).toBe(false)
  expect(result.code).toBe(code)
  expect(result.reason).toBe(new Map(Object.entries(SOLAR_STRING_COMPOSER_REASONS)).get(code))
  expect(Object.isFrozen(result)).toBe(true)
}
const accepted = (result, ids, sizes, rev, length) => {
  expect(result.ok).toBe(true)
  expect(result.params).toEqual(length === undefined
    ? { operation: 'add-string', expected_rev: rev, ordered_panel_refs: ids }
    : { operation: 'add-strings', expected_rev: rev, string_length: length, ordered_panel_refs: ids })
  expect(result.preview).toEqual({ ids, sizes })
}
const capped = (cap, assigned = []) => changed(ground, (g) => {
  g.settings.panels_in_sequence = cap
  g.electrical_zones = []
  g.strings = assigned.length ? [{ ordered_panel_refs: assigned }] : []
})

// Same block shape and byte encoding as the shared decoder's boundary fixtures.
const template = () => ({ rev: 0, provenance: {}, validity: {}, extra: {} })
const binary = (bytes) => {
  let text = ''
  for (let index = 0; index < bytes.length; index += 4096) {
    text += String.fromCharCode(...bytes.subarray(index, index + 4096))
  }
  return btoa(text)
}
const block = (count = 1, start = 1) => {
  const ids = new Uint8Array(count * 16)
  const centres = new Uint8Array(count * 16)
  const view = new DataView(centres.buffer)
  for (let index = 0; index < count; index += 1) {
    const n = start + index
    ids[index * 16 + 6] = 0x40
    ids[index * 16 + 8] = 0x80
    ids[index * 16 + 13] = (n >>> 16) & 255
    ids[index * 16 + 14] = (n >>> 8) & 255
    ids[index * 16 + 15] = n & 255
    view.setFloat64(index * 16, n, true)
    view.setFloat64(index * 16 + 8, -n, true)
  }
  return { codec: GROUND_SLOT_CODEC, count, panel_ids: binary(ids), centres: binary(centres),
    angle: 0, panel: template() }
}
const fresh = (view = makeView(), env = envelope(), host = {}) => checkStringFreshness({
  view, drawingId: 'd1', drawingVersion: 7, envelope: env, ...host,
})
const serverMappings = [
  ['INVALID_STRING_ADD_REQUEST', 'request'], ['INVALID_STRING_MULTI_ADD_REQUEST', 'request'],
  ['DUPLICATE_PANEL_MEMBERSHIP', 'duplicate'], ['STALE_GRAPH_REVISION', 'stale'],
  ['UNRESOLVED_UNITS', 'units'], ['STRING_LENGTH_NOT_SIZED', 'notSized'],
  ['STRING_TOO_LONG', 'tooLong'], ['MISSING_PANEL', 'missing'],
  ['PANEL_ALREADY_ASSIGNED', 'assigned'], ['MULTI_ADD_INFEASIBLE_COUNT', 'infeasible'],
  ['STRING_NUMBER_EXHAUSTED', 'numberExhausted'], ['panels_required', 'noPanels'],
]
const malformedCodes = [
  'GRAPH_LIMIT_EXCEEDED',
  'INVALID_JSON_OBJECT',
  'INVALID_JSON_STRING',
  'NONFINITE_NUMBER',
  'NUMBER_LIMIT_EXCEEDED',
  'INVALID_JSON_VALUE',
  'SCHEMA_LIMIT_EXCEEDED',
  'INVALID_GRAPH',
  'UNSUPPORTED_SCHEMA_VERSION',
  'UNKNOWN_UNITS',
  'INVALID_GRAPH_SCHEMA',
  'INVALID_REVISION_CHAIN',
  'DUPLICATE_APPLICATION_ID',
  'ID_KIND_MISMATCH',
  'FUTURE_ENTITY_REVISION',
  'FUTURE_SCHEDULE_REVISION',
  'INVALID_GROUND_SLOTS',
  'INSTALLATION_DESIGN_MISMATCH',
  'TRACKER_SLOT_MISMATCH',
  'DEGENERATE_TRACKER_AXIS',
  'DUPLICATE_TRACKER_SOURCE',
  'STRING_COUNT_MISMATCH',
  'PANEL_ASSIGNMENT_MISMATCH',
  'FRAME_MEMBERSHIP_MISMATCH',
  'MISSING_ELECTRICAL_ZONE',
  'INVALID_MATRIX_DIMENSIONS',
  'INVALID_MATRIX_PANEL',
  'MATRIX_CELL_MISMATCH',
  'MATRIX_SEQUENCE_MISMATCH',
  'FRAME_ASSIGNMENT_MISMATCH',
  'FRAME_SEQUENCE_MISMATCH',
  'MATRIX_INPUT_MISMATCH',
  'INVERTER_CAPACITY_EXCEEDED',
  'INVERTER_ASSIGNMENT_MISMATCH',
  'DUPLICATE_INVERTER_INPUT',
  'EQUIPMENT_TYPE_REQUIRED',
  'L2_MODE_REQUIRED',
  'DUPLICATE_EQUIPMENT_NUMBER',
  'L2_MIXED_INPUTS',
  'L2_CAPACITY_EXCEEDED',
  'L2_ASSIGNMENT_MISMATCH',
  'DUPLICATE_L2_INPUT',
  'INVALID_SCHEDULE_COLUMNS',
  'ROUTE_ENDPOINT_MISMATCH',
  'DUPLICATE_FEEDER',
  'ROUTE_PATHWAY_MISMATCH',
]

describe('pure Solar string composer', () => {
  it('SC1 Ground view preserves frame and slot order', () => {
    expect(makeView()).toEqual({ ok: true, binding: { drawingId: 'd1', drawingVersion: 7, rev: 2 },
      design: 'Ground', savedLimit: 27, rows: [
        { frameIndex: 0, frameId: ground.frames[0].id, name: 'Group 1', slotCount: 3, panelIds: P.slice(0, 3) },
        { frameIndex: 1, frameId: ground.frames[1].id, name: 'Group 2', slotCount: 2, panelIds: P.slice(3) },
      ], panels: [], assignedCount: 0 })
  })
  it('SC2 Roof view uses explicit panels and the largest saved limit', () => {
    const view = makeView(rooftop)
    expect(view.design).toBe('Roof')
    expect(view.savedLimit).toBe(14)
    expect(view.panels).toHaveLength(30)
    expect(view.panels.map((p) => p.id)).toEqual(rooftop.panels.map((p) => p.id))
    expect(view.panels[3].frameName).toBe('Multi group')
    expect(view.panels[0].frameName).toBe('Roof group')
    expect(view.rows).toEqual([])
    expect(view.assignedCount).toBe(3)
  })
  it('SC3 saved limits accept only integers and tolerate missing containers', () => {
    for (const [graph, expected] of [
      [{ settings: { panels_in_sequence: 0 }, electrical_zones: [
        { panels_in_sequence: 5 }, { panels_in_sequence: 3 }] }, 5],
      [{ settings: { panels_in_sequence: 27 } }, 27],
      [{ settings: { panels_in_sequence: true }, electrical_zones: [{ panels_in_sequence: 2 }] }, 2],
      [{ settings: { panels_in_sequence: 27.5 } }, 0],
      [{ settings: { panels_in_sequence: '27' }, electrical_zones: [{ panels_in_sequence: null }] }, 0],
      [null, 0], [[], 0], ['x', 0], [{}, 0],
      [{ settings: [], electrical_zones: 'x' }, 0],
    ]) expect(savedStringLimit(graph)).toBe(expected)
  })
  it('SC4 scope requires a saved standalone drawing', () => {
    for (const override of [{ projectId: 'p1' }, { drawingId: '' }, { drawingId: 7 },
      ...[0, 1.5, '7', -1].map((drawingVersion) => ({ drawingVersion }))]) {
      refusal(stringComposerView({ envelope: envelope(), drawingId: 'd1', drawingVersion: 7,
        projectId: null, ...override }), 'scope')
    }
  })
  it('SC5 envelope and revision failures follow the view order', () => {
    for (const [env, code] of [[null, 'unavailable'], [[], 'unavailable'],
      [envelope(ground, '7'), 'unavailable'], [envelope(ground, 8), 'stale'],
      [{ version: 8 }, 'stale'],
      [{ version: 7 }, 'unavailable'], [{ version: 7, intake: {} }, 'noPanels'],
      [envelope(null), 'noPanels'], [envelope([]), 'malformed']]) {
      refusal(stringComposerView({ envelope: env, drawingId: 'd1', drawingVersion: 7, projectId: null }), code)
    }
    refusal(stringComposerView({ envelope: { version: 7, intake: {} }, drawingId: 'd1',
      drawingVersion: 7, projectId: null }), 'noPanels')
    for (const rev of [-1, 2147483648, 1.5]) {
      refusal(makeView(changed(ground, (g) => { g.rev = rev })), 'malformed')
    }
  })
  it('SC6 unsupported and absent design refuse representation', () => {
    for (const design of ['Other', undefined]) {
      refusal(makeView(changed(ground, (g) => { g.project.installation_design = design })), 'representation')
    }
  })
  it('SC7 Ground representation and compact block checks', () => {
    refusal(makeView(changed(ground, (g) => { g.panels = [{}] })), 'representation')
    refusal(makeView(changed(ground, (g) => { g.frames[0].ground_slots.count = 4 })), 'malformed')
    refusal(makeView(changed(ground, (g) => { for (const f of g.frames) delete f.ground_slots })), 'noPanels')
    refusal(makeView(changed(ground, (g) => { g.frames[1].id = 5 })), 'malformed')
  })
  it('SC8 Roof representation and panel identity checks', () => {
    refusal(makeView(changed(rooftop, (g) => { g.frames[0].ground_slots = {} })), 'representation')
    refusal(makeView(changed(rooftop, (g) => { g.panels = [] })), 'noPanels')
    for (const id of [7, 'x'.repeat(129)]) {
      refusal(makeView(changed(rooftop, (g) => { g.panels[0].id = id })), 'malformed')
    }
  })
  it('SC9 validates string containers and references', () => {
    for (const strings of [{}, [{}], [{ ordered_panel_refs: [7] }]]) {
      refusal(makeView(changed(ground, (g) => { g.strings = strings })), 'malformed')
    }
  })
  it('SC10 views are frozen snapshots and preserve the input', () => {
    for (const graph of [ground, rooftop]) {
      const env = envelope(structuredClone(graph))
      const before = structuredClone(env)
      const view = stringComposerView({ envelope: env, drawingId: 'd1', drawingVersion: 7, projectId: null })
      expect(env).toEqual(before)
      for (const value of [view, view.binding, view.rows, view.panels, ...view.rows,
        ...view.rows.map((row) => row.panelIds), ...view.panels]) expect(Object.isFrozen(value)).toBe(true)
      env.intake.solar_design_graph.settings.panels_in_sequence = 1
      expect(view.savedLimit).toBe(graph.settings.panels_in_sequence)
    }
  })
  it('SC11 multi spans two Ground rows', () => {
    accepted(compose([range(0, 1, 3), range(1, 1, 2)], ground, true), P, [3, 2], 2, 3)
  })
  it('SC12 single uses one row', () => {
    accepted(compose([range(0, 1, 3)]), P.slice(0, 3), [3], 2)
  })
  it('SC13 reversed row queue remains electrical order', () => {
    accepted(compose([range(1, 1, 2), range(0, 1, 3)], ground, true),
      [...P.slice(3), ...P.slice(0, 3)], [3, 2], 2, 3)
  })
  it('SC14 descending slot range remains descending', () => {
    accepted(compose([range(0, 3, 1)]), P.slice(0, 3).reverse(), [3], 2)
  })
  it('SC15 single obeys the saved cap', () => refusal(compose([range(0, 1, 3)], capped(2)), 'tooLong'))
  it('SC16 multi obeys the saved cap', () => {
    refusal(compose([range(0, 1, 3), range(1, 1, 2)], ground, true, 28), 'tooLong')
  })
  it('SC17 repeated selections refuse both tools', () => {
    for (const multi of [false, true]) refusal(compose([range(), range()], ground, multi), 'duplicate')
  })
  it('SC18 assigned Ground panels refuse', () => {
    refusal(compose([range()], capped(27, P.slice(0, 3))), 'assigned')
  })
  it('SC19 unsized graphs refuse both tools', () => {
    for (const multi of [false, true]) refusal(compose([range()], capped(0), multi, 1), 'notSized')
  })
  it('SC20 empty selection refuses both tools', () => {
    for (const multi of [false, true]) refusal(compose([], ground, multi), 'empty')
  })
  it.each([
    [27, 14, false, [14, 13]], [27, 14, true, [14, 13]],
    [27, 5, false, [5, 5, 5, 4, 4, 4]], [27, 4, false, [4, 4, 4, 4, 4, 4, 3]],
    [27, 3, false, Array(9).fill(3)], [27, 2, false, [...Array(13).fill(2), 1]],
    [27, 1, false, Array(27).fill(1)], [14, 14, false, [14]],
    [13, 14, false, [13]], [12, 14, false, [12]],
  ])('SC21 Roof multi count %i length %i reverse %s', (count, length, reverse, sizes) => {
    const ids = Q.slice(0, count)
    if (reverse) ids.reverse()
    accepted(compose(ids.map(panel), rooftop, true, length), ids, sizes, 0, length)
  })
  it.each([[27, 13], [11, 14], [1, 14]])('SC22 Roof infeasible count %i length %i', (count, length) => {
    refusal(compose(Q.slice(0, count).map(panel), rooftop, true, length), 'infeasible')
  })
  it('SC23 assigned Roof panel precedes feasibility', () => {
    refusal(compose([...Q.slice(0, 10), rooftop.panels[0].id].map(panel), rooftop, true, 14), 'assigned')
  })
  it('SC24 missing Roof panel refuses', () => {
    refusal(compose([...Q.slice(0, 13), 'leaf:panel:00000000-0000-4000-8000-0000000009ff'].map(panel),
      rooftop, true, 14), 'missing')
  })
  it('SC25 repeated Roof panel refuses', () => {
    refusal(compose([...Q.slice(0, 13), Q[0]].map(panel), rooftop, true, 14), 'duplicate')
  })
  it('SC26 validates multi lengths while single ignores them', () => {
    for (const length of [901, 0, 1.5, '14', null, true]) {
      refusal(compose([panel(Q[0])], rooftop, true, length), 'length')
    }
    accepted(compose([panel(Q[0])], rooftop, false, 901), [Q[0]], [1], 0)
  })
  it('SC27 queue bounds precede entry reads', () => {
    const entry = Object.defineProperty({}, 'kind', { get() { throw new Error('Entry read') } })
    refusal(compose(Array(4097).fill(entry), rooftop), 'singleLimit')
    refusal(compose(Array(901).fill(entry), rooftop, true), 'multiLimit')
  })
  it('SC28 real compact range boundaries and count before resolution', () => {
    const graph = changed(ground, (g) => {
      g.frames = [g.frames[0]]
      g.frames[0].ground_slots = block(5000)
      g.settings.panels_in_sequence = 4096
    })
    const ids = Array.from({ length: 5000 }, (_, i) =>
      `leaf:panel:00000000-0000-4000-8000-${(i + 1).toString(16).padStart(12, '0')}`)
    accepted(compose([range(0, 1, 4096)], graph), ids.slice(0, 4096), [4096], 2)
    refusal(compose([range(0, 1, 4097)], graph), 'singleLimit')
    graph.settings.panels_in_sequence = 900
    accepted(compose([range(0, 1, 900)], graph, true, 900), ids.slice(0, 900), [900], 2, 900)
    accepted(compose([range(0, 1, 900)], graph, true, 14), ids.slice(0, 900),
      [...Array(55).fill(14), ...Array(10).fill(13)], 2, 14)
    refusal(compose([range(0, 1, 901)], graph, true, 14), 'multiLimit')
    refusal(compose([{ kind: 'range', frameIndex: 9, frameId: 'nope', fromSlot: 1, toSlot: 5000 }],
      graph, true, 14), 'multiLimit')
    graph.settings.panels_in_sequence = 899
    refusal(compose([range(0, 1, 900)], graph, true, 900), 'tooLong')
  })
  it('SC29 resolution and request shape', () => {
    for (const entry of [range(0, 1, 4), range(0, 0, 1), range(0, 1.5, 2)]) {
      refusal(compose([entry]), 'range')
    }
    refusal(compose([{ ...range(1), frameId: ground.frames[0].id }]), 'missing')
    refusal(compose([range(5)]), 'missing')
    refusal(compose([panel(P[0])]), 'request')
    refusal(compose([range()], rooftop), 'request')
    refusal(compose([null]), 'request')
    refusal(compose('x'), 'request')
    for (const entry of [{ ...range(), frameIndex: -1 }, { ...range(), frameId: 7 }]) {
      refusal(compose([entry]), 'request')
    }
    refusal(compose([panel(7)], rooftop), 'request')
    refusal(composeStringRequest({ view: makeView(), toolName: 'solar-string-delete', queue: [range()] }), 'request')
    for (const view of [null, { ok: false }]) {
      refusal(composeStringRequest({ view, toolName: STRING_ADD_TOOL, queue: [range()] }), 'unavailable')
    }
  })
  it.each([
    ['unknown tool before invalid length', () => composeStringRequest({
      view: makeView(), toolName: 'nope', queue: [], stringLength: 0 }), 'request'],
    ['length before empty queue', () => compose([], ground, true, 0), 'length'],
    ['queue limit before duplicate', () => compose(Array(901).fill(range()), ground, true), 'multiLimit'],
    ['missing before duplicate', () => compose([range(), range(), range(7)]), 'missing'],
    ['duplicate before saved cap', () => compose([range(0, 1, 3), range()], capped(2)), 'duplicate'],
    ['saved cap before assigned', () => compose([range(0, 1, 3)], capped(2, P.slice(0, 3))), 'tooLong'],
    ['unsized before assigned', () => compose([range()], capped(0, [P[0]])), 'notSized'],
    ['assigned before infeasible', () => compose([panel(Q[0]), panel(rooftop.panels[0].id)],
      rooftop, true, 14), 'assigned'],
  ])('SC30 %s', (_name, run, code) => refusal(run(), code))
  it('SC31 compose preserves inputs and returns independent arrays', () => {
    const view = makeView()
    const queue = [range(0, 1, 3)]
    const beforeView = structuredClone(view)
    const beforeQueue = structuredClone(queue)
    const run = (toolName) => composeStringRequest({ view, toolName, queue, stringLength: 3 })
    for (const tool of [STRING_ADD_TOOL, STRING_MULTI_ADD_TOOL]) {
      const result = run(tool)
      expect(Object.isFrozen(result)).toBe(true)
      expect(Object.isFrozen(result.params)).toBe(false)
      expect(Object.keys(result.params).sort()).toEqual((tool === STRING_ADD_TOOL
        ? ['operation', 'expected_rev', 'ordered_panel_refs']
        : ['operation', 'expected_rev', 'string_length', 'ordered_panel_refs']).sort())
      expect(result.params.ordered_panel_refs).toEqual(result.preview.ids)
      expect(result.params.ordered_panel_refs).not.toBe(result.preview.ids)
      expect(Object.isFrozen(result.preview)).toBe(true)
      expect(Object.isFrozen(result.preview.sizes)).toBe(true)
      result.params.ordered_panel_refs.push('nope')
      expect(result.preview.ids).toEqual(P.slice(0, 3))
      expect(run(tool).params.ordered_panel_refs).toEqual(P.slice(0, 3))
    }
    expect(view).toEqual(beforeView)
    expect(queue).toEqual(beforeQueue)
  })
  it('SC32 freshness checks host binding, envelope and revision', () => {
    const view = makeView()
    expect(fresh(view)).toEqual({ ok: true })
    expect(Object.isFrozen(fresh(view))).toBe(true)
    refusal(fresh(view, envelope(changed(ground, (g) => { g.rev = 3 }))), 'stale')
    refusal(fresh(view, envelope(ground, 8)), 'stale')
    refusal(fresh(view, envelope(), { drawingVersion: 8 }), 'stale')
    refusal(fresh(view, envelope(), { drawingId: 'd2' }), 'stale')
    for (const env of [null, { version: 7 }, envelope([]), envelope(ground, '7')]) {
      refusal(fresh(view, env), 'unavailable')
    }
    refusal(fresh({ ok: false }), 'unavailable')
  })
  it('SC33 decoder runs once on Ground and never during composition or freshness', () => {
    decodeGroundFrameSlots.mockClear()
    const view = makeView()
    expect(decodeGroundFrameSlots).toHaveBeenCalledTimes(1)
    decodeGroundFrameSlots.mockClear()
    makeView(rooftop)
    expect(decodeGroundFrameSlots).not.toHaveBeenCalled()
    composeStringRequest({ view, toolName: STRING_ADD_TOOL, queue: [range()] })
    fresh(view)
    expect(decodeGroundFrameSlots).not.toHaveBeenCalled()
  })
  it('SC34 server refusals map every listed code to client reasons', () => {
    for (const [code, key] of serverMappings) refusal(stringRunRefusal(code), key)
    for (const code of malformedCodes) refusal(stringRunRefusal(code), 'malformed')
    for (const code of ['SOMETHING_ELSE', undefined, 7, {}]) refusal(stringRunRefusal(code), 'failed')
  })
  it('SC35 reason map has exactly the frozen product sentences', () => {
    expect(Object.isFrozen(SOLAR_STRING_COMPOSER_REASONS)).toBe(true)
    expect(SOLAR_STRING_COMPOSER_REASONS).toEqual({
      scope: 'Open a saved standalone drawing before creating strings.',
      loading: 'Reading the current drawing.',
      unavailable: 'The current drawing could not be read. Reload it and try again.',
      malformed: 'The saved panel data cannot be read. Reload the drawing before creating strings.',
      representation: 'This saved panel layout is not supported by this string form.',
      noPanels: 'This drawing has no panels available for stringing.',
      empty: 'Choose at least one panel.',
      range: 'Enter whole slot numbers within this row.',
      missing: 'A selected panel is no longer available. Reload the drawing and choose again.',
      duplicate: 'A panel appears more than once. Remove the repeated selection.',
      assigned: 'A selected panel already belongs to a string. Choose unassigned panels.',
      singleLimit: 'Choose at most 4096 panels for one string.',
      multiLimit: 'Choose at most 900 panels for this run.',
      length: 'Enter a whole string length from 1 to 900.',
      notSized: 'Set a positive string length in Solar settings or complete string sizing first.',
      tooLong: 'The requested string length exceeds the saved limit.',
      infeasible: 'This panel count cannot form strings at the requested length. Change the count or length.',
      units: 'Resolve the drawing units before creating strings.',
      numberExhausted: 'The drawing has no available string numbers. Change the starting string number before trying again.',
      request: 'These string inputs cannot be submitted. Review the selection and try again.',
      stale: 'The drawing changed. Reload it and choose the panels again.',
      checking: 'Checking the current drawing before review.',
      pending: 'This step is running. Confirm or wait for it to finish.',
      failed: 'Strings were not created. Your inputs are kept.',
      finished: 'Strings created.',
      cancelled: 'The run was cancelled. Your inputs are kept.',
    })
    expect(Object.keys(SOLAR_STRING_COMPOSER_REASONS).sort()).toEqual([
      'scope', 'loading', 'unavailable', 'malformed', 'representation', 'noPanels',
      'empty', 'range', 'missing', 'duplicate', 'assigned', 'singleLimit', 'multiLimit',
      'length', 'notSized', 'tooLong', 'infeasible', 'units', 'numberExhausted', 'request',
      'stale', 'checking', 'pending', 'failed', 'finished', 'cancelled',
    ].sort())
    for (const value of Object.values(SOLAR_STRING_COMPOSER_REASONS)) {
      expect(typeof value).toBe('string')
      expect(value.length).toBeGreaterThanOrEqual(12)
      expect(value.includes(String.fromCharCode(0x2013))).toBe(false)
      expect(value.includes(String.fromCharCode(0x2014))).toBe(false)
      for (const code of [...serverMappings.map(([code]) => code), ...malformedCodes]) {
        expect(value.includes(code)).toBe(false)
      }
    }
    expect([MAX_SINGLE_REFS, MAX_MULTI_REFS, MAX_STRING_LENGTH, MAX_GRAPH_REV, MAX_COMBO_PANELS])
      .toEqual([4096, 900, 900, 2147483647, 90000])
  })
  it('SC36 combo grid matches the server digest and named cells', () => {
    const hash = createHash('sha256')
    let infeasible = 0
    for (let target = 1; target <= 900; target += 1) {
      let chunk = `${target}|`
      for (let count = 1; count <= 900; count += 1) {
        const sizes = stringComboSizes(target, count)
        if (sizes === null) {
          chunk += `${count}:x;`
          infeasible += 1
        } else {
          const sequence = stringComboSequences(target, count)
          expect(sizes).toEqual([
            ...Array(sequence[2]).fill(sequence[0]), ...Array(sequence[3]).fill(sequence[1]),
          ])
          expect(sizes.every((size) => size >= 1 && size <= target)).toBe(true)
          expect(sizes.reduce((sum, size) => sum + size, 0)).toBe(count)
          chunk += `${count}:${sequence[0]},${sequence[1]},${sequence[2]},${sequence[3]};`
        }
      }
      hash.update(chunk, 'ascii')
    }
    expect(hash.digest('hex')).toBe('32a65ae6293911ed2fd50c4e8548a117f08901e1e1879fbc2b097129e3fec96e')
    expect(infeasible).toBe(756670)
    for (const [target, count, sequence] of [
      [1, 1, [1, 0, 1, 0]], [1, 2, [1, 0, 2, 0]], [1, 27, [1, 0, 27, 0]],
      [2, 1, [2, 1, 0, 1]], [2, 27, [2, 1, 13, 1]], [3, 5, [3, 2, 1, 1]],
      [3, 27, [3, 2, 9, 0]], [4, 27, [4, 3, 6, 1]], [5, 27, [5, 4, 3, 3]],
      [13, 27, null], [14, 1, null], [14, 11, null], [14, 12, [13, 12, 0, 1]],
      [14, 13, [14, 13, 0, 1]], [14, 14, [14, 13, 1, 0]], [14, 27, [14, 13, 1, 1]],
      [14, 28, [14, 13, 2, 0]],
      [14, 900, [14, 13, 55, 10]], [899, 900, null], [900, 1, null],
      [900, 899, [900, 899, 0, 1]], [900, 900, [900, 899, 1, 0]],
    ]) {
      if (sequence === null) expect(stringComboSizes(target, count)).toBeNull()
      else {
        expect(stringComboSequences(target, count)).toEqual(sequence)
        expect(stringComboSizes(target, count)).toEqual([
          ...Array(sequence[2]).fill(sequence[0]), ...Array(sequence[3]).fill(sequence[1]),
        ])
      }
    }
    expect(stringComboSizes(14, 28)).toEqual([14, 14])
    // 810,000 cells measured at 5.0 to 5.6 s on the workstation, over vitest's 5 s default;
    // the CI runner is slower still, so this row carries its own budget.
  }, 60_000)
  it('SC37 combo bounds, empty counts, sentinel and fresh arrays', () => {
    for (const [target, count] of [[0, 5], [901, 5], [1.5, 5], [true, 5],
      [5, -1], [5, 90001], [5, 1.5]]) {
      expect(stringComboSequences(target, count)).toBeNull()
      expect(stringComboSizes(target, count)).toBeNull()
    }
    expect(stringComboSequences(5, 0)).toEqual([5, 4, 0, 0])
    expect(stringComboSizes(5, 0)).toEqual([])
    expect(stringComboSequences(14, 11)).toEqual([0, 0, 0, 0])
    expect(stringComboSizes(14, 11)).toBeNull()
    const sequence = stringComboSequences(14, 27)
    const sizes = stringComboSizes(14, 27)
    sequence[0] = 7
    sizes.push(7)
    expect(stringComboSequences(14, 27)).toEqual([14, 13, 1, 1])
    expect(stringComboSizes(14, 27)).toEqual([14, 13])
    expect(stringComboSizes(5, 0)).not.toBe(stringComboSizes(5, 0))
  })
})
