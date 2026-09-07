// W4g-7b-03c: colour, linetype and lineweight, end to end in the browser
// (never the crate): the three builders, MATCHPROP's batch, the diff's
// lowering and the Properties panel's census. Mostly pure rows; a few
// (W4g-7b-03c-g) render the real combos against a fake worker.
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { MODIFY_REASONS, forGroup, propertyReason } from '../lib/actionRegistry.js'
import { parseDrawingCommand } from '../lib/commandWords.js'
import DraftingRibbon from '../site/DraftingRibbon.jsx'
import EngineRibbonClusters from './EngineRibbonClusters.jsx'
import EngineSessionProvider, { useEngineSessionContext } from './EngineSessionProvider.jsx'
import { buildEditPayload, planMatchprop } from './engineSession.js'
import { diffPlan } from './mutationDiff.js'

afterEach(() => cleanup())

const CATALOGUE = ['ByLayer', 'ByBlock', 'Continuous', 'HIDDEN', 'DASHED']

describe('the three property builders (buildEditPayload)', () => {
  it('parses colour: ByLayer, ByBlock, a standard name, a bare index, and refuses out of range or unknown text', () => {
    expect(buildEditPayload('setColor', 's1', { aci: 'ByLayer' }).payload.aci).toBe(256)
    expect(buildEditPayload('setColor', 's1', { aci: 'ByBlock' }).payload.aci).toBe(0)
    expect(buildEditPayload('setColor', 's1', { aci: 'red' }).payload.aci).toBe(1)
    expect(buildEditPayload('setColor', 's1', { aci: 7 }).payload.aci).toBe(7)
    expect(buildEditPayload('setColor', 's1', { aci: 257 }).refusal).toBe('Property refused: a colour index is 1 to 255, ByLayer or ByBlock')
    expect(buildEditPayload('setColor', 's1', { aci: 'x' }).refusal).toBe('Property refused: a colour index is 1 to 255, ByLayer or ByBlock')
  })

  it('parses linetype by the catalogue, case-insensitively, storing the catalogue spelling; refuses one absent from it', () => {
    expect(buildEditPayload('setLinetype', 's1', { linetype: 'hidden' }, CATALOGUE).payload.linetype).toBe('HIDDEN')
    expect(buildEditPayload('setLinetype', 's1', { linetype: 'HIDDEN' }, CATALOGUE).payload.linetype).toBe('HIDDEN')
    expect(buildEditPayload('setLinetype', 's1', { linetype: 'Nope' }, CATALOGUE).refusal)
      .toBe('Property refused: linetype Nope is not loaded in this drawing')
  })

  it('parses lineweight: a standard mm value, ByLayer/Default, and refuses an off-grid one', () => {
    expect(buildEditPayload('setLineweight', 's1', { lineweight: 25 }).payload.lineweight).toBe(25)
    expect(buildEditPayload('setLineweight', 's1', { lineweight: 26 }).refusal)
      .toBe('Property refused: lineweight must be a standard value in millimetres, ByLayer, ByBlock or Default')
    expect(buildEditPayload('setLineweight', 's1', { lineweight: 'ByLayer' }).payload.lineweight).toBe(-1)
    expect(buildEditPayload('setLineweight', 's1', { lineweight: 'Default' }).payload.lineweight).toBe(-3)
  })
})

describe('planMatchprop: one batch of the layer plus only the properties that differ', () => {
  const source = { id: 's1', layer: 'A', aci: 1, linetype: 'HIDDEN', lineweight: 25, editable: true }

  it('copies the layer and all three properties as one batch when every one differs', () => {
    const dest = { id: 'd1', layer: 'B', aci: 256, linetype: 'ByLayer', lineweight: -1, editable: true }
    const session = { entities: [source, dest], selectedId: 's1' }
    expect(planMatchprop(session, { edge: 'd1' })).toEqual({
      steps: [
        { op: 'setLayer', entityId: 'd1', layer: 'A' },
        { op: 'setColor', entityId: 'd1', aci: 1 },
        { op: 'setLinetype', entityId: 'd1', linetype: 'HIDDEN' },
        { op: 'setLineweight', entityId: 'd1', lineweight: 25 },
      ],
    })
  })

  it('carries only the properties that actually differ', () => {
    const dest = { id: 'd2', layer: 'A', aci: 1, linetype: 'ByLayer', lineweight: 25, editable: true }
    const session = { entities: [source, dest], selectedId: 's1' }
    expect(planMatchprop(session, { edge: 'd2' })).toEqual({
      steps: [{ op: 'setLinetype', entityId: 'd2', linetype: 'HIDDEN' }],
    })
  })

  it('refuses a destination that is the selection itself', () => {
    const session = { entities: [source], selectedId: 's1' }
    expect(planMatchprop(session, { edge: 's1' }))
      .toEqual({ refusal: 'Match refused: the destination must be a different entity from the selection.' })
  })

  // W4g-7b-03c-f (kimi, #1121 point 6): a property is not geometry, so an
  // INSERT reference is a matchable source AND destination; a non-INSERT
  // read-only kind still refuses by name.
  it('accepts an INSERT reference as the source and as the destination', () => {
    const insertSource = { id: 'ins', layer: 'A', aci: 1, linetype: 'HIDDEN', lineweight: 25, editable: false, type: 'INSERT' }
    const dest = { id: 'd1', layer: 'B', aci: 256, linetype: 'ByLayer', lineweight: -1, editable: true }
    expect(planMatchprop({ entities: [insertSource, dest], selectedId: 'ins' }, { edge: 'd1' })).toEqual({
      steps: [
        { op: 'setLayer', entityId: 'd1', layer: 'A' },
        { op: 'setColor', entityId: 'd1', aci: 1 },
        { op: 'setLinetype', entityId: 'd1', linetype: 'HIDDEN' },
        { op: 'setLineweight', entityId: 'd1', lineweight: 25 },
      ],
    })
    const insertDest = { id: 'insd', layer: 'B', aci: 256, linetype: 'ByLayer', lineweight: -1, editable: false, type: 'INSERT' }
    expect(planMatchprop({ entities: [source, insertDest], selectedId: 's1' }, { edge: 'insd' })).toEqual({
      steps: [
        { op: 'setLayer', entityId: 'insd', layer: 'A' },
        { op: 'setColor', entityId: 'insd', aci: 1 },
        { op: 'setLinetype', entityId: 'insd', linetype: 'HIDDEN' },
        { op: 'setLineweight', entityId: 'insd', lineweight: 25 },
      ],
    })
  })

  it('still refuses a non-INSERT read-only destination by name', () => {
    const roDim = { id: 'dim', layer: 'B', aci: 256, linetype: 'ByLayer', lineweight: -1, editable: false, type: 'DIMENSION' }
    expect(planMatchprop({ entities: [source, roDim], selectedId: 's1' }, { edge: 'dim' }))
      .toEqual({ refusal: 'Match refused: the destination object is read-only in the browser engine.' })
  })

  // W4g-7b-03c-g F6: an ACI-only compare misses a true-coloured destination
  // whose NEAREST index already equals the source's ACI (everything else
  // equal too), so the batch refused "nothing to match" and the destination
  // kept its 420. A destination that still carries a true colour always gets
  // its own setColor step, which is what clears it.
  it('still copies colour onto a true-coloured destination whose nearest index already matches', () => {
    const plainSource = { id: 's2', layer: 'A', aci: 1, linetype: 'ByLayer', lineweight: -1, editable: true }
    const trueColourDest = { id: 'd3', layer: 'A', aci: 1, trueColor: [10, 20, 30], linetype: 'ByLayer', lineweight: -1, editable: true }
    expect(planMatchprop({ entities: [plainSource, trueColourDest], selectedId: 's2' }, { edge: 'd3' })).toEqual({
      steps: [{ op: 'setColor', entityId: 'd3', aci: 1 }],
    })
  })
})

describe('propertyReason: the Properties panel\'s own ladder (W4g-7b-03c-f)', () => {
  it('agrees with modifyReason on every rung except the one an INSERT reference waives', () => {
    expect(propertyReason(null)).toBe(MODIFY_REASONS.noDocument)
    expect(propertyReason({ errorKind: 'crashed' })).toBe(MODIFY_REASONS.crashed)
    expect(propertyReason({ engineParsed: true, busy: true })).toBe(MODIFY_REASONS.busy)
    expect(propertyReason({ engineParsed: true })).toBe(MODIFY_REASONS.noSelection)
    expect(propertyReason({ engineParsed: true, selected: { editable: true } })).toBe('')
  })

  it('is live for a selected INSERT reference, where modifyReason still refuses it', () => {
    const session = { engineParsed: true, selected: { editable: false, type: 'INSERT' } }
    expect(propertyReason(session)).toBe('')
  })

  it('still refuses a non-INSERT read-only kind by name', () => {
    const session = { engineParsed: true, selected: { editable: false, type: 'DIMENSION' } }
    expect(propertyReason(session)).toBe(MODIFY_REASONS.readOnlyKind)
  })

  it('the three property setters and MATCHPROP gate on it: live and enabled on an INSERT reference', () => {
    const ctx = { session: { engineParsed: true, selected: { editable: false, type: 'INSERT' } }, reach: null }
    for (const op of ['matchprop', 'setColor', 'setLinetype', 'setLineweight']) {
      const record = forGroup('modify').find((a) => a.op === op)
      expect(record.when(ctx)).toBe('')
    }
  })
})

describe('the diff: colour-only changes, styled adds and true colour', () => {
  const line = (id, extra = {}) => ({
    id: String(id), type: 'LINE', layer: '0', closed: false,
    vertices: [[0, 0, 0], [3, 4, 0]], radius: null, startDeg: null, endDeg: null, ...extra,
  })

  it('a colour-only change on a retained LINE lowers to set_color alone', () => {
    const before = [line(10, { aci: 256 })]
    const after = [line(10, { aci: 1 })]
    expect(diffPlan(before, after)).toEqual({ mutations: { set_color: [{ handle: 'A', aci: 1 }] }, count: 1, reason: null })
  })

  it('a created LINE coloured 1 is a styled add, never a set_color', () => {
    const after = [line(10, { aci: 1 })]
    expect(diffPlan([], after)).toEqual({
      mutations: { added: [{ handle: 'A', kind: 'LINE', layer: '0', pts: [[0, 0, 0], [3, 4, 0]], color: 1 }] },
      count: 1,
      reason: null,
    })
  })

  it('a true colour set in the browser is refused with its sentence', () => {
    const before = [line(10)]
    const after = [line(10, { trueColor: [1, 2, 3] })]
    expect(diffPlan(before, after)).toEqual({
      mutations: null, count: 0, reason: 'entity A has a true colour the plan cannot carry',
    })
  })

  it('an untouched true-coloured entity is carried as-is, no set', () => {
    const before = [line(10, { trueColor: [1, 2, 3] })]
    const after = [line(10, { trueColor: [1, 2, 3] })]
    expect(diffPlan(before, after)).toEqual({ mutations: {}, count: 0, reason: null })
  })

  // W4g-7b-03c-g F3: clearing a true colour back to a plain ACI can leave the
  // nearest-index projection unchanged (rgb [255,0,0] and aci 1, both before
  // and after), so an aci-only compare sees nothing to save; the crate's
  // no-op rule only exempts colour when the head still carries rgb, so the
  // clear itself always needs its own set_color.
  it('clearing a true colour at an UNCHANGED nearest index still lowers to one set_color', () => {
    const before = [line(10, { aci: 1, trueColor: [255, 0, 0] })]
    const after = [line(10, { aci: 1 })]
    expect(diffPlan(before, after)).toEqual({ mutations: { set_color: [{ handle: 'A', aci: 1 }] }, count: 1, reason: null })
  })

  // W4g-7b-03c-g: the INSERT styled add. insertOf now carries props, so a
  // created-then-coloured reference saves its colour/linetype/lineweight
  // exactly like the geometry kinds already do.
  it('a created, styled INSERT is a styled add, carrying its properties', () => {
    const insert = { id: '30', type: 'INSERT', name: 'Fixture', ip: [1, 2], rotationDeg: 90, scale: [2, 3, 1], layer: 'A', aci: 1, linetype: 'HIDDEN', lineweight: 25 }
    expect(diffPlan([], [insert])).toEqual({
      mutations: {
        added: [{
          handle: '1E', kind: 'INSERT', name: 'Fixture', pt: [1, 2, 0], rot: 90, scale: [2, 3, 1], layer: 'A',
          color: 1, linetype: 'HIDDEN', lineweight: 25,
        }],
      },
      count: 1,
      reason: null,
    })
  })
})

describe('the typed words', () => {
  it('COLOR/COL, LINETYPE/LT and LWEIGHT/LW all arm their property op', () => {
    expect(parseDrawingCommand('color')).toMatchObject({ op: 'setColor', verb: 'COLOR' })
    expect(parseDrawingCommand('col')).toMatchObject({ op: 'setColor', verb: 'COLOR' })
    expect(parseDrawingCommand('linetype')).toMatchObject({ op: 'setLinetype', verb: 'LINETYPE' })
    expect(parseDrawingCommand('lt')).toMatchObject({ op: 'setLinetype', verb: 'LINETYPE' })
    expect(parseDrawingCommand('lweight')).toMatchObject({ op: 'setLineweight', verb: 'LWEIGHT' })
    expect(parseDrawingCommand('lw')).toMatchObject({ op: 'setLineweight', verb: 'LWEIGHT' })
  })
})

describe('the Properties panel census', () => {
  it('seats Match and the three property combos, in registry order', () => {
    expect(forGroup('modify').filter((a) => a.panel === 'properties').map((a) => a.op))
      .toEqual(['matchprop', 'setColor', 'setLinetype', 'setLineweight'])
  })
})

// W4g-7b-03c-g F5 and F8/F9: the Properties combos rendered live, against a
// real (fake) worker, so the honesty of the disabled reason and the option
// lists is checked as the drafter would see it, not just the pure builder.
class LiveWorker {
  constructor() { this.posted = []; this.listeners = new Map() }
  addEventListener(type, fn) { this.listeners.set(type, fn) }
  removeEventListener(type) { this.listeners.delete(type) }
  postMessage(message) { this.posted.push(message) }
  terminate() {}
  emit(data) { act(() => { this.listeners.get('message')?.({ data }) }) }
}

function mountProperties() {
  let context = null
  function Probe() { context = useEngineSessionContext(); return null }
  const workers = []
  const createWorker = vi.fn(() => { const w = new LiveWorker(); workers.push(w); return w })
  const seat = {
    id: 'properties', label: 'Properties', kind: 'group', tools: [],
    extra: <div id="cockpit-properties-slot" className="ribbon-slot" />,
  }
  render(
    <EngineSessionProvider createWorker={createWorker}>
      <Probe />
      <DraftingRibbon clusters={[seat]}>
        <EngineRibbonClusters importOpen={false} onToggleImport={() => {}} panels={['properties']} />
      </DraftingRibbon>
    </EngineSessionProvider>,
  )
  return { workers, getContext: () => context }
}

describe('W4g-7b-03c-g F5: the combos show the ACTUAL ladder rung', () => {
  it('says "no drawing" before any document is open, never the hardcoded "select an entity"', () => {
    mountProperties()
    const select = screen.getByLabelText(/^Color/)
    expect(select.getAttribute('aria-label')).toBe(`Color (unavailable: ${MODIFY_REASONS.noDocument})`)
    expect(select.getAttribute('aria-label')).not.toContain(MODIFY_REASONS.noSelection)
  })
})

describe('W4g-7b-03c-h D3: the three word fields bound at MAX_INPUT_CHARS', () => {
  it('a 200-character LINETYPE prompt field truncates to 64 characters as it is typed', () => {
    const { workers, getContext } = mountProperties()
    act(() => { getContext().session.actions.openBytes(new Uint8Array([0]), 'x.dxf') })
    const entity = {
      id: '7', handle: '7', type: 'LINE', layer: 'A', closed: false, editable: true,
      vertices: [[0, 0, 0], [1, 1, 0]], radius: null, startDeg: null, endDeg: null,
    }
    workers[0].emit({ type: 'documentLoaded', documentId: 'x.dxf', entities: [entity], entityCount: 1, unsupported: [] })
    act(() => { getContext().session.actions.select('7') })
    act(() => { getContext().setArmed({ group: 'modify', op: 'setLinetype' }) })
    const field = screen.getByLabelText('ribbon linetype')
    fireEvent.change(field, { target: { value: 'x'.repeat(200) } })
    expect(field.value).toHaveLength(64)
  })
})

describe('W4g-7b-03c-h D2: a true-coloured entity\'s Color combo carries the rgb reading as its own selected option', () => {
  it('the select value is the rgb string, "green" is still offered, and picking it posts setColor', () => {
    const { workers, getContext } = mountProperties()
    act(() => { getContext().session.actions.openBytes(new Uint8Array([0]), 'x.dxf') })
    const entity = {
      id: '7', handle: '7', type: 'LINE', layer: 'A', closed: false, editable: true,
      vertices: [[0, 0, 0], [1, 1, 0]], radius: null, startDeg: null, endDeg: null,
      aci: 3, trueColor: [10, 20, 30],
    }
    workers[0].emit({ type: 'documentLoaded', documentId: 'x.dxf', entities: [entity], entityCount: 1, unsupported: [] })
    act(() => { getContext().session.actions.select('7') })
    const colorSelect = screen.getByLabelText(/^Color/)
    expect(colorSelect.value).toBe('rgb(10,20,30)')
    expect([...colorSelect.options].map((o) => o.value)).toContain('green')
    fireEvent.change(colorSelect, { target: { value: 'green' } })
    expect(workers[0].posted.at(-1)).toEqual({ type: 'applyEdit', op: 'setColor', payload: { entityId: '7', aci: 3 } })
  })
})

describe('W4g-7b-03c-g F8/F9: the current value is always its own option', () => {
  it('an ACI of 37 and a lineweight of 26 (both off the standard lists) render as real, selected options', () => {
    const { workers, getContext } = mountProperties()
    // The boundary (and so the fake worker) spawns lazily, on the first
    // open — a bare render leaves `workers` empty, exactly like every other
    // document-loaded row in this file reaches through `actions.openBytes`
    // rather than a file-input round trip (there is no import panel here).
    act(() => { getContext().session.actions.openBytes(new Uint8Array([0]), 'x.dxf') })
    const entity = {
      id: '7', handle: '7', type: 'LINE', layer: 'A', closed: false, editable: true,
      vertices: [[0, 0, 0], [1, 1, 0]], radius: null, startDeg: null, endDeg: null,
      aci: 37, lineweight: 26,
    }
    workers[0].emit({ type: 'documentLoaded', documentId: 'x.dxf', entities: [entity], entityCount: 1, unsupported: [] })
    act(() => { getContext().session.actions.select('7') })
    const colorSelect = screen.getByLabelText(/^Color/)
    const lwSelect = screen.getByLabelText(/^Lineweight/)
    // Before the fix these values matched no rendered <option>, so the
    // browser fell back to showing the FIRST option (ByLayer) selected —
    // making ByLayer visually unreachable (re-picking an already-"selected"
    // option fires no change).
    expect(colorSelect.value).toBe('index 37')
    expect([...colorSelect.options].map((o) => o.value)).toContain('index 37')
    expect([...colorSelect.options].map((o) => o.value)).toContain('ByLayer')
    expect(lwSelect.value).toBe('26')
    expect([...lwSelect.options].map((o) => o.value)).toContain('26')
    expect([...lwSelect.options].map((o) => o.value)).toContain('ByLayer')
    // ByLayer is now a REAL change from either combo's current state.
    fireEvent.change(colorSelect, { target: { value: 'ByLayer' } })
    expect(workers[0].posted.at(-1)).toEqual({ type: 'applyEdit', op: 'setColor', payload: { entityId: '7', aci: 256 } })
  })
})
