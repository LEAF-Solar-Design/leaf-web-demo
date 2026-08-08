/**
 * Global (non-React) error capture.
 *
 * The ErrorBoundary only sees what a component throws during render, so a
 * three.js draw tick, a stray callback, and every unawaited promise used to
 * fail with nothing recorded anywhere. These specs pin the four things that
 * make the capture trustworthy: it fires, it redacts, it stops at the cap,
 * and it stays silent for resource-load noise.
 *
 * Each spec re-imports the module (`vi.resetModules`) because the per-session
 * caps deliberately keep an in-memory floor beside sessionStorage — without a
 * fresh instance the cap spec would leak into the ones after it. The jsdom
 * window does NOT reset with the module, so afterEach drains and unhooks the
 * instance it loaded; otherwise every earlier instance would still be
 * listening and would answer the last spec's dispatched event.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

let current = null

/** Load a fresh telemetry module with fetch stubbed. */
async function loadTelemetry() {
  vi.resetModules()
  try { sessionStorage.clear() } catch { /* jsdom always has it */ }
  const fetchMock = vi.fn(() => Promise.resolve({ ok: true, status: 202 }))
  globalThis.fetch = fetchMock
  const mod = await import('./telemetry.js')
  current = mod
  return { mod, fetchMock }
}

/** The events posted to /api/telemetry after an explicit flush. */
function postedEvents(fetchMock) {
  return fetchMock.mock.calls.flatMap(([, init]) => JSON.parse(init.body).events)
}

describe('global error capture', () => {
  beforeEach(() => {
    window.history.replaceState({}, '', '/app/workspace')
  })

  afterEach(() => {
    if (current) {
      current.flushNow()  // drain into THIS spec's mock, never the next one's
      window.removeEventListener('error', current.handleErrorEvent)
      window.removeEventListener('unhandledrejection', current.handleRejectionEvent)
      current = null
    }
    delete globalThis.fetch
  })

  it('emits client.exception for a window error event, with a route but no query string', async () => {
    const { mod, fetchMock } = await loadTelemetry()
    window.history.replaceState({}, '', '/app/workspace?token=abc123secret')

    mod.handleErrorEvent({
      error: new TypeError('Cannot read properties of null'),
      message: 'Uncaught TypeError',
    })
    mod.flushNow()

    const events = postedEvents(fetchMock)
    expect(events).toHaveLength(1)
    expect(events[0]).toMatchObject({
      event_type: 'exception',
      event_name: 'client.exception',
    })
    expect(events[0].labels).toMatchObject({
      source: 'window.onerror',
      message_class: 'TypeError',
      message: 'Cannot read properties of null',
      route: '/app/workspace',
    })
    expect(events[0].labels.ua_class).toMatch(/\/(mobile|desktop)$/)
    // The query string is where invite tokens and drawing ids ride.
    expect(JSON.stringify(events[0].labels)).not.toContain('abc123secret')
  })

  it('emits for an unhandled rejection, including a non-Error reason', async () => {
    const { mod, fetchMock } = await loadTelemetry()

    mod.handleRejectionEvent({ reason: new RangeError('offset out of bounds') })
    mod.handleRejectionEvent({ reason: 'plain string rejection' })
    mod.flushNow()

    const events = postedEvents(fetchMock)
    expect(events.map((e) => e.labels.message_class))
      .toEqual(['RangeError', 'UnhandledRejection'])
    expect(events.map((e) => e.labels.source))
      .toEqual(['unhandledrejection', 'unhandledrejection'])
    expect(events[1].labels.message).toBe('plain string rejection')
  })

  it('redacts emails, token-shaped blobs, long digit runs, and URL query strings', async () => {
    const { mod, fetchMock } = await loadTelemetry()

    const error = new Error(
      'POST https://api.example.com/v1/jobs?access_token=SECRET failed for '
      + 'owner@example.com with id eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9 (case 987654321)',
    )
    mod.handleErrorEvent({ error })
    mod.flushNow()

    const { message } = postedEvents(fetchMock)[0].labels
    expect(message).toContain('https://api.example.com/v1/jobs')
    expect(message).not.toContain('SECRET')
    expect(message).not.toContain('owner@example.com')
    expect(message).not.toContain('eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9')
    expect(message).not.toContain('987654321')
    expect(message).toContain('<email>')
  })

  it('caps the message and the stack head so one exception cannot fill a body', async () => {
    const { mod, fetchMock } = await loadTelemetry()

    // Long but NOT one unbroken run of token characters — that would redact to
    // `<token>` and prove the cap nothing.
    const error = new Error('the renderer failed '.repeat(40))
    error.stack = [
      'Error: the renderer failed',
      `    at renderTick (${'/scene/deep/path'.repeat(40)}/view.js:12:34)`,
      '    at secondFrame (/src/b.js:2:2)',
    ].join('\n')
    mod.handleErrorEvent({ error })
    mod.flushNow()

    const { message, stack_head: stackHead } = postedEvents(fetchMock)[0].labels
    expect(message).toHaveLength(200)
    expect(stackHead).toHaveLength(200)
    // FIRST frame only — the throw site, not the whole call history.
    expect(stackHead).not.toContain('secondFrame')
  })

  it('stops emitting at the per-session cap so an error loop cannot drain the bucket', async () => {
    const { mod, fetchMock } = await loadTelemetry()

    for (let i = 0; i < 40; i++) {
      mod.handleErrorEvent({ error: new Error(`loop ${i}`) })
    }
    mod.flushNow()
    mod.flushNow()

    expect(postedEvents(fetchMock)).toHaveLength(10)
  })

  it('ignores resource-load error events, which carry no exception', async () => {
    const { mod, fetchMock } = await loadTelemetry()

    // A 404 <img> or a blocked <script> dispatches a plain Event: no `error`,
    // no `message`. They arrive in bursts and are not JS failures.
    mod.handleErrorEvent({ target: { tagName: 'IMG' } })
    mod.handleErrorEvent({})
    mod.handleErrorEvent(undefined)
    mod.flushNow()

    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('never throws, whatever the event carries', async () => {
    const { mod } = await loadTelemetry()

    const hostileEvent = { get error() { throw new Error('getter blew up') } }
    const hostileReason = {
      reason: { get name() { throw new Error('nope') }, message: 'ok' },
    }

    expect(() => mod.handleErrorEvent(hostileEvent)).not.toThrow()
    expect(() => mod.handleRejectionEvent(hostileReason)).not.toThrow()
    expect(() => mod.handleErrorEvent(null)).not.toThrow()
    expect(() => mod.handleRejectionEvent(null)).not.toThrow()
  })

  it('installs on the window once, and a real dispatched error event reaches the rail', async () => {
    const { mod, fetchMock } = await loadTelemetry()

    // Importing the module already installed the listeners; a second call is
    // a no-op rather than a duplicate emit.
    expect(mod.installGlobalErrorHandlers()).toBe(false)

    window.dispatchEvent(new ErrorEvent('error', {
      error: new Error('from a real event'),
      message: 'from a real event',
    }))
    mod.flushNow()

    const events = postedEvents(fetchMock)
    expect(events).toHaveLength(1)
    expect(events[0].labels.message).toBe('from a real event')
  })
})
