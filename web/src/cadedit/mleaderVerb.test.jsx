import { describe, expect, it } from 'vitest'
import { buildCreatePayload, buildEditPayload, CREATE_OPS, lowerSteps } from './engineSession.js'
import { diffPlan } from './mutationDiff.js'

const inputs = { x: '0', y: '0', x2: '3', y2: '4', text: 'Valve', style: 'Standard', layer: '0' }
const mlstyles = [{ name: 'Standard', textstyle: 'Standard', height: 0.18, arrow: 0.18, dogleg: 0.36, gap: 0.09, segments: 1 }]
const build = (patch = {}, styles = mlstyles) => buildCreatePayload('createMleader', { ...inputs, ...patch }, [], [], styles)

describe('MLEADER in the browser engine create lane', () => {
  it('uses its own create table and carries only contract operands', () => {
    expect(CREATE_OPS).not.toContain('createMleader')
    expect(build()).toEqual({ payload: { x: 0, y: 0, x2: 3, y2: 4, text: 'Valve', style: 'Standard', layer: '0' } })
    expect(build({ style: 'standard' }).payload.style).toBe('Standard')
    expect(buildCreatePayload('createMleader', inputs).payload).toBeDefined()
    expect(buildCreatePayload('constructor', inputs).refusal).toBeTruthy()
  })
  it.each([
    { x: '' }, { y: 'NaN' }, { x2: Infinity }, { y2: 'no' },
    { x2: '0', y2: '0' }, { text: '' }, { text: 'x'.repeat(257) },
    { text: ' Valve' }, { text: 'Valve ' }, { text: 'Va|lve' },
    { text: 'Va\\\\lve' }, { text: '50%' }, { text: 'café' },
    { text: 'Valve\nnext' }, { text: 'Valve\t' }, { text: 'Valve\u007f' },
  ])('refuses invalid operands before posting: %j', (patch) => {
    expect(build(patch).refusal).toBeTruthy()
  })
  it('refuses unknown and multi-segment styles when the catalogue is present', () => {
    expect(build({ style: 'Absent' }).refusal).toBe('mleader_style_unknown')
    expect(build({}, []).refusal).toBe('mleader_style_unknown')
    expect(build({}, [{ ...mlstyles[0], segments: 2 }]).refusal).toMatch(/one leader segment/)
  })
  it('lowers batch creates through the same builder and catalogue', () => {
    const entities = Object.assign([], { mlstyles })
    expect(lowerSteps([{ op: 'createMleader', inputs }], [], entities)).toEqual({
      steps: [{ op: 'createMleader', payload: build().payload }],
    })
  })
  it('lowers a created leader and refuses existing geometry and property edits', () => {
    const entity = { id: '42', type: 'MLEADER', layer: '0', style: 'Standard', vertices: [[0, 0, 0], [3, 4, 0]], text: 'Valve', textLocation: [3.45, 4.09, 0] }
    expect(diffPlan([], [entity]).mutations.added).toEqual([{
      handle: '2A', kind: 'MLEADER', layer: '0', style: 'Standard', pts: [[0, 0, 0], [3, 4, 0]], text: 'Valve',
    }])
    for (const op of ['move', 'setLayer', 'setColor', 'setLinetype', 'setLineweight', 'explode']) {
      expect(buildEditPayload(op, entity.id, {}, [], [entity]).refusal).toBe('a mleader is placed, not edited, in this round')
    }
    expect(buildEditPayload('delete', entity.id, {}, [], [entity]).refusal).toBeUndefined()
  })
})
