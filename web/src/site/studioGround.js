// The studio ground handoff (convergence W3, docs/convergence/ACCEPTANCE.md).
//
// SiteRoot always renders the studio shell for scene
// 'app': a fixed full-viewport host whose z0 layer — the GROUND — is where
// the drawing lives, with the console floating above it. The console still
// OWNS its Viewer (state, props, ref, pendingEdit, palette — nothing about
// the drawing dataflow moves in W3); it just RENDERS the element into this
// ground node via a portal. That is the W3 contract: mount the shared Viewer
// as the studio ground and prove every route-matrix row plus rollback BEFORE
// any ownership migration or Viewer deletion.
//
// null means only that the ground target is not attached yet (the first
// render, before SiteRoot's callback ref sets its state) or that nothing
// provides one (a bare test mount). There is no old shell. The drawing never
// renders inline on null: it waits for the ground, so it mounts exactly once.
import { createContext, useContext } from 'react'

export const StudioGroundContext = createContext(null)

export function useStudioGround() {
  return useContext(StudioGroundContext)
}
