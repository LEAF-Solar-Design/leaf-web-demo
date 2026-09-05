// The drafting ribbon (W4c-V1, generalized in W4d, re-seated in W4e): a
// strip of tool panels across the top of the drawing window, in the
// reference CAD grammar. STUDIO-ONLY by construction — App mounts it behind
// `studioGround && groundShowsDrawing`, so the old shell's DOM is
// byte-for-byte without it.
//
// This component RENDERS an ordered cluster list; it decides nothing. The
// clusters are built by web/src/lib/ribbonClusters.js (View, Version,
// Layers, the catalog's families, Author, the reference's panels) and by the
// engine's own consumer (web/src/cadedit/EngineRibbonClusters.jsx, passed as
// children so it can read the ONE engine session through context). Every
// enabled button is a REAL command wired to a handler App already owns.
//
// GRAMMAR (W4e, the reference's own constants, ROW_H = 26): a panel is a
// 3-row column-flow grid of tools with its label on the row underneath. A
// `large` tool is a 39px icon over a two-line 10px label and spans the three
// rows; a `small` tool is an 18px icon in a 26px square and carries its
// label on the title and accessible name; a `row` tool (layers) is a 150px
// row with a bulb, a swatch, and the name. Icons are CockpitIcon keys
// (icons8, one sprite); a key the sprite lacks renders a two-letter glyph.
//
// Honesty: a disabled tool carries its reason on the title AND in the
// accessible name ("(unavailable: …)"); a panel that is unavailable as a
// whole keeps the sentence in the DOM (.ribbon-note, read by tests and
// assistive tech) and shows it as the amber label's title. A greyed control
// with no reason is the gap ToolsPanel's lock-note closed, and the ribbon
// must not reopen it.
//
// Catalog tools still arm the exact run-decision path the rail's "Review &
// run" uses (onRequestRun -> commitCatalogDecision, source 'ribbon'). NEVER
// dispatchSlash here: that stamps slash provenance into the P2 funnel and
// silently no-ops on gated writes.
import { useLayoutEffect, useRef } from 'react'

import { accessibleName } from '../lib/actionRegistry.js'
import { formatElementId } from '../lib/elementIdentity.js'
import { familyMonogram } from '../lib/surfaceRails.js'
import CockpitIcon from './CockpitIcon.jsx'

// The band's measured height, published on the workspace card as a CSS
// variable so the floating import pane clears it (landing.css). jsdom has no
// ResizeObserver; the one-shot measurement still runs there, so the variable
// always exists.
export const RIBBON_HEIGHT_VAR = '--cockpit-ribbon-h'

function useBandHeight(ref) {
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return undefined
    const host = el.closest('.workspace-card') || el.parentElement
    if (!host) return undefined
    const publish = () => { host.style.setProperty(RIBBON_HEIGHT_VAR, `${Math.round(el.offsetHeight)}px`) }
    publish()
    if (typeof ResizeObserver === 'undefined') return () => host.style.removeProperty(RIBBON_HEIGHT_VAR)
    const observer = new ResizeObserver(publish)
    observer.observe(el)
    return () => {
      observer.disconnect()
      host.style.removeProperty(RIBBON_HEIGHT_VAR)
    }
  }, [ref])
}

export function RibbonTool({ tool }) {
  const {
    id, label, text = '', icon = '', size = 'small', swatch = '',
    title = '', reason = '', disabled = false, pressed, write = false, expanded, controls, onClick,
  } = tool
  const unavailable = disabled && reason
  return (
    <button
      type="button"
      className={`ribbon-tool${write ? ' write' : ''}`}
      data-tool={id}
      data-element-id={formatElementId('tool', id) || undefined}
      data-size={size}
      disabled={disabled}
      title={unavailable ? reason : (title || label)}
      aria-label={accessibleName(label, unavailable ? reason : '')}
      aria-pressed={typeof pressed === 'boolean' ? pressed : undefined}
      aria-expanded={typeof expanded === 'boolean' ? expanded : undefined}
      aria-controls={controls || undefined}
      onClick={onClick}
    >
      <CockpitIcon id={icon} fallback={text || label} size={size} />
      {swatch ? <span className="ribbon-swatch" style={{ background: swatch }} aria-hidden="true" /> : null}
      <span className="ribbon-tool-label">{text || label}</span>
    </button>
  )
}

// A panel widget that is not a command: the reference's ByLayer combos.
// Disabled widgets say why on their title, like disabled tools.
export function RibbonWidget({ widget }) {
  const { id, label, value = '', options = [], disabled = false, reason = '', onChange } = widget
  const unavailable = disabled && reason
  return (
    <label className="ribbon-widget" data-widget={id} title={unavailable ? reason : label}>
      <span className="ribbon-note">{label}</span>
      <select
        aria-label={accessibleName(label, unavailable ? reason : '')}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange?.(event.target.value)}
      >
        {(options.length ? options : [value]).map((opt) => <option key={opt} value={opt}>{opt}</option>)}
      </select>
    </label>
  )
}

export function RibbonCluster({ id, label, kind = 'group', note = null, extra = null, widgets = [], onLabelClick = null, labelTitle = '', children }) {
  const attrs = kind === 'family' ? { 'data-family': id } : { 'data-group': id }
  if (note) attrs['data-note'] = 'true'
  const labelBody = (
    <>
      <span className="ribbon-monogram">{familyMonogram(label)}</span>
      {label}
    </>
  )
  const title = note ? `${label}: ${note}` : (labelTitle || (onLabelClick ? `Open ${label} in the tool rail` : ''))
  return (
    <div className="ribbon-cluster" role="group" aria-label={label} {...attrs}>
      <div className="ribbon-cluster-tools">
        {children}
        {/* W4g-4b: a seat's slot (the Clipboard, Script and Properties seats
            the engine consumer portals into) sits ON the tools row, between
            the tools and the widgets. Rendered after the row it was a third
            grid item under the panel label, off the band. */}
        {extra}
        {widgets.length > 0 && (
          <div className="ribbon-widgets">
            {widgets.map((widget) => <RibbonWidget key={widget.id} widget={widget} />)}
          </div>
        )}
      </div>
      {/* The panel label sits on the row under the tools, the reference
          grammar. A family label is a real command (open that family in
          the rail); a fixed group's label is decoration, hidden from
          assistive tech because the group already carries the name. The
          note is the sentence that says why a whole panel is unavailable. */}
      {onLabelClick ? (
        <button
          type="button"
          className="ribbon-cluster-label as-button"
          title={title || `Open ${label} in the tool rail`}
          aria-label={labelTitle || `Open ${label} in the tool rail`}
          onClick={onLabelClick}
        >
          {labelBody}
        </button>
      ) : (
        <span className="ribbon-cluster-label" aria-hidden="true" title={title || undefined}>{labelBody}</span>
      )}
      {note && <span className="ribbon-note">{note}</span>}
    </div>
  )
}

export default function DraftingRibbon({ clusters = [], tab = 'draw', children = null }) {
  const list = Array.isArray(clusters) ? clusters : []
  const ref = useRef(null)
  useBandHeight(ref)
  return (
    <div
      ref={ref}
      id="drafting-ribbon"
      className="drafting-ribbon"
      role="toolbar"
      aria-label="Drafting tools"
      data-testid="drafting-ribbon"
      data-tab={tab}
    >
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
          widgets={cluster.widgets || []}
          onLabelClick={cluster.onLabelClick || null}
          labelTitle={cluster.labelTitle || ''}
          extra={cluster.extra || null}
        >
          {(cluster.tools || []).map((tool) => <RibbonTool key={tool.id} tool={tool} />)}
        </RibbonCluster>
      ))}
    </div>
  )
}
