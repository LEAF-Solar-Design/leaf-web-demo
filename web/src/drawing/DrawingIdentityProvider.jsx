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
 * This PR changes WIRING, not behavior: each mode's seed reproduces exactly
 * what its shell computed before. The one addition is the binding scope-reset
 * contract — a persistent identity must not survive a tenant/project switch.
 *
 * SEED IS FROZEN AT MOUNT, on purpose. `seedInputs` (search string, demo
 * classification, remembered live id) are captured once, so `setFromQuery()`
 * restores the identity this page load BOOTED with rather than resurrecting
 * an id a later upload happened to remember — which would quietly undo a
 * scope reset. The matrix rows are boot decisions; this keeps them that way.
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
  const seedRef = useRef(null)
  if (!seedRef.current) {
    const resolvedSearch = search ?? (typeof window === 'undefined' ? '' : window.location.search)
    const demo = publicDemo === undefined || liveDemo === undefined
      ? classifyDemo(resolvedSearch, isSignedIn())
      : { publicDemo, liveDemo }
    seedRef.current = {
      mode,
      search: resolvedSearch,
      proofMode: proofMode === undefined
        ? classifyProof(resolvedSearch, readEnvProof())
        : !!proofMode,
      publicDemo: !!demo.publicDemo,
      liveDemo: !!demo.liveDemo,
      // The operator stage's step 3: a drawing a previous upload remembered
      // for this browser session. Read ONCE, at boot, for the reason in the
      // header comment.
      liveId: mode === DRAWING_MODE_OPERATOR ? readLiveDrawingId() : null,
    }
  }

  const [identity, setIdentity] = useState(() => seedDrawingIdentity(seedRef.current))

  // The upload promotion, unchanged from SiteRoot's promoteOperatorDrawing:
  // a receipt with no drawing id promotes nothing, and only an ACCOUNT tenant
  // earns a remembered id (a guest drawing must not outlive its session).
  const setFromUpload = useCallback((receipt) => {
    const next = identityFromUploadReceipt(receipt)
    if (!next) return null
    setIdentity(next)
    if (receipt?.tenant_kind === 'account') rememberDrawingId(next.drawingId)
    return next
  }, [rememberDrawingId])

  const setFromQuery = useCallback(() => {
    const next = seedDrawingIdentity(seedRef.current)
    setIdentity(next)
    return next
  }, [])

  const reset = useCallback(() => {
    setIdentity(RESET_DRAWING_IDENTITY)
  }, [])

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
