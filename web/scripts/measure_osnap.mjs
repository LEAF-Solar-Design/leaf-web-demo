import { performance } from 'node:perf_hooks'
import { buildSnapIndex, snapPoint } from '../src/cadedit/pointPicking.js'
import { ALL_SNAP_MODES } from '../src/cadedit/snapModes.js'

let state = 0x23b1
function r() {
  state = (Math.imul(1664525, state) + 1013904223) >>> 0
  return state / 4294967296
}
const line = (id, a, b) => ({ id, layer: '0', type: 'LINE', vertices: [a, b] })
const random = []
for (let i = 0; i < 1563; i += 1) {
  const x1 = 20000 * r(), y1 = 20000 * r(), x2 = 20000 * r(), y2 = 20000 * r()
  random.push(line('l' + i, [x1, y1], [x2, y2]))
}
for (let i = 0; i < 782; i += 1) {
  const radius = 10 + 190 * r()
  const cx = radius + (20000 - 2 * radius) * r(), cy = radius + (20000 - 2 * radius) * r()
  random.push({ id: 'c' + i, layer: '0', type: 'CIRCLE', vertices: [[cx, cy]], radius })
}
const randomQueries = Array.from({ length: 1200 }, () => [20000 * r(), 20000 * r()])
const hatch = []
for (let j = 0; j < 250; j += 1) {
  const v = -24.9 + 0.2 * j
  hatch.push(line('v' + j, [v, -500], [v, 500]), line('h' + j, [-500, v], [500, v]))
}
const hatchQueries = Array.from({ length: 1000 }, (_, i) => [-20 + 0.4 * (i % 100), -20 + 4 * Math.floor(i / 100)])
function percentile(values, p) {
  const sorted = values.slice().sort((a, b) => a - b)
  return sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * p))].toFixed(6)
}
function measure(name, entities, warmQueries, queries, anchor) {
  let index
  for (let i = 0; i < 5; i += 1) index = buildSnapIndex(entities)
  const builds = []
  for (let i = 0; i < 10; i += 1) {
    const start = performance.now()
    index = buildSnapIndex(entities)
    builds.push(performance.now() - start)
  }
  const diagnostics = {}
  const opts = { modes: ALL_SNAP_MODES, anchor, diagnostics }
  for (const [x, y] of warmQueries) snapPoint(index, x, y, 10, opts)
  const timings = []
  let admitted = 0, pairs = 0, truncated = 0
  for (const [x, y] of queries) {
    const start = performance.now()
    snapPoint(index, x, y, 10, opts)
    timings.push(performance.now() - start)
    admitted = Math.max(admitted, diagnostics.admitted)
    pairs = Math.max(pairs, diagnostics.pairs)
    if (diagnostics.truncated || diagnostics.localOverflow) truncated += 1
  }
  console.log('osnap-measure dataset=' + name + ' entities=' + entities.length +
    ' fixed=' + index.n + ' prims=' + index.prims.length +
    ' build_median_ms=' + percentile(builds, 0.5) + ' build_p95_ms=' + percentile(builds, 0.95) +
    ' query_median_ms=' + percentile(timings, 0.5) + ' query_p95_ms=' + percentile(timings, 0.95) +
    ' query_max_ms=' + Math.max(...timings).toFixed(6) + ' admitted_max=' + admitted +
    ' pairs_max=' + pairs + ' truncated_queries=' + truncated + '/' + queries.length)
}
measure('random-2345', random, randomQueries.slice(0, 200), randomQueries.slice(200), [10000, 10000])
measure('hatch-500', hatch, hatchQueries.slice(0, 200), hatchQueries, [60, 60])

