// @vitest-environment node
//
// Structural pins for the console's session-controller adoption (W2b).
//
// NODE ENVIRONMENT, deliberately: esbuild refuses to load under jsdom
// ("new TextEncoder().encode('') instanceof Uint8Array is incorrectly false"),
// because jsdom's TextEncoder produces its own realm's Uint8Array. This file
// touches no DOM, so it runs in node and the rest of the suite keeps jsdom.
//
// WHY A SEPARATE FILE, AND WHY VITEST. App.jsx cannot be mounted in a unit
// runner (three.js, a dozen controllers, a live transport), so its contracts
// are pinned through the esbuild comment-stripping lens app-wiring.test.mjs
// established. That file, however, is `.mjs`: vitest's include is
// `src/**/*.test.{js,jsx}` and no npm script or gate suite runs it, so pins
// added there gate nothing. These pins live in a `.test.js` so `web-vitest`
// — the repo's always-on web suite — actually runs them.
//
// Each pin below names a contract whose loss is SILENT at build time and shows
// up only as a signed-out-looking surface over a working session, or the
// reverse: the exact defect class W2b exists to retire.

import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import esbuild from 'esbuild'

const appSource = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8')
const landingCss = readFileSync(new URL('./site/landing.css', import.meta.url), 'utf8')
const stripped = esbuild.transformSync(appSource, { loader: 'jsx' }).code

describe('App.jsx session controller adoption', () => {
  it('mounts exactly one session controller', () => {
    // SiteRoot renders the console (scene 'app') and StageScene -> ToolCast
    // (scenes site|tool) in mutually exclusive arms of one ternary, so the
    // console's own mount is the only instance on an /app page load. A second
    // one here would be a second latch over one transport.
    const mounts = stripped.match(/useSessionController\(/g) || []
    expect(mounts).toHaveLength(1)
  })

  it('retired the hand-rolled authRequired latch entirely', () => {
    expect(stripped).not.toMatch(/setAuthRequired/)
    expect(stripped).toMatch(/consoleAuthRequired\(session\.status\)/)
  })

  it('derives signedOut through the named truth table with the short circuit intact', () => {
    // `isSignedIn` is passed as the FUNCTION, not its result: passing
    // `isSignedIn()` would read localStorage on every healthy render.
    expect(stripped).toMatch(
      /consoleSignedOut\(\{\s*mock,\s*authRequired,\s*authConfigured,\s*isSignedIn\s*\}\)/,
    )
  })

  it('reports a session refusal without claiming the token is bad', () => {
    // createSessionController "WHO MAY DELETE THE TOKEN": only api.js's own 401
    // verdict and an explicit sign-out may wipe leaf.jwt. A `tokenInvalidated:
    // true` here re-opens the 2026-08-08 hole where the surface destroyed a
    // token the transport had deliberately kept.
    expect(stripped).toMatch(/requireAuth\("\/api\/session"\)/)
    expect(stripped).not.toMatch(/requireAuth\([^)]*tokenInvalidated/)
  })

  it('forwards only the RISING edge of the jobs auth signal', () => {
    // useJobController publishes `false` on every successful jobs read. Feeding
    // that back in was the console's last-writer-wins hazard across three
    // independent observers of one boolean.
    expect(stripped).toMatch(
      /onAuthRequired:\s*\(required\)\s*=>\s*\{\s*if \(required\) sessionActions\.requireAuth\("jobs"\)/,
    )
  })

  it('re-runs the session load on the controller bounded recovery', () => {
    // Without `session.recoveries` in the dep list nothing re-runs getSession
    // when a token lands after the 401 burst, and the recovery is inert.
    expect(stripped).toMatch(/sessionActions,\s*session\.recoveries\s*\]/)
  })

  it('activates only on a live 200 and never touches the machine in mock', () => {
    expect(stripped).toMatch(/if \(!mock\) sessionActions\.checking\(\)/)
    expect(stripped).toMatch(
      /if \(!mock\) sessionActions\.activate\(\{ tenant: t, tier: ti, org: o \}\)/,
    )
  })

  it('publishes the session 200 before a secondary versions request can refuse auth', () => {
    const activation = stripped.indexOf('sessionActions.activate({ tenant: t, tier: ti, org: o })')
    const versionsRead = stripped.indexOf('await getDrawingVersions(false, REQUESTED_DRAWING_ID)')
    expect(activation).toBeGreaterThan(-1)
    expect(versionsRead).toBeGreaterThan(activation)
  })

  it('keeps the auto-demo escape hatch on the same 401 branch', () => {
    // A VITE_MOCK=0 build with Auth0 unconfigured must still land zero-click on
    // the demo. Same call, same four inputs, same branch as before W2b.
    expect(stripped).toMatch(
      /shouldAutoDemo\(\{\s*authRequired:\s*true,\s*authConfigured,\s*mock,\s*signedIn:\s*isSignedIn\(\)\s*\}\)\s*\)?\s*setMock\(true\)/,
    )
  })

  it('leaves the Auth0 return leg untouched', () => {
    // ACCEPTANCE route matrix, the sacred row: the code exchange stores
    // leaf.jwt and reloads, and NOTHING may reorder it into the session
    // controller or make it await controller state.
    expect(stripped).toMatch(
      /handleRedirectCallback\(\)\.then\(\(stored\) => \{\s*if \(stored\) window\.location\.reload\(\);?\s*\}\)/,
    )
    expect(stripped).not.toMatch(/handleRedirectCallback\(\)[\s\S]{0,120}sessionActions/)
  })

  it('ends a session through the controller so sign-out is not read as an expiry', () => {
    expect(stripped).toMatch(/label:\s*"Sign out",\s*onClick:\s*sessionActions\.signOut/)
  })

  // 2026-09-02 reconciliation (row B11): the deployed /app header had no
  // reachable sign-out control, only the drawer action pinned above, behind
  // Details. This pin is on the RAW (un-transformed) source: esbuild's JSX
  // loader compiles the literal <button> below into a createElement call, so
  // the comment-stripped `stripped` lens the other pins use cannot see it.
  it('renders a persistent header sign-out control, gated on isSignedIn(), through the same controller path', () => {
    expect(appSource).toMatch(
      /<AccountSignOut signedIn=\{isSignedIn\(\)\} onSignOut=\{sessionActions\.signOut\} \/>/,
    )
  })

  it('keeps the sign-out text above AA contrast on the dark studio header', () => {
    expect(landingCss).toMatch(
      /\.studio-shell header\.top \.tc-account-signout \{\s*color: #f0997b; background: rgba\(240, 153, 123, \.12\);/,
    )

    const foreground = [240, 153, 123]
    const background = foreground.map((channel, index) => (
      channel * 0.12 + [10, 10, 10][index] * 0.88
    ))
    const luminance = (rgb) => rgb
      .map((channel) => channel / 255)
      .map((channel) => (channel <= 0.04045
        ? channel / 12.92
        : ((channel + 0.055) / 1.055) ** 2.4))
      .reduce((sum, channel, index) => sum + channel * [0.2126, 0.7152, 0.0722][index], 0)
    const lighter = luminance(foreground)
    const darker = luminance(background)
    expect((lighter + 0.05) / (darker + 0.05)).toBeGreaterThanOrEqual(4.5)
  })

  // Falsification: the pins above must FAIL on the shapes they forbid, not
  // merely pass on the shape that ships.
  it('fails when the jobs falling edge is wired back in', () => {
    const mutated = appSource.replace(
      'onAuthRequired: (required) => { if (required) sessionActions.requireAuth(\'jobs\') },',
      'onAuthRequired: (required) => sessionActions.requireAuth(\'jobs\'),',
    )
    expect(mutated).not.toEqual(appSource)
    const mutatedStripped = esbuild.transformSync(mutated, { loader: 'jsx' }).code
    expect(mutatedStripped).not.toMatch(
      /onAuthRequired:\s*\(required\)\s*=>\s*\{\s*if \(required\) sessionActions\.requireAuth\("jobs"\)/,
    )
  })

  it('fails when the surface claims deletion authority over the token', () => {
    const mutated = appSource.replace(
      "sessionActions.requireAuth('/api/session')",
      "sessionActions.requireAuth('/api/session', { tokenInvalidated: true })",
    )
    expect(mutated).not.toEqual(appSource)
    const mutatedStripped = esbuild.transformSync(mutated, { loader: 'jsx' }).code
    expect(mutatedStripped).toMatch(/requireAuth\([^)]*tokenInvalidated/)
  })
})
