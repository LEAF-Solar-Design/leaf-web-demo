// Resolves a picked entity handle against an intake's entity collections
// (polylines, inserts, 3D faces) into a small descriptor for callers like
// SelectionReadout.
//
// Extracted from App.jsx's `selection` useMemo (~line 1161) and
// site/ToolCast.jsx's `selectedEntity` (~line 197). The two differ on an
// unresolved handle (a valid intake with no entity matching it): App.jsx
// falls back to a generic `{ kind: 'entity', layer: null }` descriptor (so
// the readout still shows something is selected); ToolCast returns null (so
// an unresolved id renders no readout at all). Preserved verbatim per caller
// via the `onUnresolved` option (default: null, matching ToolCast's
// original). Both originals also short-circuit to null before touching the
// intake when there is no handle or no intake at all — that early return is
// NOT affected by onUnresolved, matching both originals exactly.
export function selectEntity(intake, handle, { onUnresolved = () => null } = {}) {
  if (!handle || !intake) return null
  const polyline = intake.polylines?.find((entity) => entity.handle === handle)
  if (polyline) return { handle, kind: 'polyline', layer: polyline.layer }
  const insert = intake.inserts?.find((entity) => entity.handle === handle)
  if (insert) return { handle, kind: 'insert', layer: insert.layer, name: insert.name }
  const face = intake.faces3d?.find((entity) => entity.handle === handle)
  if (face) return { handle, kind: '3dface', layer: face.layer }
  return onUnresolved(handle)
}
