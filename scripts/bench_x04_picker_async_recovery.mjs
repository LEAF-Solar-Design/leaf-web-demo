// X04 callback recovery in local React/JSDOM; no service or timing gate.
import assert from 'node:assert/strict'
import React from '../web/node_modules/react/index.js'
import jsdom from '../web/node_modules/jsdom/lib/api.js'
import esbuild from '../web/node_modules/esbuild/lib/main.js'
import { createRequire } from 'node:module'
import { writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('../', import.meta.url))
const bundle = await esbuild.build({
  entryPoints: [resolve(root, 'web/src/components/ModuleSetPicker.jsx')], bundle: true,
  write: false, format: 'cjs', platform: 'node', jsx: 'automatic', external: ['react', 'react/jsx-runtime'],
})
const compiled = { exports: {} }
new Function('require', 'module', 'exports', bundle.outputFiles[0].text)(
  createRequire(resolve(root, 'web/package.json')), compiled, compiled.exports)
const Picker = compiled.exports.default
const names = ['window', 'document', 'HTMLElement', 'Node', 'Event', 'MouseEvent', 'navigator', 'IS_REACT_ACT_ENVIRONMENT']
const prior = new Map(names.map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]))
const dom = new jsdom.JSDOM('<div id="root"></div>')
let reactRoot
const unhandled = []
const onUnhandled = (reason) => unhandled.push(String(reason))
const onWindowUnhandled = (event) => unhandled.push(String(event.reason))
const observations = []
process.on('unhandledRejection', onUnhandled)
dom.window.addEventListener('unhandledrejection', onWindowUnhandled)
try {
  for (const name of names) Object.defineProperty(globalThis, name, {
    configurable: true, writable: true, value: name === 'IS_REACT_ACT_ENVIRONMENT' ? true : dom.window[name],
  })
  const client = await import('../web/node_modules/react-dom/client.js')
  const container = dom.window.document.getElementById('root')
  const sets = [{ set_id: 'opaque-A', layer: 'Synthetic', block: 'Panel', count: 1 }]
  for (const cycles of [10, 100, 500]) {
    let calls = 0, failed = false
    const onSelect = (ids) => {
      assert.deepEqual(ids, ['opaque-A'])
      calls += 1
      return failed ? Promise.resolve() : Promise.reject(new Error('private benchmark failure'))
    }
    reactRoot = client.createRoot(container)
    const start = performance.now()
    const heapBefore = process.memoryUsage().heapUsed
    try {
      await React.act(async () => { reactRoot.render(React.createElement(Picker, {
        data: { sets }, drawingKey: 'synthetic', onSelect,
      })) })
      await React.act(async () => { container.querySelector('input').click() })
      for (let cycle = 0; cycle < cycles; cycle++) {
        failed = false
        await React.act(async () => { container.querySelector('button').click() })
        assert.equal(calls, cycle * 2 + 1)
        assert.ok(container.querySelector('[role="alert"]'))
        assert.equal(container.querySelector('button').disabled, false)
        assert.equal(container.querySelector('input').checked, true)
        assert.ok(!container.textContent.includes('private benchmark'))
        failed = true
        await React.act(async () => { container.querySelector('button').click() })
        assert.equal(calls, cycle * 2 + 2)
        assert.equal(container.querySelector('[role="alert"]'), null)
        assert.equal(container.querySelector('button').disabled, false)
        assert.ok(!container.textContent.includes('Counted '))
      }
      await new Promise((resolve) => setImmediate(resolve))
      assert.deepEqual(unhandled, [])
      observations.push({ cycles, callbacks: calls, unhandled_rejections: unhandled.length,
        seconds: Number(((performance.now() - start) / 1000).toFixed(6)),
        heap_before_mib: Number((heapBefore / 1048576).toFixed(3)),
        heap_after_mib: Number((process.memoryUsage().heapUsed / 1048576).toFixed(3)) })
    } finally {
      await React.act(async () => { reactRoot.unmount() })
      reactRoot = undefined
      assert.equal(container.childNodes.length, 0)
    }
  }
} finally {
  if (reactRoot) await React.act(async () => { reactRoot.unmount() })
  process.removeListener('unhandledRejection', onUnhandled)
  dom.window.removeEventListener('unhandledrejection', onWindowUnhandled)
  dom.window.close()
  for (const [name, descriptor] of prior) {
    if (descriptor) Object.defineProperty(globalThis, name, descriptor)
    else delete globalThis[name]
  }
}
const encoded = JSON.stringify({ schema: 'leaf.x04.picker-async-recovery.v1', synthetic: true,
  timing_gate: false, heap_gate: false, scope: 'Offline DOM promise recovery; heap observations include uncontrolled GC.', observations }, null, 2) + '\n'
const out = process.argv.indexOf('--out')
if (out !== -1) await writeFile(process.argv[out + 1], encoded)
process.stdout.write(encoded)
