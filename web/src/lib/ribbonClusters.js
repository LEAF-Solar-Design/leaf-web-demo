// The drafting ribbon's clusters as DATA (W4d Slice A): pure builders over
// App state, so every group the ribbon shows is one tested decision and the
// ribbon component itself stays a renderer of an ordered cluster list.
//
// Cluster shape: { id, label, kind: 'group' | 'family', note?, tools: [] }.
// Tool shape:    { id, label, title?, reason?, disabled?, pressed?, write?,
//                  expanded?, controls?, onClick }.
//
// HONESTY CONTRACT (the reason a group exists here at all): every disabled
// tool carries the sentence that says WHY (`reason`), and a group that is
// unavailable as a whole carries it as `note`. A greyed control with no
// reason is the gap ToolsPanel's lock-note closed, and the ribbon must never
// reopen it. Nothing here is a stub: every onClick is a real handler App
// already owns.
import { zoomViewer } from '../site/DrawingCockpit.jsx'

export const RIBBON_RATIONALE = 'Ribbon selection. Confirm the exact tool and parameters before it runs.'
export const ZOOM_IN = 1.25
export const ZOOM_OUT = 0.8
// A drawing can carry hundreds of layers; the ribbon is a strip, not a
// palette. Past this the cluster says how many more live in the dock.
export const MAX_LAYER_TOOLS = 10

export const REASONS = Object.freeze({
  writeLocked: 'another session holds the edit lock',
  writeUnentitled: 'your plan does not include editing tools',
  buildUnentitled: 'your plan does not include authoring tools',
  buildUnavailable: 'the authoring stage is off on this deployment',
  noDrawing: 'no drawing loaded',
  noVersions: 'no versioned drawing',
  versionBusy: 'a version change is in flight',
  running: 'a run is in flight',
  previewing: 'viewing a version, read-only',
  mutationsBlocked: 'edits are blocked on this drawing',
  nothingToUndo: 'nothing to undo',
  nothingToRedo: 'nothing to redo',
})

/**
 * The catalog families of the active surface as `family` clusters — the
 * W4c-V1 ribbon, unchanged in behaviour: real commands through the
 * catalog run path with 'ribbon' attribution, ToolsPanel-parity write gating
 * with the reason readable. An empty fold is ONE honest cluster with the
 * sentence, never a fabricated button.
 */
export function catalogClusters(families, {
  onRequestRun,
  running = false,
  previewing = false,
  writeLocked = false,
  writeEntitled = true,
  writeLockNote = '',
} = {}) {
  const list = Array.isArray(families) ? families : []
  if (list.length === 0) {
    return [{ id: 'tools', label: 'Tools', kind: 'group', note: 'No tools for this surface yet.', tools: [] }]
  }
  return list.map((fam) => ({
    id: fam.family_id,
    label: fam.label,
    kind: 'family',
    tools: (fam.capabilities || []).map((tool) => {
      const isWrite = (tool.capabilities || []).includes('drawing.write')
      const locked = !!writeLocked && isWrite
      const entBlocked = isWrite && !writeEntitled
      const reason = running
        ? REASONS.running
        : previewing
          ? REASONS.previewing
          : locked
            ? (writeLockNote || REASONS.writeLocked)
            : entBlocked
              ? REASONS.writeUnentitled
              : ''
      return {
        id: tool.name,
        label: tool.name,
        title: tool.description || tool.name,
        write: isWrite,
        disabled: !!running || !!previewing || locked || entBlocked,
        reason,
        onClick: () => onRequestRun(tool, null, RIBBON_RATIONALE, 'ribbon'),
      }
    }),
  }))
}

/** View: fit / zoom in / zoom out on the Viewer's ref surface (setView/getPose). */
export function viewCluster({ viewerRef, hasDrawing = false } = {}) {
  const reason = hasDrawing ? '' : REASONS.noDrawing
  const tool = (id, label, title, onClick) => ({
    id, label, title, disabled: !hasDrawing, reason, onClick,
  })
  return {
    id: 'view',
    label: 'View',
    kind: 'group',
    tools: [
      tool('fit', 'fit', 'Fit the drawing to the view', () => { viewerRef?.current?.setView?.('home') }),
      tool('zoom-in', 'zoom-in', 'Zoom in', () => { zoomViewer(viewerRef?.current, ZOOM_IN) }),
      tool('zoom-out', 'zoom-out', 'Zoom out', () => { zoomViewer(viewerRef?.current, ZOOM_OUT) }),
    ],
  }
}

/** Version: undo / redo / history, under EXACTLY the toolbar's gates, each with its reason. */
export function versionCluster({
  hasVersions = false,
  canUndo = false,
  canRedo = false,
  versionBusy = false,
  running = false,
  previewing = false,
  mutationsBlocked = false,
  historyOpen = false,
  onUndo,
  onRedo,
  onToggleHistory,
} = {}) {
  const shared = !hasVersions
    ? REASONS.noVersions
    : versionBusy
      ? REASONS.versionBusy
      : running
        ? REASONS.running
        : previewing
          ? REASONS.previewing
          : mutationsBlocked
            ? REASONS.mutationsBlocked
            : ''
  const undoReason = shared || (!canUndo ? REASONS.nothingToUndo : '')
  const redoReason = shared || (!canRedo ? REASONS.nothingToRedo : '')
  const historyReason = !hasVersions ? REASONS.noVersions : versionBusy ? REASONS.versionBusy : ''
  return {
    id: 'version',
    label: 'Version',
    kind: 'group',
    tools: [
      { id: 'undo', label: 'undo', title: 'Undo the last version', disabled: !!undoReason, reason: undoReason, onClick: () => onUndo?.() },
      { id: 'redo', label: 'redo', title: 'Redo the undone version', disabled: !!redoReason, reason: redoReason, onClick: () => onRedo?.() },
      {
        id: 'history',
        label: 'history',
        title: 'Open the version history',
        disabled: !!historyReason,
        reason: historyReason,
        expanded: !!historyOpen,
        onClick: () => onToggleHistory?.(),
      },
    ],
  }
}

/**
 * Layers: one pressed-state toggle per layer (the Legend's exact rule:
 * visible unless the map says false), bounded at MAX_LAYER_TOOLS with the
 * remainder named honestly.
 */
export function layersCluster({ layers, counts = {}, visibleLayers = {}, onToggle, max = MAX_LAYER_TOOLS } = {}) {
  const list = Array.isArray(layers) ? layers : []
  if (list.length === 0) {
    return { id: 'layers', label: 'Layers', kind: 'group', note: REASONS.noDrawing, tools: [] }
  }
  const bound = Math.max(0, Number.isInteger(max) ? max : MAX_LAYER_TOOLS)
  const shown = list.slice(0, bound)
  const rest = list.length - shown.length
  return {
    id: 'layers',
    label: 'Layers',
    kind: 'group',
    note: rest > 0 ? `+${rest} more in the Layers palette` : null,
    tools: shown.map((layer) => {
      const on = visibleLayers[layer] !== false
      const n = counts[layer] || 0
      return {
        id: `layer:${layer}`,
        label: layer,
        title: `${on ? 'Hide' : 'Show'} ${layer} (${n})`,
        pressed: on,
        onClick: () => onToggle?.(layer),
      }
    }),
  }
}

/**
 * Author: one real command — expand the rail and open "Author a tool".
 * Two distinct reasons, because they have two distinct fixes: a plan that
 * lacks `build` (entitlement) and a deployment whose authoring stage is off
 * (availability, the R5 rail). `entitled` is the plan's own answer;
 * `available` is the folded entitlement-AND-availability rule the rest of
 * the shell gates Generate on.
 */
export function authorCluster({ onOpen, entitled = true, available = entitled } = {}) {
  const reason = !entitled ? REASONS.buildUnentitled : !available ? REASONS.buildUnavailable : ''
  return {
    id: 'author',
    label: 'Author',
    kind: 'group',
    tools: [{
      id: 'author-tool',
      label: 'author-tool',
      title: 'Build a new tool from plain English',
      disabled: !!reason,
      reason,
      onClick: () => onOpen?.(),
    }],
  }
}
