// The drafting ribbon's clusters as DATA (W4d Slice A, W4e panels): pure
// builders over App state, so every panel the ribbon shows is one tested
// decision and the ribbon component itself stays a renderer of an ordered
// cluster list.
//
// Cluster shape: { id, label, kind: 'group' | 'family', note?, tools: [],
//                  widgets?: [] }.
// Tool shape:    { id, label, text?, icon?, size?: 'large'|'small'|'row',
//                  swatch?, title?, reason?, disabled?, pressed?, write?,
//                  expanded?, controls?, onClick }.
//   `label` is the accessible name (stable, tests key on it); `text` is the
//   ribbon's display label for large/row tools; `icon` is a CockpitIcon key.
//
// HONESTY CONTRACT (the reason a group exists here at all): every disabled
// tool carries the sentence that says WHY (`reason`), and a group that is
// unavailable as a whole carries it as `note`. A greyed control with no
// reason is the gap ToolsPanel's lock-note closed, and the ribbon must never
// reopen it. Nothing here is a stub: every onClick is a real handler App
// already owns, and the reference panels this engine cannot back yet are
// present, disabled, and say so (operator decision, W4e plan: mirror the
// reference's eight Draw-tab panels).
import { zoomViewer } from '../site/DrawingCockpit.jsx'

export const RIBBON_RATIONALE = 'Ribbon selection. Confirm the exact tool and parameters before it runs.'
export const ZOOM_IN = 1.25
export const ZOOM_OUT = 0.8
// A drawing can carry hundreds of layers; the ribbon is a strip, not a
// palette. Past this the cluster says how many more live in the pane.
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
  notInEngine: 'not in the browser engine yet',
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
  onOpenFamily = null,
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
    // The family label is a real command: open that family in the tool
    // rail (the spine's monogram used to do this; on drafting surfaces the
    // rail is hidden under the band, so the band carries the affordance).
    onLabelClick: onOpenFamily ? () => onOpenFamily(fam) : null,
    labelTitle: onOpenFamily ? `Open ${fam.label} in the tool rail (${(fam.capabilities || []).length} tools)` : '',
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
        text: tool.label || tool.name,
        icon: 'toolbox',
        size: 'large',
        title: tool.description || tool.name,
        write: isWrite,
        disabled: !!running || !!previewing || locked || entBlocked,
        reason,
        onClick: () => onRequestRun(tool, null, RIBBON_RATIONALE, 'ribbon'),
      }
    }),
  }))
}

/**
 * Rail: the one command the hidden tool rail still needs from the band —
 * expand it. On drafting surfaces under the studio the rail sits behind
 * the band (the reference has no left rail at all), so this is the
 * affordance that brings it back; the rail's own header collapses it again.
 */
export function railCluster({ onExpand } = {}) {
  return {
    id: 'rail',
    label: 'Rail',
    kind: 'group',
    tools: [{
      id: 'rail-expand',
      label: 'expand',
      text: 'Tool rail',
      icon: 'sidebar',
      size: 'large',
      title: 'Expand the tool rail',
      onClick: () => onExpand?.(),
    }],
  }
}

/** View: fit / zoom in / zoom out on the Viewer's ref surface (setView/getPose). */
export function viewCluster({ viewerRef, hasDrawing = false } = {}) {
  const reason = hasDrawing ? '' : REASONS.noDrawing
  const tool = (id, label, text, icon, title, onClick) => ({
    id, label, text, icon, size: 'large', title, disabled: !hasDrawing, reason, onClick,
  })
  return {
    id: 'view',
    label: 'View',
    kind: 'group',
    tools: [
      tool('fit', 'fit', 'Fit', 'fit', 'Fit the drawing to the view', () => { viewerRef?.current?.setView?.('home') }),
      tool('zoom-in', 'zoom-in', 'Zoom in', 'zoom-in', 'Zoom in', () => { zoomViewer(viewerRef?.current, ZOOM_IN) }),
      tool('zoom-out', 'zoom-out', 'Zoom out', 'zoom-out', 'Zoom out', () => { zoomViewer(viewerRef?.current, ZOOM_OUT) }),
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
      { id: 'undo', label: 'undo', text: 'Undo', icon: 'undo', size: 'large', title: 'Undo the last version', disabled: !!undoReason, reason: undoReason, onClick: () => onUndo?.() },
      { id: 'redo', label: 'redo', text: 'Redo', icon: 'redo', size: 'large', title: 'Redo the undone version', disabled: !!redoReason, reason: redoReason, onClick: () => onRedo?.() },
      {
        id: 'history',
        label: 'history',
        text: 'History',
        icon: 'history',
        size: 'large',
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
 * remainder named honestly. Each row is the reference's layer line: a bulb
 * (lit = shown), the layer's swatch, the name.
 */
export function layersCluster({ layers, counts = {}, visibleLayers = {}, onToggle, colorFor = null, max = MAX_LAYER_TOOLS } = {}) {
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
      const swatch = typeof colorFor === 'function' ? (colorFor(layer) || '') : ''
      return {
        id: `layer:${layer}`,
        label: layer,
        text: layer,
        icon: 'bulb',
        size: 'row',
        swatch,
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
      text: 'Author tool',
      icon: 'wand',
      size: 'large',
      title: 'Build a new tool from plain English',
      disabled: !!reason,
      reason,
      onClick: () => onOpen?.(),
    }],
  }
}

// A reference panel this engine cannot back yet: present at the reference's
// place and width, every tool disabled with the same honest sentence, the
// panel's note carrying it too. Never a click handler that pretends.
function offTool(id, label, icon, size = 'small') {
  return { id, label, text: label, icon, size, title: label, disabled: true, reason: REASONS.notInEngine, onClick: () => {} }
}

/**
 * The reference's Draw-tab panels beyond Draw and Modify (which the engine
 * consumer renders): Annotation, Layers widget (built by layersCluster),
 * Block, Properties, Groups, Clipboard — in the reference's order, at the
 * reference's shapes, all honestly unavailable.
 */
export function referencePanels() {
  const note = REASONS.notInEngine
  return [
    {
      id: 'annotation', label: 'Annotation', kind: 'group', note,
      tools: [
        offTool('annotation:text', 'Text', 'text', 'large'),
        offTool('annotation:dimensions', 'Dimensions', 'dimension', 'large'),
        offTool('annotation:leader', 'Leader', 'leader', 'large'),
      ],
    },
    {
      id: 'block', label: 'Block', kind: 'group', note,
      tools: [
        offTool('block:create', 'Create Block', 'block-create', 'large'),
        offTool('block:insert', 'Insert Block', 'block-insert', 'large'),
      ],
    },
    {
      id: 'properties', label: 'Properties', kind: 'group', note,
      tools: [offTool('properties:match', 'Match', 'match', 'large')],
      widgets: [
        { id: 'prop-color', label: 'Color', value: 'ByLayer', disabled: true, reason: note },
        { id: 'prop-linetype', label: 'Linetype', value: 'ByLayer', disabled: true, reason: note },
        { id: 'prop-lineweight', label: 'Lineweight', value: 'ByLayer', disabled: true, reason: note },
      ],
    },
    {
      id: 'groups', label: 'Groups', kind: 'group', note,
      tools: [
        offTool('groups:group', 'Group', 'group', 'large'),
        offTool('groups:ungroup', 'Ungroup', 'ungroup', 'large'),
      ],
    },
    {
      id: 'clipboard', label: 'Clipboard', kind: 'group', note,
      tools: [
        offTool('clipboard:paste', 'Paste', 'paste', 'large'),
        offTool('clipboard:cut', 'Cut', 'cut'),
        offTool('clipboard:copy', 'Copy', 'copy'),
      ],
    },
  ]
}
