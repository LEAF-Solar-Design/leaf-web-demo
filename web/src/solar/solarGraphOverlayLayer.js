import * as THREE from 'three'

// Unattached, disposable rendering resources for already projected geometry.
export function buildSolarGraphOverlayLayer(polylines) {
  const group = new THREE.Group()
  const batches = new Map()
  const resources = []
  for (const { color, pts } of polylines) {
    if (!batches.has(color)) batches.set(color, [])
    const positions = batches.get(color)
    for (let index = 1; index < pts.length; index += 1) {
      const a = pts[index - 1]
      const b = pts[index]
      positions.push(a[0], a[1], 1.5, b[0], b[1], 1.5)
    }
  }
  for (const [color, positions] of batches) {
    const geometry = new THREE.BufferGeometry()
    const attribute = new THREE.Float32BufferAttribute(positions, 3)
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
