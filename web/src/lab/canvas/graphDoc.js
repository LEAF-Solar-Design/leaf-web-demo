import { canonicalJSON } from '../contract.js'
import { contract, manifestFor, registry } from './registry.js'
import { connectionStatus } from './portCompat.js'

export const emptyGraph = () => ({ schema: 'leaf.lab-graph.v1', blocks: [], pipes: [], outputs: {} })
export const pipeId = pipe => `${pipe.from.node}/${pipe.from.port}>${pipe.to.node}/${pipe.to.port}`
export const roundTrip = document => JSON.parse(canonicalJSON(document))

function settingsDefaults(schema) {
  if (Object.hasOwn(schema, 'default')) return roundTrip(schema.default)
  if (schema.type !== 'object') return undefined
  const defaults = Object.fromEntries(Object.entries(schema.properties || {}).flatMap(([name, property]) => {
    const value = settingsDefaults(property)
    return value === undefined ? [] : [[name, value]]
  }))
  return Object.keys(defaults).length ? defaults : undefined
}

export function addBlock(document, manifest) {
  if (document.blocks.length >= 200) throw new Error('Graph has 200 blocks')
  let index = 1
  while (document.blocks.some(block => block.id === `${manifest.id}-${index}`)) index += 1
  return { ...document, blocks: [...document.blocks, {
    id: `${manifest.id}-${index}`,
    registry: { id: manifest.id, version: manifest.version.semver, code_sha256: manifest.version.code_sha256 },
    settings: settingsDefaults(manifest.settings_schema) ?? {},
  }] }
}

export function connectPipe(document, pipe, entries = registry) {
  if (document.pipes.length >= 400) throw new Error('Graph has 400 pipes')
  const result = connectionStatus(document, pipe, entries)
  if (!result.ok) throw new Error(result.reason)
  return { ...document, pipes: [...document.pipes, roundTrip(pipe)] }
}
export function disconnectPipe(document, id) {
  return { ...document, pipes: document.pipes.filter(pipe => pipeId(pipe) !== id) }
}
export function removeBlock(document, id) {
  return { ...document, blocks: document.blocks.filter(block => block.id !== id),
    pipes: document.pipes.filter(pipe => pipe.from.node !== id && pipe.to.node !== id),
    outputs: Object.fromEntries(Object.entries(document.outputs).filter(([, endpoint]) => endpoint.node !== id)) }
}
export function updateSettings(document, id, settings, entries = registry) {
  const block = document.blocks.find(item => item.id === id)
  if (!block) throw new Error('Block missing')
  contract.validate(settings, manifestFor(block, entries).settings_schema)
  const next = { ...document, blocks: document.blocks.map(item => item.id === id ? { ...item, settings } : item) }
  contract.schema(next, 'lab-graph.v1.schema.json')
  return roundTrip(next)
}
export function missingInputs(document, block, entries = registry) {
  return Object.entries(manifestFor(block, entries).inputs).filter(([name, pin]) =>
    pin.required && !pin.has_default && !document.pipes.some(pipe => pipe.to.node === block.id && pipe.to.port === name))
    .map(([name]) => name)
}
export function inputValues(document, block, upstream = {}, entries = registry) {
  return Object.fromEntries(Object.entries(manifestFor(block, entries).inputs).flatMap(([name, pin]) => {
    const pipe = document.pipes.find(edge => edge.to.node === block.id && edge.to.port === name)
    if (pipe) return Object.hasOwn(upstream, `${pipe.from.node}/${pipe.from.port}`)
      ? [[name, upstream[`${pipe.from.node}/${pipe.from.port}`]]] : []
    return pin.has_default ? [[name, pin.default]] : []
  }))
}
export function validateGraph(document, entries = registry) {
  const started = performance.now()
  try { contract.graph(document, entries); return { ready: true, problems: [], message: 'Ready', ms: performance.now() - started } }
  catch (error) { return { ready: false, problems: [error.message], message: error.message, ms: performance.now() - started } }
}
