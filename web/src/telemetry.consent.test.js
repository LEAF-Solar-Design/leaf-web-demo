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

  it('stop the moment the viewer revokes, with no reload', async () => {
    const { mod, fetchMock } = await loadTelemetry({ consented: true })
    const consent = await import('./lib/telemetryConsent.js')

    consent.setUsageConsent(false)
    expect(mod.trackUsage('menu.action', { id: 'zoom' })).toBeUndefined()
    mod.flushNow()

    expect(postedEvents(fetchMock)).toEqual([])
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
