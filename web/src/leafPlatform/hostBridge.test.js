// @vitest-environment jsdom
import { createHmac, webcrypto } from 'node:crypto'
import { TextEncoder } from 'node:util'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createLeafHostBridge, DISPATCH_MODE, LEAF_PLATFORM_CONTRACT_VERSION, PROTOCOL_VERSION } from './hostBridge.js'
import { createDiagnostics } from './diagnostics.js'
import { track } from '../telemetry.js'

vi.mock('../telemetry.js', () => ({ track: vi.fn() }))

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
    messageId: webcrypto.randomUUID(), issuedAt: new Date(now - 1000).toISOString().replace('Z', '0000+00:00'),
    expiresAt: new Date(now + 60_000).toISOString().replace('Z', '0000+00:00'), origin: location.origin,
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

function callback(commandId, status = 'applied', reason = null) {
  return envelope(ready(), {
    verb: 'host.callback',
    payload: { kind: 'callback', contractVersion: LEAF_PLATFORM_CONTRACT_VERSION,
      commandId, ...identity, nonce: webcrypto.randomUUID(), handledAt: new Date().toISOString(),
      status, reason, provenance: { source: 'autocad' } },
  })
}

async function startCommand(bridge, channel, target = 'panel:A1', action = 'focus') {
  let posted
  const sent = new Promise((resolve) => { posted = resolve })
  channel.postMessage.mockImplementationOnce(posted)
  const outcome = bridge.focusObject(target, action)
  return { sent: await sent, outcome }
}

async function completeCommand(bridge, channel, target, action) {
  const { sent, outcome } = await startCommand(bridge, channel, target, action)
  await bridge.receive(callback(sent.payload.commandId))
  await outcome
}

const invalidHandles = [
  ['missing', undefined], ['null', null], ['string', '2F4A'], ['empty', []],
  ['non-string', [123]], ['empty handle', ['']], ['non-hex', ['2G4A']],
  ['whitespace', [' 2F4A']], ['trailing newline', ['2F4A\n']],
  ['too long', ['A'.repeat(17)]], ['duplicate', ['2F4A', '2F4A']],
  ['case-insensitive duplicate', ['2f4a', '2F4A']],
  ['too many', Array.from({ length: 1001 }, (_, index) => index.toString(16))],
]

describe('Studio bridge diagnostics and telemetry', () => {
  let bridge, channel, diagnostics, clock
  beforeEach(() => {
    vi.stubGlobal('crypto', webcrypto)
    vi.stubGlobal('TextEncoder', TextEncoder)
    track.mockReset()
    clock = 0
    diagnostics = createDiagnostics({ now: () => clock })
    channel = fakeChannel()
    bridge = createLeafHostBridge({ channel, location, diagnostics, now: () => clock })
  })
  afterEach(() => {
    bridge.stop()
    vi.useRealTimers()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })
  const detailsFor = (diagnostics, phase) => diagnostics.entries().filter((entry) => entry.phase === phase).map((entry) => entry.detail)
  const eventsFor = (phase) => track.mock.calls.filter(([name, props]) => name === 'leaf_platform_bridge' && props.phase === phase).map(([, props]) => props)

  it('records startup, hello, handshake and both handshake rejection categories', async () => {
    const absent = createLeafHostBridge({ channel: null, diagnostics })
    absent.start()
    bridge.start()
    await bridge.receive({ ...ready(), origin: 'https://foreign.example' })
    await bridge.receive({ ...ready(), sessionKey: 'bad' })
    await bridge.receive(unbound())
    await bridge.receive(ready())
    expect(detailsFor(diagnostics, 'start')).toEqual([{ kind: 'none' }, { kind: 'webview' }])
    expect(detailsFor(diagnostics, 'hello-sent')).toEqual([{}])
    expect(detailsFor(diagnostics, 'handshake-rejected')).toEqual([{ reason: 'origin' }, { reason: 'shape' }])
    expect(detailsFor(diagnostics, 'handshake')).toEqual([{ kind: 'unbound' }, { kind: 'ready' }])
    expect(track.mock.calls.map(([, props]) => props.phase)).toEqual(['handshake'])
  })

  it.each([
    ['timestamp-format', { issuedAt: 'not-a-timestamp' }],
    ['timestamp-format', { expiresAt: 42 }],
    ['lifetime', { expiresAt: '2000-01-01T00:00:00.0000000+00:00' }],
    ['signature', { signature: '0'.repeat(64) }],
    ['session', { sessionId: 'foreign-session' }],
    ['session', { origin: 'https://foreign.example' }],
    ['session', { drawingRevision: identity.projectId }],
    ['verb', { verb: 'drawing.erase' }],
    ['shape', { protocolVersion: 'other' }],
    ['shape', { messageId: 1 }],
  ])('records envelope rejection %s without accepting the message', async (reason, patch) => {
    bridge.start()
    await bridge.receive(unbound())
    await bridge.receive({ ...envelope(unbound()), ...patch })
    expect(detailsFor(diagnostics, 'envelope-rejected')).toEqual([{ reason }])
    expect(bridge.state.bindingResult).toBeNull()
  })

  it('records replay and capacity rejections including simultaneous delivery', async () => {
    bridge.start()
    await bridge.receive(unbound())
    bridge.replayCapacity = 1
    const message = envelope(unbound())
    await Promise.all([bridge.receive(message), bridge.receive(message)])
    await bridge.receive(message)
    await bridge.receive(envelope(unbound()))
    expect(detailsFor(diagnostics, 'envelope-rejected')).toEqual([
      { reason: 'replay' }, { reason: 'replay' }, { reason: 'capacity' },
    ])
    expect(detailsFor(diagnostics, 'bind-result')).toHaveLength(1)
  })

  it('records an ignored malformed callback and selection payload as shape', async () => {
    bridge.start()
    await bridge.receive(ready())
    await bridge.receive(callback(webcrypto.randomUUID()))
    await bridge.receive(envelope(ready(), { verb: 'drawing.selection_changed', payload: {} }))
    expect(detailsFor(diagnostics, 'envelope-rejected')).toEqual([{ reason: 'shape' }, { reason: 'shape' }])
  })

  it('records expiry and session changes across asynchronous verification', async () => {
    vi.useFakeTimers()
    bridge.start()
    await bridge.receive(ready())
    for (const reason of ['lifetime', 'session']) {
      let finishVerification, startedVerification
      const started = new Promise((resolve) => { startedVerification = resolve })
      const verify = vi.spyOn(webcrypto.subtle, 'verify').mockImplementation(() => {
        startedVerification()
        return new Promise((resolve) => { finishVerification = resolve })
      })
      const receiving = bridge.receive(selection({ objectId: 'panel:private' }))
      await started
      if (reason === 'lifetime') vi.advanceTimersByTime(60_001)
      else await bridge.receive(ready())
      finishVerification(true)
      await receiving
      verify.mockRestore()
      expect(detailsFor(diagnostics, 'envelope-rejected').at(-1)).toEqual({ reason })
    }
    expect(bridge.state.selectedObjectId).toBeNull()
  })

  it('records sanitized bind and command outcomes without keys, signatures or identities', async () => {
    bridge.start()
    await bridge.receive(unbound())
    await bridge.bindDrawing(identity)
    await bridge.receive(envelope(unbound(), { payload: { accepted: true, reason: 'accepted' } }))
    await bridge.receive(envelope(unbound(), { payload: { accepted: false, reason: sessionKey } }))
    expect(detailsFor(diagnostics, 'bind-sent')).toEqual([{}])
    expect(detailsFor(diagnostics, 'bind-result')).toEqual([
      { status: 'accepted', reason: 'accepted' }, { status: 'rejected', reason: 'other' },
    ])
    await bridge.receive(ready())
    const messages = []
    for (const [status, reason, sanitized] of [
      ['applied', null, 'other'], ['stale', 'stale_document', 'stale_document'],
      ['rejected', identity.drawingId, 'other'], ['rejected', 'https://private.example/token', 'other'],
    ]) {
      const { sent, outcome } = await startCommand(bridge, channel, { objectHandles: ['2F4A'] })
      const response = callback(sent.payload.commandId, status, reason)
      messages.push(sent, response)
      await bridge.receive(response)
      await outcome
      expect(detailsFor(diagnostics, 'command-outcome').at(-1)).toEqual({ status, reason: sanitized })
    }
    expect(detailsFor(diagnostics, 'command-sent')).toEqual(Array(4).fill({ action: 'focus' }))
    const output = JSON.stringify(diagnostics.entries()) + diagnostics.snapshot() + JSON.stringify(track.mock.calls)
    for (const secret of [sessionKey, ...Object.values(identity), common.sessionId,
      common.documentFingerprint, '2F4A', 'https://private.example/token',
      ...messages.flatMap((message) => [message.signature, message.messageId])]) {
      expect(output).not.toContain(secret)
    }
  })

  it('records handshake, bind and command timeouts without discarding a late result', async () => {
    vi.useFakeTimers()
    bridge.start()
    await vi.advanceTimersByTimeAsync(10_000)
    expect(bridge.state.status).toBe('connecting')
    await bridge.receive(unbound())
    await bridge.bindDrawing(identity)
    await vi.advanceTimersByTimeAsync(60_000)
    expect(bridge.state.status).toBe('unbound')
    await bridge.receive(envelope(unbound()))
    expect(bridge.state.bindingResult).toContain('DWG connected.')
    await bridge.receive(ready())
    const { outcome } = await startCommand(bridge, channel)
    await vi.advanceTimersByTimeAsync(15_000)
    await expect(outcome).resolves.toMatchObject({ status: 'unknown', reason: 'timeout' })
    expect(detailsFor(diagnostics, 'timeout')).toEqual([
      { kind: 'handshake' }, { kind: 'bind' }, { kind: 'command' },
    ])
  })

  it('cancels diagnostic timeouts on results, replacement sessions and stop', async () => {
    vi.useFakeTimers()
    bridge.start()
    await bridge.receive(unbound())
    await bridge.bindDrawing(identity)
    await bridge.receive(envelope(unbound()))
    await bridge.bindDrawing(identity)
    await bridge.receive(ready())
    bridge.stop()
    bridge.start()
    bridge.stop()
    await vi.advanceTimersByTimeAsync(120_000)
    expect(detailsFor(diagnostics, 'timeout')).toEqual([])
    expect(vi.getTimerCount()).toBe(0)
  })

  it('limits lifecycle telemetry by phase and status for ten seconds, including restarts', async () => {
    bridge.start()
    await bridge.receive(unbound())
    await bridge.receive(ready())
    clock = 9999
    await bridge.receive(ready())
    expect(eventsFor('handshake')).toHaveLength(1)
    clock = 10_000
    bridge.stop()
    bridge.start()
    await bridge.receive(unbound())
    expect(eventsFor('handshake')).toHaveLength(2)
    for (const accepted of [true, true, false, false]) {
      await bridge.receive(envelope(unbound(), { payload: { accepted, reason: 'confirmed' } }))
    }
    expect(eventsFor('bind-result').map((event) => event.status)).toEqual(['accepted', 'rejected'])
    clock = 20_000
    await bridge.receive(envelope(unbound()))
    expect(eventsFor('bind-result')).toHaveLength(3)
    await bridge.receive(ready())
    for (const status of ['applied', 'applied', 'rejected', 'rejected']) {
      const { sent, outcome } = await startCommand(bridge, channel)
      await bridge.receive(callback(sent.payload.commandId, status, null))
      await outcome
    }
    expect(eventsFor('command-outcome').map((event) => event.status)).toEqual(['applied', 'rejected'])
    clock = 30_000
    await completeCommand(bridge, channel, 'panel:A1')
    expect(eventsFor('command-outcome')).toHaveLength(3)
  })

  it('limits timeout telemetry by phase while retaining every local timeout', async () => {
    vi.useFakeTimers()
    bridge.start()
    await vi.advanceTimersByTimeAsync(10_000)
    bridge.retryHello()
    clock = 9999
    await vi.advanceTimersByTimeAsync(10_000)
    expect(eventsFor('timeout')).toHaveLength(1)
    bridge.retryHello()
    clock = 10_000
    await vi.advanceTimersByTimeAsync(10_000)
    expect(eventsFor('timeout')).toHaveLength(2)
    expect(detailsFor(diagnostics, 'timeout')).toHaveLength(3)
  })

  it('sends only a counted rejection summary at most once per minute', async () => {
    bridge.start()
    await bridge.receive(ready())
    await bridge.receive(null)
    for (let i = 0; i < 250; i += 1) await bridge.receive({ signature: sessionKey, payload: identity })
    clock = 59_999
    await bridge.receive(null)
    expect(eventsFor('envelope-rejected')).toEqual([{ phase: 'envelope-rejected', count: 1 }])
    clock = 60_000
    await bridge.receive(null)
    expect(eventsFor('envelope-rejected')).toEqual([
      { phase: 'envelope-rejected', count: 1 }, { phase: 'envelope-rejected', count: 252 },
    ])
    expect(diagnostics.entries()).toHaveLength(200)
  })

  it('keeps the bridge working if a diagnostic or telemetry sink fails', async () => {
    diagnostics.record = vi.fn(() => { throw new Error('Local sink failed') })
    track.mockImplementation(() => { throw new Error('Telemetry failed') })
    bridge.start()
    await bridge.receive(ready())
    expect(bridge.state.status).toBe('connected')
    await completeCommand(bridge, channel, 'panel:A1')
    expect(bridge.state.lastCommand.status).toBe('applied')
  })
})

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
    vi.useRealTimers()
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
      envelope(unbound(), { issuedAt: new Date(now - 60_000).toISOString().replace('Z', '0000+00:00'), expiresAt: new Date(now - 1).toISOString().replace('Z', '0000+00:00') }),
      envelope(unbound(), { expiresAt: new Date(now + 121_000).toISOString().replace('Z', '0000+00:00') }),
      envelope(unbound(), { issuedAt: new Date(now + 16_000).toISOString().replace('Z', '0000+00:00') }),
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
      await completeCommand(bridge, channel, 'panel:A1', action)
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
      await completeCommand(bridge, channel, { objectHandles: handles }, action)
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

  it('retains unexpired replay IDs after more messages than the replay capacity', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-25T12:00:00.000Z'))
    const capacity = 8
    const diagnostics = createDiagnostics()
    const replayBridge = createLeafHostBridge({ channel: fakeChannel(), location, replayCapacity: capacity, diagnostics })
    bridge.stop()
    bridge = replayBridge
    const published = vi.fn()
    replayBridge.subscribe(published)
    replayBridge.start()
    await replayBridge.receive(ready())
    const original = selection({ objectId: 'panel:original' })
    await replayBridge.receive(original)
    for (let index = 0; index < capacity + 1; index += 1) {
      const message = selection({ objectId: `panel:${index}` })
      await replayBridge.receive(message)
      expect(replayBridge.seenHostMessages.has(message.messageId)).toBe(index < capacity - 1)
      expect(replayBridge.seenHostMessages.get(original.messageId)).toBe(Date.parse(original.expiresAt))
    }
    published.mockClear()
    await replayBridge.receive(original)
    expect(published).not.toHaveBeenCalled()
    expect(replayBridge.state.selectedObjectId).toBe('panel:6')
    expect(replayBridge.seenHostMessages.size).toBe(capacity)
    expect(diagnostics.entries().at(-1)).toMatchObject({ phase: 'envelope-rejected', detail: { reason: 'replay' } })
  })

  it('rejects new messages at capacity and frees only expired replay IDs', async () => {
    vi.useFakeTimers()
    const start = Date.parse('2026-09-25T12:00:00.000Z')
    vi.setSystemTime(start)
    const capacity = 8
    const diagnostics = createDiagnostics()
    const replayBridge = createLeafHostBridge({ channel: fakeChannel(), location, replayCapacity: capacity, diagnostics })
    bridge.stop()
    bridge = replayBridge
    const published = vi.fn()
    replayBridge.subscribe(published)
    replayBridge.start()
    await replayBridge.receive(ready())
    const original = selection({ objectId: 'panel:original' })
    await replayBridge.receive(original)
    vi.setSystemTime(start + 30_000)
    const retained = []
    for (let index = 0; index < capacity - 1; index += 1) {
      const message = selection({ objectId: `panel:${index}` })
      retained.push(message)
      await replayBridge.receive(message)
    }
    const overflow = selection({ objectId: 'panel:overflow' })
    published.mockClear()
    await replayBridge.receive(overflow)
    expect(diagnostics.entries().at(-1)).toMatchObject({ phase: 'envelope-rejected', detail: { reason: 'capacity' } })
    await replayBridge.receive(original)
    expect(diagnostics.entries().at(-1)).toMatchObject({ phase: 'envelope-rejected', detail: { reason: 'replay' } })
    expect(published).not.toHaveBeenCalled()
    expect(replayBridge.state.selectedObjectId).toBe('panel:6')
    expect(replayBridge.seenHostMessages.get(original.messageId)).toBe(Date.parse(original.expiresAt))
    expect(replayBridge.seenHostMessages.has(overflow.messageId)).toBe(false)
    expect(replayBridge.seenHostMessages.size).toBe(capacity)
    vi.setSystemTime(start + 60_000)
    const replacement = selection({ objectId: 'panel:new' })
    await replayBridge.receive(replacement)
    expect(published).toHaveBeenCalledTimes(1)
    expect(replayBridge.state.selectedObjectId).toBe('panel:new')
    expect(replayBridge.seenHostMessages.has(original.messageId)).toBe(false)
    expect(replayBridge.seenHostMessages.get(replacement.messageId)).toBe(Date.parse(replacement.expiresAt))
    expect(replayBridge.seenHostMessages.size).toBe(capacity)
    for (const message of retained) {
      expect(replayBridge.seenHostMessages.get(message.messageId)).toBe(Date.parse(message.expiresAt))
    }
    published.mockClear()
    await replayBridge.receive(retained[0])
    expect(published).not.toHaveBeenCalled()
    expect(replayBridge.state.selectedObjectId).toBe('panel:new')
    expect(diagnostics.entries().at(-1)).toMatchObject({ phase: 'envelope-rejected', detail: { reason: 'replay' } })
  })

  it.each([
    ['.1230000+00:00', '.123+00:00'],
    ['.0000000+00:00', '+00:00'],
    ['.1234567+00:00', '.1234567Z'],
  ])('verifies host timestamps signed as %s and delivered as %s', async (signed, delivered) => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-25T12:34:56.500Z'))
    bridge.start()
    await bridge.receive(ready())
    const message = envelope(ready(), {
      issuedAt: `2026-09-25T12:34:56${signed}`, expiresAt: `2026-09-25T12:35:56${signed}`,
      verb: 'drawing.selection_changed', payload: { kind: 'selection_event', payload: { objectId: 'panel:A1' } },
    })
    await bridge.receive({ ...message, issuedAt: `2026-09-25T12:34:56${delivered}`, expiresAt: `2026-09-25T12:35:56${delivered}` })
    expect(state.selectedObjectId).toBe('panel:A1')
  })

  it.each(['issuedAt', 'expiresAt'])('rejects malformed %s even when Date.parse accepts it', async (field) => {
    bridge.start()
    await bridge.receive(ready())
    const malformed = new Date(Date.now() + (field === 'expiresAt' ? 60_000 : -1000)).toISOString().replace('T', ' ')
    expect(Number.isFinite(Date.parse(malformed))).toBe(true)
    const message = envelope(ready(), {
      [field]: malformed, verb: 'drawing.selection_changed',
      payload: { kind: 'selection_event', payload: { objectId: 'panel:A1' } },
    })
    observe.mockClear()
    await bridge.receive(message)
    expect(observe).not.toHaveBeenCalled()
    expect(state.selectedObjectId).toBeNull()
  })

  it('rechecks expiry after asynchronous signature verification', async () => {
    vi.useFakeTimers()
    bridge.start()
    await bridge.receive(ready())
    let finishVerification
    let verificationStarted
    const started = new Promise((resolve) => { verificationStarted = resolve })
    vi.spyOn(webcrypto.subtle, 'verify').mockImplementation(() => {
      verificationStarted()
      return new Promise((resolve) => { finishVerification = resolve })
    })
    const message = selection({ objectId: 'panel:expired' })
    const receiving = bridge.receive(message)
    await started
    vi.advanceTimersByTime(60_001)
    finishVerification(true)
    await receiving
    expect(state.selectedObjectId).toBeNull()
    expect(bridge.seenHostMessages.has(message.messageId)).toBe(false)
  })

  it('accepts version 7 and other D-format GUIDs for ready and binding identities', async () => {
    const anyVersion = Object.fromEntries(Object.entries(identity).map(([key, value]) => [key, value.replace(/-4/g, '-7')]))
    bridge.start()
    await bridge.receive({ ...ready(), ...anyVersion })
    expect(state.status).toBe('connected')
    await bridge.receive(unbound())
    await bridge.bindDrawing(anyVersion)
    expect(channel.postMessage.mock.calls.at(-1)[0].payload).toMatchObject(anyVersion)
  })

  it.each([['applied', null], ['stale', 'stale_document'], ['rejected', 'selection_apply_failed']])(
    'resolves a matching signed callback as %s', async (status, reason) => {
      bridge.start()
      await bridge.receive(ready())
      const { sent, outcome } = await startCommand(bridge, channel)
      await bridge.receive(callback(sent.payload.commandId, status, reason))
      await expect(outcome).resolves.toEqual({ action: 'focus', status, reason })
      expect(state.lastCommand).toEqual({ action: 'focus', status, reason })
    },
  )

  it('ignores another command ID and resolves unknown after 15 seconds', async () => {
    vi.useFakeTimers()
    bridge.start()
    await bridge.receive(ready())
    const { outcome } = await startCommand(bridge, channel, 'panel:A1', 'select')
    const resolved = vi.fn()
    void outcome.then(resolved)
    await bridge.receive(callback(webcrypto.randomUUID()))
    await vi.advanceTimersByTimeAsync(14_999)
    expect(resolved).not.toHaveBeenCalled()
    expect(state.lastCommand).toBeNull()
    await vi.advanceTimersByTimeAsync(1)
    await expect(outcome).resolves.toEqual({ action: 'select', status: 'unknown', reason: 'timeout' })
    expect(state.lastCommand).toEqual({ action: 'select', status: 'unknown', reason: 'timeout' })
  })

  it('supersedes pending commands and clears timers on callbacks, new sessions and stop', async () => {
    vi.useFakeTimers()
    bridge.start()
    await bridge.receive(ready())
    const first = await startCommand(bridge, channel)
    const second = await startCommand(bridge, channel, 'panel:B1', 'select')
    await expect(first.outcome).resolves.toEqual({ action: 'focus', status: 'superseded', reason: null })
    expect(vi.getTimerCount()).toBe(1)
    await bridge.receive(callback(first.sent.payload.commandId))
    await bridge.receive(callback(second.sent.payload.commandId))
    await expect(second.outcome).resolves.toMatchObject({ status: 'applied', action: 'select' })
    expect(vi.getTimerCount()).toBe(0)
    const third = await startCommand(bridge, channel)
    await bridge.receive(ready())
    await expect(third.outcome).resolves.toMatchObject({ status: 'superseded' })
    expect(state.lastCommand).toBeNull()
    expect(vi.getTimerCount()).toBe(0)
    const fourth = await startCommand(bridge, channel)
    bridge.stop()
    await expect(fourth.outcome).resolves.toMatchObject({ status: 'superseded' })
    expect(state.lastCommand).toBeNull()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('times out even while signing and never sends the command afterward', async () => {
    vi.useFakeTimers()
    bridge.start()
    await bridge.receive(ready())
    let finishSigning, signingStarted
    const started = new Promise((resolve) => { signingStarted = resolve })
    vi.spyOn(webcrypto.subtle, 'sign').mockImplementation(() => {
      signingStarted()
      return new Promise((resolve) => { finishSigning = resolve })
    })
    const outcome = bridge.focusObject('panel:A1')
    await started
    await vi.advanceTimersByTimeAsync(15_000)
    await expect(outcome).resolves.toEqual({ action: 'focus', status: 'unknown', reason: 'timeout' })
    finishSigning(new Uint8Array(32).buffer)
    await vi.advanceTimersByTimeAsync(0)
    expect(channel.postMessage).toHaveBeenCalledTimes(1)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('retries hello with a fresh timestamp without discarding an existing session', async () => {
    vi.useFakeTimers()
    bridge.retryHello()
    expect(channel.postMessage).not.toHaveBeenCalled()
    bridge.start()
    const firstHello = state.helloSentAt
    vi.advanceTimersByTime(10_000)
    bridge.retryHello()
    expect(state.helloSentAt).toBe(firstHello + 10_000)
    expect(state.status).toBe('connecting')
    expect(channel.postMessage).toHaveBeenCalledTimes(2)
    await bridge.receive(ready())
    const session = state.ready
    bridge.retryHello()
    expect(state.status).toBe('connected')
    expect(state.ready).toBe(session)
    expect(channel.postMessage.mock.calls.at(-1)[0]).toEqual({ kind: 'host_bridge_hello', contractVersion: LEAF_PLATFORM_CONTRACT_VERSION })
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
    await completeCommand(bridge, channel, 'panel:A1')
    bridge.stop()
    expect(stored).not.toHaveBeenCalled()
    expect({ ...localStorage }).toEqual(localBefore)
    expect({ ...sessionStorage }).toEqual(sessionBefore)
    for (const [message] of channel.postMessage.mock.calls) expect(JSON.stringify(message)).not.toContain(sessionKey)
  })
})
