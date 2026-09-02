// The studio ground handoff (convergence W3, docs/convergence/ACCEPTANCE.md).
//
// Under LEAF_ONE_SHELL_ENABLED, SiteRoot renders the studio shell for scene
// 'app': a fixed full-viewport host whose z0 layer — the GROUND — is where
// the drawing lives, with the console floating above it. The console still
// OWNS its Viewer (state, props, ref, pendingEdit, palette — nothing about
// the drawing dataflow moves in W3); it just RENDERS the element into this
// ground node via a portal. That is the W3 contract: mount the shared Viewer
// as the studio ground and prove every route-matrix row plus rollback BEFORE
// any ownership migration or Viewer deletion.
//
// null means NO ground: the old shell (rail off, or any non-studio mount).
// Every consumer must treat null as "render inline exactly as today" — that
// IS the rollback path, so it can never be a degraded mode.
import { createContext, useContext } from 'react'

export const StudioGroundContext = createContext(null)

export function useStudioGround() {
  return useContext(StudioGroundContext)
}
