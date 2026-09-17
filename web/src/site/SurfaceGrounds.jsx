// The per-surface GROUND under the studio shell (W4a, docs/convergence/
// ACCEPTANCE.md "Surface grounds"). The W3 mount put the console's drawing on
// the z0 ground for every tab, which only reads as a workspace on CAD and
// Solar CAD. Here the ground becomes "the actual workspace" for each surface
// (operator direction 2026-09-01/02): the drawing for CAD and Solar CAD, an
// open-project BOARD for Browser, a device STAGE for the iOS ship lane.
//
// Contract, same as the viewer ground:
//   * Rendered ONLY through App's studio-ground portal (rail ON). The old
//     shell has no ground, so none of this exists there — the rollback path
//     is untouched by construction, and siteRootOneShell.test.js pins it.
//   * Every ground is props-driven and fails HONEST: real state renders,
//     absent state says so ("No jobs yet"), nothing is invented — the same
//     rule productSurfaces/workspaceProjectState/IosSurface already obey.
//   * Exactly one ground is visible at a time (the `hidden` attribute, never
//     an unmount): the drawing ground survives tab switches with its WebGL
//     context, lock, and job state, exactly as the workspace card always
//     did (`display: none`, not unmount).
import { PRODUCT_SURFACES, surfaceGround } from './productSurfaces.js'
import { ProjectBoardGround } from './ProjectBoardGround.jsx'
import { DeviceGround } from '../ios/DeviceGround.jsx'
import { NO_OCCLUDERS } from './groundWindow.js'

export { ProjectBoardGround } from './ProjectBoardGround.jsx'
export { DeviceGround } from '../ios/DeviceGround.jsx'
export { measureGroundWindow, measureContainedWindow } from './groundWindow.js'

// Standardization slice 2: DERIVED from the Surface Contract instead of a
// hand-kept literal Set, so a surface's ground is declared in exactly one
// place (productSurfaces.js) and this file cannot drift from it. Computed once
// at module load, never per call: `has` stays O(1) on the hot render path.
// The truth table is unchanged from the old `new Set(['cad','solar'])`, and
// surfaceGates.test.js pins it. That includes the unknown/undefined case, which
// still answers false, because a Set lookup misses rather than normalizing.
// `contract?.` so a record that ever ships without a contract reads as "no
// drawing ground" instead of throwing during module load (a white screen
// before any error boundary exists). productSurfaces.test.js pins contract
// presence for every id, so today this guard never fires.
const DRAWING_SURFACES = new Set(
  PRODUCT_SURFACES.filter(({ contract }) => contract?.ground === 'drawing').map(({ id }) => id),
)

// The drawing ground shows for the surfaces whose declared ground is 'drawing'.
export function groundShowsDrawing(surface) {
  return DRAWING_SURFACES.has(surface)
}

// Both non-drawing grounds, mounted once and toggled by `hidden`, so a tab
// switch never remounts a ground any more than it remounts the drawing.
export default function SurfaceGrounds({
  surface, workspaceProject, workspace, drawing, catalog, mock,
  boardVisible, onReturnToDrawing, headingRef, startFocusRequest, leavingGround = null,
  onCreateProject,
  occluders = NO_OCCLUDERS,
  studioPresentation = false, studioShell = false,
  iosEnabled, iosContract, revision,
}) {
  const projectLabel = workspaceProject?.kind === 'project'
    ? workspaceProject.label
    : workspaceProject?.drawingName || null
  return (
    <>
      {/* Slice 2: each ground is active for its DECLARED ground kind, not for
          a surface id (it used to compare the surface id to the browser and
          ios literals). An unknown surface still activates neither:
          surfaceGround falls closed to the CAD contract, whose ground is
          'drawing'. */}
      <ProjectBoardGround
        active={boardVisible ?? surfaceGround(surface) === 'board'}
        leavingGround={leavingGround}
        contained={studioShell || (boardVisible === true && groundShowsDrawing(surface))}
        occluders={occluders}
        onReturnToDrawing={onReturnToDrawing}
        onCreateProject={onCreateProject}
        headingRef={headingRef}
        startFocusRequest={startFocusRequest}
        studioPresentation={studioPresentation}
        studioShell={studioShell}
        workspaceProject={workspaceProject}
        workspace={workspace}
        drawing={drawing}
        catalog={catalog}
        mock={mock}
      />
      <DeviceGround
        active={!boardVisible && surfaceGround(surface) === 'device-stage'}
        leavingGround={leavingGround}
        studioShell={studioShell}
        occluders={occluders}
        enabled={iosEnabled}
        contract={iosContract}
        projectLabel={projectLabel}
        revision={revision}
      />
    </>
  )
}
