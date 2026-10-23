import { compatible } from '../contract.js'
import { manifestFor, registry } from './registry.js'

export function portCompatibility(output, input) {
  if (!output || !input) return { ok: false, reason: 'Pin missing' }
  if (output.mode !== input.mode) return { ok: false, reason: 'Scalar and stream modes differ' }
  if (output.unit !== input.unit) return { ok: false, reason: 'Units differ' }
  if (output.nullable && !input.nullable) return { ok: false, reason: 'Nullable output needs a nullable input' }
  return compatible(output, input) ? { ok: true, reason: '' } : { ok: false, reason: 'Schema cannot safely widen' }
}

export function connectionStatus(document, pipe, entries = registry) {
  const from = document.blocks.find(block => block.id === pipe.from.node)
  const to = document.blocks.find(block => block.id === pipe.to.node)
  if (!from || !to) return { ok: false, reason: 'Block missing' }
  const status = portCompatibility(manifestFor(from, entries).outputs[pipe.from.port], manifestFor(to, entries).inputs[pipe.to.port])
  if (!status.ok) return status
  if (document.pipes.some(existing => existing.to.node === pipe.to.node && existing.to.port === pipe.to.port)) {
    return { ok: false, reason: 'Input already connected; disconnect it first' }
  }
  const reaches = (id, visited = new Set()) => {
    if (id === pipe.from.node) return true
    if (visited.has(id)) return false
    visited.add(id)
    return document.pipes.filter(edge => edge.from.node === id).some(edge => reaches(edge.to.node, visited))
  }
  return reaches(pipe.to.node) ? { ok: false, reason: 'Cycles are refused' } : { ok: true, reason: '' }
}
