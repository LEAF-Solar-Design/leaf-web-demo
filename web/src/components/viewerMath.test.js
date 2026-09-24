// @vitest-environment node
//
// The Viewer's W3 camera math, tested with a REAL OrthographicCamera —
// Viewer.jsx itself needs WebGL, so the math lives in viewerMath.js and is
// proven here where it can run. Round-trip tolerances are 1e-6 world units.
import { describe, expect, it } from 'vitest'
import * as THREE from 'three'

import {
  applyCameraCarry,
  applyViewPose,
  cameraCarry,
  cameraCarryKey,
  cameraPose,
  captureCameraCarry,
  nextFitState,
  ndcFromClient,
  pickLineThreshold,
  resizeCameraAction,
  safeFitFrustum,
  safeCenterShift,
  safeRectCameraAction,
  unprojectClientToPlane,
} from './viewerMath.js'

const RECT = { left: 10, top: 20, width: 800, height: 600 }

describe('nextFitState', () => {
  const fitted = Object.freeze({ fitted: true, interacting: false })
  it('keeps the fit after a stationary interaction', () => {
    const started = nextFitState(fitted, 'start')
    expect(started).toEqual({ fitted: true, interacting: true })
    expect(nextFitState(started, 'end')).toEqual(fitted)
  })
  it('clears the fit only for a camera change during interaction', () => {
    const changed = nextFitState(nextFitState(fitted, 'start'), 'change')
    expect(changed).toEqual({ fitted: false, interacting: true })
    expect(nextFitState(changed, 'end')).toEqual({ fitted: false, interacting: false })
    expect(nextFitState(changed, 'fit')).toEqual({ fitted: true, interacting: true })
    expect(changed).toEqual({ fitted: false, interacting: true })
  })
  it('preserves state identity for programmatic changes and unknown events', () => {
    expect(nextFitState(fitted, 'change')).toBe(fitted)
    expect(nextFitState(fitted, 'unknown')).toBe(fitted)
    expect(nextFitState({ fitted: false, interacting: false }, 'fit')).toEqual(fitted)
  })
})

describe('safeRectCameraAction', () => {
  const from = { left: 16, top: 42, width: 1318, height: 702 }
  const to = { left: 16, top: 42, width: 1318, height: 684 }
  it.each([
    [true, null, from, 'refit'],
    [false, null, from, 'none'],
    [true, from, { ...from }, 'none'],
    [true, from, null, 'none'],
    [true, from, to, 'refit'],
    [false, from, to, 'shift'],
    [false, from, { ...to, left: NaN }, 'none'],
    [true, from, { ...to, height: Infinity }, 'none'],
    [true, from, { left: 16, top: 42, width: 1318 }, 'none'],
    [false, { ...from, top: NaN }, to, 'none'],
  ])('chooses %s, %j, %j as %s', (fitted, previous, next, expected) => {
    expect(safeRectCameraAction({ fitted, from: previous, to: next })).toBe(expected)
  })
})

describe('safeFitFrustum', () => {
  const input = { width: 1920, height: 940, bounds: { cx: 0, cy: 0, w: 1000, h: 500 } }
  it('fits and offsets the drawing inside the unobstructed rectangle', () => {
    const fit = safeFitFrustum({ ...input, safe: { left: 266, top: 197, width: 1638, height: 667 } })
    for (const [key, value] of Object.entries({ unitsPerPixel: 540 / 667, halfW: 777.2113943,
      halfH: 380.5097451, centerX: -101.1994003, centerY: 48.98050975 })) expect(fit[key]).toBeCloseTo(value, 6)
  })
  it('preserves the full-canvas fit without a safe rectangle', () => {
    const fit = safeFitFrustum(input)
    expect(fit.unitsPerPixel).toBeCloseTo(540 / 940, 9)
    expect(fit.halfW).toBeCloseTo(551.4893617, 6)
    expect(fit.halfH).toBe(270)
    expect(fit.centerX).toBe(0)
    expect(fit.centerY).toBe(0)
  })
  it('rejects non-finite inputs, missing layout and empty bounds', () => {
    for (const change of [{ width: 0 }, { height: Infinity }, { margin: NaN },
      { bounds: { cx: 0, cy: 0, w: 0, h: 0 } }, { bounds: { cx: NaN, cy: 0, w: 1, h: 1 } },
      { safe: { left: 0, top: 0, width: 0, height: 100 } }]) expect(safeFitFrustum({ ...input, ...change })).toBeNull()
  })
})

describe('safeCenterShift', () => {
  it('moves the camera at fixed scale to follow the safe centre', () => {
    const from = { left: 0, top: 0, width: 100, height: 100 }
    const to = { left: 20, top: 10, width: 100, height: 100 }
    expect(safeCenterShift({ unitsPerPixel: 0.5, from, to })).toEqual({ dx: -10, dy: 5 })
    expect(safeCenterShift({ unitsPerPixel: 0.5, to })).toEqual({ dx: 0, dy: 0 })
    expect(safeCenterShift({ unitsPerPixel: NaN, from, to })).toEqual({ dx: 0, dy: 0 })
  })
})

function flatCamera({ halfW = 400, halfH = 300, cx = 0, cy = 0, zoom = 1 } = {}) {
  const camera = new THREE.OrthographicCamera(-halfW, halfW, halfH, -halfH, -1000, 1000)
  camera.position.set(cx, cy, 100)
  camera.zoom = zoom
  camera.lookAt(cx, cy, 0)
  camera.updateProjectionMatrix()
  camera.updateMatrixWorld(true)
  return camera
}

describe('pickLineThreshold', () => {
  it.each([
    [0.0208, undefined, 0.1248],
    [0, undefined, 1],
    [NaN, undefined, 1],
    [-0.0208, undefined, 1],
    [Infinity, undefined, 1],
    [undefined, undefined, 1],
    [0.0208, 10, 0.208],
  ])('maps %s world units per pixel with aperture %s to %s', (worldPerPixel, px, expected) => {
    expect(pickLineThreshold(worldPerPixel, px)).toBeCloseTo(expected, 9)
  })
})

describe('ndcFromClient', () => {
  it('maps the rect corners and center to NDC space', () => {
    expect(ndcFromClient(RECT, 10, 20)).toEqual({ x: -1, y: 1 })
    expect(ndcFromClient(RECT, 810, 620)).toEqual({ x: 1, y: -1 })
    expect(ndcFromClient(RECT, 410, 320)).toEqual({ x: 0, y: -0 })
  })

  it('fails closed on an unlaid-out rect (hidden pane geometry)', () => {
    expect(ndcFromClient({ left: 0, top: 0, width: 0, height: 0 }, 5, 5)).toBeNull()
    expect(ndcFromClient(null, 5, 5)).toBeNull()
  })
})

describe('unprojectClientToPlane', () => {
  it('round-trips with the projection in a flat pose', () => {
    const camera = flatCamera({ cx: 50, cy: -25 })
    // Project world (50,-25) — the camera center — and unproject it back.
    const v = new THREE.Vector3(120, 75, 0).project(camera)
    const clientX = RECT.left + (v.x * 0.5 + 0.5) * RECT.width
    const clientY = RECT.top + (-v.y * 0.5 + 0.5) * RECT.height
    const world = unprojectClientToPlane(camera, RECT, clientX, clientY)
    expect(world.x).toBeCloseTo(120, 6)
    expect(world.y).toBeCloseTo(75, 6)
  })

  it('hits the drawing plane exactly under a sculpture tilt', () => {
    // Tilted camera (the sculpture pose shape): a bare NDC unproject would
    // land on the near plane; the ray-plane intersection must still return
    // the true z=0 point.
    const camera = new THREE.OrthographicCamera(-400, 400, 300, -300, -2000, 2000)
    camera.position.set(420, -200, 1150)
    camera.lookAt(0, 0, 0)
    camera.updateProjectionMatrix()
    camera.updateMatrixWorld(true)
    const world = new THREE.Vector3(37, -81, 0)
    const v = world.clone().project(camera)
    const clientX = RECT.left + (v.x * 0.5 + 0.5) * RECT.width
    const clientY = RECT.top + (-v.y * 0.5 + 0.5) * RECT.height
    const hit = unprojectClientToPlane(camera, RECT, clientX, clientY)
    expect(hit.x).toBeCloseTo(37, 4)
    expect(hit.y).toBeCloseTo(-81, 4)
  })

  it('fails closed without layout', () => {
    expect(unprojectClientToPlane(flatCamera(), { left: 0, top: 0, width: 0, height: 0 }, 1, 1)).toBeNull()
  })
})

describe('applyViewPose', () => {
  it('recenters while preserving the camera-to-target offset (tilt survives)', () => {
    const camera = flatCamera()
    camera.position.set(420, -200, 1150)
    const target = new THREE.Vector3(0, 0, 0)
    const changed = applyViewPose(camera, target, { center: { x: 100, y: 40 } })
    expect(changed).toBe(true)
    expect(target.x).toBe(100)
    expect(target.y).toBe(40)
    // Offset vector unchanged: position - target stays (420,-200,1150).
    expect(camera.position.x - target.x).toBeCloseTo(420, 6)
    expect(camera.position.y - target.y).toBeCloseTo(-200, 6)
    expect(camera.position.z - target.z).toBeCloseTo(1150, 6)
  })

  it('applies only a finite positive zoom and reports no-ops', () => {
    const camera = flatCamera({ zoom: 2 })
    const target = new THREE.Vector3()
    expect(applyViewPose(camera, target, { zoom: 0 })).toBe(false)
    expect(applyViewPose(camera, target, { zoom: -3 })).toBe(false)
    expect(applyViewPose(camera, target, { zoom: NaN })).toBe(false)
    expect(applyViewPose(camera, target, { zoom: 2 })).toBe(false)
    expect(camera.zoom).toBe(2)
    expect(applyViewPose(camera, target, { zoom: 4 })).toBe(true)
    expect(camera.zoom).toBe(4)
    expect(applyViewPose(camera, target, null)).toBe(false)
    expect(applyViewPose(camera, target, { center: { x: NaN, y: 1 } })).toBe(false)
  })
})

describe('cameraPose', () => {
  it('reports position/target/zoom and the scale readout input', () => {
    const camera = flatCamera({ zoom: 2 })
    const pose = cameraPose(camera, new THREE.Vector3(1, 2, 0), RECT)
    expect(pose.zoom).toBe(2)
    expect(pose.target).toEqual([1, 2, 0])
    // 800 world units across / zoom 2 / 800 px = 0.5 world units per pixel.
    expect(pose.worldPerPixel).toBeCloseTo(0.5, 9)
  })

  it('is null before layout — no NaN scale on a hidden pane', () => {
    expect(cameraPose(flatCamera(), new THREE.Vector3(), { left: 0, top: 0, width: 0, height: 0 })).toBeNull()
  })
})

describe('camera carry', () => {
  const KEY = 'engine:flat:a.dxf'
  const SIZE = { width: 800, height: 600 }
  function movedCamera() {
    const camera = new THREE.OrthographicCamera(-40, 40, 30, -30, -1000, 1000)
    camera.position.set(5, -3, 100)
    camera.zoom = 2.5
    camera.updateProjectionMatrix()
    return { camera, target: new THREE.Vector3(5, -3, 0) }
  }
  function capture(fitted = false) {
    const { camera, target } = movedCamera()
    return captureCameraCarry(camera, target, { key: KEY, fitted, ...SIZE })
  }
  const snapshot = (camera, target) => ({
    left: camera.left, right: camera.right, top: camera.top, bottom: camera.bottom, zoom: camera.zoom,
    position: camera.position.toArray(), target: target.toArray(),
  })

  it('camera carry: key names a flat engine document; a sculpture view has none', () => {
    for (const [intake, sculpture, expected] of [
      [{ source: 'engine', documentId: 'a.dxf' }, false, 'engine:flat:a.dxf'],
      [{ source: 'engine', documentId: 'a.dxf' }, true, ''],
      [{ source: 'engine', documentId: 'a.dxf' }, 'yes', 'engine:flat:a.dxf'],
      [{ documentId: 'a.dxf' }, false, ''],
      [{ source: 'engine', documentId: '' }, false, ''],
      [{ source: 'engine', documentId: 7 }, false, ''],
      [null, false, ''],
      [{ source: 'console', documentId: 'a.dxf' }, false, ''],
    ]) expect(cameraCarryKey(intake, sculpture), JSON.stringify([intake, sculpture])).toBe(expected)
  })

  it('camera carry: a moved view round-trips the frustum, zoom, position and target', () => {
    const { camera: a, target: targetA } = movedCamera()
    const captured = captureCameraCarry(a, targetA, { key: KEY, fitted: false, ...SIZE })
    const carried = cameraCarry(captured, { key: KEY, ...SIZE })
    expect(carried).not.toBeNull()
    const b = new THREE.OrthographicCamera(-1, 1, 1, -1, -1000, 1000)
    const targetB = new THREE.Vector3()
    expect(applyCameraCarry(b, targetB, carried)).toBe(true)
    for (const key of ['left', 'right', 'top', 'bottom', 'zoom']) expect(b[key]).toBe(a[key])
    expect(b.position.equals(a.position)).toBe(true)
    expect(targetB.equals(targetA)).toBe(true)
    a.projectionMatrix.elements.forEach((value, index) => expect(b.projectionMatrix.elements[index]).toBeCloseTo(value, 12))
    a.updateMatrixWorld()
    b.updateMatrixWorld()
    const ndcA = new THREE.Vector3(12, 7, 0).project(a)
    const ndcB = new THREE.Vector3(12, 7, 0).project(b)
    expect(ndcB.x).toBeCloseTo(ndcA.x, 12)
    expect(ndcB.y).toBeCloseTo(ndcA.y, 12)
    expect(ndcB.z).toBeCloseTo(ndcA.z, 12)
  })

  it('camera carry: the capture is a copy', () => {
    const { camera, target } = movedCamera()
    const captured = captureCameraCarry(camera, target, { key: KEY, fitted: false, ...SIZE })
    camera.position.set(90, 90, 90)
    camera.zoom = 7
    camera.left = -500
    target.set(40, 40, 40)
    expect(captured.position).toEqual([5, -3, 100])
    expect(captured.zoom).toBe(2.5)
    expect(captured.left).toBe(-40)
    expect(captured.target).toEqual([5, -3, 0])
  })

  it('camera carry: nothing carries across a different document, mode or empty key', () => {
    const captured = capture()
    expect(cameraCarry(captured, { key: 'engine:flat:b.dxf', ...SIZE })).toBeNull()
    expect(cameraCarry(captured, { key: 'engine:sculpture:a.dxf', ...SIZE })).toBeNull()
    expect(cameraCarry(captured, { key: '', ...SIZE })).toBeNull()
    expect(cameraCarry(null, { key: KEY, ...SIZE })).toBeNull()
  })

  it('camera carry: a view nobody moved refits', () => {
    expect(cameraCarry(capture(true), { key: KEY, ...SIZE })).toBeNull()
  })

  it('camera carry: a resized mount refits', () => {
    const captured = capture()
    expect(cameraCarry(captured, { key: KEY, width: 801, height: 600 })).toBeNull()
    expect(cameraCarry(captured, { key: KEY, width: 800, height: 599 })).toBeNull()
    expect(cameraCarry(captured, { key: KEY, width: 0, height: 600 })).toBeNull()
    expect(cameraCarry(captured, { key: KEY, width: 800, height: NaN })).toBeNull()
  })

  it('camera carry: a malformed pose refits', () => {
    const captured = capture()
    expect(cameraCarry(captured, { key: KEY, ...SIZE })).not.toBeNull()
    for (const change of [{ zoom: 0 }, { zoom: -1 }, { zoom: NaN }, { left: Infinity },
      { right: captured.left }, { top: captured.bottom - 1 }, { position: [5, -3] },
      { position: [5, NaN, 100] }, { target: 'not an array' }]) {
      expect(cameraCarry({ ...captured, ...change }, { key: KEY, ...SIZE })).toBeNull()
    }
  })

  it('camera carry: applying a malformed pose changes nothing', () => {
    const b = new THREE.OrthographicCamera(-1, 1, 1, -1, -1000, 1000)
    const target = new THREE.Vector3(1, 2, 3)
    const before = snapshot(b, target)
    expect(applyCameraCarry(b, target, { ...capture(), zoom: 0 })).toBe(false)
    expect(snapshot(b, target)).toEqual(before)
  })

  it('camera carry: a triple with holes is refused', () => {
    const captured = capture()
    // eslint-disable-next-line no-sparse-arrays
    for (const change of [{ position: Array(3) }, { target: [1, , 3] }]) {
      expect(cameraCarry({ ...captured, ...change }, { key: KEY, ...SIZE })).toBeNull()
      const b = new THREE.OrthographicCamera(-1, 1, 1, -1, -1000, 1000)
      const target = new THREE.Vector3(1, 2, 3)
      const before = snapshot(b, target)
      expect(applyCameraCarry(b, target, { ...captured, ...change })).toBe(false)
      expect(snapshot(b, target)).toEqual(before)
    }
  })

  it('camera carry: a resize to the same size does nothing', () => {
    const safe = { left: 0, top: 0, width: 400, height: 300 }
    const applied = { frustumWidth: 800, frustumHeight: 600 }
    expect(resizeCameraAction({ ...applied, width: 800, height: 600, fitted: true, sculpture: false, safe })).toBe('none')
    expect(resizeCameraAction({ ...applied, width: 800, height: 600, fitted: false, sculpture: false, safe })).toBe('none')
    expect(resizeCameraAction({ ...applied, width: 800, height: 600, fitted: false, sculpture: true, safe: null })).toBe('none')
    expect(resizeCameraAction({ ...applied, width: 0, height: 600, fitted: true, sculpture: false, safe })).toBe('none')
    expect(resizeCameraAction({ ...applied, width: 800, height: -1, fitted: true, sculpture: false, safe })).toBe('none')
    expect(resizeCameraAction({ ...applied, width: NaN, height: 600, fitted: true, sculpture: false, safe })).toBe('none')
  })

  it('camera carry: a real resize refits a fitted safe view and reframes anything else', () => {
    const safe = { left: 0, top: 0, width: 400, height: 300 }
    const resized = { frustumWidth: 800, frustumHeight: 600, width: 1000, height: 600 }
    expect(resizeCameraAction({ ...resized, fitted: true, sculpture: false, safe })).toBe('refit')
    expect(resizeCameraAction({ ...resized, fitted: false, sculpture: false, safe })).toBe('frustum')
    expect(resizeCameraAction({ ...resized, fitted: true, sculpture: true, safe })).toBe('frustum')
    expect(resizeCameraAction({ ...resized, fitted: true, sculpture: false, safe: null })).toBe('frustum')
  })

  it("camera carry: the resize decision follows the frustum's size, not the renderer's", () => {
    expect(resizeCameraAction({ frustumWidth: 0, frustumHeight: 0, width: 800, height: 600, fitted: true, sculpture: false, safe: null })).toBe('frustum')
    expect(resizeCameraAction({ frustumWidth: 1000, frustumHeight: 600, width: 800, height: 600, fitted: false, sculpture: false, safe: null })).toBe('frustum')
    expect(resizeCameraAction({
      frustumWidth: 800, frustumHeight: 600, width: 800, height: 600, fitted: true, sculpture: false,
      safe: { left: 0, top: 0, width: 800, height: 600 },
    })).toBe('none')
  })
})
