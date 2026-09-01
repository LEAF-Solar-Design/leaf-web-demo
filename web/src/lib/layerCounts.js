// Counts every entity (polylines + inserts + 3D faces) per layer, seeding
// every known layer to 0 first so insert/face-only layers (e.g. the
// ?fixture=edit Blocks/Surfaces layers) never read as a false 0 in the
// legend.
//
// Extracted from App.jsx's `layerCounts` useMemo (~line 1151) and
// site/ToolCast.jsx's `layerCounts` useMemo (~line 403) — identical counting
// logic (App.jsx looped each collection separately; ToolCast looped one
// concatenated array; both produce the same counts), so no caller-differing
// option is needed.
export function countEntitiesByLayer(shown) {
  const counts = {}
  for (const layer of shown?.layers || []) counts[layer] = 0
  for (const polyline of shown?.polylines || []) counts[polyline.layer] = (counts[polyline.layer] || 0) + 1
  for (const insert of shown?.inserts || []) counts[insert.layer] = (counts[insert.layer] || 0) + 1
  for (const face of shown?.faces3d || []) counts[face.layer] = (counts[face.layer] || 0) + 1
  return counts
}
