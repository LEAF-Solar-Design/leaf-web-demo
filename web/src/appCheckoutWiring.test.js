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
import { describe, expect, it } from 'vitest'

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
      /drawingId: checkoutScopeDrawingId\(\{\s*identityDrawingId: REQUESTED_DRAWING_ID,\s*drawingState,\s*requestedDrawingId: REQUESTED_DRAWING_ID\s*\}\)/,
    )
  })

  it('hands the controller the boot inputs the deferral and the handoff need', () => {
    // `bootDrawingId` is what lets the reload handoff be bootstrapped in the
    // render phase, which is what decides the claim deferral before the first
    // effect runs — the ordering the retired block got from its own
    // render-phase bootstrap. `onHolderRemint` is how a refused redemption
    // reaches this shell's holder state.
    expect(stripped).toMatch(/bootDrawingId: REQUESTED_DRAWING_ID/)
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
    expect(stripped).toMatch(
      /const onLogin = useCallback\(async \(\) => \{\s*if \(checkout\.actions\.getCapability\(\)\) await checkout\.actions\.release\(\);\s*await login\(\);/,
    )
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
