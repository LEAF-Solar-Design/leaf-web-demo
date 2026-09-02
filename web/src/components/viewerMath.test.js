// @vitest-environment node
//
// The Viewer's W3 camera math, tested with a REAL OrthographicCamera —
// Viewer.jsx itself needs WebGL, so the math lives in viewerMath.js and is
// proven here where it can run. Round-trip tolerances are 1e-6 world units.
import { describe, expect, it } from 'vitest'
import * as THREE from 'three'

import {
  applyViewPose,
  cameraPose,
  ndcFromClient,
  unprojectClientToPlane,
} from './viewerMath.js'

const RECT = { left: 10, top: 20, width: 800, height: 600 }

function flatCamera({ halfW = 400, halfH = 300, cx = 0, cy = 0, zoom = 1 } = {}) {
  const camera = new THREE.OrthographicCamera(-halfW, halfW, halfH, -halfH, -1000, 1000)
  camera.position.set(cx, cy, 100)
  camera.zoom = zoom
  camera.lookAt(cx, cy, 0)
  camera.updateProjectionMatrix()
  camera.updateMatrixWorld(true)
  return camera
}

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
