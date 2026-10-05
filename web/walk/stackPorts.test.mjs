import assert from 'node:assert/strict'
import { randomUUID } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import http from 'node:http'
import { createServer as createNetServer } from 'node:net'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import { allocatePorts, QueuedError } from './stack.mjs'

async function close(server) {
  const done = new Promise((accept, reject) => server.close((error) => error ? reject(error) : accept()))
  server.closeAllConnections()
  await done
}

async function leaseHarness({ realPorts = false } = {}) {
  const source = await readFile(new URL('./stack.mjs', import.meta.url), 'utf8')
  const leases = source.slice(source.indexOf('async function acquireLease'), source.indexOf('async function privateEnvironment'))
  const allocation = source.slice(source.indexOf('export async function allocatePorts'), source.indexOf('function commandArgs'))
    .replace('export async function', 'async function')
  const launch = source.slice(source.indexOf('    state.lease = await acquireLease'), source.indexOf('    // Fail closed if a repo-local dotenv'))
  const locks = new Map()
  const open = async (path, mode) => {
    assert.equal(mode, 'wx')
    if (locks.has(path)) throw Object.assign(new Error('occupied'), { code: 'EEXIST' })
    locks.set(path, '')
    return { writeFile: async (body) => locks.set(path, body), close: async () => {} }
  }
  const createServer = () => ({
    once: () => {}, listen: (options, ready) => ready(), close: (done) => done(),
  })
  const harness = new Function('tmpdir', 'join', 'dirname', 'randomUUID', 'open', 'readFile', 'writeFile', 'rm', 'pidAlive',
    'QueuedError', 'readFileSync', 'rmSync', 'createServer', 'roles',
    `${leases}\n${allocation}\nreturn {
      claim: async (slot) => { const state = {}; const cap = 2; ${launch} return state },
      release: (state) => releaseLeaseSync(state.lease),
    }`)(() => 'private-test-leases', join, dirname, randomUUID, open,
    async (path) => {
      if (!locks.has(path)) throw Object.assign(new Error('missing'), { code: 'ENOENT' })
      return locks.get(path)
    }, async (path, body) => locks.set(path, body), async (path) => locks.delete(path), () => true,
    QueuedError, (path) => {
      if (!locks.has(path)) throw Object.assign(new Error('missing'), { code: 'ENOENT' })
      return locks.get(path)
    }, (path) => locks.delete(path), realPorts ? createNetServer : createServer,
    ['app', 'broker', 'harness', 'web', 'proxy'])
  return { ...harness, locks }
}

test('stack ports follow machine lease indices across distinct and reused worker slots', async () => {
  const harness = await leaseHarness()
  const { locks } = harness
  const first = await harness.claim(12)
  const second = await harness.claim(13)
  try {
    assert.equal(first.lease.index, 0)
    assert.equal(second.lease.index, 1)
    assert.equal(JSON.parse(locks.get(first.lease.path)).slot, 12)
    assert.equal(JSON.parse(locks.get(second.lease.path)).slot, 13)
    assert.deepEqual(first.ports, { app: 18010, broker: 18020, harness: 18030, web: 18040, proxy: 18000 })
    assert.deepEqual(second.ports, { app: 18110, broker: 18120, harness: 18130, web: 18140, proxy: 18100 })
    assert.ok(Object.values(first.ports).every((port) => !Object.values(second.ports).includes(port)))
    harness.release(first)
    const reused = await harness.claim(12)
    try {
      assert.equal(reused.lease.index, 0)
      assert.notEqual(reused.lease.token, first.lease.token)
      assert.notDeepEqual(reused.ports, first.ports)
    } finally { harness.release(reused) }
  } finally {
    harness.release(first)
    harness.release(second)
  }
  assert.equal([...locks.keys()].filter((path) => path.endsWith('.lock')).length, 0)
})

test('two consecutive acquisitions of the same lease index choose different port blocks', async () => {
  const harness = await leaseHarness()
  const first = await harness.claim(12)
  harness.release(first)
  const second = await harness.claim(12)
  try {
    assert.equal(second.lease.index, first.lease.index)
    assert.equal(first.block, 0)
    assert.equal(second.block, 2)
    assert.notEqual(second.ports.app, first.ports.app)
    assert.equal(JSON.parse(harness.locks.get(second.lease.path)).block, second.block)
    assert.equal(harness.locks.get(join(dirname(second.lease.path), 'leaf-walk-local-slot-0.gen')), '2\n')
  } finally { harness.release(second) }
})

test('different lease indices never choose the same block over 20 acquisitions each', async () => {
  const harness = await leaseHarness()
  const blocks = [new Set(), new Set()]
  for (let cycle = 0; cycle < 20; cycle++) {
    const first = await harness.claim(12)
    let second
    try {
      second = await harness.claim(13)
      assert.equal(first.lease.index, 0)
      assert.equal(second.lease.index, 1)
      blocks[0].add(first.block)
      blocks[1].add(second.block)
    } finally {
      harness.release(first)
      if (second) harness.release(second)
    }
  }
  assert.equal(blocks[0].size, 20)
  assert.equal(blocks[1].size, 20)
  assert.ok([...blocks[0]].every((block) => !blocks[1].has(block)))
})

test('an occupied app port is skipped and the next block is returned', async () => {
  const harness = await leaseHarness({ realPorts: true })
  let block
  for (let candidate = 400; candidate < 472; candidate += 2) {
    try {
      await allocatePorts(candidate)
      await allocatePorts(candidate + 2)
      block = candidate
      break
    } catch (error) {
      if (!/ port \d+ is unavailable:/.test(error.message)) throw error
    }
  }
  assert.ok(Number.isInteger(block), 'two adjacent test blocks must be available')
  harness.locks.set(join('private-test-leases', 'leaf-walk-local-slot-0.gen'), `${block / 2}\n`)
  const occupied = http.createServer()
  let state
  try {
    await new Promise((accept, reject) => {
      occupied.once('error', reject)
      occupied.listen(18000 + 100 * block + 10, '127.0.0.1', accept)
    })
    state = await harness.claim(12)
    assert.equal(state.block, block + 2)
    assert.equal(state.ports.app, 18000 + 100 * (block + 2) + 10)
    assert.equal(harness.locks.get(join('private-test-leases', 'leaf-walk-local-slot-0.gen')), `${block / 2 + 2}\n`)
  } finally {
    if (state) harness.release(state)
    if (occupied.listening) await close(occupied)
  }
})

test('a malformed generation file falls back to zero', async () => {
  const harness = await leaseHarness()
  const path = join('private-test-leases', 'leaf-walk-local-slot-0.gen')
  harness.locks.set(path, 'not-a-generation\n')
  const state = await harness.claim(12)
  try {
    assert.equal(state.block, 0)
    assert.equal(state.ports.app, 18010)
    assert.equal(harness.locks.get(path), '1\n')
  } finally { harness.release(state) }
})
