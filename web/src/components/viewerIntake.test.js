import { expect, it } from 'vitest'
import { blockDefinitions } from './viewerIntake.js'

it('blockDefinitions reads the server object form without changing records', () => {
  const block = { base: [0, 0, 0], count: 2, complete: true, children: [] }
  const definitions = blockDefinitions({ blocks: { BLK: block } })
  expect(definitions.get('BLK')).toBe(block)
  expect(definitions.size).toBe(1)
})

it('blockDefinitions uppercases server keys', () => {
  const block = { base: [0, 0, 0] }
  expect(blockDefinitions({ blocks: { blk: block } }).get('BLK')).toBe(block)
})

it('blockDefinitions reads the engine array form', () => {
  const block = { name: 'blk', base: [0, 0, 0] }
  const definitions = blockDefinitions({ blocks: [block] })
  expect(definitions.get('BLK')).toBe(block)
  expect(definitions.size).toBe(1)
})

it('blockDefinitions accepts absent and empty blocks', () => {
  for (const intake of [undefined, null, {}, { blocks: null }, { blocks: {} }, { blocks: [] }]) {
    expect(blockDefinitions(intake).size).toBe(0)
  }
})

it('blockDefinitions ignores primitive blocks without throwing', () => {
  for (const blocks of ['blk', 3, true]) {
    expect(() => blockDefinitions({ blocks })).not.toThrow()
    expect(blockDefinitions({ blocks }).size).toBe(0)
  }
})

it('blockDefinitions skips object values that are not plain objects', () => {
  const block = {}
  const definitions = blockDefinitions({ blocks: { A: 'x', B: null, C: block, D: [] } })
  expect([...definitions]).toEqual([['C', block]])
})

it('blockDefinitions skips array items without usable names', () => {
  const block = { name: 'ok' }
  const definitions = blockDefinitions({ blocks: [{}, { name: '' }, { name: 3 }, block, null, [], 'x'] })
  expect([...definitions]).toEqual([['OK', block]])
})

it('blockDefinitions reads at most 500 object entries', () => {
  const blocks = Object.fromEntries(Array.from({ length: 600 }, (_, i) => [`block${i}`, {}]))
  const definitions = blockDefinitions({ blocks })
  expect(definitions.size).toBe(500)
  expect(definitions.has('BLOCK499')).toBe(true)
  expect(definitions.has('BLOCK500')).toBe(false)
})

it('blockDefinitions resolves the APS inspection regression shape', () => {
  // Regression for w6-viewer-block-definitions: da/intake_parse.py emits this
  // name-keyed shape, which made (intake.blocks || []).map(...) throw
  // TypeError: ... .map is not a function and crash the whole workspace.
  const block = { base: [0, 0, 0], count: 1, complete: false, children: [] }
  const intake = { blocks: { BLK: block } }
  expect(() => blockDefinitions(intake)).not.toThrow()
  expect(blockDefinitions(intake).get('BLK')).toBe(block)
})

it('blockDefinitions bounds array reads even when entries are invalid', () => {
  const blocks = Array.from({ length: 500 }, () => ({}))
  blocks.push({ name: 'beyond' })
  expect(blockDefinitions({ blocks }).size).toBe(0)
})

it('blockDefinitions ignores inherited object entries', () => {
  const blocks = Object.create({ inherited: {} })
  blocks.own = {}
  expect([...blockDefinitions({ blocks }).keys()]).toEqual(['OWN'])
})

it('blockDefinitions lets later case-insensitive duplicates overwrite earlier ones', () => {
  const first = { name: 'blk' }
  const last = { name: 'BLK' }
  for (const blocks of [[first, last], { blk: first, BLK: last }]) {
    const definitions = blockDefinitions({ blocks })
    expect(definitions.size).toBe(1)
    expect(definitions.get('BLK')).toBe(last)
  }
})
