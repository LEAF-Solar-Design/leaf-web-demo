// X04 client React/JSDOM picker observations. No browser or network resources.
import React from '../web/node_modules/react/index.js'
import jsdom from '../web/node_modules/jsdom/lib/api.js'
import esbuild from '../web/node_modules/esbuild/lib/main.js'
import { createRequire } from 'node:module'
import { writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('../', import.meta.url))
const bundle = await esbuild.build({
  entryPoints: [resolve(root, 'web/src/components/ModuleSetPicker.jsx')],
  bundle: true, write: false, format: 'cjs', platform: 'node', jsx: 'automatic',
  external: ['react', 'react/jsx-runtime'],
})
const compiled = { exports: {} }
// Resolve the same React instance as the installed DOM renderer and JSX runtime.
new Function('require', 'module', 'exports', bundle.outputFiles[0].text)(
  createRequire(resolve(root, 'web/package.json')), compiled, compiled.exports)
const Picker = compiled.exports.default
const results = []
const names = ['window', 'document', 'HTMLElement', 'Node', 'Event', 'MouseEvent', 'navigator']
const prior = new Map(names.map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]))
let client, renderer
for (const size of [100, 1000, 10000]) {
  const sets = Array.from({ length: size }, (_, index) => ({
    set_id: `set-${index}`, layer: `Synthetic ${index}`, block: 'Panel', count: 1,
  }))
  let dom, reactRoot
  try {
    dom = new jsdom.JSDOM('<div id="root"></div>')
    for (const name of names) Object.defineProperty(globalThis, name, { configurable: true, writable: true, value: dom.window[name] })
    client ??= await import('../web/node_modules/react-dom/client.js')
    renderer ??= await import('../web/node_modules/react-dom/index.js')
    const callbacks = []
    const started = performance.now()
    reactRoot = client.createRoot(dom.window.document.getElementById('root'))
    renderer.flushSync(() => reactRoot.render(React.createElement(Picker, {
      data: { sets }, drawingKey: 'synthetic', onSelect: (ids) => callbacks.push(ids),
    })))
    const inputs = [...dom.window.document.querySelectorAll('input[type="checkbox"]')]
    if (inputs.length !== size) throw new Error('Checkbox count mismatch')
    renderer.flushSync(() => inputs[0].click())
    renderer.flushSync(() => inputs.at(-1).click())
    renderer.flushSync(() => dom.window.document.querySelector('button').click())
    const expected = [sets[0].set_id, sets.at(-1).set_id]
    if (JSON.stringify(callbacks) !== JSON.stringify([expected]) || !inputs[0].checked || !inputs.at(-1).checked) {
      throw new Error('Selection callback mismatch')
    }
    results.push({ candidate_sets: size, checkbox_count: inputs.length, callback_ids: callbacks[0],
      seconds: Number(((performance.now() - started) / 1000).toFixed(6)) })
  } finally {
    if (reactRoot) renderer.flushSync(() => reactRoot.unmount())
    dom?.window.close()
    for (const [name, descriptor] of prior) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor)
      else delete globalThis[name]
    }
  }
}
const encoded = JSON.stringify({ schema: 'leaf.x04.picker-render.v1', synthetic: true, timing_gate: false,
  scope: 'Client React plus local JSDOM; no visual browser or physical keyboard proof.', results }, null, 2) + '\n'
const out = process.argv.indexOf('--out')
if (out !== -1) await writeFile(process.argv[out + 1], encoded)
process.stdout.write(encoded)
