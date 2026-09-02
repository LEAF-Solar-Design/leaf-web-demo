// The drafting ribbon (W4c-V1, generalized in W4d): a strip of tool
// clusters across the top of the drawing window, in the utility-CAD cockpit
// grammar. STUDIO-ONLY by construction — App mounts it behind
// `studioGround && groundShowsDrawing`, so the old shell's DOM is
// byte-for-byte without it.
//
// This component RENDERS an ordered cluster list; it decides nothing. The
// clusters are built by web/src/lib/ribbonClusters.js (View, Version,
// Layers, the catalog's families, Author) and by the engine's own consumer
// (web/src/cadedit/EngineRibbonClusters.jsx, passed as children so it can
// read the ONE engine session through context). Every button is a REAL
// command wired to a handler App already owns — nothing auto-runs, nothing
// is a stub.
//
// Honesty: a disabled tool carries its reason on the title AND in the
// accessible name ("(unavailable: …)"); a group that is unavailable as a
// whole says why in a visible note. A greyed control with no reason is the
// gap ToolsPanel's lock-note closed, and the ribbon must not reopen it.
//
// Catalog tools still arm the exact run-decision path the rail's "Review &
// run" uses (onRequestRun -> commitCatalogDecision, source 'ribbon'). NEVER
// dispatchSlash here: that stamps slash provenance into the P2 funnel and
// silently no-ops on gated writes.
import { familyMonogram } from '../lib/surfaceRails.js'

export function RibbonTool({ tool }) {
  const {
    id, label, title = '', reason = '', disabled = false, pressed, write = false, expanded, controls, onClick,
  } = tool
  const unavailable = disabled && reason
  return (
    <button
      type="button"
      className={`ribbon-tool${write ? ' write' : ''}`}
      data-tool={id}
      disabled={disabled}
      title={unavailable ? reason : (title || label)}
      aria-label={unavailable ? `${label} (unavailable: ${reason})` : label}
      aria-pressed={typeof pressed === 'boolean' ? pressed : undefined}
      aria-expanded={typeof expanded === 'boolean' ? expanded : undefined}
      aria-controls={controls || undefined}
      onClick={onClick}
    >
      {label}
    </button>
  )
}

export function RibbonCluster({ id, label, kind = 'group', note = null, extra = null, children }) {
  const attrs = kind === 'family' ? { 'data-family': id } : { 'data-group': id }
  return (
    <div className="ribbon-cluster" role="group" aria-label={label} {...attrs}>
      <span className="ribbon-cluster-label" aria-hidden="true">
        <span className="ribbon-monogram">{familyMonogram(label)}</span>
        {label}
      </span>
      <div className="ribbon-cluster-tools">{children}</div>
      {extra}
      {note && <span className="ribbon-note">{note}</span>}
    </div>
  )
}

export default function DraftingRibbon({ clusters = [], children = null }) {
  const list = Array.isArray(clusters) ? clusters : []
  return (
    <div className="drafting-ribbon" role="toolbar" aria-label="Drafting tools" data-testid="drafting-ribbon">
      {children}
      {list.length === 0 && !children && (
        // Honest empty: a sentence, never a fabricated cluster.
        <span className="ribbon-empty">No tools for this surface yet.</span>
      )}
      {list.map((cluster) => (
        <RibbonCluster
          key={cluster.id}
          id={cluster.id}
          label={cluster.label}
          kind={cluster.kind}
          note={cluster.note}
        >
          {(cluster.tools || []).map((tool) => <RibbonTool key={tool.id} tool={tool} />)}
        </RibbonCluster>
      ))}
    </div>
  )
}
