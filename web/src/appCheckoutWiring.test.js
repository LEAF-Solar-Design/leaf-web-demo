// @vitest-environment node
//
// Structural pins for the console's checkout-controller adoption (W2c).
//
// NODE ENVIRONMENT, deliberately: esbuild refuses to load under jsdom
// ("new TextEncoder().encode('') instanceof Uint8Array is incorrectly false"),
// because jsdom's TextEncoder produces its own realm's Uint8Array. This file
// touches no DOM, so it runs in node and the rest of the suite keeps jsdom.
//
// WHY A SEPARATE FILE, AND WHY VITEST. App.jsx cannot be mounted in a unit
// runner (three.js, a dozen controllers, a live transport), so its contracts
// are pinned through the same esbuild lens appSessionWiring.test.js uses for
// W2b. `.test.js`, so `web-vitest` — the repo's always-on web suite — runs it.
//
// The single-writer lock guards EVERY drawing write, so each pin below names a
// contract whose loss is silent at build time and shows up only as a write
// that should have been suppressed, or a Release offered over a lease this
// runtime cannot prove.
//
// The BEHAVIOUR of the lock lives in the controller's own suites
// (controllers/checkout/checkoutScope.test.jsx and
// scripts/check_checkout_identity.mjs, which drives the real store). These
// pins only assert that the console routes through that code instead of
// growing a second copy of it — which is the exact defect class W2c retires.

import { readFileSync } from 'node:fs'
import { describe, expect, it, vi } from 'vitest'
import { parse } from '@babel/parser'
import { checkoutScopeDrawingId } from './controllers/checkout/createCheckoutController.js'
import { sameSolarDrawingBinding } from './solar/solarDrawingBinding.js'
import { createSolarProjectClient } from './solar/solarProjectClient.js'

import esbuild from 'esbuild'

// Normalized to LF: the index stores LF but a Windows working tree checks out
// CRLF, and a literal \n replacement against raw CRLF source silently no-ops
// (kimi-critic review of PR #885, observation 7).
const appSource = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8').replace(/\r\n/g, '\n')

// esbuild's transform drops the ~140-line commented-out legacy block (which
// still mentions `loadCheckout`), but keeps line comments in some positions —
// and this file's comments deliberately NAME the retired identifiers. So the
// lens strips whole-line `//` too, leaving executable code only.
const strip = (source) => esbuild.transformSync(source, { loader: 'jsx' }).code
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/^[ \t]*\/\/.*$/gm, '')

const stripped = strip(appSource)

const appTree = parse(appSource, { sourceType: 'module', plugins: ['jsx'] })
function appNodes(predicate) {
  const found = []
  const walk = (node) => {
    if (!node || typeof node.type !== 'string') return
    if (predicate(node)) found.push(node)
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) value.forEach(walk)
      else if (value && typeof value.type === 'string') walk(value)
    }
  }
  walk(appTree)
  return found
}
const appText = (node) => appSource.slice(node.start, node.end)
function loginCallback() {
  const declarations = appNodes((node) => node.type === 'VariableDeclarator' && node.id.name === 'onLogin')
  expect(declarations).toHaveLength(1)
  expect(declarations[0].init.callee.name).toBe('useCallback')
  return declarations[0].init.arguments[0]
}

// The hand-rolled block, identifier by identifier. Each one was a piece of
// state or protocol the controller owns now; a survivor is a second answer.
const RETIRED = [
  'setCheckout',
  'setCheckoutUnknown',
  'setCheckoutReadFailed',
  'setCheckoutBusy',
  'capabilityRef',
  'reloadHandoffRef',
  'reloadAuthorityRef',
  'holderClaimRef',
  'bumpCheckoutAuthority',
  'checkoutSeqRef',
  'loadCheckout',
  'onTakeCheckout',
  'onReleaseCheckout',
  'lockState',
  'claimHolderId',
  'bootstrapCheckoutReloadHandoff',
  'holdCheckoutReloadAuthority',
  'stageCheckoutReloadHandoff',
  'remintSessionHolderId',
]

describe('App.jsx checkout controller adoption', () => {
  it('mounts exactly one checkout controller', () => {
    // SiteRoot renders the console (scene 'app') and StageScene -> ToolCast
    // (scenes site|tool) in mutually exclusive arms of one ternary, so the
    // console's own mount is the only instance on an /app page load. A second
    // one here would be a second capability, a second holder id and a second
    // origin-wide authority over ONE server-side lock.
    const mounts = stripped.match(/useCheckoutController\(/g) || []
    expect(mounts).toHaveLength(1)
  })

  it('retired the hand-rolled single-writer block entirely', () => {
    const survivors = RETIRED.filter((name) => stripped.includes(name))
    expect(survivors).toEqual([])
  })

  it('scopes the checkout on the drawing identity, not on the version chain alone', () => {
    // ACCEPTANCE scope-reset contract, binding. `drawingState` does NOT reset
    // on a tenant switch; the identity does. Reading the chain alone kept
    // addressing the previous tenant's drawing with its bearer capability.
    expect(stripped).toMatch(
      new RegExp('drawingId: knownGuest \\? null : checkoutScopeDrawingId\\(\\{\\s*identityDrawingId: projectMode \\? null : REQUESTED_DRAWING_ID,\\s*drawingState,\\s*requestedDrawingId: REQUESTED_DRAWING_ID\\s*\\}\\)'),
    )
  })

  it('hands the controller the boot inputs the deferral and the handoff need', () => {
    // `bootDrawingId` is what lets the reload handoff be bootstrapped in the
    // render phase, which is what decides the claim deferral before the first
    // effect runs — the ordering the retired block got from its own
    // render-phase bootstrap. `onHolderRemint` is how a refused redemption
    // reaches this shell's holder state.
    expect(stripped).toMatch(/bootDrawingId: projectMode \? null : REQUESTED_DRAWING_ID/)
    expect(stripped).toMatch(/deferForAuthCallback: isAuthRedirectCallback\(\)/)
    expect(stripped).toMatch(/onHolderRemint: setOwnHolder/)
  })

  it('composes writeLocked from the controller plus the unreadable-head lock', () => {
    // The console's write gate, unchanged in shape: the controller's decision
    // (which already folds in `unknown` and the unproven-own-lock correction)
    // OR an unreadable committed head. `previewing` stays a SEPARATE gate on
    // this surface, exactly as before.
    expect(stripped).toMatch(/const writeLocked = lock\.writeLocked \|\| drawingMutationsBlocked/)
    expect(stripped).toMatch(/const otherHeldCheckout = lock\.lockedByOther/)
    expect(stripped).toMatch(/const heldByUs = lock\.heldByUs/)
  })

  it('keeps ?demo=locked a DERIVED-state override that fabricates no answer', () => {
    // The DEV-only hook injects another session's holder. It must not fabricate
    // an ANSWER: the real `unknown` is passed straight through, so a read still
    // in flight or failed keeps suppressing writes. Passing `false` here would
    // silently enable writes during the first read on any ?demo=locked load.
    expect(stripped).toMatch(
      /const lock = demoLocked \? deriveCheckout\([\s\S]{0,400}?checkout\.unknown,\s*mock,\s*!!checkout\.actions\.getCapability\(\)\s*\) : checkout/,
    )
  })

  it('reads the bearer capability from the controller at every write call site', () => {
    // Never rendered state, never a local ref: one owner, read on demand.
    expect(stripped).toMatch(/checkoutCapability: checkout\.actions\.getCapability\(\) \|\| void 0/)
    expect(stripped).toMatch(/capability: checkout\.actions\.getCapability\(\)/)
    expect(stripped).toMatch(/const held = checkout\.actions\.getCapability\(\)/)
    expect(stripped).toMatch(/checkoutCapabilityRef\.current = checkout\.actions\.getCapability/)
  })

  it('ends the lease through the controller before leaving for Auth0', () => {
    // Byte-identical to /try's signInWithCheckoutRelease. The controller's
    // release re-reads /versions, so a read that 401s on the way to sign-in
    // lands unknown + readFailed — fail closed — instead of leaving a stale
    // "You hold the edit lock" behind a dead capability.
    // ONE sign-in path: the project branch ends at its last currency check and the
    // standalone path releases in the else, so the single login follows both.
    const guarded = loginCallback().body.body[0]
    expect(guarded.type).toBe('TryStatement')
    expect(appText(guarded.handler)).toContain('showToast')
    const statements = guarded.block.body
    expect(statements).toHaveLength(2)
    expect(statements[0].type).toBe('IfStatement')
    expect(appText(statements[0].test)).toBe('projectModeRef.current')
    expect(appText(statements[0].consequent.body.at(-1))).toBe("if (!stillCurrent()) throw new Error('Sign-in scope changed')")
    expect(appText(statements[0].consequent)).not.toContain('login(')
    const standalone = statements[0].alternate
    expect(appText(standalone.test)).toBe('checkout.actions.getCapability()')
    expect(standalone.consequent.body).toHaveLength(1)
    const release = standalone.consequent.body[0]
    expect(release.type).toBe('TryStatement')
    expect(release.block.body).toHaveLength(1)
    expect(appText(release.block.body[0])).toBe('await checkout.actions.release()')
    expect(appText(release.handler)).toContain('showToast')
    expect(appText(statements[1])).toBe('await login()')
  })

  it.each([false, true])('continues sign-in once with feedback when checkout release rejects (projectMode=%s)', async (projectMode) => {
    const binding = { kind: 'project',
      organization_id: '11111111-1111-1111-1111-111111111111',
      project_id: '22222222-2222-2222-2222-222222222222',
      drawing_id: '33333333-3333-3333-3333-333333333333',
      input_version_id: '44444444-4444-4444-4444-444444444444' }
    const snapshot = { binding, authorityKey: 'authority' }
    const release = vi.fn().mockRejectedValue(new Error('Release failed'))
    const client = { getSnapshot: () => snapshot, releaseCheckout: release }
    const controller = { getSnapshot: () => ({ generation: 7 }) }
    const projectSolarLoginRef = { current: { generation: 7, controller, projectSolar: {
      ready: true, projectClient: client, checkout: { canRelease: true },
      scope: { binding, authorityKey: 'authority' },
    } } }
    const checkout = { actions: { getCapability: vi.fn(() => 'capability'), release } }
    const login = vi.fn().mockResolvedValue(undefined)
    const showToast = vi.fn()
    const onLogin = Function('projectModeRef', 'projectSolarLoginRef', 'checkout', 'login',
      'sameSolarDrawingBinding', 'showToast', 'humanizeError', `return (${appText(loginCallback())})`)(
      { current: projectMode }, projectSolarLoginRef, checkout, login, sameSolarDrawingBinding, showToast, String)

    await onLogin()

    expect(release).toHaveBeenCalledExactlyOnceWith()
    expect(showToast).toHaveBeenCalledExactlyOnceWith({
      text: "Sign-in couldn't start. Check your connection and try again.",
    })
    expect(login).toHaveBeenCalledExactlyOnceWith()
  })

  it.each([false, true])('R13F-07 project mode deactivates both legacy checkout inputs and preserves standalone inputs (knownGuest=%s)', (knownGuest) => {
    const calls = appNodes((node) => node.type === 'CallExpression' && node.callee.name === 'useCheckoutController')
    expect(calls).toHaveLength(1)
    expect(calls[0].arguments).toHaveLength(1)
    expect(calls[0].arguments[0].type).toBe('ObjectExpression')
    const options = Function('projectMode', 'REQUESTED_DRAWING_ID', 'drawingState', 'mock',
      'ownHolder', 'isAuthRedirectCallback', 'setOwnHolder', 'checkoutScopeDrawingId', 'knownGuest',
      `return (${appText(calls[0].arguments[0])})`)
    const holder = { sentinel: 'holder' }
    const remint = vi.fn()
    for (const projectMode of [true, false]) {
      for (const identity of ['boot-drawing', null]) {
        for (const drawingState of [{ drawing_id: 'chain-drawing', version: 7 }, null]) {
          for (const mock of [true, false]) {
            for (const deferred of [true, false]) {
              const authCallback = vi.fn(() => deferred)
              const actual = options(projectMode, identity, drawingState, mock, holder,
                authCallback, remint, checkoutScopeDrawingId, knownGuest)
              expect(actual).toEqual({ mock,
                drawingId: knownGuest || projectMode ? null : checkoutScopeDrawingId({
                  identityDrawingId: identity, drawingState, requestedDrawingId: identity }),
                holder, bootDrawingId: projectMode ? null : identity,
                deferForAuthCallback: deferred, onHolderRemint: remint })
              expect(actual.holder).toBe(holder)
              expect(actual.onHolderRemint).toBe(remint)
              expect(authCallback).toHaveBeenCalledExactlyOnceWith()
            }
          }
        }
      }
    }
    expect(remint).not.toHaveBeenCalled()
  })

  it('R13F-09 project login releases through its captured client and refuses changed scope or session', async () => {
    const projectModeRef = { current: true }
    const projectSolarLoginRef = { current: null }
    const sequence = []
    const legacyRelease = vi.fn(async () => { sequence.push('legacy release') })
    const capability = vi.fn(() => 'legacy capability')
    const checkout = { actions: { getCapability: capability, release: legacyRelease } }
    const login = vi.fn(async () => { sequence.push('login') })
    const onLogin = Function('projectModeRef', 'projectSolarLoginRef', 'checkout', 'login',
      'sameSolarDrawingBinding', 'showToast', 'humanizeError', `return (${appText(loginCallback())})`)(
      projectModeRef, projectSolarLoginRef, checkout, login, sameSolarDrawingBinding, vi.fn(), String)
    const binding = { kind: 'project',
      organization_id: '11111111-1111-1111-1111-111111111111',
      project_id: '22222222-2222-2222-2222-222222222222',
      drawing_id: '33333333-3333-3333-3333-333333333333',
      input_version_id: '44444444-4444-4444-4444-444444444444' }
    const changedBinding = { ...binding, input_version_id: '55555555-5555-5555-5555-555555555555' }
    function fixture({ ready = true, canRelease = true } = {}) {
      let settle
      const release = new Promise((resolve) => { settle = resolve })
      const snapshot = { binding: { ...binding }, authorityKey: 'authority', leaseRevision: 1 }
      const live = { generation: 7 }
      const controller = { getSnapshot: () => live }
      const client = { getSnapshot: () => snapshot,
        releaseCheckout: vi.fn(() => { sequence.push('project release'); return release }) }
      const owner = { ready, projectClient: ready ? client : null,
        scope: { binding: ready ? { ...binding } : null, authorityKey: ready ? 'authority' : null },
        checkout: { canRelease } }
      const captured = { projectSolar: owner, generation: 7, controller }
      // Each render installs a fresh current-value record; the callback above
      // is retained across all fixtures and must consult these refs at invocation.
      projectModeRef.current = true
      projectSolarLoginRef.current = captured
      sequence.length = 0
      login.mockClear()
      return { client, owner, captured, snapshot, live, settle,
        replaceOwner(patch) {
          projectSolarLoginRef.current = { ...captured, projectSolar: { ...owner, ...patch } }
        } }
    }
    const unchanged = fixture()
    const leaving = onLogin()
    expect(unchanged.client.releaseCheckout).toHaveBeenCalledExactlyOnceWith()
    expect(sequence).toEqual(['project release'])
    expect(login).not.toHaveBeenCalled()
    unchanged.snapshot.leaseRevision += 1
    unchanged.replaceOwner({ scope: { ...unchanged.owner.scope, binding: { ...binding } } })
    unchanged.settle()
    await leaving
    expect(sequence).toEqual(['project release', 'login'])
    expect(login).toHaveBeenCalledExactlyOnceWith()

    const drifts = [
      ['mode', () => { projectModeRef.current = false }],
      ['binding', (run) => run.replaceOwner({ scope: { ...run.owner.scope, binding: changedBinding } })],
      ['returned binding', (run) => {
        // A, B, A: the client re-binds and publishes a new object equal to the first.
        run.snapshot.binding = { ...binding }
        run.replaceOwner({ scope: { ...run.owner.scope, binding: { ...binding } } })
      }],
      ['authority', (run) => run.replaceOwner({ scope: { ...run.owner.scope, authorityKey: 'other authority' } })],
      ['client', (run) => run.replaceOwner({ projectClient: { ...run.client } })],
      ['readiness', (run) => run.replaceOwner({ ready: false })],
      ['controller', (run) => { projectSolarLoginRef.current = { ...run.captured, controller: { getSnapshot: () => run.live } } }],
      ['rendered generation', (run) => { projectSolarLoginRef.current = { ...run.captured, generation: 8 } }],
      ['live generation', (run) => { run.live.generation = 8 }],
      ['snapshot binding', (run) => { run.snapshot.binding = changedBinding }],
      ['snapshot authority', (run) => { run.snapshot.authorityKey = 'other authority' }],
    ]
    for (const [name, drift] of drifts) {
      const run = fixture()
      const pending = onLogin()
      expect(run.client.releaseCheckout, name).toHaveBeenCalledExactlyOnceWith()
      drift(run)
      run.settle()
      await pending
      expect(login, name).not.toHaveBeenCalled()
      expect(sequence, name).toEqual(['project release'])
    }
    // Already invalid at invocation: neither release nor login is permitted.
    for (const invalidate of [
      (run) => { run.live.generation = 8 },
      (run) => { run.snapshot.binding = changedBinding },
      (run) => { run.snapshot.authorityKey = 'other authority' },
    ]) {
      const run = fixture()
      invalidate(run)
      await onLogin()
      expect(run.client.releaseCheckout).not.toHaveBeenCalled()
      expect(login).not.toHaveBeenCalled()
    }
    for (const inputs of [{ ready: true, canRelease: false }, { ready: false, canRelease: true }]) {
      const run = fixture(inputs)
      await onLogin()
      expect(run.client.releaseCheckout).not.toHaveBeenCalled()
      expect(sequence).toEqual(['login'])
    }
    expect(capability).not.toHaveBeenCalled()
    expect(legacyRelease).not.toHaveBeenCalled()

    for (const held of [true, false]) {
      fixture()
      projectModeRef.current = false
      capability.mockClear()
      capability.mockImplementation(() => {
        sequence.push('legacy capability')
        return held ? 'legacy capability' : null
      })
      let settle
      const release = new Promise((resolve) => { settle = resolve })
      legacyRelease.mockImplementationOnce(() => { sequence.push('legacy release'); return release })
      const pending = onLogin()
      expect(capability).toHaveBeenCalledExactlyOnceWith()
      if (held) {
        expect(sequence).toEqual(['legacy capability', 'legacy release'])
        expect(login).not.toHaveBeenCalled()
        settle()
      }
      await pending
      expect(sequence).toEqual(held ? ['legacy capability', 'legacy release', 'login'] : ['legacy capability', 'login'])
    }
    expect(legacyRelease).toHaveBeenCalledExactlyOnceWith()
  })

  it('R13F-24 project login refuses a scope that changed and returned while the release was pending', async () => {
    const O = '11111111-1111-1111-1111-111111111111'
    const P = '22222222-2222-2222-2222-222222222222'
    const D = '33333333-3333-3333-3333-333333333333'
    const binding = { kind: 'project', organization_id: O, project_id: P, drawing_id: D,
      input_version_id: '44444444-4444-4444-4444-444444444444' }
    const otherVersion = { ...binding, input_version_id: '55555555-5555-5555-5555-555555555555' }
    const lease = { holder: 'editor', acquired: '2026-01-01T00:00:00Z', expires: '2099-01-01T00:00:00Z',
      fence: '9007199254740993' }
    const respond = (data) => ({ ok: true, status: 200, json: async () => data })
    const identity = { organization_id: O, project_id: P, drawing_id: D }
    for (const roundTrip of [true, false]) {
      let settle = null
      const fetchImpl = vi.fn(async (url, init) => (init.method === 'POST'
        ? respond({ ...identity, acquired: true, checkout: lease, checkout_capability: 'lco1.' + 'b'.repeat(64) })
        : new Promise((resolve) => { settle = () => resolve(respond({ ...identity, released: true, checkout: null })) })))
      // The production client: it publishes a new binding object on every scope
      // change and keeps it across the release, which is what the login guard reads.
      const client = createSolarProjectClient({ fetchImpl })
      client.setScope(binding, 'authority')
      expect((await client.acquireCheckout()).ok, String(roundTrip)).toBe(true)
      const projectModeRef = { current: true }
      const controller = { getSnapshot: () => ({ generation: 7 }) }
      const owner = { ready: true, projectClient: client,
        scope: { binding: { ...binding }, authorityKey: 'authority' }, checkout: { canRelease: true } }
      const projectSolarLoginRef = { current: { projectSolar: owner, generation: 7, controller } }
      const login = vi.fn(async () => {})
      const checkout = { actions: { getCapability: vi.fn(() => null), release: vi.fn(async () => {}) } }
      const onLogin = Function('projectModeRef', 'projectSolarLoginRef', 'checkout', 'login',
        'sameSolarDrawingBinding', 'showToast', 'humanizeError', `return (${appText(loginCallback())})`)(
        projectModeRef, projectSolarLoginRef, checkout, login, sameSolarDrawingBinding, vi.fn(), String)
      const pending = onLogin()
      await vi.waitFor(() => expect(settle).toBeTypeOf('function'))
      if (roundTrip) {
        // Another version of the same drawing keeps the checkout, so the release survives it.
        client.setScope(otherVersion, 'authority')
        client.setScope(binding, 'authority')
        projectSolarLoginRef.current = { ...projectSolarLoginRef.current,
          projectSolar: { ...owner, scope: { binding: { ...binding }, authorityKey: 'authority' } } }
      }
      settle()
      await pending
      expect(client.getSnapshot().ownsCheckout, String(roundTrip)).toBe(false)
      expect(fetchImpl.mock.calls.map(([, init]) => init.method), String(roundTrip)).toEqual(['POST', 'DELETE'])
      expect(login, String(roundTrip)).toHaveBeenCalledTimes(roundTrip ? 0 : 1)
      expect(checkout.actions.getCapability, String(roundTrip)).not.toHaveBeenCalled()
      client.dispose()
    }
  })

  it('refreshes the lock after a run, where a write can change who holds it', () => {
    expect(stripped).toMatch(/loadUsage\(\);\s*checkout\.actions\.refresh\(\)/)
  })

  // Falsification: the pins above must FAIL on the shapes they forbid, not
  // merely pass on the shape that ships.
  it('fails when ?demo=locked fabricates an answered lock state', () => {
    const mutated = appSource.replace(
      '      checkout.unknown,\n      mock,',
      '      false,\n      mock,',
    )
    expect(mutated).not.toEqual(appSource)
    expect(strip(mutated)).not.toMatch(
      /const lock = demoLocked \? deriveCheckout\([\s\S]{0,400}?checkout\.unknown,\s*mock,/,
    )
  })

  it('fails when the unreadable-head lock is dropped from the write gate', () => {
    const mutated = appSource.replace(
      'const writeLocked = lock.writeLocked || drawingMutationsBlocked',
      'const writeLocked = lock.writeLocked',
    )
    expect(mutated).not.toEqual(appSource)
    expect(strip(mutated)).not.toMatch(/const writeLocked = lock\.writeLocked \|\| drawingMutationsBlocked/)
  })

  it('fails when a second controller instance is mounted', () => {
    const mutated = appSource.replace(
      '  const checkout = useCheckoutController({',
      '  const shadow = useCheckoutController({ mock })\n  const checkout = useCheckoutController({',
    )
    expect(mutated).not.toEqual(appSource)
    expect(strip(mutated).match(/useCheckoutController\(/g)).toHaveLength(2)
  })
})

describe('App.jsx hands the authored-tool cluster the same write gates as the catalog (3c)', () => {
  // The defect this pins: writeLocked and canRunWrite were passed to the two
  // catalog builders and NOT to authorCluster, so an authored WRITE tool under
  // a write lock or without write entitlement rendered enabled or reasonless.
  // No component test renders App.jsx, so the pin is over the source: the ONE
  // authorCluster call must carry both gates. Anchored on the call so a slot
  // in a neighbouring builder cannot satisfy it.
  const AUTHOR_CALL = /authorCluster\(\{(?:(?!Clusters\()[\s\S]){0,1200}?\bwriteLocked,(?:(?!Clusters\()[\s\S]){0,200}?writeEntitled:\s*canRunWrite\s*\}\)/

  it('passes writeLocked and writeEntitled into the one authorCluster call', () => {
    expect(stripped.match(/authorCluster\(/g) || []).toHaveLength(1)
    expect(stripped).toMatch(AUTHOR_CALL)
  })

  it('fails when either write gate is dropped from the authorCluster call', () => {
    const call = appSource.indexOf('const author = authorCluster({')
    expect(call).toBeGreaterThan(0)
    // The call's own closing, not the onOpen closure's inner `})`: anchor on
    // the last gate line and take the `})` after it.
    const gate = appSource.indexOf('writeEntitled: canRunWrite', call)
    expect(gate).toBeGreaterThan(call)
    const end = appSource.indexOf('})', gate)
    const head = appSource.slice(0, call)
    const body = appSource.slice(call, end)
    const tail = appSource.slice(end)
    const withoutLock = head + body.replace(/\n\s*writeLocked,/, '') + tail
    const withoutEnt = head + body.replace(/\n\s*writeEntitled: canRunWrite,/, '') + tail
    expect(withoutLock).not.toEqual(appSource)
    expect(withoutEnt).not.toEqual(appSource)
    expect(strip(withoutLock)).not.toMatch(AUTHOR_CALL)
    expect(strip(withoutEnt)).not.toMatch(AUTHOR_CALL)
  })
})
