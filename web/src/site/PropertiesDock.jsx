// The right palette (W4c-V2): the utility-CAD properties dock. STUDIO-ONLY
// by construction (App mounts it behind studioGround && groundShowsDrawing);
// rail OFF renders the same Legend and SelectionReadout inline, byte-for-
// byte, because this dock HOSTS those exact elements rather than re-
// implementing them - one source of truth for every field (ACCEPTANCE: "the
// properties dock as a grown SelectionReadout").
//
// READ-ONLY structurally: the dock consumes render slots and a derived
// geometry record. It calls no engine, no store action, and offers no edit
// affordance - ENV_CAD_EDIT ships =1 everywhere, so read-only must be a
// property of the component, never of a flag.
//
// The engine-truth line ("re-parsed from written bytes") is DELIBERATELY
// absent: rendering it here would need a second useEngineSession (a second
// worker - forbidden, engineSession.js header), and the console drawing is
// server-loaded, which ACCEPTANCE's engine-ownership rule excludes from the
// readout regardless. Owned by the W3 tail's session provider.
import { useState } from 'react'

import { formatUnits } from '../lib/entityMetrics.js'

function DockSection({ title, children, defaultOpen = true }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <section className={`dock-section${open ? '' : ' collapsed'}`}>
      <button
        type="button"
        className="dock-section-head"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        {title}
        <span className="dock-chev" aria-hidden="true">{open ? '−' : '+'}</span>
      </button>
      {open && <div className="dock-section-body">{children}</div>}
    </section>
  )
}

// The Geometry rows for the selected intake entity: client-derived truth
// only (vertex count, closure, length, area-for-closed; insert pose; face
// corners). '—' for anything non-finite - a poisoned vertex never renders
// as NaN.
export function GeometryRows({ geometry }) {
  if (!geometry) return null
  return (
    <dl className="dock-geometry" data-testid="dock-geometry">
      {'vertices' in geometry && (
        <>
          <dt>Vertices</dt><dd>{geometry.vertices}</dd>
          <dt>Closed</dt><dd>{geometry.closed ? 'yes' : 'no'}</dd>
          <dt>{geometry.closed ? 'Perimeter' : 'Length'}</dt><dd>{formatUnits(geometry.length)} u</dd>
          {geometry.area != null && (<><dt>Area</dt><dd>{formatUnits(geometry.area)} u²</dd></>)}
        </>
      )}
      {'position' in geometry && (
        <>
          {geometry.position && (
            <><dt>Position</dt><dd>{formatUnits(geometry.position[0])}, {formatUnits(geometry.position[1])}</dd></>
          )}
          {geometry.rotation != null && (<><dt>Rotation</dt><dd>{formatUnits(geometry.rotation, 1)}°</dd></>)}
          {geometry.scale && (
            <><dt>Scale</dt><dd>{geometry.scale.map((s) => formatUnits(s, 2)).join(' · ')}</dd></>
          )}
        </>
      )}
      {'corners' in geometry && (<><dt>Corners</dt><dd>{geometry.corners}</dd></>)}
    </dl>
  )
}

// The document's own facts (W4e round 2): the reference's pane is dense
// label | field rows from top to bottom, and ours read as an unfinished panel
// while nothing was selected. Client-derived truth only: the counts the
// intake carries and the extents computed from its vertices; '—' for
// anything absent, never an invented number.
export function DrawingRows({ drawing }) {
  if (!drawing) return null
  const n = (v) => (Number.isFinite(v) ? v.toLocaleString() : '—')
  const u = (v) => (Number.isFinite(v) ? `${formatUnits(v)} u` : '—')
  return (
    <dl className="dock-drawing" data-testid="dock-drawing">
      <dt>Name</dt><dd title={drawing.name || ''}>{drawing.name || '—'}</dd>
      <dt>Entities</dt><dd>{n(drawing.entities)}</dd>
      <dt>Polylines</dt><dd>{n(drawing.polylines)}</dd>
      <dt>Block inserts</dt><dd>{n(drawing.inserts)}</dd>
      <dt>3D faces</dt><dd>{n(drawing.faces)}</dd>
      <dt>Layers</dt><dd>{n(drawing.layers)}</dd>
      <dt>Layers shown</dt><dd>{n(drawing.layersShown)}</dd>
      <dt>Extents X</dt><dd>{drawing.extents ? `${formatUnits(drawing.extents.minX)} … ${formatUnits(drawing.extents.maxX)}` : '—'}</dd>
      <dt>Extents Y</dt><dd>{drawing.extents ? `${formatUnits(drawing.extents.minY)} … ${formatUnits(drawing.extents.maxY)}` : '—'}</dd>
      <dt>Width</dt><dd>{u(drawing.extents ? drawing.extents.maxX - drawing.extents.minX : NaN)}</dd>
      <dt>Height</dt><dd>{u(drawing.extents ? drawing.extents.maxY - drawing.extents.minY : NaN)}</dd>
      <dt>Source</dt><dd>{drawing.source || '—'}</dd>
    </dl>
  )
}

// Extents over the intake's vertices: one pass, tolerant of [x, y] pairs and
// {x, y} points, null when nothing finite was seen. Bounded by the intake.
export function drawingExtents(polylines) {
  const list = Array.isArray(polylines) ? polylines : []
  let minX = Infinity; let minY = Infinity; let maxX = -Infinity; let maxY = -Infinity
  for (const entity of list) {
    const pts = Array.isArray(entity?.pts) ? entity.pts : []
    for (const pt of pts) {
      const x = Array.isArray(pt) ? pt[0] : pt?.x
      const y = Array.isArray(pt) ? pt[1] : pt?.y
      if (!Number.isFinite(x) || !Number.isFinite(y)) continue
      if (x < minX) minX = x
      if (x > maxX) maxX = x
      if (y < minY) minY = y
      if (y > maxY) maxY = y
    }
  }
  return Number.isFinite(minX) && Number.isFinite(minY) ? { minX, minY, maxX, maxY } : null
}

// The pane's title row (W4e round 3): the reference's pane carries a close
// control at its right; ours is real (App unmounts the pane and the canvas
// takes the column) and the View tab's Properties tool brings it back. No
// pin: there is no auto-hide behaviour to pin, so a pin would be a dead control.
export default function PropertiesDock({ layers, selection, geometry, plan = null, drawing = null, onClose = null }) {
  return (
    <aside className="properties-dock" aria-label="Properties" data-testid="properties-dock">
      <div className="dock-title">
        <span>Properties</span>
        {onClose && (
          <button type="button" className="dock-close" aria-label="Close the properties pane" title="Close (View tab: Properties brings it back)" onClick={onClose}>×</button>
        )}
      </div>
      <DockSection title="Layers">{layers}</DockSection>
      {drawing && <DockSection title="Drawing"><DrawingRows drawing={drawing} /></DockSection>}
      <DockSection title="Selection">
        {selection}
        <GeometryRows geometry={geometry} />
      </DockSection>
      {/* What this plan includes: reference information, folded away by
          default. It was a full-width slab across the drawing before the
          cockpit (the single largest object on the page); hosted here it
          stays reachable without owning the viewport. */}
      {plan && <DockSection title="Plan" defaultOpen={false}>{plan}</DockSection>}
    </aside>
  )
}
