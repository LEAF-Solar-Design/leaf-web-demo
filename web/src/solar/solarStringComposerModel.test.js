import { describe, expect, it, vi } from 'vitest'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import { createRunSubmissionRequest } from '../runIntent.js'
import { createSolarProjectClient } from './solarProjectClient.js'
import ground from './__fixtures__/stringComposerGround.json'
import rooftop from './__fixtures__/stringComposerRooftop.json'
import { decodeGroundFrameSlots, GROUND_SLOT_CODEC } from './solarGroundSlots.js'
import {
  SOLAR_STRING_COMPOSER_REASONS, STRING_ADD_TOOL, STRING_MULTI_ADD_TOOL,
  MAX_SINGLE_REFS, MAX_MULTI_REFS, MAX_STRING_LENGTH, MAX_GRAPH_REV, MAX_COMBO_PANELS,
  stringComboSequences, stringComboSizes, savedStringLimit, stringComposerView,
  composeStringRequest, checkStringFreshness, stringRunRefusal,
  PROJECT_STRING_COMPOSER_REASONS, projectStringComposerView, composeProjectStringRequest,
  checkProjectStringFreshness, projectStringPreviewResult,
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

const projectBinding = Object.freeze({ kind: 'project',
  organization_id: '00000000-0000-4000-8000-000000000001',
  project_id: '00000000-0000-4000-8000-000000000002',
  drawing_id: '00000000-0000-4000-8000-000000000003',
  input_version_id: '00000000-0000-4000-8000-000000000004' })
const otherVersion = '00000000-0000-4000-8000-000000000005'
const digest = 'sha256:' + 'a'.repeat(64)
const contextView = (graph = ground, edits = {}) => ({
  context: { ...projectBinding, head_version_id: projectBinding.input_version_id,
    is_head: true, intake_sha256: 'b'.repeat(64), ...edits },
  intake: { solar_design_graph: structuredClone(graph), solar_design_graph_sha256: 'c'.repeat(64) },
})
const projectView = (graph = ground) => projectStringComposerView({ binding: projectBinding, contextView: contextView(graph) })
const projectCompose = (queue = [range(0, 1, 3)], graph = ground, multi = false, stringLength = 3, overrides = {}) =>
  composeProjectStringRequest({ binding: projectBinding, view: projectView(graph),
    toolName: multi ? STRING_MULTI_ADD_TOOL : STRING_ADD_TOOL, queue, stringLength,
    catalogDigest: digest, idempotencyKey: 'strings-1', ...overrides })
const projectRejected = (result, code) => {
  expect(result).toEqual({ ok: false, code, reason: PROJECT_STRING_COMPOSER_REASONS[code] })
  expect(Object.isFrozen(result)).toBe(true)
}
const capabilityRow = (tool = STRING_MULTI_ADD_TOOL) => ({ name: tool, catalog_digest: digest,
  availability: { admission_checked: true, admission: { status_code: 200, error: null },
    entitled: true, engine_ready: true, implemented: true, input_ready: true, runnable: true, refusal_reasons: [] } })
const catalogOutcome = (rows = [capabilityRow()]) => ({ ok: true, status: 200,
  data: { families: [{ capabilities: rows }] } })
const previewIntent = { tool: STRING_MULTI_ADD_TOOL, catalogDigest: digest }

describe('project Solar string composer', () => {
  it('R10-01 Legacy API refuses canonical scope with the exact legacy sentence', () => {
    expect(stringComposerView({ envelope: contextView(), drawingId: projectBinding.drawing_id,
      drawingVersion: projectBinding.input_version_id, projectId: projectBinding.project_id }))
      .toEqual({ ok: false, code: 'scope', reason: 'Open a saved standalone drawing before creating strings.' })
  })
  it('R10-02 Ground projection preserves canonical identity and freezes public containers', () => {
    const view = projectView()
    expect(view).toEqual({ ok: true, binding: projectBinding, rev: 2,
      intakeSha256: 'b'.repeat(64), graphSha256: 'c'.repeat(64), design: 'Ground', savedLimit: 27,
      rows: [{ frameIndex: 0, frameId: ground.frames[0].id, name: 'Group 1', slotCount: 3, panelIds: P.slice(0, 3) },
        { frameIndex: 1, frameId: ground.frames[1].id, name: 'Group 2', slotCount: 2, panelIds: P.slice(3) }],
      panels: [], assignedCount: 0 })
    for (const value of [view, view.binding, view.rows, view.panels, ...view.rows, ...view.rows.map((row) => row.panelIds)]) {
      expect(Object.isFrozen(value)).toBe(true)
    }
    expect(view.binding.project_id).not.toBe(ground.project.id)
  })
  it('R10-03 Roof projection preserves explicit panels and saved limit', () => {
    const view = projectView(rooftop)
    expect([view.rev, view.savedLimit, view.panels.length, view.assignedCount]).toEqual([0, 14, 30, 3])
    expect(view.panels.map((item) => item.id)).toEqual(rooftop.panels.map((item) => item.id))
    expect(view.panels[3].frameName).toBe('Multi group')
    expect(view.rows).toEqual([])
    expect(view.panels.every(Object.isFrozen)).toBe(true)
  })
  it('R10-04 Closed canonical bindings reject missing extra accessor and integer version fields', () => {
    const getter = vi.fn(() => projectBinding.input_version_id)
    const accessor = { ...projectBinding }
    Object.defineProperty(accessor, 'input_version_id', { get: getter })
    const missing = { ...projectBinding }; delete missing.drawing_id
    for (const binding of [null, undefined, missing, { ...projectBinding, extra: true },
      { ...projectBinding, input_version_id: 7 }, accessor]) {
      projectRejected(projectStringComposerView({ binding, contextView: contextView() }), 'binding_invalid')
    }
    expect(getter).not.toHaveBeenCalled()
  })
  it('R10-05 Context identity and head consistency are checked before digests', () => {
    for (const field of ['organization_id', 'project_id', 'drawing_id', 'input_version_id']) {
      projectRejected(projectStringComposerView({ binding: projectBinding,
        contextView: contextView(ground, { [field]: otherVersion }) }), 'context_unreadable')
    }
    for (const edits of [{ head_version_id: 7 }, { head_version_id: otherVersion },
      { is_head: false }, { is_head: 1 }, { intake_sha256: 'A'.repeat(64) }]) {
      projectRejected(projectStringComposerView({ binding: projectBinding, contextView: contextView(ground, edits) }), 'context_unreadable')
    }
    for (const data of [null, {}, { context: contextView().context, intake: [] }]) {
      projectRejected(projectStringComposerView({ binding: projectBinding, contextView: data }), 'context_unreadable')
    }
  })
  it('R10-06 A consistent historical context refuses before malformed digests', () => {
    projectRejected(projectStringComposerView({ binding: projectBinding,
      contextView: contextView(ground, { head_version_id: otherVersion, is_head: false, intake_sha256: null }) }), 'not_current_head')
  })
  it('R10-07 Missing graph pair and malformed companion pairs refuse distinctly', () => {
    const data = contextView()
    data.intake = {}
    projectRejected(projectStringComposerView({ binding: projectBinding, contextView: data }), 'noPanels')
    for (const intake of [{ solar_design_graph: ground }, { solar_design_graph_sha256: 'c'.repeat(64) },
      { solar_design_graph: null, solar_design_graph_sha256: 'c'.repeat(64) },
      { solar_design_graph: [], solar_design_graph_sha256: 'c'.repeat(64) },
      { solar_design_graph: ground, solar_design_graph_sha256: 'C'.repeat(64) },
      { solar_design_graph: { ...ground, rev: MAX_GRAPH_REV + 1 }, solar_design_graph_sha256: 'c'.repeat(64) }]) {
      projectRejected(projectStringComposerView({ binding: projectBinding, contextView: { ...contextView(), intake } }), 'malformed')
    }
    projectRejected(projectView(changed(ground, (g) => { g.panels = [{}] })), 'representation')
    projectRejected(projectView(changed(ground, (g) => { g.frames[0].ground_slots.count = 4 })), 'malformed')
  })
  it('R10-08 Ground single includes D and revision with independently frozen ordered refs', () => {
    const result = projectCompose()
    expect(result.request).toEqual({ tool: STRING_ADD_TOOL, params: { drawing_id: projectBinding.drawing_id,
      operation: 'add-string', expected_rev: 2, ordered_panel_refs: P.slice(0, 3) },
    catalogDigest: digest, idempotencyKey: 'strings-1' })
    expect(result.preview).toEqual({ ids: P.slice(0, 3), sizes: [3] })
    expect(result.preview.ids).not.toBe(result.request.params.ordered_panel_refs)
    for (const value of [result, result.request, result.request.params, result.request.params.ordered_panel_refs,
      result.preview, result.preview.ids, result.preview.sizes]) expect(Object.isFrozen(value)).toBe(true)
  })
  it('R10-09 Ground multi prepares one canonical request with all chunks', () => {
    const result = projectCompose([range(0, 1, 3), range(1, 1, 2)], ground, true)
    expect(result.request.params).toEqual({ drawing_id: projectBinding.drawing_id,
      operation: 'add-strings', expected_rev: 2, string_length: 3, ordered_panel_refs: P })
    expect(result.preview).toEqual({ ids: P, sizes: [3, 2] })
    const client = createSolarProjectClient({ fetchImpl: vi.fn() })
    client.setScope(projectBinding, 'session')
    const intent = client.prepareRun(result.request)
    expect(intent.params).toEqual(result.request.params)
    expect(intent.binding).toEqual(projectBinding)
    expect(intent.authorityKey).toBe('session')
    client.dispose()
  })
  it('R10-10 Server source registers canonical multi creation and requires exact ordinary admission', () => {
    const source = fs.readFileSync(process.cwd() + '/../server/solar_project_graph.py', 'utf8')
    const supported = source.match(new RegExp('SUPPORTED_TOOLS = frozenset\\(\\(([\\s\\S]*?)\\)\\)'))[1]
    expect(supported).toContain('"solar-string-multi-add"')
    expect(source).toContain('CREATION_TOOLS = frozenset((STRING_TOOL, "solar-assign-equipment", "solar-string-multi-add"))')
    const admission = fs.readFileSync(process.cwd() + '/../server/solar_project_admission.py', 'utf8')
    expect(admission).toContain('reason = "project_request_required"')
    const declaration = JSON.parse(fs.readFileSync(process.cwd() + '/../server/solar_tools/solar_string_multi_add.json', 'utf8'))
    expect(declaration.adapter).toBe('local-graph-commit')
    expect(declaration.seedable).toBe(false)
    expect(declaration.requires_persisted_graph).toBe(true)
    const ordinary = capabilityRow()
    ordinary.availability.admission_checked = false
    ordinary.availability.refusal_reasons = ['project_request_required']
    projectRejected(projectStringPreviewResult({ outcome: catalogOutcome([ordinary]), intent: previewIntent }), 'preview_refused')
  })
  it('R10-13 Descending ranges and reversed queue retain electrical ordering', () => {
    expect(projectCompose([range(0, 3, 1)]).preview.ids).toEqual(P.slice(0, 3).reverse())
    expect(projectCompose([range(1, 2, 1), range(0, 3, 1)], ground, true).preview.ids).toEqual(P.slice().reverse())
  })
  it('R10-14 Project queue refusals retain the legacy validation precedence', () => {
    const cases = [
      [[], ground, false, 3, 'empty'], [[range(), range(), range(7)], ground, false, 3, 'missing'],
      [[range(0, 1, 3), range()], capped(2), false, 3, 'duplicate'],
      [[range(0, 1, 3)], capped(2, P.slice(0, 3)), false, 3, 'tooLong'],
      [[range()], capped(0, [P[0]]), false, 3, 'notSized'],
      [[], ground, true, 0, 'length'],
      [[panel(Q[0]), panel(rooftop.panels[0].id)], rooftop, true, 14, 'assigned'],
      [[panel(Q[0])], rooftop, true, 14, 'infeasible'],
      [[range(0, 1, 4)], ground, false, 3, 'range'],
    ]
    for (const [queue, graph, multi, length, code] of cases) projectRejected(projectCompose(queue, graph, multi, length), code)
  })
  it('R10-15 Canonical count and length boundaries match the declarations', () => {
    const graph = changed(ground, (g) => {
      g.frames = [{ ...g.frames[0], ground_slots: block(5000) }]
      g.settings.panels_in_sequence = 4096
    })
    expect(projectCompose([range(0, 1, 4096)], graph).request.params.ordered_panel_refs).toHaveLength(4096)
    projectRejected(projectCompose([range(0, 1, 4097)], graph), 'singleLimit')
    expect(projectCompose([range(0, 1, 900)], graph, true, 900).preview.sizes).toEqual([900])
    projectRejected(projectCompose([range(0, 1, 901)], graph, true, 900), 'multiLimit')
    projectRejected(projectCompose([range()], graph, true, 901), 'length')
  })
  it('R10-16 Invalid digest and unsafe key refuse before any prepared preview', () => {
    for (const catalogDigest of [undefined, '', 'a'.repeat(64), 'sha256:' + 'A'.repeat(64)]) {
      projectRejected(projectCompose(undefined, ground, false, 3, { catalogDigest }), 'catalog_digest_required')
    }
    for (const idempotencyKey of [undefined, '', ' ', 'x'.repeat(257), 'é'.repeat(129), 'prefix-lco1.' + 'a'.repeat(64)]) {
      projectRejected(projectCompose(undefined, ground, false, 3, { idempotencyKey }), 'idempotency_key_required')
    }
    projectRejected(projectCompose([], ground, false, 3, { catalogDigest: '' }), 'empty')
    projectRejected(projectCompose(undefined, ground, false, 3, { binding: { ...projectBinding, input_version_id: otherVersion } }), 'scope_stale')
  })
  it('R10-17 Exact admitted rows and identical duplicates are accepted', () => {
    for (const rows of [[capabilityRow()], [capabilityRow(), capabilityRow()]]) {
      const result = projectStringPreviewResult({ outcome: catalogOutcome(rows), intent: previewIntent })
      expect(result).toEqual({ ok: true })
      expect(Object.isFrozen(result)).toBe(true)
    }
  })
  it('R10-18 Missing unchecked malformed and contradictory rows cannot arm a preview', () => {
    const edits = [
      (r) => { r.catalog_digest = 'sha256:' + 'd'.repeat(64) },
      (r) => { r.availability = null },
      (r) => { r.availability.admission_checked = false },
      (r) => { r.availability.admission.status_code = 202 },
      (r) => { r.availability.admission.error = {} },
      (r) => { r.availability.refusal_reasons = null },
      (r) => { r.availability.refusal_reasons = ['project_request_required'] },
      ...['entitled', 'engine_ready', 'implemented', 'input_ready', 'runnable'].map((key) =>
        (r) => { r.availability[key] = false }),
    ]
    for (const edit of edits) {
      const bad = capabilityRow(); edit(bad)
      for (const rows of [[bad], [capabilityRow(), bad]]) {
        projectRejected(projectStringPreviewResult({ outcome: catalogOutcome(rows), intent: previewIntent }), 'preview_refused')
      }
    }
    for (const outcome of [catalogOutcome([]), { ok: true, status: 200, data: {} },
      { ok: true, status: 200, data: { families: [] } }, catalogOutcome([capabilityRow(STRING_ADD_TOOL)])]) {
      projectRejected(projectStringPreviewResult({ outcome, intent: previewIntent }), 'preview_refused')
    }
  })
  it('R10-19 Known nested refusals use fixed semantics and unknown evidence stays unchanged', () => {
    for (const [code, expected] of [...serverMappings, ...malformedCodes.map((code) => [code, 'malformed']),
      ['SIP_R1_STALE_VERSION', 'not_current_head'], ['SIP_R7_SCOPE_STALE', 'scope_stale'],
      ['project_adapter_unavailable', 'adapter_unavailable']]) {
      const row = capabilityRow()
      row.availability.admission = { status_code: 409, error: { reason_code: code, message: 'never render me' } }
      projectRejected(projectStringPreviewResult({ outcome: catalogOutcome([row]), intent: previewIntent }), expected)
    }
    const outcome = { ok: false, status: 409, reason: 'HTTP_ERROR', envelope: { error: { message: 'private details' } } }
    const before = structuredClone(outcome)
    projectRejected(projectStringPreviewResult({ outcome, intent: previewIntent }), 'preview_refused')
    expect(outcome).toEqual(before)
  })
  it('R10-20 Freshness checks version head revision and digests without decoding or retargeting', () => {
    decodeGroundFrameSlots.mockClear()
    const view = projectView()
    const request = projectCompose()
    decodeGroundFrameSlots.mockClear()
    const check = (data, binding = projectBinding) => checkProjectStringFreshness({ binding, view, contextView: data })
    expect(check(contextView())).toEqual({ ok: true })
    expect(Object.isFrozen(check(contextView()))).toBe(true)
    projectRejected(check(contextView(), { ...projectBinding, input_version_id: otherVersion }), 'scope_stale')
    projectRejected(check(contextView(ground, { input_version_id: otherVersion })), 'context_unreadable')
    projectRejected(check(contextView(ground, { head_version_id: otherVersion, is_head: false })), 'not_current_head')
    projectRejected(check(contextView(ground, { head_version_id: otherVersion })), 'context_unreadable')
    projectRejected(check(contextView(ground, { intake_sha256: 'd'.repeat(64) })), 'scope_stale')
    const data = contextView(); data.intake.solar_design_graph_sha256 = 'e'.repeat(64)
    projectRejected(check(data), 'scope_stale')
    const revised = contextView(); revised.intake.solar_design_graph.rev += 1
    projectRejected(check(revised), 'scope_stale')
    const malformed = contextView(); malformed.intake.solar_design_graph = null
    projectRejected(check(malformed), 'malformed')
    expect(decodeGroundFrameSlots).not.toHaveBeenCalled()
    expect(request.request.params.expected_rev).toBe(2)
    expect(view.binding.input_version_id).toBe(projectBinding.input_version_id)
  })
  it('R10-32 R7 snapshots each getter once and freezes responses and prepared params', async () => {
    const getter = vi.fn(() => contextView().intake)
    const data = { context: contextView().context, get intake() { return getter() } }
    const client = createSolarProjectClient({ fetchImpl: vi.fn(async () => ({ ok: true, status: 200,
      json: async () => data })) })
    client.setScope(projectBinding, 'session')
    const outcome = await client.readContext()
    expect(outcome.ok).toBe(true)
    expect(getter).toHaveBeenCalledTimes(1)
    expect(Object.isFrozen(outcome.data.intake.solar_design_graph.frames[0])).toBe(true)
    const intent = client.prepareRun(projectCompose().request)
    expect(Object.isFrozen(intent.params)).toBe(true)
    expect(Object.isFrozen(intent.params.ordered_panel_refs)).toBe(true)
    client.dispose()
  })
  it('R10-34 Eight standalone results match the independently frozen byte oracle', () => {
    const oracle = JSON.parse(fs.readFileSync(process.cwd() + '/src/solar/__fixtures__/solarStringStandalone.main-f139ceba.json', 'utf8'))
    expect(oracle.source.commit).toBe('f139ceba6daea69b6e1570e3dc6c808712328fb9')
    const captures = {
      single: compose([range(0, 1, 3)]),
      multi: compose([range(0, 1, 3), range(1, 1, 2)], ground, true),
      descending: compose([range(0, 3, 1)]),
      roof: compose(rooftop.panels.slice(3, 6).map((p) => panel(p.id)), rooftop, true),
      empty: compose([]), duplicate: compose([range(), range()]),
    }
    captures.singleWire = createRunSubmissionRequest(STRING_ADD_TOOL, captures.single.params, 'd1', { dwgVersion: 7 })
    captures.multiWire = createRunSubmissionRequest(STRING_MULTI_ADD_TOOL, captures.multi.params, 'd1', { dwgVersion: 7 })
    expect(Object.keys(captures)).toEqual(Object.keys(oracle.captures))
    for (const [name, value] of Object.entries(captures)) {
      const json = JSON.stringify(value)
      expect(json, name).toBe(oracle.captures[name].json)
      expect(Buffer.byteLength(json), name).toBe(oracle.captures[name].bytes)
      expect(createHash('sha256').update(json).digest('hex'), name).toBe(oracle.captures[name].sha256)
    }
  })
  it('R10-35 Standalone params and independent preview IDs retain their measured mutability', () => {
    for (const multi of [false, true]) {
      const result = compose([range(0, 1, 3)], ground, multi)
      expect(Object.isFrozen(result)).toBe(true)
      expect(Object.isFrozen(result.params)).toBe(false)
      expect(Object.isFrozen(result.preview.ids)).toBe(false)
      expect(Object.isFrozen(result.preview.sizes)).toBe(true)
      result.params.operation = 'changed'
      result.params.ordered_panel_refs.push('extra')
      result.preview.ids.reverse()
      expect(compose([range(0, 1, 3)], ground, multi).preview.ids).toEqual(P.slice(0, 3))
    }
  })
  it('R10-37 Inherited canonical binding fields are refused', () => {
    projectRejected(projectStringComposerView({ binding: Object.create(projectBinding),
      contextView: contextView() }), 'binding_invalid')
  })
  it('R10-38 Both context digests require exactly 64 hex characters', () => {
    const shortIntake = contextView()
    shortIntake.context.intake_sha256 = 'b'.repeat(63)
    projectRejected(projectStringComposerView({ binding: projectBinding,
      contextView: shortIntake }), 'context_unreadable')
    const shortGraph = contextView()
    shortGraph.intake.solar_design_graph_sha256 = 'c'.repeat(63)
    projectRejected(projectStringComposerView({ binding: projectBinding,
      contextView: shortGraph }), 'malformed')
  })
  it('R10-39 A 256-byte idempotency key is accepted unchanged', () => {
    const idempotencyKey = 'x'.repeat(256)
    const result = projectCompose(undefined, ground, false, 3, { idempotencyKey })
    expect(result.ok).toBe(true)
    expect(result.request.idempotencyKey).toBe(idempotencyKey)
  })
  it('R10-40 A failed catalog outcome cannot admit accepted rows', () => {
    projectRejected(projectStringPreviewResult({ outcome: { ...catalogOutcome(), ok: false },
      intent: previewIntent }), 'preview_refused')
  })
  it('R10-41 An unknown server code keeps generic preview refusal', () => {
    projectRejected(projectStringPreviewResult({ outcome: { ok: false, status: 409, reason: 'NEW_SERVER_CODE' },
      intent: previewIntent }), 'preview_refused')
  })
  it('R10-48 Standalone revision getter retains the base result bytes', () => {
    const graph = structuredClone(ground)
    let reads = 0
    Object.defineProperty(graph, 'rev', { enumerable: true, get: () => ++reads })
    const view = makeView(graph)
    const result = composeStringRequest({ view, toolName: STRING_ADD_TOOL, queue: [range(0, 1, 3)] })
    expect(reads).toBe(4)
    expect(view.binding.rev).toBe(4)
    expect(JSON.stringify(result)).toBe(JSON.stringify({
      ok: true,
      params: {
        operation: 'add-string',
        expected_rev: 4,
        ordered_panel_refs: P.slice(0, 3)
      },
      preview: {
        ids: P.slice(0, 3),
        sizes: [3]
      }
    }))
  })
  it('R10-49 Standalone revision getter reading NaN or undefined keeps the base result bytes', () => {
    for (const values of [[1, NaN, 2, 4], [1, 0, NaN, 4], [1, undefined, 2, 4], [1, 0, undefined, 4]]) {
      const graph = structuredClone(ground)
      let reads = 0
      Object.defineProperty(graph, 'rev', { enumerable: true, get: () => values[reads++] })
      const view = makeView(graph)
      const result = composeStringRequest({ view, toolName: STRING_ADD_TOOL, queue: [range(0, 1, 3)] })
      expect(reads).toBe(4)
      expect(view.binding.rev).toBe(4)
      expect(JSON.stringify(result)).toBe(JSON.stringify({
        ok: true,
        params: {
          operation: 'add-string',
          expected_rev: 4,
          ordered_panel_refs: P.slice(0, 3)
        },
        preview: {
          ids: P.slice(0, 3),
          sizes: [3]
        }
      }))
    }
  })
  it('R10-36 Project reason map has exactly the specified frozen product sentences', () => {
    const expectedMap = {
      binding_invalid: 'Open a project drawing version before creating strings.',
      context_unreadable: 'This project drawing version could not be read. Reload it and try again.',
      not_current_head: 'This is an older drawing version. Open the current version before creating strings.',
      scope_stale: 'The drawing or session changed. Reload it and choose the panels again.',
      catalog_digest_required: 'Refresh the Solar tools before creating strings.',
      idempotency_key_required: 'This string request could not be prepared. Try again.',
      preview_required: 'Checking whether these strings can be created in this drawing.',
      preview_refused: 'These strings cannot be created. Review the refusal and try again.',
      adapter_unavailable: 'This string tool is not available for project drawings.',
      submit_unavailable: 'Project string submission is not available in this view.',
      request_invalid: 'This string request could not be prepared. Review the inputs and try again.',
      submission_uncertain: 'The submission result is unknown. Retry the same request to check it.',
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
      submitted: 'String creation was submitted.',
    }
    expect(PROJECT_STRING_COMPOSER_REASONS).toEqual(expectedMap)
    expect(Object.isFrozen(PROJECT_STRING_COMPOSER_REASONS)).toBe(true)
  })
})
