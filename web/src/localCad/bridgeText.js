import typeface from 'three/examples/fonts/helvetiker_regular.typeface.json' with { type: 'json' }

// Sample the typeface data directly so text cannot pull three's shape machinery
// into the eagerly loaded viewer chunk. Coordinates here are for height 1.
function glyphContours(glyph) {
  const commands = glyph.o?.split(' ') ?? []
  const scale = 1 / 1.013 / typeface.resolution
  const contours = []
  let contour, current
  let index = 0
  const readPoint = () => [Number(commands[index++]) * scale, Number(commands[index++]) * scale]
  const append = p => {
    const last = contour.at(-1)
    if (!last || last[0] !== p[0] || last[1] !== p[1]) contour.push(p)
  }
  while (index < commands.length) {
    const command = commands[index++]
    if (command === 'm') {
      current = readPoint()
      contour = []
      contours.push(contour)
    } else if (command === 'l') {
      append(current)
      current = readPoint()
      append(current)
    } else if (command === 'q' || command === 'b') {
      const end = readPoint()
      const c1 = readPoint()
      const c2 = command === 'b' ? readPoint() : null
      append(current)
      for (let step = 1; step <= 4; step += 1) {
        const t = step / 4, u = 1 - t
        append([0, 1].map(axis => c2
          ? u * u * u * current[axis] + 3 * u * u * t * c1[axis]
            + 3 * u * t * t * c2[axis] + t * t * t * end[axis]
          : u * u * current[axis] + 2 * u * t * c1[axis] + t * t * end[axis]))
      }
      current = end
    }
  }
  const closed = contours.filter(pts => pts.length).map(pts => {
    const first = pts[0], last = pts.at(-1)
    if (first[0] !== last[0] || first[1] !== last[1]) pts.push([...first])
    return pts
  })
  const clockwise = pts => pts.reduce((area, p, i) => {
    const next = pts[(i + 1) % pts.length]
    return area + p[0] * next[1] - next[0] * p[1]
  }, 0) < 0
  // Font.generateShapes emits each clockwise outline followed by its holes,
  // even when the typeface lists the holes before the outline.
  const shapes = closed.filter(clockwise).map(pts => ({ pts, holes: [] }))
  const contains = (pts, p) => {
    let inside = false
    for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
      const a = pts[i], b = pts[j]
      if ((a[1] > p[1]) !== (b[1] > p[1])
        && p[0] < (b[0] - a[0]) * (p[1] - a[1]) / (b[1] - a[1]) + a[0]) inside = !inside
    }
    return inside
  }
  for (const hole of closed.filter(pts => !clockwise(pts))) {
    const shape = shapes.find(candidate => contains(candidate.pts, hole[0]))
    if (shape) shape.holes.push(hole)
    else shapes.push({ pts: hole, holes: [] })
  }
  return shapes.flatMap(shape => [shape.pts, ...shape.holes])
    .map(pts => ({ closed: false, strokeOnly: true, pts }))
}

export function textGlyphs(rows, { result, layers, reserve, identity, point, maxPoints }) {
  for (const row of rows) {
    if (!row || typeof row.text !== 'string' || !point(row.pt)
      || !Number.isFinite(row.height) || row.height <= 0) continue
    if (result.points >= maxPoints) { result.truncated = true; break }
    if (row.text.length > 256) result.truncated = true
    // Expand one bounded, origin glyph at a time, then reserve its exact cost
    // BEFORE allocating positioned contours. Never expand the whole text first.
    let offset = 0
    for (const char of row.text.slice(0, 256)) {
      const text = Object.prototype.hasOwnProperty.call(typeface.glyphs, char) ? char : '?'
      const template = glyphContours(typeface.glyphs[text])
      const cost = template.reduce((n, pl) => n + pl.pts.length, 0)
      if (!reserve(cost)) break
      const pt = point(row.pt)
      const glyphs = template.map(pl => ({ ...pl, ...identity(row),
        pts: pl.pts.map(p => [pt[0] + offset + p[0] * row.height, pt[1] + p[1] * row.height, pt[2]]) }))
      result.polylines.push(...glyphs)
      if (glyphs.length) layers.add(String(row.layer || '0'))
      offset += typeface.glyphs[text].ha / typeface.resolution * row.height / 1.013
    }
  }
}
