import { describe, expect, it } from 'vitest'
import { drawingHeaderName, drawingHeaderSource, editedDrawingFileName } from './drawingHeaderName.js'

describe('drawing header source and name', () => {
  it.each([
    ['plant-room.dxf', 'plant-room.dxf', 'plant-room'],
    ['project/drawings/roof.DWG', 'roof.DWG', 'roof'],
    ['project\\roof.dxf', 'roof.dxf', 'roof'],
    ['', null, null],
  ])('uses the shown basename for %j', (dwg, source, name) => {
    expect(drawingHeaderSource({ dwg }, null, null)).toBe(source)
    expect(drawingHeaderName({ dwg }, null, null)).toBe(name)
  })

  it('uses the import basename when the active intake holds that document', () => {
    const shown = { dwg: 'old.dwg' }
    const documentId = 'imports/hand.dxf'
    const engine = { documentOrigin: 'import', documentId }
    expect(drawingHeaderSource(shown, { documentId }, engine)).toBe('hand.dxf')
    expect(drawingHeaderName(shown, { documentId }, engine)).toBe('hand')
    expect(drawingHeaderSource(shown, null, engine)).toBe('old.dwg')
  })

  it('returns null without a drawing', () => {
    expect(drawingHeaderSource(null, null, null)).toBeNull()
    expect(drawingHeaderName(null, null, null)).toBeNull()
  })
})

describe('editedDrawingFileName', () => {
  it.each([
    ['plant-room.dwg', 'plant-room.dxf'],
    ['plant-room.dxf', 'plant-room.dxf'],
    ['roof site (2).DWG', 'roof site (2).dxf'],
    ['a/b/c.dxf', 'c.dxf'],
    ['folder\\Étage 2.dwg', 'Étage 2.dxf'],
    ['', 'edited.dxf'],
    [null, 'edited.dxf'],
    [undefined, 'edited.dxf'],
    ['a'.repeat(121), 'edited.dxf'],
    ['x"y.dxf', 'edited.dxf'],
  ])('names the edited DXF for %j', (name, expected) => {
    expect(editedDrawingFileName(name)).toBe(expected)
  })
})
