// Pure geometry for the world-space lane. Camera offsets are screen units.
function finite(value, label) {
  if (!Number.isFinite(value)) throw new TypeError(`${label} must be finite`)
}

function point(value, label) {
  finite(value?.x, `${label}.x`)
  finite(value?.y, `${label}.y`)
}

function camera(value) {
  point(value, 'camera')
  finite(value?.zoom, 'camera.zoom')
  if (value.zoom <= 0) throw new RangeError('camera.zoom must be positive')
}

function rect(value) {
  point(value, 'rect')
  finite(value?.width, 'rect.width')
  finite(value?.height, 'rect.height')
  if (value.width < 0 || value.height < 0) throw new RangeError('Rectangle dimensions must be nonnegative')
}

export function worldToScreen(value, view) {
  point(value, 'point')
  camera(view)
  return { x: value.x * view.zoom + view.x, y: value.y * view.zoom + view.y }
}

export function screenToWorld(value, view) {
  point(value, 'point')
  camera(view)
  return { x: (value.x - view.x) / view.zoom, y: (value.y - view.y) / view.zoom }
}

export function boundsOfRects(rects) {
  if (!Array.isArray(rects)) throw new TypeError('rects must be an array')
  if (!rects.length) return null
  let left = Infinity
  let top = Infinity
  let right = -Infinity
  let bottom = -Infinity
  for (const value of rects) {
    rect(value)
    left = Math.min(left, value.x)
    top = Math.min(top, value.y)
    right = Math.max(right, value.x + value.width)
    bottom = Math.max(bottom, value.y + value.height)
  }
  return { x: left, y: top, width: right - left, height: bottom - top }
}

export function fitCameraToBounds(bounds, viewport, padding = 0) {
  finite(viewport?.width, 'viewport.width')
  finite(viewport?.height, 'viewport.height')
  if (viewport.width <= 0 || viewport.height <= 0) throw new RangeError('Viewport dimensions must be positive')
  finite(padding, 'padding')
  if (padding < 0 || padding * 2 >= viewport.width || padding * 2 >= viewport.height) {
    throw new RangeError('Padding must leave positive viewport dimensions')
  }
  if (bounds == null || (Array.isArray(bounds) && bounds.length === 0)) {
    return { x: 0, y: 0, zoom: 1 }
  }
  rect(bounds)
  const zoom = bounds.width === 0 && bounds.height === 0
    ? 1
    : Math.min(
      bounds.width > 0 ? (viewport.width - padding * 2) / bounds.width : Infinity,
      bounds.height > 0 ? (viewport.height - padding * 2) / bounds.height : Infinity,
    )
  return {
    x: viewport.width / 2 - (bounds.x + bounds.width / 2) * zoom,
    y: viewport.height / 2 - (bounds.y + bounds.height / 2) * zoom,
    zoom,
  }
}
