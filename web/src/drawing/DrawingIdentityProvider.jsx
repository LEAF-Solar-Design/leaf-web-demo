/**
 * DrawingIdentityProvider — the ONE owner of the current drawing identity
 * (convergence W1, docs/convergence/ACCEPTANCE.md).
 *
 * Before this, two shells each owned their own answer to "which drawing is
 * this?": App.jsx's DRAWING_SOURCE/REQUESTED_DRAWING_ID module constants and
 * SiteRoot.jsx's operatorDrawingId state. Both now seed from the same rules
 * (drawingIdentity.js) and both READ through this provider, so the converged
 * studio has one identity to move rather than two to reconcile.
 *
 * PER-MODE IDENTITY MAP (panel W1 finding 1). SiteRoot mounts ONE provider
 * above its scene branch, so a /try -> / -> /sheets -> /try round trip no
 * longer unmounts it and destroys an in-progress upload identity — the state
 * the pre-#876 SiteRoot held survived that detour and this must too. But a
 * single-slot provider whose `mode` prop moves would then serve the FIRST
 * mode's identity under the second (measured: operator -> console yielded
 * `console|null|null|empty` instead of the console's own seed). So the
 * provider holds ONE ENTRY PER MODE: each mode seeds from the boot inputs on
 * its own first activation, keeps its identity across mode switches and scene
 * detours, and `setFromUpload` / `setFromQuery` / `reset` act on the ACTIVE
 * mode's entry only.
 *
 * SEED INPUTS ARE FROZEN AT MOUNT, on purpose. The search string, the demo
 * classification and the remembered live id are captured once, so a mode that
 * activates late still seeds from what this page load BOOTED with, and
 * `setFromQuery()` restores that rather than resurrecting an id a later upload
 * happened to remember — which would quietly undo a scope reset. The matrix
 * rows are boot decisions; this keeps them that way.
 *
 * SCOPE RESET has two halves, both binding:
 *   * PROJECT (useDrawingScopeReset) — resets the ACTIVE mode's identity. A
 *     project switch happens inside one tenant; the other mode's identity is
 *     still that tenant's.
 *   * TENANT (owned here, not by a consumer that could forget to mount it) —
 *     resets EVERY mode's identity AND voids the boot seeds, so a mode that
 *     activates after the switch cannot resurrect the previous tenant's
 *     drawing out of `?drawing=` or session storage.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'

import { subscribeUnauthorized } from '../api.js'
import { isSignedIn, subscribeTokenStored } from '../auth.js'
import { liveDrawingId, rememberLiveDrawingId } from '../site/workbenchId.js'

import {
  DRAWING_MODE_CONSOLE,
  DRAWING_MODE_OPERATOR,
  EMPTY_DRAWING_IDENTITY,
  RESET_DRAWING_IDENTITY,
  authTenantScope,
  classifyDemo,
  classifyProof,
  identityFromUploadReceipt,
  isScopeSwitch,
  seedDrawingIdentity,
} from './drawingIdentity.js'

export { DRAWING_MODE_CONSOLE, DRAWING_MODE_OPERATOR }

const DrawingIdentityContext = createContext(null)

// api.js's own key. Read here, never written: this module observes the
// principal, it never manages it.
const AUTH_TOKEN_KEY = 'leaf.jwt'

// Written as the plain static member expression (never `import.meta.env?.`),
// the same shape web/src/cad/engineWorker.js uses: the optional chain defeats
// Vite's build-time replacement. VITE_CAT_PROOF is a runtime mode toggle, not
// a bundle fence, but the read stays statically foldable regardless.
function readEnvProof() {
  if (typeof import.meta !== 'undefined' && import.meta.env) return import.meta.env.VITE_CAT_PROOF
  return undefined
}

// Total: private browsing, a disabled store and a missing global are all the
// signed-out scope, never a throw into the boot path.
function readStoredAuthToken() {
  try { return globalThis.localStorage?.getItem(AUTH_TOKEN_KEY) || null } catch { return null }
}

/**
 * Every channel that can change the stored principal WITHOUT a page load, so
 * the tenant guard is edge-triggered and never polls:
 *   * auth.js publishes a same-tab token write (the post-callback exchange —
 *     the `storage` event does not fire in the writing tab);
 *   * api.js publishes a 401 whose request carried the stored token, AFTER it
 *     has removed that token — a proven-dead principal;
 *   * `storage` covers a sign-out (or a different sign-in) in ANOTHER tab.
 * Module-level so it is referentially stable: a new identity each render would
 * re-subscribe on every render.
 */
function subscribeAuthPrincipalChange(listener) {
  const offStored = subscribeTokenStored(() => listener())
  const offUnauthorized = subscribeUnauthorized(() => listener())
  const onStorage = (event) => {
    // key === null is a whole-store clear, which includes ours.
    if (!event || event.key == null || event.key === AUTH_TOKEN_KEY) listener()
  }
  globalThis.addEventListener?.('storage', onStorage)
  return () => {
    offStored?.()
    offUnauthorized?.()
    globalThis.removeEventListener?.('storage', onStorage)
  }
}

function seedForMode(inputs, targetMode) {
  return seedDrawingIdentity({
    mode: targetMode,
    search: inputs.search,
    proofMode: inputs.proofMode,
    publicDemo: inputs.publicDemo,
    liveDemo: inputs.liveDemo,
    // Step 3 of the ladder is the OPERATOR stage's alone (the console's own
    // seed never consults it), so the console entry is not handed one.
    liveId: targetMode === DRAWING_MODE_OPERATOR ? inputs.liveId : null,
  })
}

/**
 * The principal, as state, kept in step with every channel above. Re-synced
 * once on subscribe so a change that lands between the initial read and the
 * subscription cannot hide.
 */
function useAuthPrincipalScope(readAuthToken, subscribeAuthChange) {
  const [principal, setPrincipal] = useState(() => authTenantScope(readAuthToken()))
  useEffect(() => {
    const sync = () => setPrincipal(authTenantScope(readAuthToken()))
    sync()
    return subscribeAuthChange(sync)
  }, [readAuthToken, subscribeAuthChange])
  return principal
}

export function DrawingIdentityProvider({
  mode = DRAWING_MODE_OPERATOR,
  // Every seam below is injectable so the provider is testable without a
  // location, a token or session storage — and so a host that already made
  // ONE reading of the search string (SiteRoot) can hand that reading in
  // instead of forcing a second, driftable one.
  search,
  publicDemo,
  liveDemo,
  proofMode,
  readLiveDrawingId = liveDrawingId,
  rememberDrawingId = rememberLiveDrawingId,
  readAuthToken = readStoredAuthToken,
  subscribeAuthChange = subscribeAuthPrincipalChange,
  children,
}) {
  // Mode-FREE boot inputs, captured once. The per-mode seed is derived from
  // them on that mode's first activation — see the header.
  const seedInputsRef = useRef(null)
  if (!seedInputsRef.current) {
    const resolvedSearch = search ?? (typeof window === 'undefined' ? '' : window.location.search)
    const demo = publicDemo === undefined || liveDemo === undefined
      ? classifyDemo(resolvedSearch, isSignedIn())
      : { publicDemo, liveDemo }
    seedInputsRef.current = {
      search: resolvedSearch,
      proofMode: proofMode === undefined
        ? classifyProof(resolvedSearch, readEnvProof())
        : !!proofMode,
      publicDemo: !!demo.publicDemo,
      liveDemo: !!demo.liveDemo,
      // The operator stage's step 3: a drawing a previous upload remembered
      // for this browser session. Read ONCE, at boot, for the reason in the
      // header comment.
      liveId: readLiveDrawingId() || null,
    }
  }

  // Boot seeds, memoised per mode so a mode's seed is the SAME frozen record
  // every render — a lazy fill below must not hand the tree a new object each
  // pass, and StrictMode's double render must converge on one value.
  const seedCacheRef = useRef(null)
  if (!seedCacheRef.current) seedCacheRef.current = new Map()
  const seedFor = (targetMode) => {
    const cache = seedCacheRef.current
    if (!cache.has(targetMode)) cache.set(targetMode, seedForMode(seedInputsRef.current, targetMode))
    return cache.get(targetMode)
  }

  // Voided by a TENANT switch: after one, no mode may seed from boot inputs
  // that belong to the previous tenant. It never un-voids — the boot
  // provenance of this page load is spent.
  const [scopeVoided, setScopeVoided] = useState(false)
  const [identities, setIdentities] = useState(() => Object.freeze({ [mode]: seedFor(mode) }))

  const bootSeed = scopeVoided ? RESET_DRAWING_IDENTITY : seedFor(mode)

  // A mode this provider has not served yet seeds NOW, from the boot inputs —
  // never from whichever mode happened to mount first. Render-phase set on
  // THIS component (React re-renders it immediately); `identity` below already
  // carries the new mode's value, so no render ever serves the wrong one.
  let identity = identities[mode]
  if (!identity) {
    identity = bootSeed
    setIdentities((current) => (current[mode]
      ? current
      : Object.freeze({ ...current, [mode]: bootSeed })))
  }

  const setActiveIdentity = useCallback((next) => {
    setIdentities((current) => (current[mode] === next
      ? current
      : Object.freeze({ ...current, [mode]: next })))
  }, [mode])

  // The upload promotion, unchanged from SiteRoot's promoteOperatorDrawing:
  // a receipt with no drawing id promotes nothing, and only an ACCOUNT tenant
  // earns a remembered id (a guest drawing must not outlive its session).
  const setFromUpload = useCallback((receipt) => {
    const next = identityFromUploadReceipt(receipt)
    if (!next) return null
    setActiveIdentity(next)
    if (receipt?.tenant_kind === 'account') rememberDrawingId(next.drawingId)
    return next
  }, [rememberDrawingId, setActiveIdentity])

  const setFromQuery = useCallback(() => {
    setActiveIdentity(bootSeed)
    return bootSeed
  }, [bootSeed, setActiveIdentity])

  const reset = useCallback(() => {
    setActiveIdentity(RESET_DRAWING_IDENTITY)
  }, [setActiveIdentity])

  // The tenant half: EVERY mode, plus the boot seeds behind the modes that
  // have not activated yet.
  const resetAll = useCallback(() => {
    setScopeVoided(true)
    setIdentities((current) => {
      const next = {}
      for (const key of Object.keys(current)) next[key] = RESET_DRAWING_IDENTITY
      return Object.freeze(next)
    })
  }, [])

  // Owned HERE rather than by a consumer, so the tenant clause cannot be left
  // un-mounted the way the project clause could be.
  const principal = useAuthPrincipalScope(readAuthToken, subscribeAuthChange)
  const previousPrincipalRef = useRef(undefined)
  useEffect(() => {
    const previous = previousPrincipalRef.current
    const next = principal || null
    previousPrincipalRef.current = next
    // The first observation is this session learning who it is, not a switch;
    // signing IN (null -> a principal) adopts what the guest built, which is
    // the product behaviour the adoption path already depends on. Signing OUT
    // and swapping principals are both switches.
    if (previous === undefined) return
    if (!isScopeSwitch(previous, next)) return
    resetAll()
  }, [principal, resetAll])

  const value = useMemo(() => ({
    mode,
    drawingId: identity.drawingId,
    source: identity.source,
    origin: identity.origin,
    setFromUpload,
    setFromQuery,
    reset,
    resetAll,
  }), [identity, mode, reset, resetAll, setFromQuery, setFromUpload])

  return (
    <DrawingIdentityContext.Provider value={value}>
      {children}
    </DrawingIdentityContext.Provider>
  )
}

/**
 * The strict read. A surface that needs the drawing identity and is mounted
 * without a provider is a wiring bug, not a degraded mode — say so loudly.
 */
export function useDrawingIdentity() {
  const value = useContext(DrawingIdentityContext)
  if (!value) throw new Error('useDrawingIdentity must be used inside DrawingIdentityProvider')
  return value
}

/**
 * The optional read, for surfaces that are legitimately mountable on their
 * own (their own specs, a future embed): no provider means no drawing
 * identity, which is a real state, not an error.
 */
export function useDrawingIdentityOptional() {
  return useContext(DrawingIdentityContext)
}

/**
 * Scope-reset contract (ACCEPTANCE "Scope-reset contract", binding): document
 * identity is a surface that persists across interactions, so it MUST NOT
 * survive a project switch or close. Mounted by the surface that owns the
 * project selection; a no-op outside a provider.
 *
 * The first observation is never a switch — it is the session learning which
 * project is open, and clearing there would discard the boot seed.
 */
export function useDrawingScopeReset(projectId) {
  const identity = useDrawingIdentityOptional()
  const reset = identity?.reset
  const previousRef = useRef(undefined)
  useEffect(() => {
    const previous = previousRef.current
    const next = projectId || null
    previousRef.current = next
    if (previous === undefined) return
    if (!isScopeSwitch(previous, next)) return
    reset?.()
  }, [projectId, reset])
}

export { EMPTY_DRAWING_IDENTITY }
