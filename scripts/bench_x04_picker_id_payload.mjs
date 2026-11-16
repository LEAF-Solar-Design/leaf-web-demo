// X04 opaque-ID length observations in offline client React/JSDOM.
import React from '../web/node_modules/react/index.js'
import jsdom from '../web/node_modules/jsdom/lib/api.js'
import esbuild from '../web/node_modules/esbuild/lib/main.js'
import { createRequire } from 'node:module'
import { writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'

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
const observations = []
for (const length of [16, 256, 4096, 16384]) {
  const dom = new jsdom.JSDOM('<div id="root"></div>')
  let reactRoot, renderer
  try {
    for (const name of names) Object.defineProperty(globalThis, name, { configurable: true, writable: true, value: dom.window[name] })
    const client = await import('../web/node_modules/react-dom/client.js')
    renderer = await import('../web/node_modules/react-dom/index.js')
    const container = dom.window.document.getElementById('root')
    const sets = Array.from({ length: 32 }, (_, index) => ({
      set_id: `${String(index).padStart(2, '0')}\ud800${'x'.repeat(length - 3)}`,
      layer: `Synthetic ${index}`, block: 'Panel', count: 1,
    }))
    const callbacks = []
    reactRoot = client.createRoot(container)
    const start = performance.now()
    renderer.flushSync(() => reactRoot.render(React.createElement(Picker, {
      data: { sets }, drawingKey: 'synthetic', onSelect: (ids) => callbacks.push(ids),
    })))
    const renderSeconds = (performance.now() - start) / 1000
    const inputs = [...container.querySelectorAll('input')]
    assert.equal(inputs.length, 32)
    assert.equal(new Set(inputs.map((input) => input.id)).size, 32)
    const idLengths = inputs.map((input, index) => {
      const label = input.closest('label')
      assert.equal(label.control, input)
      assert.equal(label.htmlFor, input.id)
      assert.equal(dom.window.document.getElementById(input.id), input)
      assert.equal(sets[index].set_id.length, length)
      // Every UTF-16 unit is preserved; React's useId prefix may vary.
      assert.ok(input.id.endsWith(sets[index].set_id.split('').map((unit) =>
        unit.charCodeAt(0).toString(16).padStart(4, '0')).join('')))
      return input.id.length
    })
    renderer.flushSync(() => inputs[0].closest('label').click())
    renderer.flushSync(() => inputs.at(-1).closest('label').click())
    renderer.flushSync(() => container.querySelector('button').click())
    assert.deepEqual(callbacks, [[sets[0].set_id, sets.at(-1).set_id]])
    observations.push({ candidate_count: 32, opaque_id_utf16_units: length,
      render_seconds: Number(renderSeconds.toFixed(6)), dom_id_min_units: Math.min(...idLengths),
      dom_id_max_units: Math.max(...idLengths), dom_id_total_units: idLengths.reduce((a, b) => a + b, 0), callbacks: callbacks.length })
  } finally {
    if (reactRoot) renderer.flushSync(() => reactRoot.unmount())
    dom.window.close()
    for (const [name, descriptor] of prior) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor)
      else delete globalThis[name]
    }
  }
}
const encoded = JSON.stringify({ schema: 'leaf.x04.picker-id-payload.v1', synthetic: true,
  timing_gate: false, scope: 'Offline client DOM; render only timing excludes compilation, ID construction and interactions.', observations }, null, 2) + '\n'
const out = process.argv.indexOf('--out')
if (out !== -1) await writeFile(process.argv[out + 1], encoded)
process.stdout.write(encoded)
