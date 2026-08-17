// The other half of the 2026-08-17 post-callback token race: WHERE the callback
// lands. auth.js sends `redirect_uri: window.location.origin` and the deployed
// acceptance collector asserts that exactly
// (deployed_auth0_spa_origin_acceptance.mjs: `redirect_uri === TARGET_ORIGIN`),
// so Auth0 returns to `/` -- never to `/try`. The old gate armed only for `/try`
// and every other path booted the console immediately, which is what put the
// API burst in front of the token write.

import { describe, expect, it } from 'vitest'

import { bootWantsApp, shouldDeferForAuthCallback } from './authBoot.js'

const CALLBACK = '?code=abc&state=xyz'

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
    expect(bootWantsApp('?demo=1', '/try')).toBe(false)
    expect(bootWantsApp('?ops=1', '/try')).toBe(false)
    expect(bootWantsApp(CALLBACK, '/try')).toBe(false)
  })

  it('falls through to path routing on a malformed search', () => {
    expect(bootWantsApp('%', '/')).toBe(false)
    expect(bootWantsApp('', '/')).toBe(false)
  })
})
