/**
 * EngineDockProperties — the dock's Color/Linetype/Lineweight rows.
 *
 * W4g-7b-03c-d: a proof finding on PR #1121. App mounts EngineSessionProvider
 * but its own component body sits OUTSIDE the subtree that provider wraps, so
 * a hook call there always read null and the rows never rendered. This is a
 * CONSUMER, mounted INSIDE the provider (App.jsx, beside CadEditSurface): it
 * reads the ONE engine session through context and portals the rows into the
 * slot div App renders (id `cockpit-dock-properties-slot`), the same
 * re-resolving `useSlot` idiom EngineRibbonClusters.jsx already uses for the
 * Properties seat, because the slot can unmount and remount as a NEW node.
 *
 * It constructs no boundary and names no worker path (license fence;
 * engineOwnership.test.js counts consumer shapes).
 *
 * Renders nothing when the slot is absent (flag-off build, no cockpit, or the
 * slot has not mounted yet) or the engine holds no selection, honest-empty
 * like every other row.
 */
import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'

import { useEngineSessionOptional } from './EngineSessionProvider.jsx'
import { formatColor, formatLineweight } from './engineSession.js'
import { formatMeasurement } from './engineIntake.js'

export const DOCK_PROPERTIES_SLOT_ID = 'cockpit-dock-properties-slot'

// See EngineRibbonClusters.jsx's useSlot for why this re-resolves every
// render instead of once at mount.
function useSlot(id) {
  const [node, setNode] = useState(null)
  useEffect(() => {
    if (typeof document === 'undefined') return undefined
    const found = document.getElementById(id) || null
    setNode((prev) => (prev === found ? prev : found))
    return undefined
  })
  return node
}

export default function EngineDockProperties() {
  const engine = useEngineSessionOptional()
  const slot = useSlot(DOCK_PROPERTIES_SLOT_ID)
  const session = engine?.session ?? null
  const entity = session
    ? (session.entities || []).find((e) => e.id === session.selectedId) || null
    : null
  if (!slot || !entity) return null
  // W4g-7b-04c: a selected DIMENSION's measurement, read-only, through this
  // same slot idiom (the dock's Geometry section otherwise has no field for
  // it: entityGeometry knows nothing of a DIMENSION's projection).
  const isDimension = entity.type === 'DIMENSION' && Number.isFinite(entity.measurement)
  return createPortal(
    <dl className="dock-properties" data-testid="dock-properties">
      <dt>Color</dt><dd>{formatColor(entity)}</dd>
      <dt>Linetype</dt><dd>{typeof entity.linetype === 'string' && entity.linetype ? entity.linetype : 'ByLayer'}</dd>
      <dt>Lineweight</dt><dd>{formatLineweight(Number.isFinite(entity.lineweight) ? entity.lineweight : -1)}</dd>
      {isDimension && <><dt>Measurement</dt><dd>{formatMeasurement(entity.measurement)}</dd></>}
    </dl>,
    slot,
  )
}
