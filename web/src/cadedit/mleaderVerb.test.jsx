import { describe, expect, it } from 'vitest'
import { buildCreatePayload, buildEditPayload, CREATE_OPS, lowerSteps } from './engineSession.js'
import { diffPlan } from './mutationDiff.js'

const inputs = { x: '0', y: '0', x2: '3', y2: '4', text: 'Valve', style: 'Standard', layer: '0' }
const mlstyles = [{ name: 'Standard', textstyle: 'Standard', height: 0.18, arrow: 0.18, dogleg: 0.36, gap: 0.09, segments: 1 }]
const build = (patch = {}, styles = mlstyles) => buildCreatePayload('createMleader', { ...inputs, ...patch }, [], [], {}, styles)

describe('MLEADER in the browser engine create lane', () => {
  it('uses its own create table and carries only contract operands', () => {
    expect(CREATE_OPS).not.toContain('createMleader')
    expect(build()).toEqual({ payload: { x: 0, y: 0, x2: 3, y2: 4, text: 'Valve', style: 'Standard', layer: '0' } })
    expect(build({ style: 'standard' }).payload.style).toBe('Standard')
    for (const style of ['', '   ', undefined]) expect(build({ style }).payload.style).toBe('Standard')
    expect(build({ style: '' }, [{ ...mlstyles[0], name: 'STANDARD' }]).payload.style).toBe('STANDARD')
    expect(build({ text: 'x'.repeat(200) }).payload.text).toHaveLength(200)
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
    expect(build({ style: '' }, []).refusal).toBe('mleader_style_unknown')
    expect(build({ style: ' ' }, [{ ...mlstyles[0], name: 'Other' }]).refusal).toBe('mleader_style_unknown')
    expect(build({}, [{ ...mlstyles[0], segments: null }]).payload).toBeDefined()
    expect(build({}, [{ ...mlstyles[0], segments: 2 }]).refusal).toMatch(/one leader segment/)
  })
  it('passes Standard through for an empty style when no catalogue is available', () => {
    for (const style of ['', '   ', undefined]) {
      expect(buildCreatePayload('createMleader', { ...inputs, style }).payload.style).toBe('Standard')
    }
  })
  it('refuses caret text with the reader-specific sentence', () => {
    for (const text of ['A^ B', 'A^B']) expect(build({ text }).refusal).toBe("mleader text cannot contain ^: the engine's DXF reader rewrites it")
  })
  it('uses the server name grammar for both style and layer', () => {
    expect(build({ style: 'Notes(1)' }, [{ ...mlstyles[0], name: 'Notes(1)' }]).refusal).toMatch(/style.*server name rule/)
    expect(build({ layer: 'Notes(1)' }).refusal).toMatch(/layer.*server name rule/)
    for (const name of ['Notes 1', 'Notes_1.$-']) {
      expect(build({ style: name, layer: name }, [{ ...mlstyles[0], name }]).payload).toMatchObject({ style: name, layer: name })
    }
    expect(build({}, [{ ...mlstyles[0], segments: 0 }]).refusal).toMatch(/one leader segment/)
  })
  it('compares points at the server drawing precision', () => {
    expect(build({ x: 30, y: 23, x2: 30.0004, y2: 23 }).refusal).toBe('the two points coincide at the drawing precision (0.001)')
    expect(build({ x: 30, y: 23, x2: 30.001, y2: 23 }).payload).toBeDefined()
    expect(build({ x: -0.0004, y: 0, x2: 0, y2: -0 }).refusal).toMatch(/coincide/)
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
