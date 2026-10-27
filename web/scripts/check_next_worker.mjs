import assert from 'node:assert/strict'
import { resolve } from 'node:path'
import { bundleGraph } from './bundle-assets.mjs'

const graph = bundleGraph(resolve(process.argv[2] || 'dist'))
const workers = [...graph.assets].filter(([file, code]) => file.endsWith('.js') &&
  code.includes('engine_unavailable') && code.includes('bad_document_bytes'))
assert.ok(workers.length, 'the emitted CAD engine worker is missing')
for (const [file] of workers) {
  assert.ok(graph.reachable.has(file), `CAD worker is unreachable from HTML: ${file}`)
  assert.ok([...graph.dependencies].some(([parent, targets]) => parent !== file &&
    graph.reachable.has(parent) && targets.has(file) && /new\s+(?:\w+\.)?Worker\s*\(/.test(graph.assets.get(parent))),
  `no reachable Worker spawn references ${file}`)
}
console.log(`CAD worker reachability: ${workers.map(([file]) => file).join(', ')}`)
