// The ONE definition of what each mode mounts into WorkspaceControllerProvider
// (convergence bug (c), docs/convergence/ACCEPTANCE.md; sol finding: the two
// call sites had drifted structurally and merging them blind would change the
// console's version/undo/redo wiring).
//
// Every divergence between the modes is a NAMED DECISION here, not an
// accident of two files:
//
//   * retryNotFound is the CONSOLE's alone. The console's converse attach
//     retries a not_found once through the cache reset (App's original
//     `retryNotFound: true`, commit 146da651); the operator stage never did,
//     because its drawing can legitimately be absent (empty operator session)
//     and a retry loop against a drawing that does not exist is just latency.
//
//   * The console's converse session attaches CONSOLE_CONVERSE_DRAWING_ID,
//     even under a `?drawing=` boot. PARITY, on purpose: App attached its
//     module-const DEFAULT_DRAWING_ID from the day converse existed, and the
//     provider mount ported that literal. Whether the console's agent session
//     should follow the drawing identity instead is a REAL coherence question.
//     The W3 MOUNT PR (studio ground portal) kept this parity ON PURPOSE: it
//     moves only where the Viewer renders, never the drawing dataflow. The
//     decision belongs to the W3 TAIL (retiring the console's inline render
//     path and migrating drawing state onto the shared controller), where the
//     drawing identity and the converse session are wired in one place with
//     their own test. Do not change it as a side effect of a refactor.
//
//   * drawingOptions are the OPERATOR's alone. The stage binds its loaders to
//     the public-demo flag (the one reading SiteRoot made of `?demo`) and
//     wires the intake/selection callbacks into StageScene's local state. The
//     console passes NO options: useDrawingVersionController's defaults are
//     the authenticated api.js loaders the console has always used.
//
// The W3 one-shell mount calls these with its mode and moves the mount point;
// the shapes themselves are already converged here.
import {
  getDrawingIntake,
  getDrawingVersions,
  redoDrawing,
  undoDrawing,
} from '../api.js'

export const CONSOLE_CONVERSE_DRAWING_ID = 'rooftop_demo'

// Frozen module constant: the console mount has no per-render inputs, and a
// stable drawingOptions identity keeps the provider's onVersionEvent/onError
// callbacks (which depend on it) referentially stable across renders.
const CONSOLE_MOUNT = Object.freeze({
  drawingId: CONSOLE_CONVERSE_DRAWING_ID,
  retryNotFound: true,
  drawingOptions: Object.freeze({}),
})

export function consoleWorkspaceMount() {
  return CONSOLE_MOUNT
}

export function operatorWorkspaceMount({
  drawingId,
  publicDemo = false,
  onApplyIntake,
  onResetSelection,
} = {}) {
  return {
    drawingId,
    retryNotFound: false,
    drawingOptions: {
      loadHead: (id) => getDrawingIntake(publicDemo, id, 'head'),
      loadVersion: (id, version) => getDrawingIntake(publicDemo, id, version),
      // /try does not render delta chips. Its recovery-only restore controls
      // need the stable version list but not the larger delta response /app
      // uses.
      loadVersions: (id) => getDrawingVersions(publicDemo, id),
      undoVersion: (id, capability) => undoDrawing(publicDemo, id, capability),
      redoVersion: (id, capability) => redoDrawing(publicDemo, id, capability),
      onApplyIntake,
      onResetSelection,
    },
  }
}
