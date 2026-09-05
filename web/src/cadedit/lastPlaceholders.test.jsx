// W4g-4b: the last engine-backable placeholders. POINT and ELLIPSE are draw
// creates (the crate makes them, the projection carries an ellipse's axis
// and ratio, the mapper draws them, the snap index sees them); MATCHPROP is
// a Modify record seated in the reference's Properties panel that copies
// the selection's layer to a picked object as ONE setLayer step. Pure rows
// plus the seating, no worker.
import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import DraftingRibbon from '../site/DraftingRibbon.jsx'
import { forGroup } from '../lib/actionRegistry.js'
import { parseDrawingCommand } from '../lib/commandWords.js'

import EngineRibbonClusters, { PROMPTS } from './EngineRibbonClusters.jsx'
import EngineSessionProvider from './EngineSessionProvider.jsx'
import { CREATE_OPS, buildCreatePayload, buildEditPayload, lowerSteps, planMatchprop } from './engineSession.js'
import { ELLIPSE_SEGMENTS, POINT_MARK, entityToPolyline } from './engineIntake.js'
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
const RO = { id: '11', handle: '11', index: 2, type: 'INSERT', layer: 'Other', closed: false, editable: false, vertices: [], radius: null, startDeg: null, endDeg: null }
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
    expect(entityToPolyline({ ...ellipse, majorAxis: null })).toBeNull()
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
    expect(planMatchprop(session([H, RO], '7'), { edge: '11' }).refusal).toBe('Match refused: the destination object is read-only in the browser engine.')
    expect(planMatchprop(session([H, { ...V, layer: 'Source' }], '7'), { edge: '9' }).refusal).toBe('Match refused: the destination is already on layer Source.')
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
    for (const a of forGroup('modify')) if (a.op !== 'matchprop') expect(a.panel).toBe('modify')
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
      extra: <div id="cockpit-properties-slot" className="ribbon-cluster-tools" />,
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
    const modify = document.querySelector('.ribbon-cluster[data-group="modify"]')
    expect([...modify.querySelectorAll('[data-tool]')].map((el) => el.dataset.tool)).not.toContain('modify:matchprop')
    expect(modify.querySelectorAll('.ribbon-tool')).toHaveLength(18)
    const draw = document.querySelector('.ribbon-cluster[data-group="draw"]')
    const drawIds = [...draw.querySelectorAll('[data-tool]')].map((el) => el.dataset.tool)
    expect(drawIds).toEqual(['draw:createLine', 'draw:createPolyline', 'draw:createCircle', 'draw:createArc', 'draw:createRectangle', 'draw:createEllipse', 'draw:createPoint'])
    expect(drawIds).not.toContain('draw:ellipse')
    expect(document.querySelectorAll('.ribbon-cluster[data-group="properties"]')).toHaveLength(1)
  })
})
