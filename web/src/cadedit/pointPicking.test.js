import { describe, expect, it } from 'vitest'

import { PICK_SEQUENCES, applyPick, currentStep, ghostFor, startPicking, wantsPick } from './pointPicking.js'

describe('pointPicking (W4f slice A1): clicks on the drawing answer the prompts', () => {
  it('a line takes two points, then wants nothing more; the ghost runs from the first point to the cursor', () => {
    let s = startPicking('createLine')
    expect(currentStep(s).keys).toEqual(['x', 'y'])
    expect(ghostFor(s, 5, 5)).toBeNull()
    let r = applyPick(s, 10.12345, -3, {})
    expect(r.writes).toEqual([['x', '10.123'], ['y', '-3']])
    s = r.state
    expect(ghostFor(s, 20, 4)).toEqual({ pts: [[10.12345, -3], [20, 4]], closed: false })
    r = applyPick(s, 20, 4, {})
    expect(r.writes).toEqual([['x2', '20'], ['y2', '4']])
    s = r.state
    expect(wantsPick(s)).toBe(false)
    expect(ghostFor(s, 30, 30)).toBeNull()
    expect(applyPick(s, 1, 1, {}).writes).toEqual([])
  })

  it('a circle takes its centre then a radius point; the ghost is the circle under the cursor', () => {
    let s = startPicking('createCircle')
    s = applyPick(s, 0, 0, {}).state
    const ghost = ghostFor(s, 3, 4)
    expect(ghost.closed).toBe(true)
    expect(ghost.pts).toHaveLength(48)
    for (const [x, y] of ghost.pts) expect(Math.abs(Math.hypot(x, y) - 5)).toBeLessThan(1e-9)
    const r = applyPick(s, 3, 4, {})
    expect(r.writes).toEqual([['r', '5']])
    expect(wantsPick(r.state)).toBe(false)
    // A radius click ON the centre is refused (nothing written, same step).
    expect(applyPick(s, 0, 0, {})).toEqual({ state: s, writes: [] })
    expect(PICK_SEQUENCES.createArc[1].kind).toBe('radius')
  })

  it('a polyline appends every click, the first click replacing the sample list, and always wants more', () => {
    let s = startPicking('createPolyline')
    let r = applyPick(s, 0, 0, { pts: '0,0 100,0 100,50' })
    expect(r.writes).toEqual([['pts', '0,0']])
    s = r.state
    r = applyPick(s, 10, 0, { pts: '0,0' })
    expect(r.writes).toEqual([['pts', '0,0 10,0']])
    s = r.state
    r = applyPick(s, 10, 4.5, { pts: '0,0 10,0' })
    expect(r.writes).toEqual([['pts', '0,0 10,0 10,4.5']])
    expect(wantsPick(r.state)).toBe(true)
    expect(ghostFor(r.state, 0, 4)).toEqual({ pts: [[0, 0], [10, 0], [10, 4.5], [0, 4]], closed: false })
  })

  it('a move takes a base point then a destination and writes the displacement; the ghost is base to cursor', () => {
    let s = startPicking('move')
    let r = applyPick(s, 5, 5, {})
    expect(r.writes).toEqual([])
    s = r.state
    expect(ghostFor(s, 8, 9)).toEqual({ pts: [[5, 5], [8, 9]], closed: false })
    r = applyPick(s, 8, 9, {})
    expect(r.writes).toEqual([['dx', '3'], ['dy', '4']])
    expect(wantsPick(r.state)).toBe(false)
    expect(startPicking('moveVertex').sequence).toEqual(PICK_SEQUENCES.moveVertex)
  })

  it('ops with nothing to pick, and non-finite clicks, are refused without writes', () => {
    const none = startPicking('deleteVertex')
    expect(none.sequence).toBeNull()
    expect(wantsPick(none)).toBe(false)
    expect(applyPick(none, 1, 1, {}).writes).toEqual([])
    const line = startPicking('createLine')
    expect(applyPick(line, NaN, 1, {})).toEqual({ state: line, writes: [] })
    expect(applyPick(line, 1, Infinity, {})).toEqual({ state: line, writes: [] })
    expect(ghostFor(line, NaN, 1)).toBeNull()
    expect(applyPick(startPicking('createLine'), -0.00001, 2, {}).writes).toEqual([['x', '0'], ['y', '2']])
  })

  it('a chain point opens LINE at its next-point step, the ghost runs from it, one click finishes (W4f-3)', () => {
    const s = startPicking('createLine', [10, 20])
    expect(s.step).toBe(1)
    expect(currentStep(s)).toEqual({ kind: 'point', keys: ['x2', 'y2'] })
    expect(ghostFor(s, 30, 40)).toEqual({ pts: [[10, 20], [30, 40]], closed: false })
    const { state, writes } = applyPick(s, 30, 40, {})
    expect(writes).toEqual([['x2', '30'], ['y2', '40']])
    expect(wantsPick(state)).toBe(false)
    // A chain point that is not finite, or not a pair, or an op whose first
    // step is not a point, opens normally.
    expect(startPicking('createLine', [NaN, 1]).step).toBe(0)
    expect(startPicking('createLine', [1]).step).toBe(0)
    expect(startPicking('createLine', null).step).toBe(0)
    expect(startPicking('move', [1, 2]).step).toBe(0)
    expect(startPicking('createPolyline', [1, 2]).step).toBe(0)
  })
})
