import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render } from '@testing-library/react'
import Viewer from './Viewer.jsx'
import { insertGlyphScale } from './viewerMath.js'
import intake from './__fixtures__/roof-dot-intake.json'

const gl = vi.hoisted(() => ({ scene: null }))

vi.mock('three', async (importOriginal) => {
  const actual = await importOriginal()
  class WebGLRenderer {
    constructor() { this.domElement = document.createElement('canvas') }
    setPixelRatio() {}
    setSize() {}
    render(scene) { gl.scene = scene }
    dispose() {}
  }
  return { ...actual, WebGLRenderer }
})

vi.mock('three/examples/jsm/controls/OrbitControls.js', async () => {
  const { Vector3 } = await vi.importActual('three')
  class OrbitControls {
    constructor() { this.target = new Vector3() }
    addEventListener() {}
    removeEventListener() {}
    update() {}
    dispose() {}
  }
  return { OrbitControls }
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  gl.scene = null
})

it.each([
  [[180, 180, 180], [1, 1]],
  [[-2, 3, 1], [-1, 1]],
  [[1, -0.5, 1], [1, -1]],
  [undefined, [1, 1]],
  [[NaN, Infinity, 1], [1, 1]],
  [[0, 0, 0], [1, 1]],
])('uses only mirror signs from insert scale %j', (scale, expected) => {
  expect(insertGlyphScale(scale)).toEqual(expected)
})

it('draws a single view-relative glyph for the incomplete roof circuit tag', () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  render(<Viewer intake={intake} drawingKey="roof-site" colorForLayer={() => '#888888'} />)

  const glyphs = []
  gl.scene.traverse(object => {
    if (object.userData.kind === 'insert') glyphs.push(object)
  })
  expect(glyphs).toHaveLength(1)
  const [glyph] = glyphs
  expect(glyph.userData.handle).toBe(intake.inserts.find(insert => insert.name === '1L-Dot').handle)
  expect(Math.abs(glyph.scale.x)).toBe(1)
  expect(Math.abs(glyph.scale.y)).toBe(1)

  const points = [
    ...intake.polylines.flatMap(polyline => polyline.pts),
    ...intake.inserts.map(insert => [insert.x, insert.y]),
  ]
  const xs = points.map(([x]) => x)
  const ys = points.map(([, y]) => y)
  const span = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys))
  expect(glyph.geometry.parameters.width * Math.abs(glyph.scale.x)).toBeLessThanOrEqual(span * 0.05)
})
