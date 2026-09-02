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

export default function PropertiesDock({ layers, selection, geometry }) {
  return (
    <aside className="properties-dock" aria-label="Properties" data-testid="properties-dock">
      <DockSection title="Layers">{layers}</DockSection>
      <DockSection title="Selection">
        {selection}
        <GeometryRows geometry={geometry} />
      </DockSection>
    </aside>
  )
}
