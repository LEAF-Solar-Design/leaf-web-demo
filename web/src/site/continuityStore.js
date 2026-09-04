// ---------------------------------------------------------------------------
// THE CONTINUITY STORE (standardization slice 4b, docs/convergence/
// SURFACE-CONTRACT.md "What slice 4b changed").
//
// The F-8 continuity rail and the AccountSignOut control used to be rendered by
// ProductSurfaceTabs, i.e. by whichever SCENE was mounted. Inside one scene
// that already satisfied the never-remounts contract (a profile switch
// re-renders the tabs; it never remounts them), but the /try <-> /app
// crossing swaps the scene, so both controls were torn down and rebuilt with
// the honest-empty first paint, and the rail forgot the last derived state.
//
// Slice 4b hoists their OWNER above the scene ternary: SiteRoot mounts ONE
// ContinuityStore (site/ContinuityStore.jsx) inside the DrawingIdentityProvider
// that already survives every scene, and that store renders the rail and the
// sign-out into a HOST element it owns for the life of the page. The active
// scene's SurfaceFrame publishes the derived state it already carries
// (activeSurface, workspaceProject, catalog, signedIn, onSignOut) through
// `useContinuityPublish`, and ProductSurfaceTabs ADOPTS the host node into the
// nav where the two controls always stood (`useContinuityHost`), so:
//
//   - the DOM position, class names, testids and rendered markup of both
//     controls are byte-identical to before (continuityHoist.test.jsx pins
//     them against a fixture captured on the untouched tree);
//   - the rail is the SAME node across a surface switch AND across the scene
//     crossing, and it shows the last published state until the new scene
//     publishes its own;
//   - no second session or catalog controller exists above the scenes: the
//     store holds a SNAPSHOT the scene publishes, never a controller.
//
// This module is the context and the hooks only, with no component and no
// import of any component, so ProductSurfaceTabs.jsx can read the host from
// here while site/ContinuityStore.jsx renders ProductSurfaceTabs' own
// ContinuityRail / AccountSignOut exports without an import cycle.
//
// Fails closed everywhere: outside a store `useContinuityHost` is null (the
// tabs adopt nothing, render nothing extra, never throw) and
// `useContinuityPublish` is a no-op.
// ---------------------------------------------------------------------------
import { createContext, useContext, useLayoutEffect } from 'react'

import { normalizeProductSurface, productSurfaceFromSearch } from './productSurfaces.js'

// Undefined (not null) is the "no provider" sentinel, like SurfaceFrame's.
export const ContinuityStoreContext = createContext(undefined)

// The class the host carries. landing.css gives it `display: contents`, so the
// host contributes NO box: the rail and the sign-out are flex items of the nav
// exactly as they were when the tabs rendered them inline. Pinned by
// continuityHoist.test.jsx against landing.css.
export const CONTINUITY_HOST_CLASS = 'tc-continuity-host'

/**
 * The snapshot the rail renders. `activeSurface` starts from the boot search
 * string (the same reading both scenes make of `?surface=`), the rest from the
 * honest-empty defaults so a first paint before any scene has published shows
 * ContinuityRail's EMPTY_WORKSPACE_PROJECT fallback and no sign-out.
 */
export function initialContinuitySnapshot(search = '') {
  return Object.freeze({
    activeSurface: productSurfaceFromSearch(search),
    workspaceProject: null,
    catalog: null,
    signedIn: false,
    onSignOut: null,
  })
}

const SNAPSHOT_KEYS = Object.freeze(['activeSurface', 'workspaceProject', 'catalog', 'signedIn', 'onSignOut'])

/**
 * Normalizes what a scene publishes into exactly the five fields the rail
 * reads, with types the rail can trust: an unknown surface id falls to the
 * default surface (normalizeProductSurface, the same rule the tabs apply), a non-object project
 * or catalog becomes null (the rail's fallback), signedIn is coerced to a
 * boolean and onSignOut must be a function or it is dropped. A publisher can
 * never hand the rail a shape it did not already accept from its props.
 */
export function normalizeContinuitySnapshot(next) {
  const src = next && typeof next === 'object' ? next : {}
  const project = src.workspaceProject
  const catalog = src.catalog
  return Object.freeze({
    activeSurface: normalizeProductSurface(src.activeSurface),
    workspaceProject: project && typeof project === 'object' ? project : null,
    catalog: catalog && typeof catalog === 'object' ? catalog : null,
    signedIn: src.signedIn === true,
    onSignOut: typeof src.onSignOut === 'function' ? src.onSignOut : null,
  })
}

/** Field-by-identity equality, so an unchanged publish never re-renders the rail. */
export function sameContinuitySnapshot(a, b) {
  if (a === b) return true
  if (!a || !b) return false
  for (const key of SNAPSHOT_KEYS) if (a[key] !== b[key]) return false
  return true
}

/**
 * The host element the store renders into and the nav adopts. Created ONCE per
 * store; `display: contents` comes from its class (landing.css), so it never
 * carries inline style and never changes the flex layout it joins. Returns
 * null with no document (a non-DOM environment), which the store treats as
 * "render nothing".
 */
export function createContinuityHost(doc = typeof document === 'undefined' ? null : document) {
  if (!doc || typeof doc.createElement !== 'function') return null
  const host = doc.createElement('div')
  host.className = CONTINUITY_HOST_CLASS
  return host
}

/** The store's context value, or undefined outside a provider. */
export function useContinuityStore() {
  return useContext(ContinuityStoreContext)
}

/**
 * The host node the rail and the sign-out live in, for the ONE nav that
 * adopts it (ProductSurfaceTabs). null outside a store, so a tabs band
 * mounted alone renders its tablist and nothing else.
 */
export function useContinuityHost() {
  return useContinuityStore()?.host ?? null
}

/**
 * The active scene publishes its derived state here. A layout effect, so the
 * rail reflects the scene's values in the same commit the scene's nav adopts
 * the host, before paint: a surface switch pulses the rail (its own effect on
 * activeSurface) and a scene crossing that lands on the same surface does not.
 * Deliberately NO cleanup: the store keeps the last snapshot across the
 * crossing on purpose, which is the whole point of the hoist. A no-op outside
 * a store.
 */
export function useContinuityPublish({ activeSurface, workspaceProject, catalog, signedIn, onSignOut }) {
  const publish = useContinuityStore()?.publish ?? null
  useLayoutEffect(() => {
    if (!publish) return
    publish({ activeSurface, workspaceProject, catalog, signedIn, onSignOut })
  }, [publish, activeSurface, workspaceProject, catalog, signedIn, onSignOut])
}
