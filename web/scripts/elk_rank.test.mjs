import assert from 'node:assert/strict'
import test from 'node:test'
import ELK from 'elkjs/lib/elk.bundled.js'
import { createDependencyLayoutRunner } from '../src/lab/canvas/dependencyLayout.js'

test('real ELK lays out three blocks in dependency order', { timeout: 60000 }, async () => {
  let engineGraph
  const runner = createDependencyLayoutRunner(() => {
    const elk = new ELK()
    const layout = elk.layout.bind(elk)
    elk.layout = async graph => {
      const result = await layout(graph)
      engineGraph = result
      return result
    }
    return elk
  })
  try {
    const positions = await runner.run({ blocks: ['c', 'a', 'b'].map(id => ({ id })), pipes: [
      { from: { node: 'a', port: 'out' }, to: { node: 'b', port: 'in' } },
      { from: { node: 'b', port: 'out' }, to: { node: 'c', port: 'in' } },
    ] })
    const raw = Object.fromEntries(engineGraph.children.map(node => [node.id, node]))
    assert.ok(raw.a.y < raw.b.y && raw.b.y < raw.c.y, 'ELK itself assigns top-to-bottom ranks')
    assert.deepEqual(Object.keys(positions).sort(), ['a', 'b', 'c'])
    assert.ok(positions.a.y < positions.b.y && positions.b.y < positions.c.y)
    assert.ok(Object.values(positions).every(({ x, y }) => Number.isFinite(x) && Number.isFinite(y)))
  } finally {
    runner.cancel()
  }
})
