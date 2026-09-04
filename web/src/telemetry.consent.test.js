/**
 * The consent gate inside `buildEvent` (slice 13c).
 *
 * The product promise is a negative one — "no usage-shaped data is collected
 * before the toggle is on" — so it is tested at the seam where the decision is
 * made AND in both directions: a refused event must never reach the wire, and
 * a consented one must, or the gate would be indistinguishable from a feature
 * that simply does not work.
 *
 * Every spec loads a FRESH module pair (`vi.resetModules`) because both the
 * consent cache and telemetry's buffer live in module scope; the listeners
 * telemetry.js installs on import are recorded and removed afterwards so no
 * earlier instance answers a later spec.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

const KEY = 'leaf.telemetry.usage_consent.v1'

let current = null
let installed = []

/** Load telemetry.js with fetch stubbed and `consented` already decided. */
async function loadTelemetry({ consented = false, buildDisabled = false } = {}) {
  vi.resetModules()
  try {
    localStorage.clear()
    sessionStorage.clear()
    if (consented) localStorage.setItem(KEY, 'granted')
  } catch { /* jsdom always has both */ }
  if (buildDisabled) vi.stubEnv('VITE_TELEMETRY_DISABLED', '1')

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

/** Everything that actually reached POST /api/telemetry. */
function postedEvents(fetchMock) {
  return fetchMock.mock.calls.flatMap(([, init]) => JSON.parse(init.body).events)
}

afterEach(() => {
  if (current) {
    current.flushNow()   // drain into THIS spec's mock, never the next one's
    current = null
  }
  for (const [type, fn, opts] of installed) window.removeEventListener(type, fn, opts)
  installed = []
  delete globalThis.fetch
  vi.unstubAllEnvs()
  try { localStorage.clear() } catch { /* no-op */ }
})

describe('usage-shaped events without consent', () => {
  it('are never built', async () => {
    const { mod } = await loadTelemetry()
    expect(mod.buildEvent('search.submitted', { q_len: 4 }, 'custom_event', mod.EVENT_CLASS.usage))
      .toBeUndefined()
  })

  it('never leave: nothing is queued and nothing is posted', async () => {
    const { mod, fetchMock } = await loadTelemetry()

    expect(mod.trackUsage('search.submitted', { q_len: 4 })).toBeUndefined()
    expect(mod.trackUsage('menu.action', { id: 'zoom' })).toBeUndefined()
    mod.flushNow()

    expect(fetchMock).not.toHaveBeenCalled()
    expect(postedEvents(fetchMock)).toEqual([])
  })

  it('are not held for a later consent to release', async () => {
    // A refused event does not exist. Granting consent afterwards must not
    // retroactively ship what was refused while the switch was off.
    const { mod, fetchMock } = await loadTelemetry()
    mod.trackUsage('search.submitted', { q_len: 4 })

    const consent = await import('./lib/telemetryConsent.js')
    consent.setUsageConsent(true)
    mod.flushNow()

    expect(postedEvents(fetchMock)).toEqual([])
  })
})

describe('usage-shaped events WITH consent', () => {
  it('are built and posted (the gate is a gate, not a wall)', async () => {
    const { mod, fetchMock } = await loadTelemetry({ consented: true })

    expect(mod.trackUsage('search.submitted', { q_len: 4 })).toBeDefined()
    mod.flushNow()

    const events = postedEvents(fetchMock)
    expect(events).toHaveLength(1)
    expect(events[0]).toMatchObject({ event_name: 'search.submitted', event_type: 'custom_event' })
    expect(events[0].labels).toMatchObject({ q_len: 4 })
  })

  it('refuse every NEW event the moment the viewer revokes, with no reload', async () => {
    // NEW events only. The events already queued when the switch went off
    // are the next describe block, which is a separate guarantee: this one
    // alone was once read as the whole promise and was not.
    const { mod, fetchMock } = await loadTelemetry({ consented: true })
    const consent = await import('./lib/telemetryConsent.js')

    consent.setUsageConsent(false)
    expect(mod.trackUsage('menu.action', { id: 'zoom' })).toBeUndefined()
    mod.flushNow()

    expect(postedEvents(fetchMock)).toEqual([])
  })
})

describe('a usage event ALREADY QUEUED when the viewer revokes', () => {
  // The half the first version of this slice got wrong, and the reason this
  // suite exists. `buildEvent` gates at BUILD time, so an event queued while
  // consented sat in the shared buffer behind the 5 s timer and was posted by
  // whatever drained it next — the timer, `flushNow`, the pagehide beacon, or
  // the 2 s post-failure retry. Each of those seams gets a spec here, because
  // a fix that closed only the timer would have looked green.

  it('is purged from the buffer and never reaches the wire', async () => {
    const { mod, fetchMock } = await loadTelemetry({ consented: true })
    const consent = await import('./lib/telemetryConsent.js')

    expect(mod.trackUsage('search.submitted', { q_len: 4 })).toBeDefined()
    consent.setUsageConsent(false)
    mod.flushNow()

    expect(postedEvents(fetchMock)).toEqual([])
  })

  it('is dropped without taking the product events queued beside it', async () => {
    // The purge is surgical. Product events are not consent-gated, and losing
    // the operational record on every revoke would be its own defect.
    const { mod, fetchMock } = await loadTelemetry({ consented: true })
    const consent = await import('./lib/telemetryConsent.js')

    mod.track('run.finished', { ok: true })
    mod.trackUsage('search.submitted', { q_len: 4 })
    mod.track('version.restored', { n: 2 })
    consent.setUsageConsent(false)
    mod.flushNow()

    expect(postedEvents(fetchMock).map((e) => e.event_name))
      .toEqual(['run.finished', 'version.restored'])
  })

  it('is dropped even with a nearly full batch waiting behind the timer', async () => {
    // FLUSH_AT is 20, so 19 queued events is the worst case that can sit
    // unflushed: the reviewer measured "up to 20 events / 5 s of usage-shaped
    // data" reaching fetch after the switch went off.
    const { mod, fetchMock } = await loadTelemetry({ consented: true })
    const consent = await import('./lib/telemetryConsent.js')

    for (let i = 0; i < 19; i += 1) mod.trackUsage('menu.action', { id: i })
    consent.setUsageConsent(false)
    mod.flushNow()

    expect(postedEvents(fetchMock)).toEqual([])
  })

  it('does not leave with the pagehide beacon, though product events still do', async () => {
    // Revoking and then closing the tab is one continuous human action, so
    // this is the seam a revoke is most likely to race.
    const { mod } = await loadTelemetry({ consented: true })
    const consent = await import('./lib/telemetryConsent.js')
    const beacon = vi.fn(() => true)
    const orig = Object.getOwnPropertyDescriptor(navigator, 'sendBeacon')
    Object.defineProperty(navigator, 'sendBeacon', {
      value: beacon, configurable: true, writable: true,
    })

    try {
      mod.trackUsage('search.submitted', { q_len: 4 })
      mod.track('run.finished', { ok: true })
      consent.setUsageConsent(false)
      window.dispatchEvent(new Event('pagehide'))

      expect(beacon).toHaveBeenCalledTimes(1)
      const blob = beacon.mock.calls[0][1]
      // A jsdom Blob has no .text(), and undici's Response stringifies it
      // rather than reading it, so FileReader is the one reader that works.
      const raw = await new Promise((res, rej) => {
        const fr = new FileReader()
        fr.onload = () => res(String(fr.result))
        fr.onerror = () => rej(fr.error)
        fr.readAsText(blob)
      })
      const body = JSON.parse(raw)
      expect(body.events.map((e) => e.event_name)).toEqual(['run.finished'])
    } finally {
      if (orig) Object.defineProperty(navigator, 'sendBeacon', orig)
      else delete navigator.sendBeacon
    }
  })

  it('is dropped from a batch already retrying when the revoke lands', async () => {
    // flush() posts once and, on failure, replays THE SAME batch 2 s later
    // from a closure that had already captured it. Without a re-check at the
    // retry, a revoke inside that window sent the usage events anyway.
    const { mod, fetchMock } = await loadTelemetry({ consented: true })
    const consent = await import('./lib/telemetryConsent.js')
    vi.useFakeTimers()

    try {
      fetchMock.mockImplementation(() => Promise.reject(new Error('offline')))
      mod.trackUsage('search.submitted', { q_len: 4 })
      mod.track('run.finished', { ok: true })
      mod.flushNow()
      await vi.advanceTimersByTimeAsync(0)
      expect(fetchMock).toHaveBeenCalledTimes(1)

      consent.setUsageConsent(false)
      fetchMock.mockImplementation(() => Promise.resolve({ ok: true, status: 202 }))
      await vi.advanceTimersByTimeAsync(2100)

      expect(fetchMock).toHaveBeenCalledTimes(2)
      const retried = JSON.parse(fetchMock.mock.calls[1][1].body).events
      expect(retried.map((e) => e.event_name)).toEqual(['run.finished'])
    } finally {
      vi.useRealTimers()
    }
  })

  it('is DESTROYED inside the retry window too: revoke then re-grant posts no usage event', async () => {
    // The retry batch lives OUT of state.buffer, in a closure the 2 s timer
    // holds, so a purge of the buffer alone never reaches it. Without the
    // pending-batch registry this spec is the counter-example to the whole
    // "destructive revoke" claim: consent is TRUE again at the moment the
    // retry fires, so the send-time fence waves it through and the viewer's
    // revoked search query is posted anyway.
    const { mod, fetchMock } = await loadTelemetry({ consented: true })
    const consent = await import('./lib/telemetryConsent.js')
    vi.useFakeTimers()

    try {
      fetchMock.mockImplementation(() => Promise.reject(new Error('offline')))
      mod.trackUsage('search.submitted', { q_len: 4 })
      mod.track('run.finished', { ok: true })
      mod.flushNow()
      await vi.advanceTimersByTimeAsync(0)
      expect(fetchMock).toHaveBeenCalledTimes(1)

      consent.setUsageConsent(false)   // the revoke DESTROYS the usage half...
      consent.setUsageConsent(true)    // ...and a re-grant inside the window finds nothing
      fetchMock.mockImplementation(() => Promise.resolve({ ok: true, status: 202 }))
      await vi.advanceTimersByTimeAsync(2100)

      expect(fetchMock).toHaveBeenCalledTimes(2)
      const retried = JSON.parse(fetchMock.mock.calls[1][1].body).events
      expect(retried.map((e) => e.event_name)).toEqual(['run.finished'])
      expect(consent.usageConsentGranted()).toBe(true)   // non-vacuous: consent IS back on
    } finally {
      vi.useRealTimers()
    }
  })

  it('is DESTROYED while the first POST is still in flight: a revoke mid-request decides what the retry may carry', async () => {
    // The third place a usage event can be: neither in state.buffer nor yet
    // handed to the 2 s retry, but inside a POST that has not resolved. If the
    // batch were registered only in the failure callback, a revoke landing
    // during the request would find an empty registry, the rejection would
    // arm a retry with the usage event intact, and a re-grant inside the
    // window would post the revoked event again. Registering BEFORE the
    // request leaves closes that gap; this spec pins it.
    const { mod, fetchMock } = await loadTelemetry({ consented: true })
    const consent = await import('./lib/telemetryConsent.js')
    vi.useFakeTimers()

    try {
      let rejectFirst
      fetchMock.mockImplementation(() => new Promise((_, reject) => { rejectFirst = reject }))
      mod.trackUsage('search.submitted', { q_len: 4 })
      mod.track('run.finished', { ok: true })
      mod.flushNow()
      await vi.advanceTimersByTimeAsync(0)
      expect(fetchMock).toHaveBeenCalledTimes(1)   // on the wire, unresolved

      consent.setUsageConsent(false)   // revoke while the request is in flight
      consent.setUsageConsent(true)    // re-grant before it fails
      fetchMock.mockImplementation(() => Promise.resolve({ ok: true, status: 202 }))
      rejectFirst(new Error('offline'))
      await vi.advanceTimersByTimeAsync(2100)

      expect(fetchMock).toHaveBeenCalledTimes(2)
      const retried = JSON.parse(fetchMock.mock.calls[1][1].body).events
      expect(retried.map((e) => e.event_name)).toEqual(['run.finished'])
      expect(consent.usageConsentGranted()).toBe(true)   // non-vacuous: consent IS back on
    } finally {
      vi.useRealTimers()
    }
  })

  it('cannot grow the pending-retry set without bound', async () => {
    // The registry that makes the purge reach in-flight batches is itself a
    // queue, so it is capped (RETRY_BATCH_MAX = 8). A host rejecting every
    // POST arms at most that many retries; the rest are dropped, which
    // telemetry is allowed to do and an unbounded set is not.
    const { mod, fetchMock } = await loadTelemetry({ consented: true })
    vi.useFakeTimers()

    try {
      fetchMock.mockImplementation(() => Promise.reject(new Error('offline')))
      for (let i = 0; i < 12; i += 1) {
        mod.track(`run.finished.${i}`, { ok: true })
        mod.flushNow()
        await vi.advanceTimersByTimeAsync(0)
      }
      expect(fetchMock).toHaveBeenCalledTimes(12)   // 12 first attempts

      await vi.advanceTimersByTimeAsync(2100)
      expect(fetchMock).toHaveBeenCalledTimes(20)   // + 8 retries, not 12
    } finally {
      vi.useRealTimers()
    }
  })

  it('is DESTROYED by the revoke, not merely fenced: a re-grant cannot resurrect it', async () => {
    // This is the spec that separates the two halves of the fix. The wire-time
    // fence alone would let these events sit in the buffer through the revoke
    // and then POST the moment the viewer turned the switch back on, which is
    // the same leak on a longer fuse. The buffer purge is what makes the
    // revoke destructive, so this goes red if the subscription is removed.
    const { mod, fetchMock } = await loadTelemetry({ consented: true })
    const consent = await import('./lib/telemetryConsent.js')

    mod.trackUsage('search.submitted', { q_len: 4 })
    consent.setUsageConsent(false)
    consent.setUsageConsent(true)
    mod.flushNow()

    expect(postedEvents(fetchMock)).toEqual([])
  })

  it('carries no internal class marker to the wire', async () => {
    // The marker the purge reads is on a Symbol key precisely so it cannot be
    // serialized. If it ever becomes a string key, this spec goes red before
    // an internal label reaches the ingest door.
    const { mod, fetchMock } = await loadTelemetry({ consented: true })

    mod.trackUsage('search.submitted', { q_len: 4 })
    mod.flushNow()

    const [event] = postedEvents(fetchMock)
    expect(Object.keys(event).sort())
      .toEqual(['client_ts', 'event_name', 'event_type', 'labels'])
  })
})

describe('product events', () => {
  it('are unchanged by consent: they send with the switch off', async () => {
    const { mod, fetchMock } = await loadTelemetry()

    expect(mod.track('run.finished', { ok: true })).toBeDefined()
    mod.flushNow()

    const events = postedEvents(fetchMock)
    expect(events).toHaveLength(1)
    expect(events[0]).toMatchObject({ event_name: 'run.finished' })
  })

  it('still stop at the build-time kill switch', async () => {
    const { mod, fetchMock } = await loadTelemetry({ consented: true, buildDisabled: true })

    expect(mod.TELEMETRY_BUILD_DISABLED).toBe(true)
    expect(mod.track('run.finished', { ok: true })).toBeUndefined()
    expect(mod.trackUsage('search.submitted', { q_len: 4 })).toBeUndefined()
    mod.flushNow()

    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('an event with no class', () => {
  it('is treated as usage-shaped and refused without consent', async () => {
    const { mod } = await loadTelemetry()
    expect(mod.buildEvent('mystery', {}, 'custom_event')).toBeUndefined()
    expect(mod.buildEvent('mystery', {}, 'custom_event', undefined)).toBeUndefined()
  })

  it('is refused for any class that is not the exact product literal', async () => {
    const { mod } = await loadTelemetry()
    for (const cls of ['Product', 'PRODUCT', 'prod', '', null, 0, true, {}]) {
      expect(mod.buildEvent('mystery', {}, 'custom_event', cls), `class=${String(cls)}`)
        .toBeUndefined()
    }
  })

  it('becomes sendable once consent is granted, like any usage event', async () => {
    const { mod } = await loadTelemetry({ consented: true })
    expect(mod.buildEvent('mystery', {}, 'custom_event')).toBeDefined()
  })
})
