import { describe, expect, it } from 'vitest'
import { authorMock } from './mockAuthor.js'
import { runMock } from './mockEngine.js'
import { matchPrompt } from './mockNlPrompt.js'
import { applyDelete, headIntake, list, redo, seedBase, undo } from './mockVersions.js'

const tools = [
  { name: 'count-by-layer' },
  { name: 'highlight-panels-near-edge' },
  { name: 'select-by-layer' },
]

describe('demo request intent', () => {
  it('keeps the distance constraint in a near-edge count', () => {
    expect(matchPrompt('how many panels are within 60 in of the edge', tools)).toMatchObject({
      tool: 'highlight-panels-near-edge', params: { distance_in: 60 },
    })
  })

  it('keeps the distance from the author example when routing it', () => {
    expect(matchPrompt('count panels within 24in of the roof edge', tools)).toMatchObject({
      tool: 'highlight-panels-near-edge', params: { distance_in: 24 },
    })
  })

  it('requests a new tool when no spatial tool is available', () => {
    expect(matchPrompt('count panels within 24in of the roof edge', tools.slice(0, 1))).toMatchObject({
      lane: 'build', tool: null, params: {},
    })
  })

  it('routes the multiword layer from the author example', () => {
    expect(matchPrompt('select everything on the Panel Groups layer', tools)).toMatchObject({
      tool: 'select-by-layer', params: { layer: 'Panel Groups' },
    })
  })

  it('uses the requested layer in both the authored defaults and code', () => {
    const authored = authorMock('select everything on the Panel Groups layer')
    expect(authored.tool.params.properties.layer.default).toBe('Panel Groups')
    expect(authored.code).toContain('(leaf:param "layer" "Panel Groups")')
    const intake = { polylines: [
      { handle: 'A1', layer: 'Panels' },
      { handle: 'B2', layer: 'Panel Groups' },
    ] }
    expect(runMock(authored.tool, { layer: authored.tool.params.properties.layer.default }, intake).overlay)
      .toEqual({ highlight_handles: ['B2'] })
  })

  it('preserves quoted layer names without changing later defaults', () => {
    const authored = authorMock('select everything on layer "Panel Groups"')
    expect(authored.tool.params.properties.layer.default).toBe('Panel Groups')
    expect(authored.code).toContain('(leaf:param "layer" "Panel Groups")')
    expect(authorMock('select all panels').tool.params.properties.layer.default).toBe('Panels')
  })

  it('treats adding up panel area as a read-only measurement', () => {
    const authored = authorMock('add up the total panel area')
    expect(authored.tool.engine_op).toBe('measure_area')
    expect(authored.tool.capabilities).toEqual(['drawing.read'])
    expect(authored.code).not.toContain('entdel')
  })

  it('rejects moving panels instead of generating a delete', () => {
    expect(authorMock('move the selected panel near the edge')).toMatchObject({ unsupported: true })
  })

  it('rejects inserting panels instead of generating a delete', () => {
    expect(authorMock('insert a panel on the Panels layer')).toMatchObject({ unsupported: true })
  })

  it('rejects adding panels instead of generating a delete', () => {
    expect(authorMock('add a panel')).toMatchObject({ unsupported: true })
  })

  it('rejects unknown delete targets without removing the last panel', () => {
    const intake = { polylines: [{ handle: 'A1' }, { handle: 'B2' }] }
    const tool = { name: 'delete-marked-panel', engine_op: 'delete_marked_panel' }
    const routed = matchPrompt('delete panel 7FA4', tools)
    expect(runMock(tool, routed.params, intake)).toMatchObject({
      ok: false, result: null, overlay: null,
      error: 'That panel is not in this drawing. Select a panel and try again.',
    })
    seedBase(intake)
    const before = list()
    expect(() => applyDelete('7FA4')).toThrow('That panel is not in this drawing.')
    expect(list()).toEqual(before)
    expect(headIntake()).toEqual(intake)
  })

  it('requires an explicit selection when no delete handle is supplied', () => {
    const intake = { polylines: [{ handle: 'A1' }, { handle: 'B2' }] }
    const tool = { name: 'delete-marked-panel', engine_op: 'delete_marked_panel' }
    expect(runMock(tool, {}, intake)).toMatchObject({
      ok: false, result: null, error: 'Select a panel before deleting it.',
    })
    seedBase(intake)
    const before = list()
    expect(() => applyDelete()).toThrow('Select a panel before deleting it.')
    expect(list()).toEqual(before)
    expect(headIntake()).toEqual(intake)
  })

  it('does not parse a partial handle or an unrelated number as a delete target', () => {
    expect(matchPrompt('delete panel A1oops', tools).params).toEqual({})
    expect(matchPrompt('delete panel near row 123', tools).params).toEqual({})
  })

  it('preserves redo history when an invalid delete is rejected', () => {
    seedBase({ polylines: [{ handle: 'A1' }, { handle: 'B2' }] })
    applyDelete('A1')
    undo()
    const before = list()
    expect(() => applyDelete('missing')).toThrow()
    expect(list()).toEqual(before)
    expect(redo().intake.polylines).toEqual([{ handle: 'B2' }])
  })

  it('deletes only the explicit target in both paths', () => {
    const intake = { polylines: [{ handle: 'A1' }, { handle: 'B2' }] }
    const tool = { name: 'delete-marked-panel', engine_op: 'delete_marked_panel' }
    const result = runMock(tool, { handle: 'A1' }, intake)
    expect(result.ok).toBe(true)
    expect(result.result.intake.polylines).toEqual([{ handle: 'B2' }])
    seedBase(intake)
    expect(applyDelete('A1').intake).toEqual(result.result.intake)
    expect(intake.polylines).toHaveLength(2)
  })
})
