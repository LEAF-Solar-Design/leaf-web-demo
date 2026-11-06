// Match bridgeIntake: Z must already be a finite number, without coercion.
export const vertexZ = point => Number.isFinite(point?.[2]) ? point[2] : 0

// Keep the sampler's XYZ, including its existing nonplanar bulge contract.
export function vertexSegments(pts, closed) {
  const positions = []
  for (let i = 0; i < pts.length - (closed ? 0 : 1); i++) {
    const a = pts[i], b = pts[(i + 1) % pts.length]
    positions.push(a[0], a[1], vertexZ(a), b[0], b[1], vertexZ(b))
  }
  return positions
}
