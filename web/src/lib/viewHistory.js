// The drawing viewer's navigation history (S3, navigation profile rules R4
// "Exit: Back versus Up" and R13 "Restore complete navigation state").
//
// Each entry is a full navigation snapshot: the camera pose (the Viewer's
// getPose() result), focus, selection, query, and a COPY of the layer visibility
// map, so Back returns to exactly what was shown. This is navigation, never an
// edit history: engine undo and version undo stay separate.
//
// Bounded: at most `limit` entries (default 50); a push past the bound drops
// the oldest, so memory is O(limit) however long a session navigates. A null
// or malformed pose (the viewer before layout) is never pushed: a snapshot
// with no camera could not be restored.
export const VIEW_HISTORY_LIMIT = 50

/** World bounds {minX, minY, maxX, maxY} of every entity on one layer of an intake: polyline `pts`, insert
 *  `pt` and 3D face `p1..p4`, the same sources the Viewer fits to (Viewer.jsx). One pass per entity list, no
 *  allocation per point. A zero width or height is padded to 1 unit so a single point still frames. Null for
 *  a null layer, a missing intake, or a layer with no geometry. Lives here, not in viewerMath.js, so the main
 *  chunk never imports three.js (the Viewer is lazy). */
export function layerBounds(intake, layer) {
  if (layer == null || !intake || typeof intake !== 'object') return null
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
  const grow = (p) => {
    if (!Array.isArray(p) || !Number.isFinite(p[0]) || !Number.isFinite(p[1])) return
    if (p[0] < minX) minX = p[0]
    if (p[0] > maxX) maxX = p[0]
    if (p[1] < minY) minY = p[1]
    if (p[1] > maxY) maxY = p[1]
  }
  for (const pl of intake.polylines || []) if (pl?.layer === layer) for (const p of pl.pts || []) grow(p)
  for (const ins of intake.inserts || []) if (ins?.layer === layer) grow(ins.pt)
  for (const f of intake.faces3d || []) {
    if (f?.layer !== layer) continue
    grow(f.p1); grow(f.p2); grow(f.p3); grow(f.p4)
  }
  if (!Number.isFinite(minX) || !Number.isFinite(minY)) return null
  if (maxX - minX <= 0) { minX -= 0.5; maxX += 0.5 }
  if (maxY - minY <= 0) { minY -= 0.5; maxY += 0.5 }
  return { minX, minY, maxX, maxY }
}

function poseUsable(pose) {
  return !!pose && typeof pose === 'object'
    && Array.isArray(pose.target) && Number.isFinite(pose.target[0]) && Number.isFinite(pose.target[1])
}

export function createViewHistory(limit = VIEW_HISTORY_LIMIT) {
  const bound = Number.isInteger(limit) && limit > 0 ? limit : VIEW_HISTORY_LIMIT
  const entries = []
  return {
    /** Push one snapshot; returns false (and stores nothing) for a null pose. */
    push({ pose, selectedHandle = null, selectedHandles, focusedId = null, query = '', drawingKey = null, visibleLayers = {} } = {}) {
      if (!poseUsable(pose)) return false
      entries.push({
        pose: { ...pose, target: [...pose.target], position: Array.isArray(pose.position) ? [...pose.position] : pose.position },
        selectedHandle: selectedHandle ?? null,
        selectedHandles: Array.isArray(selectedHandles) ? [...selectedHandles] : selectedHandle == null ? [] : [selectedHandle],
        focusedId,
        query,
        drawingKey,
        visibleLayers: { ...(visibleLayers || {}) },
      })
      if (entries.length > bound) entries.splice(0, entries.length - bound)
      return true
    },
    /** Remove and return the newest snapshot, or null when empty. */
    pop() {
      return entries.length ? entries.pop() : null
    },
    /** The newest snapshot without removing it, or null when empty. */
    peek() {
      return entries.length ? entries[entries.length - 1] : null
    },
    clear() {
      entries.length = 0
    },
    size() {
      return entries.length
    },
  }
}
