/**
 * The consent rail (slice 13c).
 *
 * This module is the single gate standing between a person's search queries
 * and a BigQuery table, so its specs are written as a truth table over the
 * ways a browser can lie to it: no key, the wrong value, a store that refuses
 * to write, a store whose very property access throws. Every one of them must
 * read NOT CONSENTED.
 *
 * Each spec loads a FRESH module (`vi.resetModules`) because the granted value
 * is cached in module scope on purpose (hot-path clause 2, see the source):
 * without a fresh instance a spec would inherit the previous one's cache and
 * pass for the wrong reason.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

const KEY = 'leaf.telemetry.usage_consent.v1'

/** A minimal Storage stand-in. `behaviour` lets a spec make it hostile. */
function fakeStore(initial = {}, behaviour = {}) {
  const data = { ...initial }
  return {
    data,
    getItem: (k) => (behaviour.throwOnRead ? (() => { throw new Error('locked') })() : (k in data ? data[k] : null)),
    setItem: (k, v) => {
      if (behaviour.throwOnWrite) throw new Error('quota')
      data[k] = String(v)
    },
    removeItem: (k) => {
      if (behaviour.throwOnWrite) throw new Error('quota')
      delete data[k]
    },
  }
}

/** Install `store` as globalThis.localStorage and import a fresh module. */
async function loadConsent(store) {
  vi.resetModules()
  const descriptor = store === 'throws'
    ? { get() { throw new Error('storage is disabled') }, configurable: true }
    : { value: store, writable: true, configurable: true }
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  restore.push(() => {
    if (previous) Object.defineProperty(globalThis, 'localStorage', previous)
    else delete globalThis.localStorage
  })
  Object.defineProperty(globalThis, 'localStorage', descriptor)
  return import('./telemetryConsent.js')
}

let restore = []
afterEach(() => {
  for (const undo of restore.reverse()) undo()
  restore = []
})

describe('reading consent', () => {
  it('is OFF when nothing was ever stored', async () => {
    const mod = await loadConsent(fakeStore())
    expect(mod.usageConsentGranted()).toBe(false)
  })

  it('is ON when the exact grant was persisted', async () => {
    const mod = await loadConsent(fakeStore({ [KEY]: 'granted' }))
    expect(mod.usageConsentGranted()).toBe(true)
  })

  it('fails closed on every value that is not the exact grant', async () => {
    for (const stored of ['1', 'true', 'GRANTED', 'granted ', 'yes', '', 'denied']) {
      const mod = await loadConsent(fakeStore({ [KEY]: stored }))
      expect(mod.usageConsentGranted(), `stored=${JSON.stringify(stored)}`).toBe(false)
      for (const undo of restore.splice(0).reverse()) undo()
    }
  })

  it('fails closed when getItem throws (private mode, storage-locked webview)', async () => {
    const mod = await loadConsent(fakeStore({ [KEY]: 'granted' }, { throwOnRead: true }))
    expect(mod.usageConsentGranted()).toBe(false)
  })

  it('fails closed when the localStorage property itself throws', async () => {
    const mod = await loadConsent('throws')
    expect(mod.usageConsentGranted()).toBe(false)
  })

  it('fails closed when there is no storage at all', async () => {
    const mod = await loadConsent(undefined)
    expect(mod.usageConsentGranted()).toBe(false)
  })
})

describe('writing consent', () => {
  it('persists a grant under the versioned key and reads back ON', async () => {
    const store = fakeStore()
    const mod = await loadConsent(store)

    expect(mod.setUsageConsent(true)).toBe(true)
    expect(store.data[KEY]).toBe('granted')
    expect(mod.usageConsentGranted()).toBe(true)
    expect(mod.readUsageConsentFrom(store)).toBe(true)
  })

  it('removes the key on revoke rather than storing a "no"', async () => {
    const store = fakeStore({ [KEY]: 'granted' })
    const mod = await loadConsent(store)

    expect(mod.setUsageConsent(false)).toBe(false)
    expect(KEY in store.data).toBe(false)
    expect(mod.usageConsentGranted()).toBe(false)
  })

  it('treats any non-true argument as a revoke', async () => {
    const mod = await loadConsent(fakeStore({ [KEY]: 'granted' }))
    expect(mod.setUsageConsent('yes')).toBe(false)
    expect(mod.usageConsentGranted()).toBe(false)
  })

  it('still governs this tab when the write throws', async () => {
    // A revoke MUST take effect immediately even where it cannot be persisted:
    // the alternative is measuring someone who just said stop.
    const mod = await loadConsent(fakeStore({ [KEY]: 'granted' }, { throwOnWrite: true }))
    expect(mod.usageConsentGranted()).toBe(true)
    expect(mod.setUsageConsent(false)).toBe(false)
    expect(mod.usageConsentGranted()).toBe(false)
  })
})

describe('subscribers', () => {
  it('notifies on a real change and not on a no-op write', async () => {
    const mod = await loadConsent(fakeStore())
    const seen = []
    const off = mod.subscribeUsageConsent((v) => seen.push(v))

    mod.setUsageConsent(false)         // already off: no notification
    mod.setUsageConsent(true)
    mod.setUsageConsent(true)          // already on: no notification
    mod.setUsageConsent(false)
    off()
    mod.setUsageConsent(true)          // unsubscribed: not seen

    expect(seen).toEqual([true, false])
  })

  it('survives a listener that throws', async () => {
    const mod = await loadConsent(fakeStore())
    const seen = []
    restore.push(mod.subscribeUsageConsent(() => { throw new Error('bad listener') }))
    restore.push(mod.subscribeUsageConsent((v) => seen.push(v)))

    expect(() => mod.setUsageConsent(true)).not.toThrow()
    expect(seen).toEqual([true])
  })

  it('bounds the subscriber set instead of growing without limit', async () => {
    const mod = await loadConsent(fakeStore())
    const seen = []
    for (let i = 0; i < 40; i++) restore.push(mod.subscribeUsageConsent(() => seen.push(i)))
    mod.setUsageConsent(true)
    expect(seen.length).toBe(32)
  })

  it('picks up a revoke made in another tab', async () => {
    const store = fakeStore({ [KEY]: 'granted' })
    const mod = await loadConsent(store)
    const seen = []
    restore.push(mod.subscribeUsageConsent((v) => seen.push(v)))

    delete store.data[KEY]                       // the other tab's revoke
    expect(mod.refreshUsageConsent()).toBe(false)
    expect(mod.usageConsentGranted()).toBe(false)
    expect(seen).toEqual([false])
  })
})
