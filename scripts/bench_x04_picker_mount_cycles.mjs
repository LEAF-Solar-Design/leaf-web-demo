// X04 repeated client mounts in one offline JSDOM. Heap values are observations.
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
const names = ['window', 'document', 'HTMLElement', 'Node', 'Event', 'MouseEvent', 'navigator']
const prior = new Map(names.map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]))
const dom = new jsdom.JSDOM('<div id="root"></div>')
const observations = []
let reactRoot, renderer
try {
  for (const name of names) Object.defineProperty(globalThis, name, { configurable: true, writable: true, value: dom.window[name] })
  const client = await import('../web/node_modules/react-dom/client.js')
  renderer = await import('../web/node_modules/react-dom/index.js')
  const container = dom.window.document.getElementById('root')
  for (let cycle = 0; cycle < 25; cycle++) {
    const sets = Array.from({ length: 32 }, (_, index) => ({
      set_id: `cycle-${cycle}-set-${index}`, layer: `Synthetic ${index}`, block: 'Panel', count: 1,
    }))
    const callbacks = []
    try {
      reactRoot = client.createRoot(container)
      renderer.flushSync(() => reactRoot.render(React.createElement(Picker, {
        data: { sets }, drawingKey: `drawing-${cycle}`, onSelect: (ids) => callbacks.push(ids),
      })))
      const inputs = [...container.querySelectorAll('input')]
      if (inputs.length !== 32 || new Set(inputs.map((input) => input.id)).size !== 32) throw new Error('Input identity mismatch')
      renderer.flushSync(() => inputs[0].click())
      renderer.flushSync(() => inputs.at(-1).click())
      renderer.flushSync(() => container.querySelector('button').click())
      if (JSON.stringify(callbacks) !== JSON.stringify([[sets[0].set_id, sets.at(-1).set_id]])) throw new Error('Stale or duplicate callback')
    } finally {
      if (reactRoot) renderer.flushSync(() => reactRoot.unmount())
      reactRoot = undefined
    }
    if (container.childNodes.length !== 0 || dom.window.document.querySelector('fieldset, input, button')) throw new Error('Picker nodes remain after unmount')
    observations.push({ cycle: cycle + 1, callbacks: callbacks.length, remaining_picker_nodes: 0,
      heap_mib: Number((process.memoryUsage().heapUsed / 1048576).toFixed(3)) })
  }
} finally {
  if (reactRoot) renderer.flushSync(() => reactRoot.unmount())
  dom.window.close()
  for (const [name, descriptor] of prior) {
    if (descriptor) Object.defineProperty(globalThis, name, descriptor)
    else delete globalThis[name]
  }
}
const encoded = JSON.stringify({ schema: 'leaf.x04.picker-mount-cycles.v1', synthetic: true,
  timing_gate: false, heap_gate: false, scope: 'Client React/JSDOM,25 sequential mount cycles; GC timing uncontrolled.', observations }, null, 2) + '\n'
const out = process.argv.indexOf('--out')
if (out !== -1) await writeFile(process.argv[out + 1], encoded)
process.stdout.write(encoded)
