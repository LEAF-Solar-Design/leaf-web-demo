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
let installed = []

/** Load a fresh telemetry module with fetch stubbed.
 *
 * Every listener the module registers on import is recorded so afterEach can
 * remove ALL of them, not just the two exported handlers: the pagehide ->
 * beaconFlush listener is not exported, and one per re-import would otherwise
 * accumulate on the shared jsdom window. */
async function loadTelemetry() {
  vi.resetModules()
  try { sessionStorage.clear() } catch { /* jsdom always has it */ }
  const fetchMock = vi.fn(() => Promise.resolve({ ok: true, status: 202 }))
  globalThis.fetch = fetchMock

  installed = []
  const realAdd = window.addEventListener.bind(window)
  window.addEventListener = (type, fn, opts) => {
    installed.push([type, fn, opts])
    return realAdd(type, fn, opts)
  }
  try {
    const mod = await import('./telemetry.js')
    current = mod
    return { mod, fetchMock }
  } finally {
    window.addEventListener = realAdd
  }
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
      current = null
    }
    for (const [type, fn, opts] of installed) window.removeEventListener(type, fn, opts)
    installed = []
    delete globalThis.fetch
  })

  it('emits client.exception for a window error event', async () => {
    const { mod, fetchMock } = await loadTelemetry()

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
      route: 'app',
    })
    expect(events[0].labels.ua_class).toMatch(/\/(mobile|desktop)$/)
  })

  it('labels the route with the app scene, never the pathname a customer name rides in', async () => {
    const { mod, fetchMock } = await loadTelemetry()
    // A shape this product really serves: customer name AND invite code, both
    // in the path, where no query-string rule would ever have seen them.
    window.history.replaceState({}, '', '/app/Alice-Smith/invite/7uP9-kL2_mN4qR8s?t=x')

    mod.handleErrorEvent({ error: new Error('boom') })
    mod.flushNow()

    const labels = postedEvents(fetchMock)[0].labels
    expect(labels.route).toBe('app')          // one of four static scene names
    const serialized = JSON.stringify(labels)
    expect(serialized).not.toContain('Alice-Smith')
    expect(serialized).not.toContain('7uP9-kL2_mN4qR8s')
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

  it('drops whole URIs, emails, and identifier-shaped runs from the message', async () => {
    const { mod, fetchMock } = await loadTelemetry()

    const error = new Error(
      'POST https://api.example.com/invite/AKIAIOSFODNN7EXAMPLE/Alice-Smith/plan.dwg -> 403 '
      + 'for owner@example.com id eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9 case 987654321',
    )
    mod.handleErrorEvent({ error })
    mod.flushNow()

    const { message } = postedEvents(fetchMock)[0].labels
    // The whole URI goes, not just its query: the PATH carried the credential
    // and the customer name.
    expect(message).not.toContain('api.example.com')
    expect(message).not.toContain('AKIAIOSFODNN7EXAMPLE')  // 20 chars: under any 24 rule
    expect(message).not.toContain('Alice-Smith')
    expect(message).not.toContain('owner@example.com')
    expect(message).not.toContain('eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9')
    expect(message).not.toContain('987654321')
    expect(message).toContain('<url>')
    expect(message).toContain('<email>')
  })

  it('redacts a RELATIVE path with a query, which is what this app actually throws', async () => {
    const { mod, fetchMock } = await loadTelemetry()

    // api.js throws relative, not absolute: `POST /api/drawings/<id>/checkout`.
    // An absolute-URL-only rule would have let every one of these through.
    mod.handleErrorEvent({
      error: new Error('POST /api/drawings/dwg-8823/checkout?invite=abc123xyz -> 409'),
    })
    mod.flushNow()

    const { message } = postedEvents(fetchMock)[0].labels
    expect(message).not.toContain('dwg-8823')
    expect(message).not.toContain('abc123xyz')
    expect(message).not.toContain('invite=')
    // The route SHAPE is the diagnostic value and survives.
    expect(message).toContain('/api/drawings/<id>/checkout')
    expect(message).toContain('409')
  })

  it('redacts percent-encoded values, which survive every other rule', async () => {
    const { mod, fetchMock } = await loadTelemetry()

    mod.handleErrorEvent({ error: new Error('lookup failed owner%40example.com') })
    mod.flushNow()

    const { message } = postedEvents(fetchMock)[0].labels
    expect(message).not.toContain('owner%40example.com')
    expect(message).toContain('<enc>')
  })

  it('refuses a message_class that is not identifier-shaped', async () => {
    const { mod, fetchMock } = await loadTelemetry()

    // `name` is an ordinary writable property, so on a foreign object it is
    // arbitrary attacker text, not a class.
    const hostile = new Error('x')
    hostile.name = 'owner@example.com leaked via the class label'
    mod.handleErrorEvent({ error: hostile })
    mod.flushNow()

    const { message_class: cls } = postedEvents(fetchMock)[0].labels
    expect(cls).toBe('Error')
    expect(cls).not.toContain('owner@example.com')
  })

  it('reduces the stack head to fn@file:line:col and never carries an inline payload', async () => {
    const { mod, fetchMock } = await loadTelemetry()

    const real = new Error('render failed')
    real.stack = [
      'Error: render failed at /scene/tick',   // a MESSAGE line containing " at "
      '    at renderTick (https://platform-staging.leafdesign.ai/assets/index-DytZ.js:12:34)',
      '    at secondFrame (/src/b.js:2:2)',
    ].join('\n')

    const inline = new Error('inline failed')
    inline.stack = 'Error\n    at evil (data:text/javascript;base64,c2VjcmV0LXNvdXJjZQ==:1:1)'

    mod.handleErrorEvent({ error: real })
    mod.handleErrorEvent({ error: inline })
    mod.flushNow()

    const [a, b] = postedEvents(fetchMock).map((e) => e.labels.stack_head)
    // Function + file + position kept; host and directories dropped.
    expect(a).toBe('renderTick@index-DytZ.js:12:34')
    expect(a).not.toContain('leafdesign.ai')
    expect(a).not.toContain('secondFrame')   // FIRST frame only
    // The message line was not mistaken for a frame.
    expect(a).not.toContain('/scene/tick')
    // A data: URI frame can inline a whole source file.
    expect(b).toBe('evil@<inline>')
    expect(b).not.toContain('c2VjcmV0LXNvdXJjZQ')
  })

  it('caps the message length so one exception cannot fill a body', async () => {
    const { mod, fetchMock } = await loadTelemetry()

    // Long but NOT one unbroken identifier run — that would redact to
    // `<token>` and prove the cap nothing.
    mod.handleErrorEvent({ error: new Error('the renderer failed '.repeat(40)) })
    mod.flushNow()

    expect(postedEvents(fetchMock)[0].labels.message).toHaveLength(200)
  })

  it('stops emitting at the per-session cap so an error loop cannot drain the bucket', async () => {
    const { mod, fetchMock } = await loadTelemetry()

    for (let i = 0; i < 40; i++) {
      mod.handleErrorEvent({ error: new Error(`distinct failure number ${i}`) })
    }
    mod.flushNow()
    mod.flushNow()

    expect(postedEvents(fetchMock)).toHaveLength(10)
  })

  it('spends nothing on repeats, so a ResizeObserver loop cannot crowd out a real crash', async () => {
    const { mod, fetchMock } = await loadTelemetry()

    // The classic pair for an app with a three.js viewer and resizable panels.
    for (let i = 0; i < 200; i++) {
      mod.handleErrorEvent({ error: new Error('ResizeObserver loop limit exceeded') })
    }
    mod.handleErrorEvent({ error: new TypeError('the real crash, seen last') })
    mod.flushNow()

    const events = postedEvents(fetchMock)
    expect(events).toHaveLength(2)
    expect(events[1].labels.message).toBe('the real crash, seen last')
  })

  it('keeps the ErrorBoundary budget separate from the global one', async () => {
    const { mod, fetchMock } = await loadTelemetry()

    // A storm of DISTINCT global errors exhausts the global budget...
    for (let i = 0; i < 30; i++) {
      mod.handleErrorEvent({ error: new Error(`storm ${i}`) })
    }
    // ...and the boundary's own crash record still lands.
    mod.trackException({ message_class: 'TypeError', component_stack_hash: '42' })
    mod.flushNow()

    const events = postedEvents(fetchMock)
    expect(events).toHaveLength(11)
    expect(events[10].labels).toMatchObject({
      message_class: 'TypeError',
      component_stack_hash: '42',
    })
    expect(events[10].labels.source).toBeUndefined()
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
