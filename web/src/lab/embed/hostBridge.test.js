import { createHash, webcrypto } from 'node:crypto'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { canonicalJSON, createContract } from '../contract.js'
import embedSchema from '../contract/lab-embed.v1.schema.json'
import graphSchema from '../contract/lab-graph.v1.schema.json'
import manifestSchema from '../contract/leaf-block-manifest.v1.schema.json'
import { createLabHostBridge, mintChannel, sessionPrincipalProof, sourceShaFromGit, studioSourceSha } from './hostBridge.js'
import { DEFAULT_HOST_ORIGINS, hostOrigins } from './hostOrigins.js'

const document = { schema: 'leaf.lab-graph.v1', blocks: [], pipes: [], outputs: {} }
const draft = { document, ready: true, problems: [] }
const hash = (value) => createHash('sha256').update(canonicalJSON(value)).digest('hex')
const rawHash = (value) => createHash('sha256').update(value).digest('hex')
const schema = createContract({
  'lab-embed.v1.schema.json': embedSchema,
  'lab-graph.v1.schema.json': graphSchema,
  'fixtures/lab/leaf-block-manifest.v1.schema.json': manifestSchema,
})

function harness(options = {}) {
  const parent = { postMessage: vi.fn() }
  const listeners = new Set()
  const win = {
    self: {}, top: {}, parent,
    addEventListener: vi.fn((kind, callback) => listeners.add(callback)),
    removeEventListener: vi.fn((kind, callback) => listeners.delete(callback)),
  }
  const bridge = createLabHostBridge({
    win, cryptoImpl: webcrypto, principalProof: 'a'.repeat(64), sourceSha: 'b'.repeat(64),
    now: () => 1000, clock: () => 50, ...options,
  })
  const hello = () => parent.postMessage.mock.calls[0][0]
  const event = (overrides = {}, source = parent, origin = DEFAULT_HOST_ORIGINS[0]) => ({
    source, origin, data: { ...hello(), payload: { accepted: true }, ...overrides },
  })
  return { bridge, win, parent, hello, event, listeners,
    admit: () => bridge.receive(event()),
    sent: () => parent.postMessage.mock.calls.slice(DEFAULT_HOST_ORIGINS.length).map(([message]) => message),
  }
}

afterEach(() => vi.unstubAllGlobals())

describe('Studio host bootstrap', () => {
  it('mints a cryptographic lowercase hex channel and hashes the raw Unicode subject', async () => {
    const random = vi.fn((bytes) => bytes.fill(171))
    expect(mintChannel({ getRandomValues: random })).toBe('ab'.repeat(16))
    expect(random).toHaveBeenCalledWith(expect.any(Uint8Array))
    expect(harness().bridge.channel).not.toBe(harness().bridge.channel)
    const subject = 'auth0|studio-用户'
    const encoded = btoa(String.fromCharCode(...new TextEncoder().encode(JSON.stringify({ sub: subject }))))
    const proof = await sessionPrincipalProof({ storage: { getItem: () => `header.${encoded}.signature` }, cryptoImpl: webcrypto })
    expect(proof).toBe(rawHash(subject))
    expect(proof).not.toContain(subject)
    await expect(sessionPrincipalProof({ storage: { getItem: () => null }, cryptoImpl: webcrypto })).rejects.toThrow()
    await expect(sessionPrincipalProof({ storage: { getItem: () => 'h.eyJzdWIiOjR9.s' }, cryptoImpl: webcrypto })).rejects.toThrow()
  })

  it('replaces build-time origins without accepting wildcards, paths or credentials', () => {
    expect(hostOrigins()).toEqual(DEFAULT_HOST_ORIGINS)
    expect(hostOrigins(' https://host.example,https://host.example,*,https://host.example/path,https://user@host.example '))
      .toEqual(['https://host.example'])
    expect(hostOrigins('')).toEqual([])
  })

  it('derives a stable sha256 from full and abbreviated build Git SHA strings without fetching', async () => {
    const fetchImpl = vi.fn()
    const digest = vi.fn((...args) => webcrypto.subtle.digest(...args))
    const cryptoImpl = { subtle: { digest } }
    for (const gitSha of ['a'.repeat(40), 'abcdef01']) {
      expect(await sourceShaFromGit(gitSha, webcrypto)).toBe(rawHash(gitSha))
      expect(await studioSourceSha({ gitSha, cryptoImpl, fetchImpl })).toBe(rawHash(gitSha))
      expect(await studioSourceSha({ gitSha, cryptoImpl, fetchImpl })).toBe(rawHash(gitSha))
    }
    expect(digest).toHaveBeenCalledTimes(2)
    expect(fetchImpl).not.toHaveBeenCalled()
    expect(() => sourceShaFromGit('unknown', webcrypto)).toThrow()
  })

  it('hashes the health Git SHA with only one fetch when no build revision is available', async () => {
    const gitSha = 'c'.repeat(40)
    const fetchImpl = vi.fn(async () => ({ ok: true, json: async () => ({ source_sha: gitSha }) }))
    const options = { gitSha: null, cryptoImpl: webcrypto, fetchImpl }
    expect(await studioSourceSha(options)).toBe(rawHash(gitSha))
    expect(await studioSourceSha(options)).toBe(rawHash(gitSha))
    expect(fetchImpl).toHaveBeenCalledOnce()
    expect(fetchImpl).toHaveBeenCalledWith('/api/health')
  })

  it('sends one complete hello to every allowed origin, starts once and never uses a wildcard', async () => {
    const h = harness()
    expect(await h.bridge.proposal(draft)).toBe(false)
    expect(await h.bridge.run()).toBe(false)
    expect(await h.bridge.start()).toBe(true)
    await h.bridge.start()
    expect(h.parent.postMessage.mock.calls.map(([, origin]) => origin)).toEqual(DEFAULT_HOST_ORIGINS)
    expect(h.hello()).toMatchObject({
      schema: 'leaf.lab-embed.v1', version: 1, kind: 'shell.ready', payload: {},
      channel: expect.stringMatching(/^[0-9a-f]{32}$/), principal_proof: 'a'.repeat(64),
      source_sha: 'b'.repeat(64), proposal_revision: 0, proposal_hash: hash({}),
      sequence: 0, sent_at_ms: 1000, expires_at_ms: 31000, latency_us: 0,
    })
    expect(() => schema.schema(h.hello(), 'lab-embed.v1.schema.json')).not.toThrow()
    expect(await h.bridge.proposal(draft)).toBe(false)
    h.bridge.stop()
    expect(h.listeners.size).toBe(0)
  })

  it('admits only the first allowed parent ack, then locks the exact origin', async () => {
    const h = harness()
    await h.bridge.start()
    expect(await h.bridge.receive(h.event({}, {}, DEFAULT_HOST_ORIGINS[0]))).toBe(false)
    expect(await h.bridge.receive(h.event({}, h.parent, 'https://unlisted.example'))).toBe(false)
    expect(await h.bridge.receive(h.event({ version: 2 }))).toBe(false)
    expect(await h.bridge.receive(h.event({ channel: '0'.repeat(32) }))).toBe(false)
    expect(await h.bridge.receive(h.event({ principal_proof: '0'.repeat(64) }))).toBe(false)
    expect(await h.bridge.receive(h.event({ payload: {} }))).toBe(false)
    expect(await h.admit()).toBe(true)
    expect(h.bridge.admitted).toBe(true)
    expect(h.bridge.lockedOrigin).toBe(DEFAULT_HOST_ORIGINS[0])
    expect(await h.bridge.receive(h.event({ sequence: 1 }, h.parent, DEFAULT_HOST_ORIGINS[1]))).toBe(false)
    await h.bridge.proposal(draft, 48)
    expect(h.parent.postMessage.mock.calls.at(-1)[1]).toBe(DEFAULT_HOST_ORIGINS[0])
    expect(h.sent()[0]).toMatchObject({ kind: 'proposal', payload: draft, proposal_revision: 1,
      proposal_hash: hash(document), sequence: 1, latency_us: 2000 })
    h.bridge.stop()
  })

  it('posts nothing when unframed, unconfigured, unauthenticated or stopped during bootstrap', async () => {
    for (const options of [{ origins: [] }, { principalProof: '' }, { sourceSha: 'unknown' }]) {
      const h = harness(options)
      expect(await h.bridge.start()).toBe(false)
      expect(h.parent.postMessage).not.toHaveBeenCalled()
      h.bridge.stop()
    }
    const h = harness()
    h.win.self = h.win.top
    expect(await h.bridge.start()).toBe(false)
    expect(h.parent.postMessage).not.toHaveBeenCalled()
    const pending = harness({ principalProof: Promise.resolve('a'.repeat(64)) })
    const started = pending.bridge.start()
    pending.bridge.stop()
    expect(await started).toBe(false)
    expect(pending.parent.postMessage).not.toHaveBeenCalled()
  })
})

describe('admitted Lab channel', () => {
  it('rejects a restore with a bad hash before touching the canvas or replay state', async () => {
    const onRestore = vi.fn()
    const h = harness({ onRestore })
    await h.bridge.start()
    await h.admit()
    const restore = h.event({ kind: 'proposal', sequence: 1,
      proposal_hash: '0'.repeat(64), payload: { ...draft, restore: true } })
    expect(await h.bridge.receive(restore)).toBe(false)
    expect(onRestore).not.toHaveBeenCalled()
    restore.data.proposal_hash = hash(document)
    expect(await h.bridge.receive(restore)).toBe(true)
    expect(onRestore).toHaveBeenCalledOnce()
    h.bridge.stop()
  })

  it('waits for a restored canvas to apply, acknowledges it and signs the restored document hash', async () => {
    let applied
    const onRestore = vi.fn(() => new Promise((resolve) => { applied = resolve }))
    const h = harness({ onRestore })
    await h.bridge.start()
    await h.admit()
    const restored = h.bridge.receive(h.event({ kind: 'proposal', sequence: 1,
      proposal_revision: 3, proposal_hash: hash(document), payload: { restore: true, ...draft } }))
    await vi.waitFor(() => expect(onRestore).toHaveBeenCalledOnce())
    expect(h.sent()).toEqual([])
    applied()
    expect(await restored).toBe(true)
    expect(h.sent()[0]).toMatchObject({ kind: 'shell.ready', payload: { restored: true }, sequence: 1,
      proposal_revision: 0, proposal_hash: hash({}) })
    await h.bridge.run()
    expect(h.sent()[1]).toMatchObject({ kind: 'signed-request', payload: { request_hash: hash(document), handoff: 'server-sign' } })
    h.bridge.stop()
  })

  it('serializes every committed draft and Run so requests bind the current document', async () => {
    const h = harness()
    await h.bridge.start()
    await h.admit()
    const next = { ...draft, document: { ...document, outputs: { result: { node: 'strategy-1', port: 'result' } } }, ready: false, problems: ['Input required'] }
    await Promise.all([h.bridge.proposal(draft), h.bridge.proposal(next), h.bridge.run()])
    const sent = h.sent()
    expect(sent.map((message) => message.sequence)).toEqual([1, 2, 3])
    expect(sent[1]).toMatchObject({ kind: 'proposal', payload: next, proposal_revision: 2, proposal_hash: hash(next.document) })
    expect(sent[2]).toMatchObject({ kind: 'signed-request', proposal_revision: 0, proposal_hash: hash({}),
      payload: { request_hash: hash(next.document), handoff: 'server-sign' } })
    h.bridge.stop()
  })

  it('stores validated source rows and counts replayed, malformed and out-of-order messages', async () => {
    const onSource = vi.fn()
    const h = harness({ onSource })
    await h.bridge.start()
    await h.admit()
    const rows = [{ price: 1 }]
    const source = h.event({ kind: 'source.value', sequence: 2, payload: { rows } })
    expect(await h.bridge.receive(source)).toBe(true)
    expect(onSource).toHaveBeenCalledWith(rows)
    expect(h.bridge.sourceRows).toEqual(rows)
    expect(await h.bridge.receive(source)).toBe(false)
    expect(await h.bridge.receive(h.event({ kind: 'source.value', sequence: 1, payload: { rows: [] } }))).toBe(false)
    expect(await h.bridge.receive(h.event({ kind: 'source.value', sequence: 3, payload: { rows: Array(201).fill({}) } }))).toBe(false)
    expect(await h.bridge.receive(h.event({ kind: 'source.value', sequence: 3, payload: { rows: [] }, expires_at_ms: 1000 }))).toBe(false)
    expect(await h.bridge.receive(h.event({ kind: 'source.value', sequence: 3, payload: { rows: [] } }))).toBe(true)
    expect(h.bridge.rejectedCount).toBe(4)
    h.bridge.stop()
  })

  it('uses the real message listener and stops all posting on a host source change', async () => {
    const onReload = vi.fn()
    const h = harness({ onReload })
    await h.bridge.start()
    await h.admit()
    for (const listener of h.listeners) listener(h.event({ sequence: 1, source_sha: 'c'.repeat(64) }))
    await vi.waitFor(() => expect(onReload).toHaveBeenCalledOnce())
    expect(h.bridge.admitted).toBe(false)
    expect(await h.bridge.proposal(draft)).toBe(false)
    expect(await h.bridge.run()).toBe(false)
    expect(h.sent()).toEqual([])
    expect(h.listeners.size).toBe(0)
  })
})

describe('amended v1 payload schemas', () => {
  it.each([{ accepted: true }, { restored: true }, {}])('accepts shell.ready %j and rejects extra or non-boolean fields', async (payload) => {
    const h = harness()
    await h.bridge.start()
    const message = { ...h.hello(), payload }
    expect(() => schema.schema(message, 'lab-embed.v1.schema.json')).not.toThrow()
    for (const invalid of [{ accepted: 'true' }, { restored: 1 }, { intent: 'ready' }]) {
      expect(() => schema.schema({ ...message, payload: invalid }, 'lab-embed.v1.schema.json')).toThrow()
    }
    h.bridge.stop()
  })

  it('accepts proposal drafts and restores, rejects missing, extra and invalid fields', async () => {
    const h = harness()
    await h.bridge.start()
    const message = { ...h.hello(), kind: 'proposal', payload: draft }
    expect(() => schema.schema(message, 'lab-embed.v1.schema.json')).not.toThrow()
    expect(() => schema.schema({ ...message, payload: { ...draft, restore: true } }, 'lab-embed.v1.schema.json')).not.toThrow()
    for (const payload of [{}, { ...draft, extra: true }, { ...draft, ready: 'yes' }, { ...draft, restore: 'yes' },
      { ...draft, problems: Array(65).fill('problem') }, { ...draft, problems: [1] }, { ...draft, document: {} }]) {
      expect(() => schema.schema({ ...message, payload }, 'lab-embed.v1.schema.json')).toThrow()
    }
    h.bridge.stop()
  })
})
