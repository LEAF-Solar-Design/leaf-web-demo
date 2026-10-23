import { describe, expect, it } from 'vitest'
import { compatible } from '../contract.js'
import { portCompatibility, connectionStatus } from './portCompat.js'
import { addBlock, connectPipe, disconnectPipe, emptyGraph, inputValues, missingInputs, pipeId, removeBlock, roundTrip, updateSettings, validateGraph } from './graphDoc.js'
import { contract, manifests, registry } from './registry.js'

const pin = (overrides = {}) => ({ mode: 'scalar', schema: { type: 'number' }, unit: 'none', nullable: false, required: false, has_default: false, ...overrides })
const manifest = manifests.find(item => item.id === 'adjust-none')
const graph = () => addBlock(addBlock(emptyGraph(), manifest), manifest)
const pipe = { from: { node: 'adjust-none-1', port: 'result' }, to: { node: 'adjust-none-2', port: 'value' } }

describe('contract-compatible wiring', () => {
  it.each([
    [pin(), pin(), true], [pin({ mode: 'stream' }), pin(), false],
    [pin({ unit: 'usd' }), pin(), false], [pin({ nullable: true }), pin(), false],
    [pin({ schema: { type: 'integer' } }), pin(), true],
    [pin(), pin({ schema: { type: 'integer' } }), false],
    [pin({ schema: { type: 'integer', minimum: 2, maximum: 5 } }), pin({ schema: { type: 'number', minimum: 0, maximum: 10 } }), true],
  ])('matches contract for %j -> %j', (output, input, expected) => {
    expect(portCompatibility(output, input).ok).toBe(expected)
    expect(portCompatibility(output, input).ok).toBe(compatible(output, input))
  })
  it('refuses unknown pins, occupied inputs and cycles without mutating the document', () => {
    const original = graph()
    expect(connectionStatus(original, { ...pipe, from: { ...pipe.from, port: 'missing' } }).reason).toMatch(/missing/i)
    const connected = connectPipe(original, pipe)
    expect(original.pipes).toEqual([])
    expect(() => connectPipe(connected, pipe)).toThrow(/already connected/)
    expect(() => connectPipe(connected, { from: { node: pipe.to.node, port: 'result' }, to: { node: pipe.from.node, port: 'value' } })).toThrow(/Cycles/)
    expect(() => connectPipe(original, { from: pipe.from, to: { node: pipe.from.node, port: 'value' } })).toThrow(/Cycles/)
  })
  it('wires, disconnects, removes dependent outputs and round-trips without layout metadata', () => {
    const document = connectPipe(graph(), pipe)
    document.outputs = { value: pipe.from }
    expect(contract.graph(roundTrip(document), registry)).toBe(true)
    expect(roundTrip(document)).toEqual(document)
    expect(Object.keys(document.blocks[0]).sort()).toEqual(['id', 'registry', 'settings'])
    expect(Object.keys(document.pipes[0]).sort()).toEqual(['from', 'to'])
    expect(disconnectPipe(document, pipeId(pipe)).pipes).toEqual([])
    const removed = removeBlock(document, pipe.from.node)
    expect(removed.pipes).toEqual([])
    expect(removed.outputs).toEqual({})
  })
  it('validates settings and rejects secret-bearing and out-of-bounds JSON', () => {
    const document = graph()
    expect(updateSettings(document, 'adjust-none-1', { max_tokens: 128 }).blocks[0].settings).toEqual({ max_tokens: 128 })
    expect(() => updateSettings(document, 'adjust-none-1', { calibration_intercept: 999 })).toThrow()
    expect(() => updateSettings(document, 'adjust-none-1', { api_token: 'x' })).toThrow()
    expect(document.blocks[0].settings).toEqual({})
  })
  it('marks missing required inputs and resolves only declared defaults', () => {
    const required = { ...manifest, inputs: { value: pin({ required: true }), auxiliary: pin({ has_default: true, default: 9 }) } }
    const entries = { ...registry, 'adjust-none@1.0.0': required }
    const document = graph()
    expect(missingInputs(document, document.blocks[1], entries)).toEqual(['value'])
    expect(validateGraph(document, entries).ready).toBe(false)
    expect(inputValues(document, document.blocks[1], {}, entries)).toEqual({ auxiliary: 9 })
    const connected = connectPipe(document, pipe, entries)
    expect(missingInputs(connected, connected.blocks[1], entries)).toEqual([])
    expect(inputValues(connected, connected.blocks[1], { 'adjust-none-1/result': 42 }, entries)).toEqual({ value: 42, auxiliary: 9 })
    expect(inputValues(connected, connected.blocks[1], {}, entries)).toEqual({ auxiliary: 9 })
  })
})
