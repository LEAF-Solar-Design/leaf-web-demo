// Camera math for the Viewer's imperative surface (convergence W3 pre-work:
// the shared shell's status bar, cursor readout and view snaps consume
// unproject/setView/getPose through the Viewer ref).
//
// Pure functions over three.js objects, NO renderer and NO DOM: everything
// here runs under node vitest with a real OrthographicCamera, so the math is
// tested where the component cannot be (Viewer.jsx needs WebGL). Viewer.jsx
// stays a thin wrapper that supplies the camera, the canvas rect and the
// controls target, and calls controls.update() after a mutation.
import * as THREE from 'three'

export function nextFitState(state, event) {
  switch (event) {
    case 'fit': return { fitted: true, interacting: state.interacting }
    case 'start': return { fitted: state.fitted, interacting: true }
    case 'change': return state.interacting ? { fitted: false, interacting: true } : state
    case 'end': return { fitted: state.fitted, interacting: false }
    default: return state
  }
}

export function safeRectCameraAction({ fitted, from, to }) {
  const keys = ['left', 'top', 'width', 'height']
  const valid = (rect) => !!rect && keys.every((key) => Number.isFinite(rect[key]))
  if (!valid(to)) return 'none'
  const hasFrom = valid(from)
  if (hasFrom && keys.every((key) => from[key] === to[key])) return 'none'
  if (fitted) return 'refit'
  return hasFrom ? 'shift' : 'none'
}

/** Fit world bounds into a canvas-local drawing rectangle. */
export function safeFitFrustum({ width, height, safe, bounds, margin = 1.08 }) {
  const rect = safe ?? { left: 0, top: 0, width, height }
  if (!bounds) return null
  const { cx, cy, w, h } = bounds
  if (![width, height, rect.left, rect.top, rect.width, rect.height, cx, cy, w, h, margin].every(Number.isFinite)
    || width <= 0 || height <= 0 || rect.width <= 0 || rect.height <= 0 || (w <= 0 && h <= 0)) return null
  const unitsPerPixel = Math.max(margin * w / rect.width, margin * h / rect.height)
  return {
    halfW: width * unitsPerPixel / 2,
    halfH: height * unitsPerPixel / 2,
    centerX: cx + (width / 2 - rect.left - rect.width / 2) * unitsPerPixel,
    centerY: cy + (rect.top + rect.height / 2 - height / 2) * unitsPerPixel,
    unitsPerPixel,
  }
}

/** Preserve the world point under a moving safe centre at the current scale. */
export function safeCenterShift({ unitsPerPixel, from, to }) {
  if (!from || !to || ![unitsPerPixel, from.left, from.top, from.width, from.height,
    to.left, to.top, to.width, to.height].every(Number.isFinite)) return { dx: 0, dy: 0 }
  return {
    dx: (from.left + from.width / 2 - to.left - to.width / 2) * unitsPerPixel,
    dy: (to.top + to.height / 2 - from.top - from.height / 2) * unitsPerPixel,
  }
}

/** Pixel click aperture in world units, or the default line threshold. */
export function pickLineThreshold(worldPerPixel, px = 6) {
  return Number.isFinite(worldPerPixel) && worldPerPixel > 0 ? worldPerPixel * px : 1
}

// A zero-area rect means the canvas is not laid out (hidden pane, mid-mount):
// every function here answers null rather than NaN. The hidden browser pane
// lies about geometry (innerWidth 0) and NaN coordinates poison every
// downstream toFixed — fail closed on missing layout.
function rectUsable(rect) {
  return !!rect && rect.width > 0 && rect.height > 0
}

/** Client pixel -> normalized device coordinates ({x,y} in [-1,1]) or null. */
export function ndcFromClient(rect, clientX, clientY) {
  if (!rectUsable(rect)) return null
  return {
    x: ((clientX - rect.left) / rect.width) * 2 - 1,
    y: -(((clientY - rect.top) / rect.height) * 2 - 1),
  }
}

/**
 * Client pixel -> the drawing plane (world z=0), or null when the rect is
 * unusable or the ray misses the plane (a camera looking exactly edge-on).
 *
 * Ray-plane intersection, NOT a bare NDC unproject: in sculpture mode the
 * camera is tilted, and unprojecting z=0 NDC lands on the near plane, not the
 * drawing. This is exact in both flat and sculpture poses.
 */
export function unprojectClientToPlane(camera, rect, clientX, clientY) {
  const ndc = ndcFromClient(rect, clientX, clientY)
  if (!ndc) return null
  const raycaster = new THREE.Raycaster()
  raycaster.setFromCamera(new THREE.Vector2(ndc.x, ndc.y), camera)
  const out = new THREE.Vector3()
  const hit = raycaster.ray.intersectPlane(
    new THREE.Plane(new THREE.Vector3(0, 0, 1), 0),
    out,
  )
  return hit ? { x: out.x, y: out.y } : null
}

/**
 * Recenter/zoom the camera while PRESERVING the camera-to-target offset
 * vector, so a sculpture-mode tilt survives a recenter (setting position.x/y
 * naively would flatten the view). Mutates camera and target; the caller owns
 * controls.update(). Returns true when anything changed.
 *
 * pose: { center?: {x, y}, zoom?: number } — absent fields keep their value;
 * zoom must be a finite positive number or it is ignored (a zero/negative
 * zoom inverts the projection and renders nothing).
 */
export function applyViewPose(camera, target, pose) {
  if (!pose || typeof pose !== 'object') return false
  let changed = false
  const { center, zoom } = pose
  if (center && Number.isFinite(center.x) && Number.isFinite(center.y)) {
    const offset = camera.position.clone().sub(target)
    target.set(center.x, center.y, target.z)
    camera.position.copy(target).add(offset)
    changed = true
  }
  if (Number.isFinite(zoom) && zoom > 0 && zoom !== camera.zoom) {
    camera.zoom = zoom
    changed = true
  }
  if (changed) camera.updateProjectionMatrix()
  return changed
}

/**
 * The camera pose as plain data for the status bar and view chips:
 * position/target arrays (4-decimal, matching the dataset test hook), zoom,
 * near/far, and worldPerPixel — the scale readout's input — or null without
 * a usable rect (worldPerPixel is undefined on an unlaid-out canvas).
 */
export function cameraPose(camera, target, rect) {
  if (!rectUsable(rect)) return null
  const round = (value) => Number(value.toFixed(4))
  return {
    position: camera.position.toArray().map(round),
    target: target.toArray().map(round),
    zoom: camera.zoom,
    near: camera.near,
    far: camera.far,
    worldPerPixel: (camera.right - camera.left) / camera.zoom / rect.width,
  }
}
