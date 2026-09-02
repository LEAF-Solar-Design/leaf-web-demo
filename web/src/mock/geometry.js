// Small geometry helpers shared by the mock engine. All operate on the
// intake §1 polyline shape ({ layer, closed, pts:[[x,y,z]...], handle }).
//
// polyArea moved to lib/entityMetrics.js (W4c-V2: the properties dock
// renders the same shoelace the mock engine trusts) and is re-exported so
// every existing mock import keeps working.
export { polyArea } from '../lib/entityMetrics.js'

export function centroid(pts) {
  let sx = 0, sy = 0
  for (const p of pts) { sx += p[0]; sy += p[1] }
  return [sx / pts.length, sy / pts.length]
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
