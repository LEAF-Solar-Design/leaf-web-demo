// Synthetic X04 report update cycles with exact DOM membership and cleanup.
import React from '../web/node_modules/react/index.js'
import jsdom from '../web/node_modules/jsdom/lib/api.js'
import esbuild from '../web/node_modules/esbuild/lib/main.js'
import { createRequire } from 'node:module'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { writeFile } from 'node:fs/promises'
import assert from 'node:assert/strict'

const rootPath = fileURLToPath(new URL('../', import.meta.url))
const bundle = await esbuild.build({ entryPoints: [resolve(rootPath, 'web/src/components/DrawingDependencyReport.jsx')],
  bundle: true, write: false, format: 'cjs', platform: 'node', jsx: 'automatic', external: ['react', 'react/jsx-runtime'] })
const compiled = { exports: {} }
new Function('require', 'module', 'exports', bundle.outputFiles[0].text)(
  createRequire(resolve(rootPath, 'web/package.json')), compiled, compiled.exports)
const Report = compiled.exports.default
const names = ['window', 'document', 'HTMLElement', 'Node', 'Event', 'MouseEvent', 'navigator']
const prior = new Map(names.map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]))
const dom = new jsdom.JSDOM('<div id="root"></div>')
const observations = []
const reports = ['alpha', 'beta'].map((drawing) => ({ coverage_note: `${drawing} observed records`,
  dependencies: Array.from({ length: 16 }, (_, index) => ({ kind: 'image', name: `${drawing}-${index}.png`, disposition: 'image omitted' })) }))
const snapshot = JSON.stringify(reports)
let root, renderer
try {
  for (const name of names) Object.defineProperty(globalThis, name, { configurable: true, writable: true, value: dom.window[name] })
  const client = await import('../web/node_modules/react-dom/client.js')
  renderer = await import('../web/node_modules/react-dom/index.js')
  const container = dom.window.document.getElementById('root')
  root = client.createRoot(container)
  for (const cycles of [10, 100, 1000]) {
    const beforeHeap = process.memoryUsage().heapUsed
    const start = performance.now()
    for (let cycle = 0; cycle < cycles; cycle++) {
      const report = reports[cycle % 2]
      renderer.flushSync(() => root.render(React.createElement(Report, { report })))
      const currentNames = [...container.querySelectorAll('tbody tr')].map((row) => row.children[1].textContent)
      assert.deepEqual(currentNames, report.dependencies.map((item) => item.name))
      assert.equal(container.querySelector('[aria-label="Dependency coverage"]').textContent, report.coverage_note)
      assert.equal(container.querySelectorAll('bdi').length, 16)
      assert.ok(!container.textContent.includes(cycle % 2 ? 'alpha-' : 'beta-'))
    }
    assert.equal(JSON.stringify(reports), snapshot)
    observations.push({ cycles, current_rows: 16, seconds: Number(((performance.now() - start) / 1000).toFixed(6)),
      heap_before_mib: Number((beforeHeap / 1048576).toFixed(3)),
      heap_after_mib: Number((process.memoryUsage().heapUsed / 1048576).toFixed(3)) })
  }
} finally {
  if (root) renderer.flushSync(() => root.unmount())
  dom.window.close()
  for (const [name, descriptor] of prior) {
    if (descriptor) Object.defineProperty(globalThis, name, descriptor)
    else delete globalThis[name]
  }
}
const encoded = JSON.stringify({ schema: 'leaf.x04.report-update-cycles.v1', synthetic: true,
  timing_gate: false, heap_gate: false, scope: 'Offline React/JSDOM updates; uncontrolled GC.', observations }, null, 2) + '\n'
const out = process.argv.indexOf('--out')
if (out !== -1) await writeFile(process.argv[out + 1], encoded)
process.stdout.write(encoded)
