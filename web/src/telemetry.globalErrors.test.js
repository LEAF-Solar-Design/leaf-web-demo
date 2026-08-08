/**
 * Global (non-React) error capture.
 *
 * The ErrorBoundary only sees what a component throws during render, so a
 * three.js draw tick, a stray callback, and every unawaited promise used to
 * fail with nothing recorded anywhere. These specs pin the four things that
 * make the capture trustworthy: it fires, NO FREE TEXT LEAVES THE BROWSER, it
 * stops at the cap, and it stays silent for resource-load noise.
 *
 * There is NO cross-emitter de-duplication (see telemetry.js's comment above
 * `emitGlobalException`): the global handlers and the ErrorBoundary each emit
 * `client.exception` independently, with no shared occurrence key and no
 * same-signature suppression. Four review rounds tried to build one and each
 * either broke under ordinary batching/pagehide timing or, in the final
 * attempt, silently merged two genuinely distinct same-signature crashes into
 * one row -- worse than the duplicate it was meant to remove. These specs do
 * not assert against a double-emit; where one is possible (a dev-mode React
 * crash reaching both the global handler and the boundary) it is documented,
 * not policed.
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

/** telemetry.js's own timing, mirrored rather than imported: it is private on
 * purpose, and the "emits a global row straight away, holding nothing back"
 * spec below exists to prove nothing is held across it. */
const FLUSH_AT = 20             // events per batch

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

function exceptionsIn(events) {
  return events.filter((e) => e.event_name === 'client.exception')
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
      route: 'app',
    })
    expect(events[0].labels.ua_class).toMatch(/\/(mobile|desktop)$/)
    // The message travels as a stable hash, never as text.
    expect(events[0].labels.message).toBeUndefined()
    expect(events[0].labels.message_hash).toMatch(/^\d+$/)
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
    expect(events[1].labels.message_hash).toMatch(/^\d+$/)
  })

  it('accepts only a PLATFORM error name, because name is writable', async () => {
    const { mod, fetchMock } = await loadTelemetry()

    // An identifier-SHAPED check is not enough: `name` is an ordinary
    // writable property, so `Promise.reject({name: 'AliceSmith'})` is a legal
    // rejection any visitor can produce and it would have passed one.
    mod.handleRejectionEvent({ reason: { name: 'AliceSmith', message: 'probe' } })
    const spaced = new Error('x')
    spaced.name = 'owner@example.com leaked via the class label'
    mod.handleErrorEvent({ error: spaced })
    // A real platform name still comes through.
    mod.handleErrorEvent({ error: new RangeError('genuine') })
    mod.flushNow()

    const classes = postedEvents(fetchMock).map((e) => e.labels.message_class)
    expect(classes).toEqual(['UnhandledRejection', 'Other', 'RangeError'])
    expect(JSON.stringify(classes)).not.toContain('AliceSmith')
    expect(JSON.stringify(classes)).not.toContain('owner@example.com')
  })

  it('never lets free text out, whatever the message contains', async () => {
    const { mod, fetchMock } = await loadTelemetry()

    // Every case two adversarial reviews found surviving a redactor. None of
    // them can survive a hash.
    const hostile = [
      'Failed to load /app/alice/settings',
      "Validation failed for customer 'Alice Smith'",
      'Request to 192.168.1.42 refused',
      'open failed: C:\\Users\\Alice\\Customer-X\\site-plan.dwg',
      'id deadbeef rejected',
      'GET https://api.example.com/invite/AKIAIOSFODNN7EXAMPLE -> 403',
      'contact owner@example.com about 555-0142',
    ]
    for (const m of hostile) mod.handleErrorEvent({ error: new Error(m) })
    mod.flushNow()

    const serialized = JSON.stringify(postedEvents(fetchMock))
    for (const needle of [
      'alice', 'Alice', 'Customer-X', '192.168', 'deadbeef',
      'AKIAIOSFODNN7EXAMPLE', 'owner@example.com', '555-0142', 'settings',
    ]) {
      expect(serialized).not.toContain(needle)
    }
  })

  it('hashes the message stably, so distinct failures stay distinguishable', async () => {
    const { mod, fetchMock } = await loadTelemetry()

    mod.handleErrorEvent({ error: new Error('alpha') })
    mod.handleErrorEvent({ error: new RangeError('beta') })
    mod.flushNow()

    const [a, b] = postedEvents(fetchMock).map((e) => e.labels.message_hash)
    expect(a).not.toBe(b)          // different failures do not collide
    expect(a).toMatch(/^\d+$/)     // and it is an opaque number, not text
  })

  it('hashes the throw site instead of exporting it, so a crafted stack leaks nothing', async () => {
    const { mod, fetchMock } = await loadTelemetry()

    // `FRAME_FN_RE`/`FRAME_FILE_RE` once checked SHAPE, not provenance, so
    // every one of these put its payload straight into a label.
    const crafted = [
      'Error\n    at AliceSmith (https://x/assets/index.js:1:2)',
      'AliceSmith@https://x/assets/customerSecret:3:4',
      'Error\n    at deadbeef (data:text/javascript;base64,c2VjcmV0:1:1)',
    ]
    for (const stack of crafted) {
      const e = new Error('x')
      e.stack = stack
      mod.handleErrorEvent({ error: e })
    }
    mod.flushNow()

    const events = postedEvents(fetchMock)
    const serialized = JSON.stringify(events)
    for (const needle of ['AliceSmith', 'customerSecret', 'deadbeef', 'c2VjcmV0', 'index.js']) {
      expect(serialized).not.toContain(needle)
    }
    // Still grouped: three distinct throw sites, three distinct digests.
    const hashes = events.map((e) => e.labels.stack_hash)
    expect(new Set(hashes).size).toBe(3)
    for (const h of hashes) expect(h).toMatch(/^\d+$/)
    expect(events[0].labels.stack_head).toBeUndefined()
  })

  it('distinguishes two rejections that stringify identically', async () => {
    const { mod, fetchMock } = await loadTelemetry()

    // Both are "[object Object]", so hashing the stringification alone would
    // make them indistinguishable in the stored labels.
    mod.handleRejectionEvent({ reason: { code: 4 } })
    mod.handleRejectionEvent({ reason: { code: 5 } })
    mod.flushNow()

    const events = postedEvents(fetchMock)
    expect(events).toHaveLength(2)
    expect(events[0].labels.message_hash).not.toBe(events[1].labels.message_hash)
  })

  it('does not collide on the short inputs the 32-bit hash merged', async () => {
    const { mod, fetchMock } = await loadTelemetry()

    // "Aa" and "BB" both hashed to 2112 under the previous shift-hash, so the
    // two distinct failures were indistinguishable once hashed.
    mod.handleErrorEvent({ error: new Error('Aa') })
    mod.handleErrorEvent({ error: new Error('BB') })
    mod.flushNow()

    const events = postedEvents(fetchMock)
    expect(events).toHaveLength(2)
    expect(events[0].labels.message_hash).not.toBe(events[1].labels.message_hash)
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

  it('caps only the global path, so a storm cannot silence the boundary', async () => {
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
    const boundaryRows = events.filter((e) => e.labels.source === undefined)
    expect(boundaryRows).toHaveLength(1)
    expect(boundaryRows[0].labels).toMatchObject({
      message_class: 'TypeError',
      component_stack_hash: '42',
    })
  })

  it('never caps the ErrorBoundary, so a crash-and-reload loop stays visible', async () => {
    const { mod, fetchMock } = await loadTelemetry()

    // The boundary was briefly given a 5-per-session budget for symmetry with
    // the global one. It is a regression, not symmetry: the boundary fires
    // once per crash and the card it renders invites a reload, so five
    // crash-and-reload cycles in one browser session would silence every LATER
    // production crash on that tab -- and the crash a user hits after already
    // hitting several is the one most worth seeing.
    for (let i = 0; i < 12; i++) {
      mod.trackException({
        message_class: 'TypeError',
        component_stack_hash: String(i).padStart(16, '0'),
      })
    }
    mod.flushNow()

    const events = postedEvents(fetchMock)
    expect(events).toHaveLength(12)
    expect(events[11].labels.component_stack_hash).toBe('0000000000000011')
  })

  it('emits a global row straight away, holding nothing back', async () => {
    const { mod, fetchMock } = await loadTelemetry()

    // No pending list, no hold timer: a three.js draw tick throws and React
    // never hears about it, so it is an ordinary queued event like any other,
    // out with the next flush.
    mod.handleErrorEvent({ error: new Error('a callback nothing renders') })
    for (let i = 0; i < FLUSH_AT - 1; i++) mod.track(`filler.${i}`)

    const exceptions = exceptionsIn(postedEvents(fetchMock))
    expect(exceptions).toHaveLength(1)
    expect(exceptions[0].labels.source).toBe('window.onerror')
    expect(exceptions[0].labels.dedup_key).toBeUndefined()
  })

  it('lets a global row leave with the pagehide beacon, so a torn-down tab keeps its crash', async () => {
    const { mod } = await loadTelemetry()

    const sent = []
    const hadBeacon = 'sendBeacon' in navigator
    navigator.sendBeacon = (url, blob) => { sent.push(blob); return true }
    try {
      mod.handleErrorEvent({ error: new Error('crash, then the tab closes') })
      window.dispatchEvent(new Event('pagehide'))

      expect(sent).toHaveLength(1)
      // FileReader, not Blob.text(): this jsdom's Blob has no text().
      const body = await new Promise((resolve, reject) => {
        const reader = new FileReader()
        reader.onload = () => resolve(String(reader.result))
        reader.onerror = () => reject(reader.error)
        reader.readAsText(sent[0])
      })
      const events = JSON.parse(body).events
      expect(events).toHaveLength(1)
      expect(events[0].event_name).toBe('client.exception')
      expect(events[0].labels.source).toBe('window.onerror')
    } finally {
      if (!hadBeacon) delete navigator.sendBeacon
    }
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
    expect(events[0].event_name).toBe('client.exception')
    expect(events[0].labels.source).toBe('window.onerror')
  })
})
