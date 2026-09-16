// The other half of the 2026-08-17 post-callback token race: WHERE the callback
// lands. auth.js sends `redirect_uri: window.location.origin` and the deployed
// acceptance collector asserts that exactly
// (deployed_auth0_spa_origin_acceptance.mjs: `redirect_uri === TARGET_ORIGIN`),
// so Auth0 returns to `/` -- never to `/try`. The old gate armed only for `/try`
// and every other path booted the console immediately, which is what put the
// API burst in front of the token write.

import { afterEach, describe, expect, it, vi } from 'vitest'

import { bootWantsApp, shouldDeferForAuthCallback } from './authBoot.js'

const CALLBACK = '?code=abc&state=xyz'

// The one-shell rail is read ONCE at module evaluation (runtimeFlags.js), so
// each rail state needs a fresh module graph: mock the flag module the way
// authBoot.js reads it, then import a new authBoot instance behind it.
async function bootWantsAppWithRail(enabled) {
  vi.resetModules()
  vi.doMock('../lib/runtimeFlags.js', () => ({
    ONE_SHELL_ENABLED: enabled,
    readOneShellEnabled: () => enabled,
  }))
  const mod = await import('./authBoot.js')
  return mod.bootWantsApp
}

describe('auth callback deferral', () => {
  it('defers on the origin landing the SPA actually redirects to', () => {
    expect(shouldDeferForAuthCallback(CALLBACK)).toBe(true)
  })

  it('defers on every path, not just /try', () => {
    // The regression: the console-booting paths are exactly the ones the old
    // /try-only gate left ungated, so those must defer too.
    for (const path of ['/', '/try', '/app', '/sheets']) {
      expect({ path, deferred: shouldDeferForAuthCallback(CALLBACK) })
        .toEqual({ path, deferred: true })
    }
    expect(bootWantsApp(CALLBACK, '/')).toBe(true)
  })

  it('defers on a failed callback so the error branch is not raced either', () => {
    expect(shouldDeferForAuthCallback('?error=access_denied&state=xyz')).toBe(true)
  })

  it('does not defer an ordinary load', () => {
    for (const search of ['', '?demo=1', '?ops=1', '?drawing=cat-panels', '?state=xyz', '?code=abc']) {
      expect(shouldDeferForAuthCallback(search)).toBe(false)
    }
  })
})

describe('console boot back-compat', () => {
  it('keeps every pre-existing deep link booting the console', () => {
    expect(bootWantsApp('?fixture=cat', '/')).toBe(true)
    expect(bootWantsApp('?dev=1', '/')).toBe(true)
    expect(bootWantsApp('?drawing=cat-panels', '/try')).toBe(true)
    expect(bootWantsApp('?demo=1', '/')).toBe(true)
    expect(bootWantsApp('?ops=1', '/')).toBe(true)
    expect(bootWantsApp(CALLBACK, '/')).toBe(true)
  })

  it('keeps /try on the stage for the surface-scoped params', () => {
    // No runtime flags in this environment, so the one-shell rail reads OFF.
    expect(bootWantsApp('?demo=1', '/try')).toBe(false)
    expect(bootWantsApp('?ops=1', '/try')).toBe(false)
    expect(bootWantsApp(CALLBACK, '/try')).toBe(false)
  })

  it('falls through to path routing on a malformed search', () => {
    expect(bootWantsApp('%', '/')).toBe(false)
    expect(bootWantsApp('', '/')).toBe(false)
  })
})

describe('one-shell demo entry', () => {
  afterEach(() => {
    vi.doUnmock('../lib/runtimeFlags.js')
    vi.resetModules()
  })

  it('boots the forwarded /try?demo=1 into the cockpit demo with the rail on', async () => {
    const wantsApp = await bootWantsAppWithRail(true)
    expect(wantsApp('?demo=1', '/try')).toBe(true)
    // The landing's own Try button target, unchanged by the rail.
    expect(wantsApp('?demo=1', '/app')).toBe(true)
  })

  it('keeps /try?demo=1 on the stage with the rail off', async () => {
    const wantsApp = await bootWantsAppWithRail(false)
    expect(wantsApp('?demo=1', '/try')).toBe(false)
    expect(wantsApp('?demo=1', '/app')).toBe(true)
  })

  it('only the literal demo=1 on /try counts, whatever the rail says', async () => {
    for (const enabled of [true, false]) {
      const wantsApp = await bootWantsAppWithRail(enabled)
      expect({ enabled, bare: wantsApp('', '/try') }).toEqual({ enabled, bare: false })
      expect({ enabled, off: wantsApp('?demo=0', '/try') }).toEqual({ enabled, off: false })
      expect({ enabled, tour: wantsApp('?demo=tour', '/try') }).toEqual({ enabled, tour: false })
      expect({ enabled, ops: wantsApp('?ops=1', '/try') }).toEqual({ enabled, ops: false })
      expect({ enabled, callback: wantsApp(CALLBACK, '/try') }).toEqual({ enabled, callback: false })
    }
  })

  it('never throws on a malformed search with the rail on', async () => {
    const wantsApp = await bootWantsAppWithRail(true)
    expect(wantsApp('%', '/try')).toBe(false)
    expect(wantsApp('%', '/')).toBe(false)
  })
})
