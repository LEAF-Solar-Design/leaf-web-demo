// @vitest-environment jsdom
// M3 S4: the page half of drawing.send_copy against the plugin's golden wire
// (Branch2025 Tests/Fixtures/web-bridge/leaf.send-copy.v1.golden.json at c70db75f, vendored
// byte for byte). The page must produce the exact request bytes and verify all three results.
import { createHmac, webcrypto } from 'node:crypto'
import { TextEncoder } from 'node:util'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import golden from './__fixtures__/leaf.send-copy.v1.golden.json'
import { createLeafHostBridge, isDrawingFingerprint } from './hostBridge.js'
import { fingerprintLine } from './sendCopy.js'

const session = golden.session
const location = { origin: session.origin }
const REQUEST_ID = '33333333-3333-4333-8333-333333333333'
const goldenCase = (name) => golden.cases.find((entry) => entry.name === name)
const wire = (name) => JSON.parse(goldenCase(name).wire)
const unboundHandshake = () => ({
  kind: 'host_bridge_unbound', protocolVersion: golden.protocolVersion, contractVersion: 'leaf.platform.v1alpha1',
  sessionId: session.sessionId, nonce: session.nonce, origin: session.origin,
  sessionKey: Buffer.from(session.sessionKeyHex, 'hex').toString('base64'),
  connectedAt: '2026-11-25T11:59:00.0000000+00:00',
  documentFingerprint: `sha256:${'a'.repeat(64)}`, drawingRevision: '00000000-0000-0000-0000-000000000000',
})

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (value && typeof value === 'object') return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`
  return JSON.stringify(value)
}
function sign(envelope) {
  const { signature: _ignored, ...unsigned } = envelope
  return { ...unsigned, signature: createHmac('sha256', Buffer.from(session.sessionKeyHex, 'hex')).update(canonical(unsigned)).digest('hex') }
}
const resultEnvelope = (payload, messageId = 'test-result') => sign({ ...wire('sent'), messageId, payload })
const bindResult = (reason) => sign({ ...wire('declined'), messageId: `bind-${reason}`, verb: 'drawing.bind_result',
  payload: { accepted: false, reason } })

function fakeChannel() {
  const listeners = new Set()
  return {
    postMessage: vi.fn(),
    addEventListener: vi.fn((type, listener) => listeners.add(listener)),
    removeEventListener: vi.fn((type, listener) => listeners.delete(listener)),
  }
}

describe('drawing.send_copy against the plugin golden wire', () => {
  let bridge, channel, randomUUID
  beforeEach(async () => {
    randomUUID = vi.fn(() => webcrypto.randomUUID())
    vi.stubGlobal('crypto', { subtle: webcrypto.subtle, getRandomValues: (array) => webcrypto.getRandomValues(array), randomUUID })
    vi.stubGlobal('TextEncoder', TextEncoder)
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-11-25T12:00:00.000Z'))
    channel = fakeChannel()
    bridge = createLeafHostBridge({ channel, location })
    bridge.start()
    await bridge.receive(unboundHandshake())
    expect(bridge.state.status).toBe('unbound')
    channel.postMessage.mockClear()
  })
  afterEach(() => {
    bridge.stop()
    vi.useRealTimers()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  async function request() {
    randomUUID.mockReturnValueOnce(REQUEST_ID).mockReturnValueOnce('golden-send-copy-request')
    const result = bridge.sendCopy()
    await vi.waitFor(() => expect(channel.postMessage).toHaveBeenCalled())
    vi.setSystemTime(new Date('2026-11-25T12:00:30.000Z'))
    return { result }
  }

  it('posts the exact golden request bytes and signature', async () => {
    await request()
    expect(channel.postMessage.mock.calls[0][0]).toEqual(wire('request'))
    expect(channel.postMessage.mock.calls[0][0].signature).toBe(goldenCase('request').signature)
  })

  it('accepts the golden sent result and prints the plugin line', async () => {
    const { result } = await request()
    await bridge.receive(wire('sent'))
    const sent = await result
    expect(sent.status).toBe('sent')
    expect(sent.fileName).toBe('Parking.dwg')
    expect(sent.sha256).toBe('8e0fcf8e938c676a238c148d33457d2214a061c8cdd3e2dc1f3ae9b21f3a5c67')
    expect(new TextDecoder().decode(sent.bytes)).toContain('SECTION')
    expect(sent.bytes.length).toBe(70)
    expect(isDrawingFingerprint(sent.fingerprint, sent.sha256, 70)).toBe(true)
    // DrawingFingerprintV1.Line for the same fingerprint, character for character.
    expect(fingerprintLine(sent.fingerprint, sent.fileName)).toBe('Parking.dwg | sent copy 8e0fcf8e | BUILDING 1, SITE 1 | 2 objects in model space')
    expect(bridge.pendingSend).toBeNull()
  })

  it.each([['declined', 'declined', 'user_declined'], ['failed', 'failed', 'export_failed']])('accepts the golden %s result', async (name, status, reason) => {
    const { result } = await request()
    await bridge.receive(wire(name))
    expect(await result).toEqual({ status, reason })
  })

  it('rejects a tampered signature and keeps waiting', async () => {
    const { result } = await request()
    const tampered = { ...wire('sent'), signature: '0'.repeat(64) }
    await bridge.receive(tampered)
    expect(bridge.pendingSend).not.toBeNull()
    await bridge.receive(wire('declined'))
    expect((await result).status).toBe('declined')
  })

  it('ignores a result for another request and refuses a replay', async () => {
    const { result } = await request()
    await bridge.receive(resultEnvelope({ ...wire('declined').payload, requestId: '44444444-4444-4444-8444-444444444444' }, 'other'))
    expect(bridge.pendingSend).not.toBeNull()
    await bridge.receive(wire('failed'))
    expect((await result).reason).toBe('export_failed')
    await bridge.receive(wire('failed'))
    expect(bridge.pendingSend).toBeNull()
  })

  it('treats sent without a fingerprint, a wrong digest or extra keys as malformed', async () => {
    for (const payload of [
      { ...wire('sent').payload, fingerprint: null },
      { ...wire('sent').payload, sha256: 'b'.repeat(64) },
      { ...wire('sent').payload, extra: 1 },
      { ...wire('sent').payload, fileName: 'C:\\drawings\\Parking.dwg' },
      { ...wire('declined').payload, reason: 'export_failed' },
      { ...wire('failed').payload, reason: 'made_up' },
    ]) {
      const { result } = await request()
      await bridge.receive(resultEnvelope(payload, `bad-${webcrypto.randomUUID()}`))
      expect(await result).toEqual({ status: 'failed', reason: 'invalid_result' })
      vi.setSystemTime(new Date('2026-11-25T12:00:00.000Z'))
      channel.postMessage.mockClear()
    }
  })

  it.each([['verb_not_allowed', 'plugin_outdated'], ['unbound_verb_not_allowed', 'plugin_outdated'], ['expired', 'host_refused']])(
    'maps a %s bind refusal while sending to %s', async (reason, mapped) => {
      const { result } = await request()
      await bridge.receive(bindResult(reason))
      expect(await result).toEqual({ status: 'failed', reason: mapped })
      expect(bridge.state.bindingResult).toBeNull()
    })

  it('never completes a send from an accepted bind result', async () => {
    const { result } = await request()
    await bridge.receive(sign({ ...wire('declined'), messageId: 'bind-ok', verb: 'drawing.bind_result', payload: { accepted: true, reason: null } }))
    expect(bridge.pendingSend).not.toBeNull()
    await bridge.receive(wire('declined'))
    expect((await result).status).toBe('declined')
  })

  it('stops waiting at 200 s and ignores a late result', async () => {
    const { result } = await request()
    await vi.advanceTimersByTimeAsync(200_000)
    expect(await result).toEqual({ status: 'failed', reason: 'timeout' })
  })

  it('ends the send when AutoCAD reconnects', async () => {
    const { result } = await request()
    await bridge.receive(unboundHandshake())
    expect(await result).toEqual({ status: 'failed', reason: 'session_changed' })
  })

  it('allows one send at a time and no bind beside it', async () => {
    await request()
    await expect(bridge.sendCopy()).rejects.toThrow(/already/)
    await expect(bridge.bindDrawing({ platformTenantId: REQUEST_ID, projectId: REQUEST_ID, drawingId: REQUEST_ID, drawingVersionId: REQUEST_ID }))
      .rejects.toThrow(/being sent/)
  })
})
