// Small geometry helpers shared by the mock engine. All operate on the
// intake §1 polyline shape ({ layer, closed, pts:[[x,y,z]...], handle }).

export function centroid(pts) {
  let sx = 0, sy = 0
  for (const p of pts) { sx += p[0]; sy += p[1] }
  return [sx / pts.length, sy / pts.length]
}

// Shoelace area (absolute), in drawing-unit^2.
export function polyArea(pts) {
  let a = 0
  for (let i = 0; i < pts.length; i++) {
    const [x1, y1] = pts[i]
    const [x2, y2] = pts[(i + 1) % pts.length]
    a += x1 * y2 - x2 * y1
  }
  return Math.abs(a) / 2
}

export function bounds(polylines) {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
  for (const pl of polylines) {
    for (const p of pl.pts) {
      if (p[0] < minX) minX = p[0]
      if (p[1] < minY) minY = p[1]
      if (p[0] > maxX) maxX = p[0]
      if (p[1] > maxY) maxY = p[1]
    }
  }
  return { minX, minY, maxX, maxY }
}
