// The drafting ribbon's clusters as DATA (W4d Slice A, W4e panels): pure
// builders over App state, so every panel the ribbon shows is one tested
// decision and the ribbon component itself stays a renderer of an ordered
// cluster list.
//
// Cluster shape: { id, label, kind: 'group' | 'family', note?, tools: [],
//                  widgets?: [] }.
// Tool shape:    { id, label, text?, icon?, size?: 'large'|'small'|'row',
//                  swatch?, title?, mcpSource?: { server_id, tool }, reason?,
//                  disabled?, pressed?, write?, expanded?, controls?, onClick }.
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
import { REASONS, forCluster, ribbonTool } from './actionRegistry.js'
import { isWriteTool, toolIcon, toolMcpSource, toolPlacementSize, toolPlacementTab } from './toolRecord.js'

// The reason vocabulary moved to the action registry with slice 10a, because
// `when(ctx)` is the registry's half of the honesty contract this file's header
// states. Re-exported unchanged so every importer and every pinned test reads
// the same frozen object it always did.
export { REASONS } from './actionRegistry.js'

export const RIBBON_RATIONALE = 'Ribbon selection. Confirm the exact tool and parameters before it runs.'
export const ZOOM_IN = 1.25
export const ZOOM_OUT = 0.8
// A drawing can carry hundreds of layers; the ribbon is a strip, not a
// palette. Past this the cluster says how many more live in the pane.
export const MAX_LAYER_TOOLS = 10

// Every catalog tool used to be one hardcoded icon and one hardcoded size. The
// record now answers both, and a record that answers neither renders exactly as
// it did — that equality is pinned in ribbonClusters.test.js.
export const CATALOG_TOOL_NOTE_ALL_PLACED = 'Every catalog tool sits on its own ribbon tab.'

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
  engineDirty = false,
} = {}) {
  const list = Array.isArray(families) ? families : []
  if (list.length === 0) {
    return [{ id: 'tools', label: 'Tools', kind: 'group', note: 'No tools for this surface yet.', tools: [] }]
  }
  const gate = { onRequestRun, running, previewing, writeLocked, writeEntitled, writeLockNote, engineDirty }
  // Tools that name their own tab leave this cluster (they are built by
  // catalogTabClusters instead); a family whose tools ALL moved leaves no
  // empty panel behind.
  const clusters = []
  for (const fam of list) {
    const tools = (fam.capabilities || []).filter((tool) => !toolPlacementTab(tool))
    if (tools.length === 0 && (fam.capabilities || []).length > 0) continue
    clusters.push(familyCluster(fam, tools, gate, onOpenFamily))
  }
  if (clusters.length === 0) {
    return [{ id: 'tools', label: 'Tools', kind: 'group', note: CATALOG_TOOL_NOTE_ALL_PLACED, tools: [] }]
  }
  return clusters
}

/**
 * The same catalog families as PER-TAB clusters, for the tools whose record
 * names a `placement.tab`. Cluster id is `<family_id>@<tab>` so a family can
 * seat tools on more than one tab and the two clusters stay distinct; the
 * label is the family's, unchanged.
 *
 * Returns an object keyed by tab id, holding only the tabs that actually got
 * tools — so a catalog where nothing declares a placement returns `{}` and the
 * ribbon is byte-identical to today.
 */
export function catalogTabClusters(families, {
  onRequestRun,
  onOpenFamily = null,
  running = false,
  previewing = false,
  writeLocked = false,
  writeEntitled = true,
  writeLockNote = '',
  engineDirty = false,
} = {}) {
  const list = Array.isArray(families) ? families : []
  const gate = { onRequestRun, running, previewing, writeLocked, writeEntitled, writeLockNote, engineDirty }
  const byTab = {}
  for (const fam of list) {
    // One pass per family, bucketed by tab: no per-tab rescan of the catalog.
    const buckets = new Map()
    for (const tool of fam.capabilities || []) {
      const tab = toolPlacementTab(tool)
      if (!tab) continue
      const bucket = buckets.get(tab)
      if (bucket) bucket.push(tool)
      else buckets.set(tab, [tool])
    }
    for (const [tab, tools] of buckets) {
      const cluster = familyCluster(fam, tools, gate, onOpenFamily)
      cluster.id = `${fam.family_id}@${tab}`
      if (byTab[tab]) byTab[tab].push(cluster)
      else byTab[tab] = [cluster]
    }
  }
  return byTab
}

/** One family cluster over an explicit tool list. The single tool projection. */
function familyCluster(fam, tools, gate, onOpenFamily) {
  const { onRequestRun, running, previewing, writeLocked, writeEntitled, writeLockNote, engineDirty } = gate
  return {
    id: fam.family_id,
    label: fam.label,
    kind: 'family',
    // The family label is a real command: open that family in the tool
    // rail (the spine's monogram used to do this; on drafting surfaces the
    // rail is hidden under the band, so the band carries the affordance).
    onLabelClick: onOpenFamily ? () => onOpenFamily(fam) : null,
    labelTitle: onOpenFamily ? `Open ${fam.label} in the tool rail (${(fam.capabilities || []).length} tools)` : '',
    tools: tools.map((tool) => {
      const isWrite = isWriteTool(tool)
      const locked = !!writeLocked && isWrite
      const entBlocked = isWrite && !writeEntitled
      const dirtyBlocked = isWrite && !!engineDirty
      // A tool projected from a connected MCP server (mcp_source) has no run
      // path on any surface yet — the projection itself is stubbed to emit
      // nothing until a later slice — so it is unconditionally unrunnable,
      // ahead of every transient gate below.
      const mcpSource = toolMcpSource(tool)
      const reason = mcpSource
        ? REASONS.mcpToolNotWired
        : running
          ? REASONS.running
          : previewing
            ? REASONS.previewing
            : locked
              ? (writeLockNote || REASONS.writeLocked)
              : entBlocked
                ? REASONS.writeUnentitled
                : dirtyBlocked
                  ? REASONS.unsavedEngineEdits
                  : ''
      return {
        id: tool.name,
        label: tool.name,
        text: tool.label || tool.name,
        // The record's own answer, with today's literals as the default.
        icon: toolIcon(tool),
        size: toolPlacementSize(tool),
        title: tool.description || tool.name,
        write: isWrite,
        ...(mcpSource ? { mcpSource } : {}),
        disabled: !!mcpSource || !!running || !!previewing || locked || entBlocked || dirtyBlocked,
        reason,
        onClick: () => onRequestRun(tool, null, RIBBON_RATIONALE, 'ribbon'),
      }
    }),
  }
}

/**
 * Rail: the one command the hidden tool rail still needs from the band —
 * expand it. On drafting surfaces under the studio the rail sits behind
 * the band (the reference has no left rail at all), so this is the
 * affordance that brings it back; the rail's own header collapses it again.
 */
export function railCluster({ onExpand } = {}) {
  const ctx = { onExpand: () => onExpand?.() }
  return {
    id: 'rail',
    label: 'Rail',
    kind: 'group',
    tools: forCluster('rail').map((action) => ribbonTool(action, ctx)),
  }
}

/**
 * View: fit / zoom in / zoom out on the Viewer's ref surface (setView/getPose),
 * and (W4e round 3) the Properties pane toggle when the caller owns one: a
 * pressed-state tool, the way back after the pane's own close control.
 */
export function viewCluster({ viewerRef, hasDrawing = false, paneOpen = null, onTogglePane = null } = {}) {
  // The four records live in the registry; this builder supplies the CONTEXT
  // they close over (the viewer ref surface is React's, never the registry's).
  const ctx = {
    hasDrawing,
    paneOpen,
    onFit: () => { viewerRef?.current?.setView?.('home') },
    onZoomIn: () => { zoomViewer(viewerRef?.current, ZOOM_IN) },
    onZoomOut: () => { zoomViewer(viewerRef?.current, ZOOM_OUT) },
    onTogglePane: () => onTogglePane?.(),
  }
  const tools = []
  for (const action of forCluster('view')) {
    // The pane toggle is seated only when the caller owns a pane; without one
    // there is nothing to toggle, so the record is absent rather than dead.
    if (action.id === 'properties-pane') {
      if (typeof onTogglePane !== 'function') continue
      tools.push(ribbonTool(action, ctx, { pressed: !!paneOpen }))
      continue
    }
    tools.push(ribbonTool(action, ctx))
  }
  return { id: 'view', label: 'View', kind: 'group', tools }
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
  // undo / redo / history are three registry records under EXACTLY the
  // toolbar's gates; the shared ladder (and history's shorter one, because
  // reading the chain is never a write) lives in each record's `when`.
  const ctx = {
    hasVersions,
    canUndo,
    canRedo,
    versionBusy,
    running,
    previewing,
    mutationsBlocked,
    onUndo: () => onUndo?.(),
    onRedo: () => onRedo?.(),
    onToggleHistory: () => onToggleHistory?.(),
  }
  return {
    id: 'version',
    label: 'Version',
    kind: 'group',
    tools: forCluster('version').map((action) => ribbonTool(
      action,
      ctx,
      action.id === 'history' ? { expanded: !!historyOpen } : {},
    )),
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
export function authorCluster({
  onOpen, entitled = true, available = entitled,
  // The tool the author card just published, if any. It joins the cluster so
  // "run the one I just made" is a ribbon command too, and it says honestly
  // when the catalog has not caught up: no digest, no runnable tool.
  authored = null, onUseAuthored = null, running = false, previewing = false,
  // ToolsPanel-parity write gating (same rungs, same REASONS strings as
  // familyCluster): an authored WRITE tool is exactly as honest about a held
  // edit lock or a plan without editing tools as any catalog tool is.
  writeLocked = false, writeEntitled = true, writeLockNote = '',
} = {}) {
  // The two distinct reasons (an unentitled plan, an authoring stage that is
  // off) are the record's own ladder; this builder supplies only the context.
  const tools = forCluster('author').map((action) => ribbonTool(action, {
    entitled,
    available,
    onOpen: () => onOpen?.(),
  }))
  if (authored && authored.name) {
    const settled = typeof authored.catalog_digest === 'string' && !!authored.catalog_digest
    const isWrite = isWriteTool(authored)
    const locked = !!writeLocked && isWrite
    const entBlocked = isWrite && !writeEntitled
    const authoredReason = !settled
      ? REASONS.publishing
      : running
        ? REASONS.running
        : previewing
          ? REASONS.previewing
          : locked
            ? (writeLockNote || REASONS.writeLocked)
            : entBlocked
              ? REASONS.writeUnentitled
              : ''
    tools.push({
      id: `authored:${authored.name}`,
      label: authored.name,
      text: authored.name,
      icon: toolIcon(authored),
      size: toolPlacementSize(authored),
      title: authored.description || `Run ${authored.name}`,
      write: isWrite,
      disabled: !!authoredReason,
      reason: authoredReason,
      onClick: () => onUseAuthored?.(authored),
    })
  }
  return { id: 'author', label: 'Author', kind: 'group', tools }
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
    // W4g-5c: the engine renders a REAL Clipboard panel when the cad_edit
    // flag is on, and App drops this one then. It stays here for the flag-off
    // build, where the reference's row would otherwise lose a panel.
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
