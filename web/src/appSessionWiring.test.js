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
import { describe, expect, it, vi } from 'vitest'
import { parse } from '@babel/parser'

import esbuild from 'esbuild'

const appSource = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8')
const tok = `Bearer ${'valid-token'}`
const landingCss = readFileSync(new URL('./site/landing.css', import.meta.url), 'utf8')
const stripped = esbuild.transformSync(appSource, { loader: 'jsx' }).code

// Select executable effects by their API call, as appSessionBootstrap does,
// so a matching comment or a call in the drawing effect cannot satisfy a pin.
const tree = parse(appSource, { sourceType: 'module', plugins: ['jsx'] })
function find(root, predicate) {
  const found = []
  function walk(node) {
    if (!node || typeof node.type !== 'string') return
    if (predicate(node)) found.push(node)
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) value.forEach(walk)
      else if (value?.type) walk(value)
    }
  }
  walk(root)
  return found
}
function effectCalling(name) {
  const effects = find(tree, node => node.type === 'CallExpression' && node.callee.name === 'useEffect'
    && find(node.arguments[0], child => child.type === 'CallExpression' && child.callee.name === name).length > 0)
  expect(effects, `unique ${name} effect`).toHaveLength(1)
  return effects[0]
}
const proof = effectCalling('getSessionIdentity')
const drawing = effectCalling('getSession')
const text = node => appSource.slice(node.start, node.end)

function proofHarness(mock = false, effect = proof) {
  let resolve, reject
  const pending = new Promise((yes, no) => { resolve = yes; reject = no })
  const scope = {}
  const snapshot = { tokenRevision: 0, recoveries: 0, reason: null, status: 'checking' }
  const sessionActions = {
    checking: vi.fn(), activate: vi.fn(() => { snapshot.status = 'active' }), requireAuth: vi.fn(),
  }
  const context = {
    mock, sessionProofScope: scope, sessionProofScopeRef: { current: scope },
    session: { ...snapshot, controller: { getSnapshot: () => snapshot } }, sessionActions,
    sessionAuthorization: tok, authHeaders: () => ({ Authorization: tok }),
    getSessionIdentity: vi.fn(() => pending), setTenant: vi.fn(), setTier: vi.fn(), setOrg: vi.fn(),
    setSignedIn: vi.fn(), isSignedIn: () => true, adoptOrgId: vi.fn(),
    setLoadErr: vi.fn(), humanizeError: String, is401: error => error?.status === 401,
    setIdentityFailure: vi.fn(),
    shouldAutoDemo: () => false, authConfigured: true, setMock: vi.fn(),
    useMemo: fn => fn(), REQUESTED_DRAWING_ID: 'legacy', DRAWING_SOURCE: 'legacy',
    requestedDrawingIdRef: { current: 'legacy' }, isScopeCurrent: () => true,
    hasDrawingSelection: () => true, isEditFixture: false, knownGuest: false,
    resetDrawing: vi.fn(), setDrawingLoad: vi.fn(), resetCatalogTransient: vi.fn(),
    clearToast: vi.fn(), setDrawer: vi.fn(), clearAgentSession: vi.fn(),
    mockVersions: { reset: vi.fn(), seedBase: vi.fn() }, seatIntake: vi.fn(),
    getSession: vi.fn(() => pending), getDrawingVersions: vi.fn(async () => ({ head: 1 })),
    classifyDrawingLoadFailure: () => 'transient',
  }
  const shared = find(tree, node => node.type === 'VariableDeclarator' && node.id.name === 'sessionProof')[0]
  context.sessionProof = Function(...Object.keys(context), `return (${text(shared.init)})`)(...Object.values(context))
  const run = Function(...Object.keys(context), `return (${text(effect.arguments[0])})`)(...Object.values(context))
  const cleanup = run()
  const settle = async (value, refused = false) => {
    if (refused) reject(value)
    else resolve(value)
    await Promise.resolve()
    await Promise.resolve()
  }
  return { context, snapshot, cleanup, settle }
}

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

  it.each([['/api/session/identity', proof], ['/api/session', drawing]])('reports a refusal from %s without claiming the token is bad', async (path, effect) => {
    // createSessionController "WHO MAY DELETE THE TOKEN": only api.js's own 401
    // verdict and an explicit sign-out may wipe leaf.jwt. A `tokenInvalidated:
    // true` here re-opens the 2026-08-08 hole where the surface destroyed a
    // token the transport had deliberately kept.
    const h = proofHarness(false, effect)
    await h.settle({ status: 401 }, true)
    if (effect === proof) expect(h.context.getSessionIdentity).toHaveBeenCalledExactlyOnceWith(false)
    else expect(h.context.getSession).toHaveBeenCalledExactlyOnceWith(false, 'legacy')
    expect(h.context.sessionActions.requireAuth).toHaveBeenCalledExactlyOnceWith(path)
    expect(h.context.sessionActions.activate).not.toHaveBeenCalled()
    expect(stripped).not.toMatch(/requireAuth\([^)]*tokenInvalidated/)
    h.cleanup()
  })

  it('forwards only the RISING edge of the jobs auth signal', () => {
    // useJobController publishes `false` on every successful jobs read. Feeding
    // that back in was the console's last-writer-wins hazard across three
    // independent observers of one boolean.
    expect(stripped).toMatch(
      /onAuthRequired:\s*\(required\)\s*=>\s*\{\s*if \(required\) sessionActions\.requireAuth\("jobs"\)/,
    )
  })

  it('re-runs the identity proof on the controller bounded recovery', () => {
    // Recovery must invalidate both proof effects and their shared render fence.
    expect(proof.arguments[1].type).toBe('ArrayExpression')
    expect(proof.arguments[1].elements.map(text)).toContain('session.recoveries')
    expect(drawing.arguments[1].elements.map(text)).toContain('session.recoveries')
    const scopes = find(tree, node => node.type === 'VariableDeclarator' && node.id.name === 'sessionProofScope')
    expect(scopes).toHaveLength(1)
    expect(scopes[0].init.arguments[1].elements.map(text)).toContain('session.recoveries')
  })

  it.each([['identity', proof], ['legacy', drawing]])('activates on a live %s proof and never touches the machine in mock', async (_, effect) => {
    const identity = { tenant: 'tenant-a', tier: 'hosted_pro', org: 'org-a' }
    const live = proofHarness(false, effect)
    expect(live.context.sessionActions.checking).toHaveBeenCalledTimes(effect === proof ? 1 : 0)
    expect(live.context.sessionActions.activate).not.toHaveBeenCalled()
    await live.settle(identity)
    expect(live.context.sessionActions.activate).toHaveBeenCalledExactlyOnceWith(identity)
    live.cleanup()

    const stale = proofHarness(false, effect)
    stale.cleanup()
    await stale.settle(identity)
    expect(stale.context.sessionActions.activate).not.toHaveBeenCalled()

    const demo = proofHarness(true, effect)
    expect(demo.context.getSessionIdentity).not.toHaveBeenCalled()
    await demo.settle(identity)
    for (const action of Object.values(demo.context.sessionActions)) expect(action).not.toHaveBeenCalled()
    demo.cleanup()
  })

  it.each([['identity', proof], ['legacy', drawing]])('fences %s success and refusal across token revisions, recoveries, sign-out and scope changes', async (_, effect) => {
    for (const fence of ['tokenRevision', 'recoveries', 'signout', 'scope']) {
      for (const refused of [false, true]) {
        const h = proofHarness(false, effect)
        if (fence === 'scope') h.context.sessionProofScopeRef.current = {}
        else if (fence === 'signout') h.snapshot.reason = 'signed_out'
        else h.snapshot[fence] += 1
        await h.settle(refused ? { status: 401 } : { tenant: 'stale' }, refused)
        expect(h.context.sessionActions.activate).not.toHaveBeenCalled()
        expect(h.context.sessionActions.requireAuth).not.toHaveBeenCalled()
        expect(h.context.setLoadErr).not.toHaveBeenCalledWith(expect.any(String))
        h.cleanup()
      }
    }
  })

  it.each([404, 'network', 'non-json', 'timeout'])('offers recovery for identity failure %s without rejecting the token', async status => {
    const h = proofHarness()
    await h.settle({ status }, true)
    expect(h.context.setLoadErr).not.toHaveBeenCalled()
    expect(h.context.setIdentityFailure).toHaveBeenLastCalledWith({
      scope: h.context.sessionProofScope, message: String({ status }),
    })
    expect(h.context.sessionActions.requireAuth).not.toHaveBeenCalled()
    expect(h.context.sessionActions.activate).not.toHaveBeenCalled()
    h.cleanup()
  })

  it('proves identity independently of a secondary drawing versions request', () => {
    expect(proof).not.toBe(drawing)
    expect(find(proof, node => node.type === 'CallExpression'
      && ['getSession', 'getDrawingVersions'].includes(node.callee.name))).toHaveLength(0)
    for (const effect of [proof, drawing]) {
      expect(find(effect, node => node.type === 'CallExpression'
        && node.callee.object?.name === 'sessionProof' && node.callee.property.name === 'activate')).toHaveLength(1)
    }
    expect(find(drawing, node => node.type === 'CallExpression'
      && node.callee.name === 'getDrawingVersions')).toHaveLength(1)
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

  it.each(['/api/session/identity', '/api/session'])('fails when the %s surface claims deletion authority over the token', path => {
    const mutated = appSource.replace(
      `sessionActions.requireAuth('${path}')`,
      `sessionActions.requireAuth('${path}', { tokenInvalidated: true })`,
    )
    expect(mutated).not.toEqual(appSource)
    const mutatedStripped = esbuild.transformSync(mutated, { loader: 'jsx' }).code
    expect(mutatedStripped).toMatch(/requireAuth\([^)]*tokenInvalidated/)
  })
})
