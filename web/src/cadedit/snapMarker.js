// B1b: the object snap marker's glyph per snap kind, as pure geometry. Each
// glyph is a set of unit paths (corners at -1..1) scaled by half the marker
// size about the snapped point; every adjacent pair of a path is one segment.
// Fails closed: a missing or non-finite point, a nonpositive or non-finite
// size, or a kind outside SNAP_MODES draws nothing. Allocates only the
// returned segments (called when the snap changes, never per frame).
import { SNAP_MODES } from './snapModes.js'

// Sixteen points round the unit circle, closed on the first.
const RING = Object.freeze([
  ...Array.from({ length: 16 }, (_, i) => Object.freeze([Math.cos(i * Math.PI / 8), Math.sin(i * Math.PI / 8)])),
  Object.freeze([1, 0]),
])

const GLYPHS = Object.freeze({
  endpoint: [[[-1, -1], [1, -1], [1, 1], [-1, 1], [-1, -1]]],
  midpoint: [[[-1, -1], [1, -1], [0, 1], [-1, -1]]],
  centre: [RING],
  quadrant: [[[0, 1], [1, 0], [0, -1], [-1, 0], [0, 1]]],
  intersection: [[[-1, -1], [1, 1]], [[-1, 1], [1, -1]]],
  perpendicular: [[[-1, 1], [-1, -1], [1, -1]], [[-1, 0], [0, 0], [0, -1]]],
  tangent: [RING, [[-1, 1], [1, 1]]],
  nearest: [[[-1, -1], [1, 1], [-1, 1], [1, -1], [-1, -1]]],
  insertion: [[[-1, 0], [1, 0]], [[0, -1], [0, 1]]],
})

const KINDS = new Set(SNAP_MODES.map((mode) => mode.kind))

/** { x, y, kind } and a size in world units -> [[[x1, y1], [x2, y2]], ...]; [] when invalid. */
export function snapMarkerSegments(pt, size = 1) {
  if (!pt || typeof pt !== 'object' || ![pt.x, pt.y, size].every(Number.isFinite) || size <= 0 || !KINDS.has(pt.kind)) return []
  const h = size / 2
  const at = ([ux, uy]) => [pt.x + h * ux, pt.y + h * uy]
  const out = []
  for (const path of GLYPHS[pt.kind]) {
    for (let i = 1; i < path.length; i += 1) out.push([at(path[i - 1]), at(path[i])])
  }
  return out
}
