export const finiteXY = point => Array.isArray(point)
  && Number.isFinite(point[0]) && Number.isFinite(point[1])

// Only render coordinates cross this frame. Intake and pick descriptors stay in WCS.
export function drawingOrigin(polylines = [], inserts = [], faces = []) {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
  const grow = point => {
    if (!finiteXY(point)) return
    minX = Math.min(minX, point[0]); minY = Math.min(minY, point[1])
    maxX = Math.max(maxX, point[0]); maxY = Math.max(maxY, point[1])
  }
  for (const polyline of polylines) for (const point of polyline.pts || []) grow(point)
  for (const insert of inserts) grow(insert.pt)
  for (const face of faces) for (const point of [face.p1, face.p2, face.p3, face.p4]) grow(point)
  return Number.isFinite(minX)
    ? { x: minX / 2 + maxX / 2, y: minY / 2 + maxY / 2 }
    : { x: 0, y: 0 }
}

// Subtract in double precision, before a caller allocates a Float32 buffer.
// Reject an invalid buffer as a whole rather than joining unrelated segments.
export function localPositions(positions, origin) {
  const local = []
  for (let i = 0; i < positions.length; i += 3) {
    const x = positions[i] - origin.x, y = positions[i + 1] - origin.y
    if (!Number.isFinite(Math.fround(x)) || !Number.isFinite(Math.fround(y))) return []
    local.push(x, y, positions[i + 2])
  }
  return local
}
