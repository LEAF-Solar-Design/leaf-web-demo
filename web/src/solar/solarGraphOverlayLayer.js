import * as THREE from 'three'

// Unattached, disposable rendering resources for already projected geometry.
export function buildSolarGraphOverlayLayer(polylines) {
  const group = new THREE.Group()
  const batches = new Map()
  const resources = []
  // Size each GPU attribute first so there is no temporary number array or
  // second copy of every vertex when the next saved group appears.
  for (const { color, pts } of polylines) {
    if (!batches.has(color)) batches.set(color, { length: 0, offset: 0 })
    batches.get(color).length += Math.max(0, pts.length - 1) * 6
  }
  for (const batch of batches.values()) {
    batch.positions = new Float32Array(batch.length)
  }
  for (const { color, pts } of polylines) {
    const batch = batches.get(color)
    const { positions } = batch
    let offset = batch.offset
    for (let index = 1; index < pts.length; index += 1) {
      const a = pts[index - 1]
      const b = pts[index]
      positions[offset++] = a[0]
      positions[offset++] = a[1]
      positions[offset++] = 1.5
      positions[offset++] = b[0]
      positions[offset++] = b[1]
      positions[offset++] = 1.5
    }
    batch.offset = offset
  }
  for (const [color, { positions }] of batches) {
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
  }
  let disposed = false
  return { group, dispose: () => {
    if (disposed) return
    disposed = true
    group.removeFromParent()
    group.clear()
    for (const { geometry, material } of resources) {
      geometry.dispose()
      material.dispose()
    }
  } }
}
