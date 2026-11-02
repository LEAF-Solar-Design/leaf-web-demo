import { TextDecoder, TextEncoder } from 'node:util'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./api.js', () => ({
  config: { apiBase: '/backend', tenant: 'tenant-fixture' },
  authHeaders: vi.fn(), noteUnauthorized: vi.fn((res) => res),
}))
vi.mock('./telemetry.js', () => ({ trackErrorShown: vi.fn(), trackStreamDown: vi.fn() }))

import { authHeaders, noteUnauthorized } from './api.js'
import { openStream } from './converse.js'

// Fixed fake credentials exist only in the mocked auth helper.
const auth = ['Bearer', 'fixture-secret'].join(' ')
const next = ['Bearer', 'refreshed-fixture'].join(' ')
const encoder = new TextEncoder()
const envelope = (seq, type = 'text_delta', data = {}) => ({
  v: 1, session_id: 'session', turn_id: 'turn', seq, type, data,
})
const frame = (env) => `event: ${env.type}\ndata: ${JSON.stringify(env)}\n\n`
const flush = async () => { for (let i = 0; i < 30; i += 1) await Promise.resolve() }
const handles = []
const open = (...args) => {
  const handle = openStream(...args)
  handles.push(handle)
  return handle
}

// A controllable reader lets each assertion happen while the HTTP body is
// still open, and can deliberately resolve a read after cancellation.
function source() {
  const queue = []
  let waiting
  const send = (chunk) => {
    if (waiting) { const resolve = waiting; waiting = null; resolve(chunk) }
    else queue.push(chunk)
  }
  const reader = {
    read: vi.fn(() => queue.length ? Promise.resolve(queue.shift()) : new Promise(resolve => { waiting = resolve })),
    cancel: vi.fn(async () => {}), releaseLock: vi.fn(),
  }
  return {
    reader,
    response: { ok: true, status: 200, body: { getReader: () => reader } },
    bytes: (value) => send({ value, done: false }),
    text: (value) => send({ value: encoder.encode(value), done: false }),
    end: () => send({ done: true }),
  }
}

function transport(streamResponse, transcript = () => ({ ok: true, status: 200, json: async () => ({ events: [] }) })) {
  const fetcher = vi.fn((url, init) => url.includes('/stream?')
    ? streamResponse(url, init) : transcript(url, init))
  vi.stubGlobal('fetch', fetcher)
  return fetcher
}
const streamCalls = (fetcher) => fetcher.mock.calls.filter(([url]) => url.includes('/stream?'))

beforeEach(() => {
  vi.useFakeTimers()
  vi.stubGlobal('TextDecoder', TextDecoder)
  vi.stubGlobal('EventSource', undefined)
  authHeaders.mockReturnValue({ Authorization: auth })
})
afterEach(() => {
  handles.splice(0).forEach(handle => handle.close())
  vi.clearAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('authenticated conversation SSE', () => {
  it('sends bearer and tenant headers without putting credentials in the URL', async () => {
    const body = source()
    const fetcher = transport(() => body.response)
    const onEvent = vi.fn()
    open('session/with space', 7, { onEvent })
    await flush()
    const [url, init] = streamCalls(fetcher)[0]
    expect(url).toBe('/backend/api/sessions/session%2Fwith%20space/stream?after_seq=7')
    expect(url).not.toContain('fixture-secret')
    expect(url).not.toContain('tenant-fixture')
    expect(init).toMatchObject({ method: 'GET', headers: {
      Accept: 'text/event-stream', Authorization: auth, 'X-Tenant-Id': 'tenant-fixture',
    } })
    expect(init.signal.aborted).toBe(false)
    const env = envelope(8)
    body.text(frame(env))
    await flush()
    expect(onEvent).toHaveBeenCalledWith(env)
    expect(body.reader.cancel).not.toHaveBeenCalled()
    expect(fetcher.mock.calls.find(([path]) => path.includes('/transcript?'))[1].headers)
      .toMatchObject({ Authorization: auth, 'X-Tenant-Id': 'tenant-fixture' })
  })

  it('decodes split UTF-8, CRLF and multiline data, ignoring heartbeat and malformed frames', async () => {
    const body = source()
    transport(() => body.response)
    const onEvent = vi.fn()
    const handle = open('session', 0, { onEvent })
    await flush()
    const env = envelope(1, 'text_delta', { text: 'Roof ☀ café' })
    const json = JSON.stringify(env).replace(',"data":', ',\r\ndata: "data":')
    const bytes = encoder.encode(`: heartbeat\r\n\r\ndata: broken\r\n\r\ndata: null\r\n\r\ndata: {}\r\n\r\ndata: {"type":"text_delta","seq":"bad"}\r\n\r\nevent: text_delta\r\ndata: ${json}\r\n\r\n`)
    // One-byte chunks split both multibyte characters and CRLF boundaries.
    for (const byte of bytes) body.bytes(Uint8Array.of(byte))
    await flush()
    // Drain the queued asynchronous reads without advancing the poll timer.
    for (let i = 0; i < bytes.length; i += 1) await Promise.resolve()
    expect(onEvent).toHaveBeenCalledTimes(1)
    expect(onEvent).toHaveBeenCalledWith(env)
    expect(handle.seq).toBe(1)
  })

  it('reconnects from the shared cursor with refreshed headers and dedupes replay and polling', async () => {
    const first = source(), second = source()
    let attempts = 0
    let events = []
    const fetcher = transport(() => (++attempts === 1 ? first : second).response,
      () => ({ ok: true, status: 200, json: async () => ({ events }) }))
    const onEvent = vi.fn(), turn_complete = vi.fn()
    const handle = open('session', 0, { onEvent, turn_complete })
    await flush()
    first.text(frame(envelope(1)))
    await flush()
    first.end()
    await flush()
    authHeaders.mockReturnValue({ Authorization: next })
    await vi.advanceTimersByTimeAsync(1000)
    expect(streamCalls(fetcher)[1][0]).toContain('after_seq=1')
    expect(streamCalls(fetcher)[1][1].headers.Authorization).toBe(next)
    const terminal = envelope(2, 'turn_complete', { status: 'completed' })
    second.text(frame(envelope(1)) + frame(terminal))
    await flush()
    events = [envelope(1), terminal]
    await handle.reconcile()
    expect(onEvent.mock.calls.map(([env]) => env.seq)).toEqual([1, 2])
    expect(turn_complete).toHaveBeenCalledWith(terminal.data, terminal)
    expect(handle.seq).toBe(2)
  })

  it('preserves polling window expansion for gaps while the authenticated stream is open', async () => {
    const body = source()
    const fetcher = transport(() => body.response, (url) => ({
      ok: true, status: 200,
      json: async () => ({ events: url.endsWith('limit=200')
        ? [envelope(5)] : [1, 2, 3, 4, 5].map(seq => envelope(seq)) }),
    }))
    const onEvent = vi.fn()
    open('session', 0, { onEvent })
    await flush()
    expect(fetcher.mock.calls.some(([url]) => url.endsWith('limit=800'))).toBe(true)
    expect(onEvent.mock.calls.map(([env]) => env.seq)).toEqual([1, 2, 3, 4, 5])
    body.text(frame(envelope(5)) + frame(envelope(6)))
    await flush()
    expect(onEvent.mock.calls.map(([env]) => env.seq)).toEqual([1, 2, 3, 4, 5, 6])
  })

  it.each([401, 403])('stops on HTTP %s without an unauthorized retry loop', async (status) => {
    const fetcher = transport(() => ({ ok: false, status }))
    const onConnectionState = vi.fn()
    open('session', 0, { onConnectionState })
    await flush()
    expect(onConnectionState).toHaveBeenCalledWith(status === 401 ? 'authentication' : 'workspace')
    expect(noteUnauthorized).toHaveBeenCalledWith(expect.objectContaining({ status }),
      '/api/sessions/session/stream', auth)
    const count = fetcher.mock.calls.length
    await vi.advanceTimersByTimeAsync(60000)
    expect(fetcher).toHaveBeenCalledTimes(count)
    expect(streamCalls(fetcher)).toHaveLength(1)
  })

  it('aborts both transports and rejects late frames after close', async () => {
    const body = source()
    const fetcher = transport(() => body.response, () => new Promise(() => {}))
    const onEvent = vi.fn()
    const handle = open('session', 0, { onEvent })
    await flush()
    handle.close()
    expect(fetcher.mock.calls.every(([, init]) => init.signal.aborted)).toBe(true)
    expect(body.reader.cancel).toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
    body.text(frame(envelope(1)))
    await flush()
    await vi.advanceTimersByTimeAsync(60000)
    expect(onEvent).not.toHaveBeenCalled()
    expect(fetcher).toHaveBeenCalledTimes(2)
    expect(body.reader.releaseLock).toHaveBeenCalled()
  })

  it('cancels a scheduled reconnect when closed', async () => {
    const body = source()
    const fetcher = transport(() => body.response)
    const handle = open('session')
    await flush()
    body.end()
    await flush()
    handle.close()
    await vi.advanceTimersByTimeAsync(60000)
    expect(streamCalls(fetcher)).toHaveLength(1)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('retries transient fetch failures and suppresses a response arriving after close', async () => {
    let resolveResponse
    let attempts = 0
    const body = source()
    const fetcher = transport(() => {
      attempts += 1
      if (attempts === 1) return Promise.reject(new TypeError('Disconnected'))
      return new Promise(resolve => { resolveResponse = resolve })
    })
    const onEvent = vi.fn()
    const handle = open('session', 4, { onEvent })
    await flush()
    await vi.advanceTimersByTimeAsync(1000)
    expect(streamCalls(fetcher)).toHaveLength(2)
    handle.close()
    resolveResponse(body.response)
    await flush()
    expect(streamCalls(fetcher)[1][1].signal.aborted).toBe(true)
    expect(body.reader.read).not.toHaveBeenCalled()
    expect(onEvent).not.toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('aborts the active stream when transcript polling refuses authentication', async () => {
    const body = source()
    let refused = false
    const fetcher = transport(() => body.response, () => ({
      ok: !refused, status: refused ? 401 : 200, json: async () => ({ events: [] }),
    }))
    const onEvent = vi.fn()
    const handle = open('session', 0, { onEvent })
    await flush()
    refused = true
    await handle.reconcile()
    expect(streamCalls(fetcher)[0][1].signal.aborted).toBe(true)
    body.text(frame(envelope(1)))
    await flush()
    expect(onEvent).not.toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('retains native EventSource for public callers', async () => {
    authHeaders.mockReturnValue({})
    const native = { addEventListener: vi.fn(), close: vi.fn() }
    const EventSource = vi.fn(function () { return native })
    vi.stubGlobal('EventSource', EventSource)
    const fetcher = transport(() => { throw new Error('Unexpected fetch stream') })
    const onEvent = vi.fn()
    const handle = open('session', 0, { onEvent })
    await flush()
    expect(EventSource).toHaveBeenCalledWith('/backend/api/sessions/session/stream?after_seq=0')
    native.onmessage({ data: JSON.stringify(envelope(1)) })
    expect(onEvent).toHaveBeenCalledWith(envelope(1))
    expect(streamCalls(fetcher)).toHaveLength(0)
    handle.close()
    expect(native.close).toHaveBeenCalled()
  })
})
