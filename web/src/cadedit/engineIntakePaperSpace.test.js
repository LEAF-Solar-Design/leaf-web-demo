import { expect, it } from 'vitest'
import { engineIntake, projectServerBlocks, projectionSummary } from './engineIntake.js'

it('projects the model while counting the entire source document', () => {
  const line = { id: 1, type: 'LINE', layer: 'Roof', vertices: [[100, 100, 0], [101, 100, 0]] }
  const insert = { id: 2, type: 'INSERT', layer: 'Roof', name: 'Panel', ip: [110, 100, 0],
    rotationDeg: 0, scale: [1, 1, 1], modelSpace: true }
  const paperInsert = { ...insert, id: 3, layer: 'Sheet', name: 'Titleblk-Info', ip: [0, 0, 0], modelSpace: false }
  const paperText = { id: 4, type: 'TEXT', layer: 'Sheet', vertices: [[0, 0, 0]], height: 2, text: 'Sheet 1', modelSpace: false }
  const blocks = [{ name: 'Panel', base: [0, 0, 0], complete: true, children: [line] }]
  const model = engineIntake([line, insert], '', blocks)
  const drawing = engineIntake([line, insert, paperInsert, paperText], '', blocks)
  expect(drawing.polylines).toHaveLength(2)
  expect(drawing.polylines).toEqual(model.polylines)
  expect(drawing.inserts.map((row) => row.name)).toEqual(['Panel'])
  expect(drawing.diagnostics.incompleteInserts).toBe(0)
  expect(drawing.sourceSummary).toEqual({ entityCount: 4, countsByType: { LINE: 1, INSERT: 2, TEXT: 1 },
    countsByLayer: { Roof: 2, Sheet: 2 } })
})

it('sizes model point markers from model extents alone', () => {
  const point = { type: 'POINT', id: 1, vertices: [[100, 100, 0]] }
  const sheet = { type: 'LINE', id: 2, modelSpace: false, vertices: [[0, 0, 0], [100000, 0, 0]] }
  const model = engineIntake([point])
  expect(model.polylines).toHaveLength(1)
  expect(engineIntake([point, sheet]).polylines).toEqual(model.polylines)
})

it('excludes paper-space markers and their diagnostics from server projection', () => {
  const drawing = projectServerBlocks({ inserts: [
    { name: 'Title', pt: [0, 0, 0], space: 'paper' },
    { name: 'Panel', pt: [80, 80, 0], space: 'model' },
  ] })
  expect(drawing.inserts.map((row) => row.name)).toEqual(['Panel'])
  expect(drawing.incompleteInserts).toBe(1)
})

it.each([
  [{}, ''],
  [{ incompleteInserts: 1 }, '1 block shown in part'],
  [{ incompleteInserts: 2 }, '2 blocks shown in part'],
  [{ incompleteInserts: 2, truncated: 1 }, 'Partial drawing view'],
])('summarizes projection diagnostics %o', (diagnostics, summary) => {
  expect(projectionSummary(diagnostics)).toBe(summary)
})
