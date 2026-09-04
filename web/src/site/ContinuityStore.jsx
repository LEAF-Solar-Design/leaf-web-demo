// ---------------------------------------------------------------------------
// ContinuityStore (standardization slice 4b): the ONE owner of the F-8
// continuity rail and the AccountSignOut control, mounted by SiteRoot ABOVE
// the scene ternary, inside the DrawingIdentityProvider that already survives
// every scene. See site/continuityStore.js for the contract and the reasons.
//
// What it renders: its children untouched, plus a React portal carrying
// <ContinuityRail> and <AccountSignOut> into a host <div> it creates once and
// holds for the life of the page. The host is NOT placed anywhere by this
// component. ProductSurfaceTabs adopts it into the nav where the two controls
// always stood, so the visible DOM is byte-identical; between scenes the host
// sits detached with its children intact, and React keeps reconciling into it
// because a portal targets the node, not the node's position. That is what
// makes the rail the SAME element across the /try <-> /app crossing.
//
// What it owns: a SNAPSHOT of the active scene's derived state, published by
// that scene's SurfaceFrame. Never a session controller, never a catalog
// controller: SiteRoot forbids a second instance of either above the scenes,
// and the ONE EngineSessionProvider stays App's engine mount.
// ---------------------------------------------------------------------------
import { useCallback, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'

import { AccountSignOut, ContinuityRail } from '../components/ProductSurfaceTabs.jsx'

import {
  ContinuityStoreContext,
  createContinuityHost,
  initialContinuitySnapshot,
  normalizeContinuitySnapshot,
  sameContinuitySnapshot,
} from './continuityStore.js'

/**
 * @param search  the boot search string (SiteRoot's ONE reading of it), which
 *                seeds `activeSurface` before any scene has published.
 */
export default function ContinuityStore({ search = '', children = null }) {
  // Created once. useState's initializer, not useMemo: the host's identity is
  // state the page keeps, and useMemo is documented as a cache React may drop.
  const [host] = useState(() => createContinuityHost())
  const [snapshot, setSnapshot] = useState(() => initialContinuitySnapshot(search))
  // Stable for the life of the store, so consumers' effects depend on a
  // constant and the context value below never churns on a publish.
  const publish = useCallback((next) => {
    const normalized = normalizeContinuitySnapshot(next)
    setSnapshot((prev) => (sameContinuitySnapshot(prev, normalized) ? prev : normalized))
  }, [])
  const value = useMemo(() => ({ host, publish }), [host, publish])
  return (
    <ContinuityStoreContext.Provider value={value}>
      {children}
      {/* No host means no document: render nothing rather than throw. The
          element order inside the host is the order the tabs rendered inline
          (rail, then sign-out), pinned by continuityHoist.test.jsx. */}
      {host && createPortal(
        <>
          <ContinuityRail
            activeSurface={snapshot.activeSurface}
            workspaceProject={snapshot.workspaceProject}
            catalog={snapshot.catalog}
          />
          <AccountSignOut signedIn={snapshot.signedIn} onSignOut={snapshot.onSignOut} />
        </>,
        host,
      )}
    </ContinuityStoreContext.Provider>
  )
}
