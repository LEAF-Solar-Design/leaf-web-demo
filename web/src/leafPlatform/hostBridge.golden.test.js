// @vitest-environment jsdom
// Golden wire fixture from the AutoCAD plugin (Branch2025 Tests/Fixtures/web-bridge), vendored
// byte-for-byte. Every case must be accepted exactly as the plugin posts it, and tampered copies
// must be refused, so the page and the plugin cannot drift on canonicalization or session checks.
import { webcrypto } from 'node:crypto'
import { TextEncoder } from 'node:util'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import golden from './__fixtures__/leaf.web-bridge.v1.golden.json'
import { createLeafHostBridge, PROTOCOL_VERSION } from './hostBridge.js'

const CASE_NAMES = ['ready', 'selection-handles', 'selection-empty', 'bind-accepted', 'bind-rejected',
  'callback-applied', 'callback-stale', 'callback-rejected']
const ENVELOPE_NAMES = CASE_NAMES.filter((name) => name !== 'ready')
const UNBOUND_REVISION = '00000000-0000-0000-0000-000000000000'
const location = { origin: golden.session.origin }

function goldenCase(name) {
  const found = golden.cases.find((entry) => entry.name === name)
  if (!found) throw new Error(`golden fixture ${golden.schema} is missing case "${name}"; regenerate or restore it`)
  return found
}

// A fresh parse per delivery: WebView2 hands the page a new object for every posted message.
const wire = (name) => JSON.parse(goldenCase(name).wire)

// The fixture carries no unbound handshake; the plugin sends one for the same session before a bind.
function unboundFrom(ready) {
  const unbound = { ...ready, kind: 'host_bridge_unbound', drawingRevision: UNBOUND_REVISION }
  for (const key of ['platformTenantId', 'projectId', 'drawingId', 'drawingVersionId', 'bridgeEndpoint']) delete unbound[key]
  return unbound
}

const midpoint = (envelope) => new Date((Date.parse(envelope.issuedAt) + Date.parse(envelope.expiresAt)) / 2)

function fakeChannel() {
  const listeners = new Set()
  return {
    postMessage: vi.fn(),
    addEventListener: vi.fn((type, listener) => listeners.add(listener)),
    removeEventListener: vi.fn((type, listener) => listeners.delete(listener)),
    emit: (data) => { for (const listener of listeners) listener({ data }) },
  }
}

describe('Studio host bridge against the plugin golden wire fixture', () => {
  let bridge, channel, observe, state, randomUUID
  beforeEach(() => {
    randomUUID = vi.fn(() => webcrypto.randomUUID())
    vi.stubGlobal('crypto', { subtle: webcrypto.subtle, getRandomValues: (array) => webcrypto.getRandomValues(array), randomUUID })
    vi.stubGlobal('TextEncoder', TextEncoder)
    vi.useFakeTimers()
    channel = fakeChannel()
    bridge = createLeafHostBridge({ channel, location })
    observe = vi.fn((next) => { state = next })
    bridge.subscribe(observe)
    bridge.start()
  })
  afterEach(() => {
    bridge.stop()
    vi.useRealTimers()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  // Pins the clock inside the envelope's window, then puts the bridge in the state that envelope targets.
  async function arrive(name) {
    const envelope = wire(name)
    vi.setSystemTime(midpoint(envelope))
    await bridge.receive(wire('ready'))
    if (envelope.verb === 'drawing.bind_result') await bridge.receive(unboundFrom(wire('ready')))
    if (envelope.verb !== 'host.callback') return { envelope, outcome: null }
    let posted
    const sent = new Promise((resolve) => { posted = resolve })
    channel.postMessage.mockImplementationOnce(posted)
    randomUUID.mockReturnValueOnce(envelope.payload.commandId)
    const outcome = bridge.focusObject({ objectHandles: ['A1', 'B2'] }, 'select')
    expect((await sent).payload.commandId).toBe(envelope.payload.commandId)
    return { envelope, outcome }
  }

  it('is the golden schema for this protocol and carries every case', () => {
    expect(golden.schema).toBe('leaf.web-bridge.golden.v1')
    expect(golden.protocolVersion, 'fixture protocolVersion differs from the page PROTOCOL_VERSION').toBe(PROTOCOL_VERSION)
    expect(golden.testOnly).toBe(true)
    const names = golden.cases.map((entry) => entry.name)
    const missing = CASE_NAMES.filter((name) => !names.includes(name))
    expect(missing, `golden fixture dropped cases: ${missing.join(', ')}`).toEqual([])
    for (const name of CASE_NAMES) {
      const entry = goldenCase(name)
      const parsed = wire(name)
      if (name === 'ready') {
        expect(entry.kind).toBe('ready-object')
        expect(entry.signature).toBeNull()
        expect(parsed.protocolVersion).toBe(PROTOCOL_VERSION)
      } else {
        expect(entry.kind, name).toBe('envelope')
        expect(parsed.verb, name).toBe(entry.verb)
        expect(parsed.signature, name).toBe(entry.signature)
        expect(parsed.protocolVersion, name).toBe(PROTOCOL_VERSION)
      }
    }
  })

  it('connects on the ready case and holds the fixture session key', () => {
    channel.emit(wire('ready'))
    expect(state.status).toBe('connected')
    expect(state.ready.sessionId).toBe(golden.session.sessionId)
    expect(state.ready.nonce).toBe(golden.session.nonce)
    expect(state.ready.origin).toBe(golden.session.origin)
    expect(Buffer.from(state.ready.sessionKey, 'base64').toString('hex')).toBe(golden.session.sessionKeyHex)
  })

  it('ignores the ready case on a page at a different origin', async () => {
    const foreign = createLeafHostBridge({ channel: fakeChannel(), location: { origin: 'https://foreign.example' } })
    foreign.start()
    await foreign.receive(wire('ready'))
    expect(foreign.state.status).toBe('connecting')
    expect(foreign.state.ready).toBeNull()
    foreign.stop()
  })

  it('accepts the handle selection case', async () => {
    await arrive('selection-handles')
    observe.mockClear()
    await bridge.receive(wire('selection-handles'))
    expect(observe).toHaveBeenCalledTimes(1)
    expect(state.selectedObjectId).toBeNull()
    expect(state.selectedHandles).toEqual(['A1', 'B2'])
  })

  it('accepts the empty selection case and clears a prior selection', async () => {
    await arrive('selection-handles')
    await bridge.receive(wire('selection-handles'))
    expect(state.selectedHandles).toEqual(['A1', 'B2'])
    vi.setSystemTime(midpoint(wire('selection-empty')))
    observe.mockClear()
    await bridge.receive(wire('selection-empty'))
    expect(observe).toHaveBeenCalledTimes(1)
    expect(state.selectedObjectId).toBeNull()
    expect(state.selectedHandles).toBeNull()
  })

  it.each([
    ['bind-accepted', 'DWG connected. Starting the signed cross-probe session.'],
    ['bind-rejected', 'DWG connection was not changed (user declined).'],
  ])('accepts the %s case', async (name, bindingResult) => {
    const { envelope } = await arrive(name)
    expect(envelope.drawingRevision).toBe(UNBOUND_REVISION)
    expect(state.status).toBe('unbound')
    observe.mockClear()
    await bridge.receive(wire(name))
    expect(observe).toHaveBeenCalledTimes(1)
    expect(state.bindingResult).toBe(bindingResult)
  })

  it.each([
    ['callback-applied', 'applied', null],
    ['callback-stale', 'stale', 'stale_document'],
    ['callback-rejected', 'rejected', 'action_not_allowed'],
  ])('accepts the %s case', async (name, status, reason) => {
    const { outcome } = await arrive(name)
    await bridge.receive(wire(name))
    await expect(outcome).resolves.toEqual({ action: 'select', status, reason })
    expect(state.lastCommand).toEqual({ action: 'select', status, reason })
  })

  it.each(ENVELOPE_NAMES)('refuses tampered copies of %s, then accepts the genuine one', async (name) => {
    const { envelope, outcome } = await arrive(name)
    const digit = envelope.signature[0] === '0' ? '1' : '0'
    const tampered = [
      { ...wire(name), signature: `${digit}${envelope.signature.slice(1)}` },
      { ...wire(name), sessionId: '99999999-9999-4999-8999-999999999999' },
      { ...wire(name), origin: 'https://foreign.example' },
    ]
    const before = state
    observe.mockClear()
    for (const message of tampered) await bridge.receive(message)
    expect(observe, `${name}: a tampered copy changed bridge state`).not.toHaveBeenCalled()
    expect(state).toBe(before)
    expect(bridge.seenHostMessages.has(envelope.messageId)).toBe(false)
    await bridge.receive(wire(name))
    expect(observe, `${name}: the genuine envelope was not accepted after the tampered copies`).toHaveBeenCalledTimes(1)
    if (outcome) await expect(outcome).resolves.toMatchObject({ status: envelope.payload.status })
  })
})
