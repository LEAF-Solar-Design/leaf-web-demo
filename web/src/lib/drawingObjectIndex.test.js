// @vitest-environment node
import { expect, it } from 'vitest'
import { buildDrawingObjectIndex } from './drawingObjectIndex.js'

const entity = (id, layer = 'Panels') => ({ id, type: 'LWPOLYLINE', layer, closed: true, vertices: [[-2, -3, 0], [4, 5, 0]] })
const build = (args = {}) => buildDrawingObjectIndex({ drawingKey: 'a', ...args })
const object = (id, kind, rest = {}) => ({ id, kind, rev: 0, provenance: {}, extra: {}, validity: {}, ...rest })
const graph = (rest) => ({ drawingKey: 'a', graph: { graph_schema_version: 1, source_hash: 'abc', frames: [], panels: [], strings: [], ...rest } })

it('preserves u64 handles and ids across reorder and reparse', () => {
  const entities = [entity('18446744073709551615'), entity('33339')]
  const first = build({ entities }), next = build({ entities: JSON.parse(JSON.stringify([...entities].reverse())) })
  expect(first.byId.has('h:FFFFFFFFFFFFFFFF')).toBe(true)
  expect([...next.byId.keys()].sort()).toEqual([...first.byId.keys()].sort())
  expect(first.resolve('0xffffffffffffffff').matches[0].engineIds).toEqual(['18446744073709551615'])
  expect(first.resolve(' zoom to 823b ').matches[0]).toMatchObject({ id: 'h:823B', name: 'LWPOLYLINE · handle 823B', path: 'drawing / Layers / Panels / LWPOLYLINE · handle 823B', parentKind: 'layer-fallback' })
  expect(Object.isFrozen(first)).toBe(true)
})

it('joins graph metadata to its handle and separates physical and electrical membership', () => {
  const index = build({ entities: [entity('33339')], solarGraph: graph({
    frames: [object('f', 'frame', { name: 'Roof frame', panel_refs: ['p'] })],
    panels: [object('p', 'panel', { frame_ref: 'f', matrix_cell: { row: 0, col: 1 }, assignment: { string_ref: 's', seq: 1 }, provenance: { source_handle: '823b' } })],
    strings: [object('s', 'string', { circuit_tag: 'String 12', ordered_panel_refs: ['p'], inverter_ref: null })],
  }) })
  expect(index.records.filter((r) => r.handles.includes('823B'))).toHaveLength(1)
  expect(index.resolve('p').matches[0]).toMatchObject({ id: 'h:823B', kind: 'panel', physicalParentId: 'g:f', parentKind: 'physical', electrical: [{ id: 'g:s', kind: 'string' }] })
  expect(index.byId.get('g:f').bounds).toEqual({ minX: -2, minY: -3, maxX: 4, maxY: 5 })
  expect(index.byId.get('layer:Panels').bounds).toEqual(index.byId.get('g:f').bounds)
  expect(index.byId.get('h:823B').path).toContain('Roof frame')
})

it('resolves duplicate names ambiguously, ids before aliases, and rejects cross-drawing metadata', () => {
  const solarGraph = graph({ frames: [object('f1', 'frame', { name: 'Roof' }), object('f2', 'frame', { name: 'Roof' })] })
  expect(build({ solarGraph }).resolve('go to roof').status).toBe('ambiguous')
  expect(build({ solarGraph }).resolve('g:f1').status).toBe('unique')
  const rejected = build({ solarGraph: { ...solarGraph, drawingKey: 'b' } })
  expect(rejected.resolve('Roof').status).toBe('missing')
  expect(rejected.issues).toEqual([{ code: 'drawing-key-mismatch', count: 1 }])
})

it('counts invalid bounds, broken references and cycles without throwing', () => {
  const index = build({ solarGraph: graph({ frames: [object('a', 'frame', { parent_ref: 'b', panel_refs: ['missing'] }),
    object('b', 'frame', { parent_ref: 'a', bounds: { minX: 0, minY: 0, maxX: Infinity, maxY: 1 } })] }) })
  expect(index.issues).toEqual(expect.arrayContaining([{ code: 'unknown-reference', count: 1 }, { code: 'cycle', count: 2 }, { code: 'invalid-bounds', count: 1 }]))
  expect(index.byId.get('g:a').physicalParentId).toBeNull()
  expect(index.byId.get('g:b').bounds).toBeNull()
})

it('builds 2,345 deterministic graphless records and caps sorted resolution at 50', () => {
  const entities = Array.from({ length: 2345 }, (_, i) => entity(String(i + 1)))
  const index = build({ entities })
  expect(index.records).toHaveLength(2346)
  for (const r of index.records.filter((r) => r.kind !== 'layer')) {
    expect(r.name).toBe(`LWPOLYLINE · handle ${r.handles[0]}`)
    expect(r.physicalParentId).toBe('layer:Panels')
  }
  expect(index.resolve('0x929')).toMatchObject({ status: 'unique', matches: [{ id: 'h:929' }] })
  const result = index.resolve('handle')
  expect(result.status).toBe('ambiguous'); expect(result.matches).toHaveLength(50); expect(result.truncated).toBe(true)
  expect(result.matches.map((r) => r.path)).toEqual(result.matches.map((r) => r.path).sort((a, b) => a.localeCompare(b)))
  expect(index.resolve('   ')).toEqual({ status: 'missing', query: '', matches: [] })
})

it('pads degenerate geometry and ignores block definitions as placed records', () => {
  const index = build({ intake: { inserts: [{ handle: 'A', layer: 'Symbols', pt: [-5, -6] }], blocks: [{ name: 'definition' }] } })
  expect(index.byHandle.get('A').bounds).toEqual({ minX: -5.5, minY: -6.5, maxX: -4.5, maxY: -5.5 })
  expect(index.records).toHaveLength(2)
})

it('counts non-finite geometry and malformed explicit bounds, without inventing usable bounds', () => {
  const index = build({ entities: [entity('1')], intake: { polylines: [shapeWithInvalidPoint(),
    { handle: 'B', bounds: { minX: 4, maxX: 2, minY: 0, maxY: 1 } }] } })
  expect(index.byHandle.get('A').bounds).toBeNull()
  expect(index.byHandle.get('B').bounds).toBeNull()
  expect(index.issues).toContainEqual({ code: 'invalid-bounds', count: 2 })
})

function shapeWithInvalidPoint() { return { handle: 'A', pts: [[0, 0], [Infinity, 1]] } }
