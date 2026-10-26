import React from 'react'
import { createRoot } from 'react-dom/client'
import LabCanvas from '../../src/lab/canvas/LabCanvas.jsx'
import { manifests } from '../../src/lab/canvas/registry.js'
import '../../src/lab/lab.css'

// No backend, host bridge, admission call or StrictMode in this production proof.
// LabCanvas retains its own validation and correctly reports the admission cap.
const compatible = (output, input) => ['mode', 'unit', 'nullable'].every(key => output[key] === input[key]) &&
  output.schema.type === input.schema.type
const candidates = manifests.flatMap(manifest => Object.entries(manifest.outputs).map(([output, pin]) => ({
  manifest, output, inputs: Object.entries(manifest.inputs).filter(([, input]) => compatible(pin, input)).map(([name]) => name),
})))
const chosen = candidates.find(item => item.inputs.length >= 3 &&
  Number.isFinite(item.manifest.latency_budget_us?.p50) && Number.isFinite(item.manifest.latency_budget_us?.p99) &&
  !(item.manifest.settings_schema.required || []).length)
if (!chosen) throw new Error('The embedded registry needs a defaultable block with three compatible inputs')

function defaults(schema) {
  if (Object.hasOwn(schema, 'default')) return structuredClone(schema.default)
  if (schema.type !== 'object') return undefined
  const values = Object.entries(schema.properties || {}).flatMap(([key, child]) => {
    const value = defaults(child)
    return value === undefined ? [] : [[key, value]]
  })
  return values.length ? Object.fromEntries(values) : undefined
}

const { manifest, output, inputs } = chosen
const blocks = Array.from({ length: 500 }, (_, index) => ({
  id: `perf-${String(index).padStart(3, '0')}`,
  registry: { id: manifest.id, version: manifest.version.semver, code_sha256: manifest.version.code_sha256 },
  settings: defaults(manifest.settings_schema) ?? {},
}))
// 100 source blocks, 400 destinations: 200 * 3 + 200 * 2 = 1000.
// Two dependency layers; each destination input is used exactly once.
const pipes = blocks.slice(100).flatMap((block, index) => inputs.slice(0, index < 200 ? 3 : 2).map((port, slot) => ({
  from: { node: blocks[(index + slot) % 100].id, port: output },
  to: { node: block.id, port },
})))
if (blocks.length !== 500 || pipes.length !== 1000) throw new Error('Unexpected perf fixture size')
const document = { schema: 'leaf.lab-graph.v1', blocks, pipes, outputs: {} }
const measurements = { pipes: Object.fromEntries(pipes.map((pipe, index) => [
  `${pipe.from.node}/${pipe.from.port}>${pipe.to.node}/${pipe.to.port}`,
  { p50_us: manifest.latency_budget_us.p50 * (index % 2 ? 2 : 0.5),
    p99_us: manifest.latency_budget_us.p99 * (index % 2 ? 2 : 0.5), count: 100, value: index },
])) }

const style = window.document.createElement('style')
style.textContent = `
  html, body, #root { margin: 0; width: 100%; height: 100%; overflow: hidden; }
  #root { padding: 0; }
  #root .lab-canvas-editor { height: 100%; grid-template-columns: 200px minmax(0, 1fr) 330px; }
  #root .lab-canvas-region { display: flex; flex-direction: column; min-height: 0; }
  #root .lab-flow { flex: 1; height: auto; min-height: 0; }
  #root .lab-palette, #root .lab-inspector { grid-column: auto; max-height: 100%; }
`
window.document.head.append(style)

const container = window.document.getElementById('root')
const observer = new MutationObserver(() => {
  if (container.querySelectorAll('.react-flow__node .lab-node-card').length !== 500) return
  observer.disconnect()
  requestAnimationFrame(() => {
    performance.mark('lab-perf:mount-end')
    const measure = performance.measure('lab-perf:mount', 'lab-perf:mount-start', 'lab-perf:mount-end')
    window.__labPerf = { blocks: blocks.length, pipes: pipes.length, mountMs: measure.duration }
  })
})
observer.observe(container, { childList: true, subtree: true })
const root = createRoot(container)
performance.mark('lab-perf:mount-start')
root.render(<LabCanvas initialDocument={document} measurements={measurements} />)
