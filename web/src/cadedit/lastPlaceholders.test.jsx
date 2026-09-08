// W4g-4b: the last engine-backable placeholders. POINT and ELLIPSE are draw
// creates (the crate makes them, the projection carries an ellipse's axis
// and ratio, the mapper draws them, the snap index sees them); MATCHPROP is
// a Modify record seated in the reference's Properties panel that copies
// the selection's layer to a picked object as ONE setLayer step. Pure rows
// plus the seating, no worker.
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import DraftingRibbon, { RibbonWidget } from '../site/DraftingRibbon.jsx'
import { DEFERRED_REASONS, forGroup } from '../lib/actionRegistry.js'
import { parseDrawingCommand } from '../lib/commandWords.js'

import EngineRibbonClusters, { PROMPTS, promptKeys } from './EngineRibbonClusters.jsx'
import EngineSessionProvider, { useEngineSessionContext } from './EngineSessionProvider.jsx'
import { CREATE_OPS, buildCreatePayload, buildEditPayload, lowerSteps, planMatchprop } from './engineSession.js'
import { ELLIPSE_SEGMENTS, POINT_MARK, POINT_MARK_FRACTION, engineIntake, entityToPolyline, pointMarkSize } from './engineIntake.js'
import { diffPlan } from './mutationDiff.js'
import { ELLIPSE_GHOST_RATIO, PICK_SEQUENCES, SNAP_KIND, applyPick, buildSnapIndex, ghostFor, startPicking } from './pointPicking.js'

class IdleWorker {
  constructor() { this.listeners = new Map() }
  addEventListener(type, fn) { this.listeners.set(type, fn) }
  removeEventListener(type) { this.listeners.delete(type) }
  postMessage() {}
  terminate() {}
}

const H = { id: '7', handle: '7', index: 0, type: 'LINE', layer: 'Source', closed: false, editable: true, vertices: [[0, 0, 0], [10, 0, 0]], radius: null, startDeg: null, endDeg: null }
const V = { id: '9', handle: '9', index: 1, type: 'LINE', layer: 'Other', closed: false, editable: true, vertices: [[5, -5, 0], [5, 5, 0]], radius: null, startDeg: null, endDeg: null }
// W4g-7b-03c-f: an INSERT reference projects editable: false for GEOMETRY
// alone; MATCHPROP copies properties, so it is no longer refused as a
// destination. RO_DIM is a non-INSERT read-only kind, which still refuses.
const RO = { id: '11', handle: '11', index: 2, type: 'INSERT', layer: 'Other', closed: false, editable: false, vertices: [], radius: null, startDeg: null, endDeg: null }
const RO_DIM = { id: '12', handle: '12', index: 3, type: 'DIMENSION', layer: 'Other', closed: false, editable: false, vertices: [], radius: null, startDeg: null, endDeg: null }
const session = (entities, selectedId) => ({ entities, selectedId })

afterEach(() => cleanup())

describe('W4g-4b POINT and ELLIPSE creates', () => {
  it('the create op list carries both, and the builders read their operands strictly', () => {
    expect(CREATE_OPS).toContain('createPoint')
    expect(CREATE_OPS).toContain('createEllipse')
    expect(buildCreatePayload('createPoint', { x: '3', y: '4', layer: ' P ' })).toEqual({ payload: { x: 3, y: 4, layer: 'P' } })
    expect(buildCreatePayload('createPoint', { x: 'a', y: '4' }).refusal).toBe('Point refused: x and y must both be numbers.')
    // The axis endpoint is picked ABSOLUTE and sent RELATIVE to the centre.
    expect(buildCreatePayload('createEllipse', { x: '10', y: '0', x2: '15', y2: '0', ratio: '0.5', layer: 'E' })).toEqual({ payload: { cx: 10, cy: 0, ax: 5, ay: 0, ratio: 0.5, layer: 'E' } })
    expect(buildCreatePayload('createEllipse', { x: '10', y: '0', x2: '10', y2: '0', ratio: '0.5' }).refusal).toBe('Ellipse refused: the axis endpoint must differ from the centre.')
    expect(buildCreatePayload('createEllipse', { x: '10', y: '0', x2: '15', y2: '0', ratio: 'r' }).refusal).toBe('Ellipse refused: the ratio must be a number.')
    expect(buildCreatePayload('createEllipse', { x: '10', y: '0', x2: '15', y2: '0', ratio: '0' }).refusal).toBe('Ellipse refused: the ratio (minor to major) must be greater than 0 and at most 1.')
    expect(buildCreatePayload('createEllipse', { x: '10', y: '0', x2: '15', y2: '0', ratio: '1.5' }).refusal).toBe('Ellipse refused: the ratio (minor to major) must be greater than 0 and at most 1.')
    expect(buildCreatePayload('createEllipse', { x: '10', y: '0', x2: '15', y2: '0', ratio: '1' }).payload.ratio).toBe(1)
    expect(buildCreatePayload('createEllipse', { x: '10', y: 'n', x2: '15', y2: '0', ratio: '1' }).refusal).toMatch(/must all be numbers/)
  })

  it('the words, the picks and the ellipse ghost', () => {
    expect(parseDrawingCommand('po')).toMatchObject({ op: 'createPoint', verb: 'POINT' })
    expect(parseDrawingCommand('point')).toMatchObject({ op: 'createPoint', verb: 'POINT' })
    expect(parseDrawingCommand('el')).toMatchObject({ op: 'createEllipse', verb: 'ELLIPSE' })
    expect(parseDrawingCommand('ellipse')).toMatchObject({ op: 'createEllipse', verb: 'ELLIPSE' })
    expect(parseDrawingCommand('ma')).toMatchObject({ op: 'matchprop', verb: 'MATCHPROP', group: 'modify' })
    expect(parseDrawingCommand('matchprop')).toMatchObject({ op: 'matchprop', verb: 'MATCHPROP' })
    expect(PICK_SEQUENCES.createPoint).toEqual([{ kind: 'point', keys: ['x', 'y'] }])
    expect(PICK_SEQUENCES.createEllipse.map((s) => s.kind)).toEqual(['point', 'point'])
    expect(PICK_SEQUENCES.matchprop).toEqual([{ kind: 'edge', keys: ['edge', 'ex', 'ey'] }])
    // The ghost after the centre: an ellipse through the cursor along the axis, the prompt's default ratio across it.
    let state = startPicking('createEllipse')
    ;({ state } = applyPick(state, 10, 0, {}))
    const ghost = ghostFor(state, 15, 0)
    expect(ghost.closed).toBe(true)
    expect(ghost.pts).toHaveLength(48)
    expect(ghost.pts[0]).toEqual([15, 0])
    const top = ghost.pts[12]
    expect(top[0]).toBeCloseTo(10, 9)
    expect(top[1]).toBeCloseTo(5 * ELLIPSE_GHOST_RATIO, 9)
    expect(ghostFor(state, 10, 0)).toBeNull()
    expect(ghostFor(startPicking('createPoint'), 3, 3)).toBeNull()
  })

  it('the mapper draws a POINT as a marker and an ELLIPSE from its axis and ratio; the snap index sees both', () => {
    const point = { id: '20', type: 'POINT', layer: 'P', closed: false, editable: true, vertices: [[3, 4, 1]], radius: null, startDeg: null, endDeg: null }
    const marker = entityToPolyline(point)
    expect(marker.closed).toBe(false)
    expect(marker.pts).toHaveLength(5)
    expect(marker.pts[2]).toEqual([3, 4, 1])
    for (const p of marker.pts) expect(Math.max(Math.abs(p[0] - 3), Math.abs(p[1] - 4))).toBeLessThanOrEqual(POINT_MARK + 1e-12)
    const ellipse = { id: '21', type: 'ELLIPSE', layer: 'E', closed: true, editable: true, vertices: [[10, 0, 0]], majorAxis: [5, 0], ratio: 0.5, radius: null, startDeg: null, endDeg: null }
    const drawn = entityToPolyline(ellipse)
    expect(drawn.closed).toBe(true)
    expect(drawn.pts).toHaveLength(ELLIPSE_SEGMENTS)
    expect(drawn.pts[0]).toEqual([15, 0, 0])
    // Every sample satisfies the ellipse equation about the centre: (dx / 5)^2 + (dy / 2.5)^2 = 1.
    for (const p of drawn.pts) expect(((p[0] - 10) / 5) ** 2 + (p[1] / 2.5) ** 2).toBeCloseTo(1, 9)
    // A tilted axis turns the whole figure with it.
    const tilted = entityToPolyline({ ...ellipse, majorAxis: [0, 5] })
    expect(tilted.pts[0]).toEqual([10, 5, 0])
    // Missing or bad axis / ratio: nothing drawn, never a throw.
    expect(entityToPolyline({ ...ellipse, majorAxis: [0, 0] })).toBeNull()
    expect(entityToPolyline({ ...ellipse, ratio: 0 })).toBeNull()
    // A ratio above 1 is not a DXF ellipse (the minor axis would be the major): drawn as nothing, never with swapped axes.
    expect(entityToPolyline({ ...ellipse, ratio: 1.5 })).toBeNull()
    expect(entityToPolyline({ ...ellipse, ratio: 1 })).not.toBeNull()
    expect(entityToPolyline({ ...ellipse, majorAxis: null })).toBeNull()
    // The marker's size follows the drawing: a share of its larger extent, floored for a drawing of points alone.
    expect(pointMarkSize({ w: 1000, h: 200 })).toBe(1000 * POINT_MARK_FRACTION)
    expect(pointMarkSize({ w: 10, h: 10 })).toBe(POINT_MARK)
    expect(pointMarkSize(null)).toBe(POINT_MARK)
    const wide = { id: '22', type: 'LINE', layer: '0', closed: false, editable: true, vertices: [[0, 0, 0], [1000, 0, 0]], radius: null, startDeg: null, endDeg: null }
    const built = engineIntake([wide, point], 'x.dxf')
    const mark = built.polylines.find((pl) => pl.handle === '14')
    expect(mark.pts[0][0]).toBeCloseTo(3 - 5, 9)
    expect(engineIntake([point], 'y.dxf').polylines[0].pts[0][0]).toBeCloseTo(3 - POINT_MARK, 9)
    const index = buildSnapIndex([point, ellipse])
    expect(index.n).toBe(2)
    expect([...index.kinds]).toEqual([SNAP_KIND.END, SNAP_KIND.CENTRE])
    expect([index.xs[0], index.ys[0]]).toEqual([3, 4])
    expect([index.xs[1], index.ys[1]]).toEqual([10, 0])
  })

  it('the save-time diff sees a POINT or an ELLIPSE the browser made and refuses the plan, so the sidecar leg carries it', () => {
    const point = { id: '20', type: 'POINT', layer: 'P', closed: false, editable: true, vertices: [[3, 4, 0]], radius: null, startDeg: null, endDeg: null }
    expect(diffPlan([H], [H, point]).reason).toBe('entity 14 is a POINT the plan cannot carry, and it was added')
    const ellipse = { id: '21', type: 'ELLIPSE', layer: 'E', closed: true, editable: true, vertices: [[10, 0, 0]], majorAxis: [5, 0], ratio: 0.5, radius: null, startDeg: null, endDeg: null }
    expect(diffPlan([H, ellipse], [H, { ...ellipse, ratio: 0.25 }]).reason).toBe('entity 15 is a ELLIPSE the plan cannot carry, and it changed')
    expect(diffPlan([H, ellipse], [H, ellipse])).toEqual({ mutations: {}, count: 0, reason: null })
  })
})

describe('W4g-4b MATCHPROP', () => {
  it('reads its one operand: a destination other than the selection', () => {
    expect(buildEditPayload('matchprop', '7', { edge: '' }).refusal).toBe('Match refused: select the destination object by clicking it on the drawing.')
    expect(buildEditPayload('matchprop', '7', { edge: '7' }).refusal).toBe('Match refused: the destination must be a different entity from the selection.')
    expect(buildEditPayload('matchprop', '7', { edge: ' 9 ' })).toEqual({ payload: { entityId: '7', edge: '9' } })
  })

  it('plans ONE setLayer step on the destination with the source\'s layer, and refuses what would change nothing', () => {
    expect(planMatchprop(session([H, V], '7'), { edge: '9' })).toEqual({ steps: [{ op: 'setLayer', entityId: '9', layer: 'Source' }] })
    expect(planMatchprop(session([H, V], '7'), { edge: '13' }).refusal).toBe('Match refused: the destination object is no longer in the document.')
    expect(planMatchprop(session([H, V], '13'), { edge: '9' }).refusal).toBe('Match refused: the selected entity is no longer in the document.')
    // W4g-7b-03c-f: an INSERT reference is a matchable destination (its own
    // properties, never its block children); only a non-INSERT read-only
    // kind still refuses by name.
    expect(planMatchprop(session([H, RO], '7'), { edge: '11' })).toEqual({ refusal: 'Match refused: an INSERT keeps its layer in this round.' })
    expect(planMatchprop(session([H, RO_DIM], '7'), { edge: '12' }).refusal).toBe('Match refused: the destination object is read-only in the browser engine.')
    // W4g-7b-03c: MATCHPROP now checks the layer AND the three properties, so
    // "nothing would change" covers all four rather than naming the layer alone.
    expect(planMatchprop(session([H, { ...V, layer: 'Source' }], '7'), { edge: '9' }).refusal).toBe('Match refused: nothing to match.')
    expect(planMatchprop(session([{ ...H, layer: '' }, V], '7'), { edge: '9' }).refusal).toBe('Match refused: the selection has no layer to copy.')
    // The step lowers through the same builder a single setLayer op uses.
    expect(lowerSteps([{ op: 'setLayer', entityId: '9', layer: 'Source' }])).toEqual({ steps: [{ op: 'setLayer', payload: { entityId: '9', layer: 'Source' } }] })
    expect(lowerSteps([{ op: 'setLayer', entityId: '9', layer: '  ' }]).refusal).toBe('Edit refused: a layer step names no layer.')
  })

  it('is a Modify record seated in the Properties panel, prompted for one pick; the Draw column is whole', () => {
    const rec = forGroup('modify').find((a) => a.op === 'matchprop')
    expect(rec.id).toBe('modify:matchprop')
    expect(rec.panel).toBe('properties')
    expect(rec.icon).toBe('match')
    // W4g-7b-03c: setColor/setLinetype/setLineweight join matchprop in the
    // Properties panel; every other Modify record still sits in its own.
    const propertiesOps = new Set(['matchprop', 'setColor', 'setLinetype', 'setLineweight'])
    for (const a of forGroup('modify')) if (!propertiesOps.has(a.op)) expect(a.panel).toBe('modify')
    expect(PROMPTS.matchprop.steps.map((s) => s.ask)).toEqual(['Select destination object:'])
    expect(PROMPTS.createPoint.steps.map((s) => s.ask)).toEqual(['Specify a point:', 'Layer:'])
    expect(PROMPTS.createEllipse.steps.map((s) => s.ask)).toEqual(['Specify center of ellipse:', 'Specify endpoint of axis:', 'Specify ratio (minor to major, 0 to 1):', 'Layer:'])
    const draw = forGroup('draw').map((a) => a.op)
    expect(draw).toContain('createEllipse')
    expect(draw).toContain('createPoint')
    expect(forGroup('draw').find((a) => a.op === 'createEllipse').panel).toBe('draw')
  })

  it('renders INSIDE the Properties slot App seats after Block, not in the Modify cluster; the Draw panel holds no placeholder', () => {
    const seat = {
      id: 'properties', label: 'Properties', kind: 'group', tools: [],
      widgets: [{ id: 'prop-color', label: 'Color', value: 'ByLayer', disabled: true, reason: 'not in the browser engine yet' }],
      extra: <div id="cockpit-properties-slot" className="ribbon-slot" />,
    }
    render(
      <EngineSessionProvider createWorker={vi.fn(() => new IdleWorker())}>
        <DraftingRibbon clusters={[seat]}>
          <EngineRibbonClusters importOpen={false} onToggleImport={() => {}} panels={['draw', 'modify', 'properties']} />
        </DraftingRibbon>
      </EngineSessionProvider>,
    )
    const slot = document.getElementById('cockpit-properties-slot')
    expect([...slot.querySelectorAll('[data-tool]')].map((el) => el.dataset.tool)).toEqual(['modify:matchprop'])
    // The slot sits ON the tools row (inside .ribbon-cluster-tools, before the widgets), never under the label.
    expect(slot.parentElement.className).toBe('ribbon-cluster-tools')
    expect(slot.nextElementSibling?.className).toBe('ribbon-widgets')
    const modify = document.querySelector('.ribbon-cluster[data-group="modify"]')
    expect([...modify.querySelectorAll('[data-tool]')].map((el) => el.dataset.tool)).not.toContain('modify:matchprop')
    expect(modify.querySelectorAll('.ribbon-tool')).toHaveLength(18)
    const draw = document.querySelector('.ribbon-cluster[data-group="draw"]')
    const drawIds = [...draw.querySelectorAll('[data-tool]')].map((el) => el.dataset.tool)
    expect(drawIds).toEqual(['draw:createLine', 'draw:createPolyline', 'draw:createCircle', 'draw:createArc', 'draw:createRectangle', 'draw:createEllipse', 'draw:createPoint'])
    expect(drawIds).not.toContain('draw:ellipse')
    expect(document.querySelectorAll('.ribbon-cluster[data-group="properties"]')).toHaveLength(1)
  })

  it('the Properties portal survives a tab switch away and back, which re-creates the slot as a NEW node', () => {
    const seat = () => ({
      id: 'properties', label: 'Properties', kind: 'group', tools: [],
      extra: <div id="cockpit-properties-slot" className="ribbon-slot" />,
    })
    const createWorker = vi.fn(() => new IdleWorker())
    const ui = (clusters, panels) => (
      <EngineSessionProvider createWorker={createWorker}>
        <DraftingRibbon clusters={clusters}>
          <EngineRibbonClusters importOpen={false} onToggleImport={() => {}} panels={panels} />
        </DraftingRibbon>
      </EngineSessionProvider>
    )
    const { rerender } = render(ui([seat()], ['draw', 'modify', 'properties']))
    const first = document.getElementById('cockpit-properties-slot')
    expect(first.querySelectorAll('[data-tool="modify:matchprop"]')).toHaveLength(1)
    rerender(ui([], []))
    expect(document.getElementById('cockpit-properties-slot')).toBeNull()
    expect(document.querySelectorAll('[data-tool="modify:matchprop"]')).toHaveLength(0)
    rerender(ui([seat()], ['draw', 'modify', 'properties']))
    const again = document.getElementById('cockpit-properties-slot')
    expect(again).not.toBe(first)
    expect(again.querySelectorAll('[data-tool="modify:matchprop"]')).toHaveLength(1)
    expect(document.querySelectorAll('[data-tool="modify:matchprop"]')).toHaveLength(1)
  })
})

// W4g-7b-05c: the flag-ON engine panels' own honest placeholders (Leader,
// Create Block) carry the same specific reason the flag-off static panels
// do, not the generic "not in the browser engine yet".
describe('W4g-7b-05c: the deferred controls carry their own reason with the flag on', () => {
  it('the Groups panel has two real tools and no placeholders', () => {
    render(<EngineSessionProvider createWorker={vi.fn(() => new IdleWorker())}>
      <DraftingRibbon clusters={[]}><EngineRibbonClusters panels={['groups']} /></DraftingRibbon>
    </EngineSessionProvider>)
    const panel = document.querySelector('[data-group="groups"]')
    expect([...panel.querySelectorAll('[data-tool]')].map((tool) => tool.dataset.tool)).toEqual(['groups:group', 'groups:ungroup'])
    expect(forGroup('groups').map((action) => action.op)).toEqual(['group', 'ungroup'])
    expect(panel.textContent).not.toContain('not in the browser engine yet')
  })
  it('Leader and Create Block are both live and document-gated', () => {
    render(
      <EngineSessionProvider createWorker={vi.fn(() => new IdleWorker())}>
        <DraftingRibbon clusters={[]}>
          <EngineRibbonClusters importOpen={false} onToggleImport={() => {}} panels={['annotation', 'block']} />
        </DraftingRibbon>
      </EngineSessionProvider>,
    )
    expect(document.querySelector('[data-tool="annotation:leader"]')).toBeNull()
    const leader = document.querySelector('[data-tool="draw:createMleader"]')
    expect(leader.disabled).toBe(true)
    expect(leader.title).toBe('no drawing in the browser engine yet')
    expect(leader.getAttribute('aria-label')).toBe('Leader (unavailable: no drawing in the browser engine yet)')
    const create = document.querySelector('[data-tool="draw:createBlock"]')
    expect(create.disabled).toBe(true)
    expect(create.getAttribute('aria-label')).toBe('create block (unavailable: no drawing in the browser engine yet)')
  })
})

describe('W4g-7b-03c-g F7: RibbonWidget applies a keyboard walk ONCE, on Enter or blur', () => {
  it('a mouse/pointer change still applies at once', () => {
    const onChange = vi.fn()
    render(<RibbonWidget widget={{ id: 'w', label: 'Color', value: 'ByLayer', options: ['ByLayer', 'red', 'blue'], onChange }} />)
    fireEvent.change(screen.getByLabelText('Color'), { target: { value: 'red' } })
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(onChange).toHaveBeenCalledWith('red')
  })

  it('three ArrowDown-driven changes then Enter post exactly ONE op, the last value', () => {
    const onChange = vi.fn()
    render(<RibbonWidget widget={{ id: 'w', label: 'Color', value: 'ByLayer', options: ['ByLayer', 'red', 'yellow', 'green'], onChange }} />)
    const select = screen.getByLabelText('Color')
    fireEvent.keyDown(select, { key: 'ArrowDown' })
    fireEvent.change(select, { target: { value: 'red' } })
    fireEvent.keyDown(select, { key: 'ArrowDown' })
    fireEvent.change(select, { target: { value: 'yellow' } })
    fireEvent.keyDown(select, { key: 'ArrowDown' })
    fireEvent.change(select, { target: { value: 'green' } })
    expect(onChange).not.toHaveBeenCalled()
    fireEvent.keyDown(select, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(onChange).toHaveBeenCalledWith('green')
  })

  it('a keyboard walk with no Enter still applies once, on blur', () => {
    const onChange = vi.fn()
    render(<RibbonWidget widget={{ id: 'w', label: 'Color', value: 'ByLayer', options: ['ByLayer', 'red', 'yellow'], onChange }} />)
    const select = screen.getByLabelText('Color')
    fireEvent.keyDown(select, { key: 'ArrowDown' })
    fireEvent.change(select, { target: { value: 'red' } })
    fireEvent.keyDown(select, { key: 'ArrowDown' })
    fireEvent.change(select, { target: { value: 'yellow' } })
    fireEvent.blur(select)
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(onChange).toHaveBeenCalledWith('yellow')
    // A later focus/blur with no new change posts nothing more.
    fireEvent.focus(select)
    fireEvent.blur(select)
    expect(onChange).toHaveBeenCalledTimes(1)
  })

  it('W4g-7b-03c-h D1: the walk is held in state, so the select shows every buffered step, not just the option adjacent to value', () => {
    const onChange = vi.fn()
    const { rerender } = render(<RibbonWidget widget={{ id: 'w', label: 'Color', value: 'ByLayer', options: ['ByLayer', 'red', 'yellow', 'green'], onChange }} />)
    const select = screen.getByLabelText('Color')
    fireEvent.keyDown(select, { key: 'ArrowDown' })
    fireEvent.change(select, { target: { value: 'red' } })
    expect(select.value).toBe('red')
    expect(onChange).not.toHaveBeenCalled()
    fireEvent.keyDown(select, { key: 'ArrowDown' })
    fireEvent.change(select, { target: { value: 'yellow' } })
    expect(select.value).toBe('yellow')
    fireEvent.keyDown(select, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(onChange).toHaveBeenCalledWith('yellow')
    rerender(<RibbonWidget widget={{ id: 'w', label: 'Color', value: 'yellow', options: ['ByLayer', 'red', 'yellow', 'green'], onChange }} />)
    expect(select.value).toBe('yellow')
  })

  it('W4g-7b-03c-h D1: Escape abandons the walk, no onChange, and a following blur posts nothing', () => {
    const onChange = vi.fn()
    render(<RibbonWidget widget={{ id: 'w', label: 'Color', value: 'ByLayer', options: ['ByLayer', 'red', 'yellow'], onChange }} />)
    const select = screen.getByLabelText('Color')
    fireEvent.keyDown(select, { key: 'ArrowDown' })
    fireEvent.change(select, { target: { value: 'red' } })
    fireEvent.keyDown(select, { key: 'Escape' })
    expect(onChange).not.toHaveBeenCalled()
    expect(select.value).toBe('ByLayer')
    fireEvent.blur(select)
    expect(onChange).not.toHaveBeenCalled()
  })
})

// W4g-7b-04c-4 F1: the ribbon's own live validation (armedGroup === 'draw'
// path) reads `buildCreatePayload` with the session's loaded dimstyles, the
// same fourth argument the store's own run() already passed; before this fix
// it called buildCreatePayload with no dimstyles at all, so a dimension held
// Run disabled forever with "dimension style Standard is not loaded", never
// mind what the drafter typed.
describe('W4g-7b-04c-4 F1: the ribbon\'s own live validation sees the loaded dimstyles catalogue', () => {
  it('MLEADER uses the reference prompts and carries every input through promptKeys', () => {
    expect(PROMPTS.createMleader.steps.slice(0, 3).map((step) => step.ask)).toEqual([
      'Specify leader arrowhead location:', 'Specify leader landing location:', 'Enter text:',
    ])
    expect([...promptKeys('createMleader')]).toEqual(['x', 'y', 'x2', 'y2', 'text', 'style', 'layer'])
    expect(forGroup('draw').find((action) => action.op === 'createMleader')).toMatchObject({ panel: 'annotation', icon: 'leader' })
  })

  class ScriptedWorker {
    constructor() { this.posted = []; this.listeners = new Map() }
    addEventListener(type, fn) { this.listeners.set(type, fn) }
    removeEventListener(type) { this.listeners.delete(type) }
    postMessage(message) { this.posted.push(message) }
    terminate() {}
    emit(data) { act(() => { this.listeners.get('message')?.({ data }) }) }
  }

  function fileOf(name = 'one.dxf') {
    const bytes = new TextEncoder().encode('0\nEOF\n')
    const file = new File([bytes], name, { type: 'application/dxf' })
    file.arrayBuffer = async () => bytes.buffer.slice(0)
    Object.defineProperty(file, 'size', { value: bytes.length })
    return file
  }

  it('MLEADER selects loaded multileader styles, validates segments, and defaults to Standard', async () => {
    const workers = []
    const handle = {}
    function Probe() { handle.context = useEngineSessionContext(); return null }
    render(
      <EngineSessionProvider createWorker={() => { const w = new ScriptedWorker(); workers.push(w); return w }}>
        <Probe />
        <DraftingRibbon clusters={[]}><EngineRibbonClusters panels={['annotation']} /></DraftingRibbon>
      </EngineSessionProvider>,
    )
    await act(async () => { await handle.context.session.actions.open(fileOf()) })
    workers[0].emit({ type: 'documentLoaded', documentId: 'one.dxf', entities: [], entityCount: 0, unsupported: [],
      dimstyles: ['DimensionOnly'], mlstyles: [{ name: 'Standard', segments: 1 }, { name: 'Notes', segments: 1 }, { name: 'Multi', segments: 2 }] })
    fireEvent.click(document.querySelector('[data-tool="draw:createMleader"]'))
    const style = screen.getByLabelText('ribbon style')
    expect([...style.options].map((option) => option.text)).toEqual(['Standard (default)', 'Standard', 'Notes', 'Multi'])
    for (const [key, value] of Object.entries({ x: '30', y: '23', x2: '35', y2: '26', text: 'Valve' })) {
      fireEvent.change(screen.getByLabelText(`ribbon ${key}`, { exact: true }), { target: { value } })
    }
    expect(screen.getByTestId('cockpit-prompt-run')).not.toBeDisabled()
    fireEvent.change(style, { target: { value: 'Multi' } })
    expect(screen.getByTestId('cockpit-prompt-run')).toBeDisabled()
    expect(screen.getByTestId('cockpit-prompt-note')).toHaveTextContent('the style must use one leader segment')
    fireEvent.change(style, { target: { value: 'Notes' } })
    fireEvent.click(screen.getByTestId('cockpit-prompt-run'))
    expect(workers[0].posted.filter((m) => m.type === 'applyEdit')).toEqual([
      { type: 'applyEdit', op: 'createMleader', payload: { x: 30, y: 23, x2: 35, y2: 26, text: 'Valve', style: 'Notes', layer: '' } },
    ])
  })

  it('arming DAL, filling the six points, sees Run live and posts ONE createDimension, dimtype ALIGNED', async () => {
    const workers = []
    const createWorker = vi.fn(() => { const w = new ScriptedWorker(); workers.push(w); return w })
    const handle = {}
    function Probe() { handle.context = useEngineSessionContext(); return null }
    render(
      <EngineSessionProvider createWorker={createWorker}>
        <Probe />
        <DraftingRibbon clusters={[]}>
          <EngineRibbonClusters importOpen={false} onToggleImport={() => {}} panels={['draw', 'modify', 'annotation']} />
        </DraftingRibbon>
      </EngineSessionProvider>,
    )
    await act(async () => { await handle.context.session.actions.open(fileOf()) })
    workers[0].emit({
      type: 'documentLoaded', documentId: 'one.dxf', entities: [], entityCount: 0, unsupported: [], dimstyles: ['Standard'],
    })
    // Armed the way W4g-7b-03c-g F1 arms its own typed word: through the
    // provider's setArmed, never by clicking a portaled Annotation tool by
    // role (a unit render of EngineRibbonClusters never mounts App's seat
    // slot, so no such button exists here).
    act(() => { handle.context.setArmed({ group: 'draw', op: 'dimAligned' }) })
    fireEvent.change(screen.getByLabelText('ribbon x', { exact: true }), { target: { value: '0' } })
    fireEvent.change(screen.getByLabelText('ribbon y', { exact: true }), { target: { value: '0' } })
    fireEvent.change(screen.getByLabelText('ribbon x2', { exact: true }), { target: { value: '3' } })
    fireEvent.change(screen.getByLabelText('ribbon y2', { exact: true }), { target: { value: '4' } })
    fireEvent.change(screen.getByLabelText('ribbon dx', { exact: true }), { target: { value: '1.5' } })
    fireEvent.change(screen.getByLabelText('ribbon dy', { exact: true }), { target: { value: '6' } })
    // Held Run has aria-label "Run (unavailable: ...)"; this only matches
    // once liveRefusal is empty, i.e. once the ribbon's own buildCreatePayload
    // call actually saw the loaded dimstyles catalogue.
    const runButton = screen.getByRole('button', { name: 'Run' })
    expect(runButton).not.toBeDisabled()
    fireEvent.click(runButton)
    const posted = workers[0].posted.filter((m) => m.type === 'applyEdit')
    expect(posted).toHaveLength(1)
    expect(posted[0]).toEqual({
      type: 'applyEdit', op: 'createDimension',
      payload: { dimtype: 'ALIGNED', x1: 0, y1: 0, x2: 3, y2: 4, dx: 1.5, dy: 6, rotationDeg: 0, style: 'Standard', layer: '' },
    })
  })
})
