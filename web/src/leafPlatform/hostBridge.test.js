// @vitest-environment jsdom
import { createHmac, webcrypto } from 'node:crypto'
import { TextEncoder } from 'node:util'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createLeafHostBridge, DISPATCH_MODE, LEAF_PLATFORM_CONTRACT_VERSION, PROTOCOL_VERSION } from './hostBridge.js'

const identity = {
  platformTenantId: '11111111-1111-4111-8111-111111111111',
  projectId: '22222222-2222-4222-8222-222222222222',
  drawingId: '33333333-3333-4333-8333-333333333333',
  drawingVersionId: '44444444-4444-4444-8444-444444444444',
}
const sessionKey = Buffer.alloc(32, 7).toString('base64')
const location = { origin: 'https://platform.leafdesign.ai' }
const common = {
  protocolVersion: PROTOCOL_VERSION, contractVersion: LEAF_PLATFORM_CONTRACT_VERSION,
  sessionId: 'session-1234567890123456', nonce: 'nonce-1234567890123456',
  origin: location.origin, sessionKey, connectedAt: '2026-09-24T12:00:00.000Z',
  documentFingerprint: `sha256:${'a'.repeat(64)}`,
}
const ready = () => ({ ...common, kind: 'host_bridge_ready', ...identity })
const unbound = () => ({ ...common, kind: 'host_bridge_unbound', drawingRevision: '00000000-0000-0000-0000-000000000000' })

// Independent signer: node HMAC verifies the browser SubtleCrypto wire bytes.
function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (value && typeof value === 'object') return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`
  return JSON.stringify(value)
}

function sign(body) {
  return { ...body, signature: createHmac('sha256', Buffer.from(sessionKey, 'base64')).update(canonical(body)).digest('hex') }
}

function envelope(handshake, overrides = {}) {
  const now = Date.now()
  return sign({
    protocolVersion: PROTOCOL_VERSION, sessionId: handshake.sessionId,
    messageId: webcrypto.randomUUID(), issuedAt: new Date(now - 1000).toISOString(),
    expiresAt: new Date(now + 60_000).toISOString(), origin: location.origin,
    verb: 'drawing.bind_result', drawingFingerprint: handshake.documentFingerprint,
    drawingRevision: handshake.drawingRevision ?? handshake.drawingVersionId,
    payload: { accepted: true }, ...overrides,
  })
}

function fakeChannel() {
  const listeners = new Set()
  return {
    postMessage: vi.fn(),
    addEventListener: vi.fn((type, listener) => listeners.add(listener)),
    removeEventListener: vi.fn((type, listener) => listeners.delete(listener)),
    emit: (data) => { for (const listener of listeners) listener({ data }) },
  }
}

function selection(payload) {
  return envelope(ready(), {
    verb: 'drawing.selection_changed',
    payload: { kind: 'selection_event', contractVersion: LEAF_PLATFORM_CONTRACT_VERSION,
      drawingVersionId: identity.drawingVersionId, payload },
  })
}

const invalidHandles = [
  ['missing', undefined], ['null', null], ['string', '2F4A'], ['empty', []],
  ['non-string', [123]], ['empty handle', ['']], ['non-hex', ['2G4A']],
  ['whitespace', [' 2F4A']], ['trailing newline', ['2F4A\n']],
  ['too long', ['A'.repeat(17)]], ['duplicate', ['2F4A', '2F4A']],
  ['case-insensitive duplicate', ['2f4a', '2F4A']],
  ['too many', Array.from({ length: 1001 }, (_, index) => index.toString(16))],
]

describe('Studio AutoCAD host bridge', () => {
  let bridge, channel, observe, state
  beforeEach(() => {
    vi.stubGlobal('crypto', webcrypto)
    vi.stubGlobal('TextEncoder', TextEncoder)
    channel = fakeChannel()
    bridge = createLeafHostBridge({ channel, location })
    observe = vi.fn((next) => { state = next })
    bridge.subscribe(observe)
  })
  afterEach(() => {
    bridge.stop()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('sends hello once until stopped and removes its listener', () => {
    bridge.start()
    bridge.start()
    expect(channel.postMessage).toHaveBeenCalledTimes(1)
    expect(channel.postMessage).toHaveBeenCalledWith({ kind: 'host_bridge_hello', contractVersion: LEAF_PLATFORM_CONTRACT_VERSION })
    expect(state.status).toBe('connecting')
    bridge.stop()
    expect(channel.removeEventListener).toHaveBeenCalledTimes(1)
    bridge.start()
    expect(channel.postMessage).toHaveBeenCalledTimes(2)
  })

  it('stays unavailable without a WebView channel', () => {
    const absent = createLeafHostBridge({ channel: null, location })
    expect(absent.start().status).toBe('unavailable')
    expect(absent.start().status).toBe('unavailable')
  })

  it('accepts a ready message through the channel only for the page origin', () => {
    bridge.start()
    channel.emit(ready())
    expect(state.status).toBe('connected')
    expect(state.selectedObjectId).toBeNull()
  })

  it.each([
    { pipeName: 'leaf-platform-bridge', hostVersion: '1.0.0', hostProcessId: 1234, readySentinelPath: 'C:/Leaf/ready.json' },
    {},
    { pipeName: '' },
    { pipeName: 'p'.repeat(512), hostVersion: 'v'.repeat(512), readySentinelPath: 's'.repeat(512), hostProcessId: Number.MAX_SAFE_INTEGER },
  ])('connects a bound DWG with a valid bridgeEndpoint: %j', async (bridgeEndpoint) => {
    bridge.start()
    await bridge.receive(unbound())
    expect(state.status).toBe('unbound')
    channel.emit({ ...ready(), bridgeEndpoint })
    expect(state.status).toBe('connected')
    expect(state.selectedObjectId).toBeNull()
  })

  it.each([
    { pipeName: 'leaf-platform-bridge', extra: true },
    { hostProcessId: '1234' },
    null,
    [],
    'endpoint',
    undefined,
    new Date(),
    { hostProcessId: 0 },
    { hostProcessId: -1 },
    { hostProcessId: 1.5 },
    { hostProcessId: Number.MAX_SAFE_INTEGER + 1 },
    { pipeName: 123 },
    { hostVersion: null },
    { readySentinelPath: false },
    { pipeName: 'p'.repeat(513) },
    { hostVersion: 'v'.repeat(513) },
    { readySentinelPath: 's'.repeat(513) },
  ].map((bridgeEndpoint) => [bridgeEndpoint]))('ignores a ready message with malformed bridgeEndpoint: %j', async (bridgeEndpoint) => {
    bridge.start()
    observe.mockClear()
    await bridge.receive({ ...ready(), bridgeEndpoint })
    expect(state.status).toBe('connecting')
    expect(state.ready).toBeNull()
    expect(observe).not.toHaveBeenCalled()
  })

  it('ignores an unbound message with bridgeEndpoint', async () => {
    bridge.start()
    observe.mockClear()
    await bridge.receive({ ...unbound(), bridgeEndpoint: { pipeName: 'leaf-platform-bridge' } })
    expect(state.status).toBe('connecting')
    expect(state.ready).toBeNull()
    expect(observe).not.toHaveBeenCalled()
  })

  it.each([
    { origin: 'https://foreign.example' }, { extra: true },
    { drawingId: 'not-a-uuid' }, { sessionKey: 'short' },
  ])('ignores invalid ready messages: %j', async (patch) => {
    bridge.start()
    await bridge.receive({ ...ready(), ...patch })
    expect(state.status).toBe('connecting')
  })

  it('validates the exact unbound shape and origin', async () => {
    bridge.start()
    for (const patch of [{ extra: 1 }, { origin: 'https://foreign.example' }, { drawingRevision: identity.drawingVersionId }, { sessionKey: 'short' }]) {
      await bridge.receive({ ...unbound(), ...patch })
      expect(state.status).toBe('connecting')
    }
    await bridge.receive(unbound())
    expect(state.status).toBe('unbound')
  })

  it('requires all four UUIDs and signs the canonical binding envelope', async () => {
    bridge.start()
    await bridge.receive(unbound())
    for (const invalid of [{}, { ...identity, drawingId: 'bad' }, { ...identity, extra: identity.projectId }]) {
      await expect(bridge.bindDrawing(invalid)).rejects.toThrow('Invalid platform drawing identity')
    }
    expect(channel.postMessage).toHaveBeenCalledTimes(1)
    await bridge.bindDrawing(identity)
    const sent = channel.postMessage.mock.calls[1][0]
    const { signature, ...body } = sent
    expect(signature).toBe(sign(body).signature)
    expect(body.verb).toBe('drawing.bind')
    expect(body.payload).toEqual({ ...identity, documentFingerprint: common.documentFingerprint })
    expect(body.drawingRevision).toBe(unbound().drawingRevision)
    expect(body.origin).toBe(location.origin)
    expect(body.issuedAt).toMatch(/\.\d{7}\+00:00$/)
    expect(Date.parse(body.expiresAt) - Date.parse(body.issuedAt)).toBe(60_000)
    expect(JSON.stringify(sent)).not.toContain(sessionKey)
    expect(state.bindingResult).toBe('Waiting for confirmation in AutoCAD.')
  })

  it('accepts a signed binding result once, including concurrent replay', async () => {
    bridge.start()
    await bridge.receive(unbound())
    const message = envelope(unbound())
    observe.mockClear()
    await Promise.all([bridge.receive(message), bridge.receive(message)])
    await bridge.receive(message)
    expect(observe).toHaveBeenCalledTimes(1)
    expect(state.bindingResult).toBe('DWG connected. Starting the signed cross-probe session.')
  })

  it('ignores tampering, expiration, excessive lifetime and future issuance', async () => {
    bridge.start()
    await bridge.receive(unbound())
    const valid = envelope(unbound())
    const now = Date.now()
    const invalid = [
      { ...valid, payload: { accepted: false } },
      { ...valid, signature: '0'.repeat(64) },
      envelope(unbound(), { issuedAt: new Date(now - 60_000).toISOString(), expiresAt: new Date(now - 1).toISOString() }),
      envelope(unbound(), { expiresAt: new Date(now + 121_000).toISOString() }),
      envelope(unbound(), { issuedAt: new Date(now + 16_000).toISOString() }),
      envelope(unbound(), { origin: 'https://foreign.example' }),
    ]
    observe.mockClear()
    for (const message of invalid) await bridge.receive(message)
    expect(observe).not.toHaveBeenCalled()
    expect(state.bindingResult).toBeNull()
  })

  it('takes a signed selection and signs select and focus commands', async () => {
    bridge.start()
    await bridge.receive(ready())
    const selected = envelope(ready(), {
      verb: 'drawing.selection_changed',
      payload: { kind: 'selection_event', contractVersion: LEAF_PLATFORM_CONTRACT_VERSION,
        drawingVersionId: identity.drawingVersionId, payload: { objectId: 'panel:A1' } },
    })
    await bridge.receive(selected)
    expect(state.selectedObjectId).toBe('panel:A1')
    expect(state.selectedHandles).toBeNull()
    await expect(bridge.focusObject('invalid object!')).rejects.toThrow('Invalid drawing object identity')
    for (const action of ['select', 'focus']) {
      await bridge.focusObject('panel:A1', action)
      const { signature, ...body } = channel.postMessage.mock.calls.at(-1)[0]
      expect(signature).toBe(sign(body).signature)
      expect(body.verb).toBe('drawing.focus_objects')
      expect(body.payload.action).toBe(action)
      expect(body.payload.dispatchMode).toBe(DISPATCH_MODE)
      expect(body.payload.payload).toEqual({ objectId: 'panel:A1' })
      expect(body.drawingRevision).toBe(identity.drawingVersionId)
    }
  })

  it.each([
    ['single', ['2f4a']],
    ['multiple with maximum handle length', ['2f4a', 'aBcDeF0123456789']],
    ['maximum count', Array.from({ length: 1000 }, (_, index) => index.toString(16))],
  ])('takes a signed %s handle selection and signs select and focus commands', async (_, handles) => {
    bridge.start()
    await bridge.receive(ready())
    await bridge.receive(selection({ objectId: 'panel:A1' }))
    await bridge.receive(selection({ objectHandles: handles }))
    const normalized = handles.map((handle) => handle.toUpperCase())
    expect(state.selectedObjectId).toBeNull()
    expect(state.selectedHandles).toEqual(normalized)
    for (const action of ['select', 'focus']) {
      await bridge.focusObject({ objectHandles: handles }, action)
      const { signature, ...body } = channel.postMessage.mock.calls.at(-1)[0]
      expect(signature).toBe(sign(body).signature)
      expect(body.verb).toBe('drawing.focus_objects')
      expect(body.payload.action).toBe(action)
      expect(body.payload.dispatchMode).toBe(DISPATCH_MODE)
      expect(body.payload.payload).toEqual({ objectHandles: normalized })
      expect(body.drawingRevision).toBe(identity.drawingVersionId)
      expect(body.issuedAt).toMatch(/\.\d{7}\+00:00$/)
      expect(Date.parse(body.expiresAt) - Date.parse(body.issuedAt)).toBe(60_000)
    }
  })

  it('prefers a valid object id and falls back to handles for an invalid object id', async () => {
    bridge.start()
    await bridge.receive(ready())
    await bridge.receive(selection({ objectHandles: ['2f4a'] }))
    await bridge.receive(selection({ objectId: 'panel:A1', objectHandles: ['2f4a'] }))
    expect(state.selectedObjectId).toBe('panel:A1')
    expect(state.selectedHandles).toBeNull()
    await bridge.receive(selection({ objectId: 'invalid object!', objectHandles: ['2f4a'] }))
    expect(state.selectedObjectId).toBeNull()
    expect(state.selectedHandles).toEqual(['2F4A'])
    await bridge.receive(selection({ objectId: 'invalid object!' }))
    expect(state.selectedObjectId).toBeNull()
    expect(state.selectedHandles).toBeNull()
  })

  it.each(invalidHandles)('clears selection for %s handles', async (_, objectHandles) => {
    bridge.start()
    await bridge.receive(ready())
    for (const previous of [{ objectId: 'panel:A1' }, { objectHandles: ['2F4A'] }]) {
      await bridge.receive(selection(previous))
      await bridge.receive(selection({ objectHandles }))
      expect(state.selectedObjectId).toBeNull()
      expect(state.selectedHandles).toBeNull()
    }
  })

  it.each(invalidHandles)('rejects focus commands with %s handles without sending', async (_, objectHandles) => {
    bridge.start()
    await bridge.receive(ready())
    channel.postMessage.mockClear()
    await expect(bridge.focusObject({ objectHandles })).rejects.toThrow('Invalid drawing object identity')
    expect(channel.postMessage).not.toHaveBeenCalled()
  })

  it('rejects malformed focus targets and invalid actions with handles', async () => {
    bridge.start()
    await bridge.receive(ready())
    channel.postMessage.mockClear()
    for (const target of [null, [], {}, { objectId: 'panel:A1' },
      { objectHandles: ['2F4A'], objectId: 'panel:A1' }, { objectHandles: ['2F4A'], extra: true }]) {
      await expect(bridge.focusObject(target)).rejects.toThrow('Invalid drawing object identity')
    }
    await expect(bridge.focusObject({ objectHandles: ['2F4A'] }, 'erase')).rejects.toThrow('Invalid drawing action')
    expect(channel.postMessage).not.toHaveBeenCalled()
  })

  it('clears handles when the drawing session changes or stops', async () => {
    expect(state.selectedHandles).toBeNull()
    bridge.start()
    expect(state.selectedHandles).toBeNull()
    for (const next of [ready(), unbound()]) {
      await bridge.receive(ready())
      await bridge.receive(selection({ objectHandles: ['2F4A'] }))
      await bridge.receive(next)
      expect(state.selectedHandles).toBeNull()
    }
    await bridge.receive(ready())
    await bridge.receive(selection({ objectHandles: ['2F4A'] }))
    bridge.stop()
    expect(state.selectedHandles).toBeNull()
  })

  it('bounds the replay cache to the newest 1024 messages', async () => {
    bridge.start()
    await bridge.receive(ready())
    for (let index = 0; index < 1025; index += 1) {
      await bridge.receive(envelope(ready(), { messageId: `message-${index}`, verb: 'host.callback' }))
    }
    expect(bridge.seenHostMessages.size).toBe(1024)
    expect(bridge.seenHostMessages.has('message-0')).toBe(false)
    expect(bridge.seenHostMessages.has('message-1024')).toBe(true)
  })

  it('does not publish an old result or send a command after stop', async () => {
    bridge.start()
    await bridge.receive(unbound())
    const pendingReceive = bridge.receive(envelope(unbound()))
    const pendingSend = bridge.bindDrawing(identity)
    const rejectedSend = expect(pendingSend).rejects.toThrow('AutoCAD connection changed')
    bridge.stop()
    await pendingReceive
    await rejectedSend
    expect(state.status).toBe('unavailable')
    expect(channel.postMessage).toHaveBeenCalledTimes(1)
  })

  it('never persists the session key or other bridge state', async () => {
    const stored = vi.spyOn(Storage.prototype, 'setItem')
    const localBefore = { ...localStorage }
    const sessionBefore = { ...sessionStorage }
    bridge.start()
    await bridge.receive(unbound())
    await bridge.bindDrawing(identity)
    await bridge.receive(envelope(unbound()))
    await bridge.receive(ready())
    await bridge.focusObject('panel:A1')
    bridge.stop()
    expect(stored).not.toHaveBeenCalled()
    expect({ ...localStorage }).toEqual(localBefore)
    expect({ ...sessionStorage }).toEqual(sessionBefore)
    for (const [message] of channel.postMessage.mock.calls) expect(JSON.stringify(message)).not.toContain(sessionKey)
  })
})
