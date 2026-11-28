import * as THREE from 'three'

// Unattached, disposable rendering resources for already projected geometry.
export function buildSolarGraphOverlayLayer(polylines, { x = 0, y = 0 } = {}) {
  const group = new THREE.Group()
  const batches = new Map()
  const resources = []
  const pickBatches = new Map()
  const routes = new Map()
  // Size each GPU attribute first so there is no temporary number array or
  // second copy of every vertex when the next saved group appears.
  for (const { color, pts } of polylines) {
    if (!batches.has(color)) batches.set(color, { length: 0, offset: 0, routeStarts: new Map() })
    batches.get(color).length += Math.max(0, pts.length - 1) * 6
  }
  for (const batch of batches.values()) {
    batch.positions = new Float32Array(batch.length)
  }
  for (const { color, pts, route } of polylines) {
    const batch = batches.get(color)
    const { positions } = batch
    const start = batch.offset
    let offset = start
    for (let index = 1; index < pts.length; index += 1) {
      const a = pts[index - 1]
      const b = pts[index]
      positions[offset++] = a[0] - x
      positions[offset++] = a[1] - y
      positions[offset++] = 1.5
      positions[offset++] = b[0] - x
      positions[offset++] = b[1] - y
      positions[offset++] = 1.5
    }
    batch.offset = offset
    if (route && offset > start) {
      // Three reports the segment's first vertex index for non-indexed LineSegments.
      for (let segment = start; segment < offset; segment += 6) batch.routeStarts.set(segment / 3, route.id)
      routes.set(route.id, { positions, start, end: offset })
    }
  }
  for (const [color, { positions, routeStarts }] of batches) {
    const geometry = new THREE.BufferGeometry()
    const attribute = new THREE.BufferAttribute(positions, 3)
    attribute.setUsage(THREE.StaticDrawUsage)
    geometry.setAttribute('position', attribute)
    const material = new THREE.LineBasicMaterial({ color, depthTest: false,
      depthWrite: false, transparent: false, opacity: 1 })
    const mesh = new THREE.LineSegments(geometry, material)
    mesh.renderOrder = 8
    group.add(mesh)
    resources.push({ geometry, material })
    if (routeStarts.size) pickBatches.set(mesh, routeStarts)
  }
  const pickObjects = [...pickBatches.keys()]
  let selected = null
  let highlight = null
  function disposeHighlight() {
    if (!highlight) return
    highlight.removeFromParent()
    highlight.geometry.dispose()
    highlight.material.dispose()
    highlight = null
  }
  function selectRoute(id) {
    if (disposed || id === selected) return
    disposeHighlight()
    selected = id
    const route = routes.get(id)
    if (!route) return
    const { positions, start, end } = route
    const geometry = new THREE.BufferGeometry()
    geometry.setAttribute('position', new THREE.BufferAttribute(positions.slice(start, end), 3))
    const material = new THREE.LineBasicMaterial({ color: '#fef08a', depthTest: false, depthWrite: false })
    highlight = new THREE.LineSegments(geometry, material)
    highlight.renderOrder = 10
    group.add(highlight)
  }
  let disposed = false
  return { group, pickObjects, selectRoute, routesFromHits: hits => {
    const ids = new Set()
    for (const hit of hits) {
      const id = pickBatches.get(hit.object)?.get(hit.index)
      if (id != null) ids.add(id)
    }
    return [...ids]
  }, dispose: () => {
    if (disposed) return
    disposed = true
    disposeHighlight()
    group.removeFromParent()
    group.clear()
    for (const { geometry, material } of resources) {
      geometry.dispose()
      material.dispose()
    }
  } }
}
