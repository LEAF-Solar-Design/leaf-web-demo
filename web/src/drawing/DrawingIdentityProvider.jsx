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
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'

import { isSignedIn } from '../auth.js'
import { liveDrawingId, rememberLiveDrawingId } from '../site/workbenchId.js'

import {
  DRAWING_MODE_CONSOLE,
  DRAWING_MODE_OPERATOR,
  EMPTY_DRAWING_IDENTITY,
  RESET_DRAWING_IDENTITY,
  classifyDemo,
  classifyProof,
  identityFromUploadReceipt,
  isScopeSwitch,
  seedDrawingIdentity,
} from './drawingIdentity.js'

export { DRAWING_MODE_CONSOLE, DRAWING_MODE_OPERATOR }

const DrawingIdentityContext = createContext(null)

// Written as the plain static member expression (never `import.meta.env?.`),
// the same shape web/src/cad/engineWorker.js uses: the optional chain defeats
// Vite's build-time replacement. VITE_CAT_PROOF is a runtime mode toggle, not
// a bundle fence, but the read stays statically foldable regardless.
function readEnvProof() {
  if (typeof import.meta !== 'undefined' && import.meta.env) return import.meta.env.VITE_CAT_PROOF
  return undefined
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
  // every render — the lazy fill below must not hand the tree a new object
  // each pass, and StrictMode's double render must converge on one value.
  const seedCacheRef = useRef(null)
  if (!seedCacheRef.current) seedCacheRef.current = new Map()
  const seedFor = (targetMode) => {
    const cache = seedCacheRef.current
    if (!cache.has(targetMode)) cache.set(targetMode, seedForMode(seedInputsRef.current, targetMode))
    return cache.get(targetMode)
  }

  const [identities, setIdentities] = useState(() => Object.freeze({ [mode]: seedFor(mode) }))

  const bootSeed = seedFor(mode)

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

  const value = useMemo(() => ({
    mode,
    drawingId: identity.drawingId,
    source: identity.source,
    origin: identity.origin,
    setFromUpload,
    setFromQuery,
    reset,
  }), [identity, mode, reset, setFromQuery, setFromUpload])

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
 * Resets the ACTIVE mode's identity: a project switch happens inside one
 * tenant, so the other mode's identity is still that tenant's.
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
