import { useEffect, useRef, forwardRef, useImperativeHandle } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'

// Top-down 2D plan viewer for intake polylines (CONTRACT §1). Plain three.js
// with an orthographic camera + OrbitControls locked to pan+zoom (no rotate).
// Static geometry (fills + borders) is built once per layer; highlight /
// markers / overlay polylines (from a Result envelope §3) rebuild on change.

const Viewer = forwardRef(function Viewer(
  { intake, colorForLayer, visibleLayers, highlightHandles, markers, overlayPolylines },
  ref,
) {
  const mountRef = useRef(null)
  const stateRef = useRef(null)

  // --- one-time scene build ------------------------------------------------
  useEffect(() => {
    if (!intake) return
    const mount = mountRef.current
    const width = mount.clientWidth
    const height = mount.clientHeight

    const scene = new THREE.Scene()
    scene.background = new THREE.Color('#0d1117')

    const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, -1000, 1000)
    const renderer = new THREE.WebGLRenderer({ antialias: true })
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    renderer.setSize(width, height)
    mount.appendChild(renderer.domElement)

    const controls = new OrbitControls(camera, renderer.domElement)
    controls.enableRotate = false
    controls.screenSpacePanning = true
    controls.mouseButtons = {
      LEFT: THREE.MOUSE.PAN,
      MIDDLE: THREE.MOUSE.DOLLY,
      RIGHT: THREE.MOUSE.PAN,
    }
    controls.zoomSpeed = 1.3

    // bounds
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
    for (const pl of intake.polylines) {
      for (const p of pl.pts) {
        if (p[0] < minX) minX = p[0]
        if (p[1] < minY) minY = p[1]
        if (p[0] > maxX) maxX = p[0]
        if (p[1] > maxY) maxY = p[1]
      }
    }
    const dataW = maxX - minX || 1
    const dataH = maxY - minY || 1
    const cx = (minX + maxX) / 2
    const cy = (minY + maxY) / 2

    // group polylines by layer -> build fill + border geometry
    const byLayer = new Map()
    for (const pl of intake.polylines) {
      if (!byLayer.has(pl.layer)) byLayer.set(pl.layer, [])
      byLayer.get(pl.layer).push(pl)
    }

    const handleIndex = new Map() // handle -> pts (for highlight rebuild)
    const layerGroups = new Map()

    for (const [layer, polys] of byLayer) {
      const col = new THREE.Color(colorForLayer(layer))
      const group = new THREE.Group()

      const fillPos = []
      const linePos = []
      for (const pl of polys) {
        const pts = pl.pts
        handleIndex.set(pl.handle, pts)
        // fan-triangulate (panels are convex quads) for a subtle fill
        for (let i = 1; i < pts.length - 1; i++) {
          fillPos.push(pts[0][0], pts[0][1], 0)
          fillPos.push(pts[i][0], pts[i][1], 0)
          fillPos.push(pts[i + 1][0], pts[i + 1][1], 0)
        }
        // border segments
        for (let i = 0; i < pts.length; i++) {
          const a = pts[i]
          const b = pts[(i + 1) % pts.length]
          if (i === pts.length - 1 && !pl.closed) break
          linePos.push(a[0], a[1], 0.1, b[0], b[1], 0.1)
        }
      }

      const fillGeom = new THREE.BufferGeometry()
      fillGeom.setAttribute('position', new THREE.Float32BufferAttribute(fillPos, 3))
      const fillMat = new THREE.MeshBasicMaterial({
        color: col, transparent: true, opacity: 0.14, depthWrite: false,
      })
      const fillMesh = new THREE.Mesh(fillGeom, fillMat)
      group.add(fillMesh)

      const lineGeom = new THREE.BufferGeometry()
      lineGeom.setAttribute('position', new THREE.Float32BufferAttribute(linePos, 3))
      const lineMat = new THREE.LineBasicMaterial({ color: col, transparent: true, opacity: 0.9 })
      const lines = new THREE.LineSegments(lineGeom, lineMat)
      group.add(lines)

      scene.add(group)
      layerGroups.set(layer, group)
    }

    // dynamic overlay containers (rebuilt on prop change)
    const highlightGroup = new THREE.Group(); scene.add(highlightGroup)
    const markerGroup = new THREE.Group(); scene.add(markerGroup)
    const overlayGroup = new THREE.Group(); scene.add(overlayGroup)

    function applyFrustum() {
      const w = mount.clientWidth, h = mount.clientHeight
      const viewAspect = w / h
      const dataAspect = dataW / dataH
      const margin = 1.08
      let halfW, halfH
      if (dataAspect > viewAspect) {
        halfW = (dataW / 2) * margin
        halfH = halfW / viewAspect
      } else {
        halfH = (dataH / 2) * margin
        halfW = halfH * viewAspect
      }
      camera.left = -halfW; camera.right = halfW
      camera.top = halfH; camera.bottom = -halfH
      camera.updateProjectionMatrix()
    }

    function fitToBounds() {
      applyFrustum()
      camera.position.set(cx, cy, 100)
      camera.zoom = 1
      camera.updateProjectionMatrix()
      controls.target.set(cx, cy, 0)
      controls.update()
    }
    fitToBounds()

    let raf
    function animate() { raf = requestAnimationFrame(animate); controls.update(); renderer.render(scene, camera) }
    animate()

    function onResize() {
      const w = mount.clientWidth, h = mount.clientHeight
      renderer.setSize(w, h)
      // recompute frustum for new aspect, preserving zoom + target
      applyFrustum()
    }
    const ro = new ResizeObserver(onResize)
    ro.observe(mount)

    stateRef.current = {
      scene, camera, renderer, controls, layerGroups, handleIndex,
      highlightGroup, markerGroup, overlayGroup, fitToBounds,
      dataSpan: Math.max(dataW, dataH),
    }

    return () => {
      cancelAnimationFrame(raf)
      ro.disconnect()
      controls.dispose()
      renderer.dispose()
      if (renderer.domElement.parentNode) renderer.domElement.parentNode.removeChild(renderer.domElement)
      scene.traverse((o) => { o.geometry?.dispose?.(); o.material?.dispose?.() })
      stateRef.current = null
    }
  }, [intake, colorForLayer])

  useImperativeHandle(ref, () => ({
    fit: () => stateRef.current?.fitToBounds(),
  }), [])

  // --- layer visibility ----------------------------------------------------
  useEffect(() => {
    const s = stateRef.current
    if (!s) return
    for (const [layer, group] of s.layerGroups) {
      group.visible = visibleLayers ? visibleLayers[layer] !== false : true
    }
  }, [visibleLayers])

  // --- highlight handles ---------------------------------------------------
  useEffect(() => {
    const s = stateRef.current
    if (!s) return
    const g = s.highlightGroup
    g.clear()
    if (!highlightHandles || highlightHandles.length === 0) return
    const fillPos = [], linePos = []
    for (const h of highlightHandles) {
      const pts = s.handleIndex.get(h)
      if (!pts) continue
      for (let i = 1; i < pts.length - 1; i++) {
        fillPos.push(pts[0][0], pts[0][1], 1)
        fillPos.push(pts[i][0], pts[i][1], 1)
        fillPos.push(pts[i + 1][0], pts[i + 1][1], 1)
      }
      for (let i = 0; i < pts.length; i++) {
        const a = pts[i], b = pts[(i + 1) % pts.length]
        linePos.push(a[0], a[1], 1.1, b[0], b[1], 1.1)
      }
    }
    const fg = new THREE.BufferGeometry()
    fg.setAttribute('position', new THREE.Float32BufferAttribute(fillPos, 3))
    const fm = new THREE.MeshBasicMaterial({ color: '#ffb300', transparent: true, opacity: 0.35, depthTest: false, depthWrite: false })
    const fMesh = new THREE.Mesh(fg, fm); fMesh.renderOrder = 10; g.add(fMesh)

    const lg = new THREE.BufferGeometry()
    lg.setAttribute('position', new THREE.Float32BufferAttribute(linePos, 3))
    const lm = new THREE.LineBasicMaterial({ color: '#ffce4d', transparent: true, opacity: 1, depthTest: false })
    const lMesh = new THREE.LineSegments(lg, lm); lMesh.renderOrder = 11; g.add(lMesh)
  }, [highlightHandles])

  // --- markers -------------------------------------------------------------
  useEffect(() => {
    const s = stateRef.current
    if (!s) return
    const g = s.markerGroup
    g.clear()
    if (!markers || markers.length === 0) return
    const r = s.dataSpan * 0.012
    const pos = []
    for (const m of markers) {
      const [x, y] = m.pt
      pos.push(x - r, y, 2, x + r, y, 2)
      pos.push(x, y - r, 2, x, y + r, 2)
      // small diamond
      pos.push(x - r, y, 2, x, y + r, 2)
      pos.push(x, y + r, 2, x + r, y, 2)
      pos.push(x + r, y, 2, x, y - r, 2)
      pos.push(x, y - r, 2, x - r, y, 2)
    }
    const geo = new THREE.BufferGeometry()
    geo.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3))
    const mat = new THREE.LineBasicMaterial({ color: '#ff5c5c', depthTest: false })
    const mesh = new THREE.LineSegments(geo, mat); mesh.renderOrder = 12; g.add(mesh)
  }, [markers])

  // --- overlay polylines ---------------------------------------------------
  useEffect(() => {
    const s = stateRef.current
    if (!s) return
    const g = s.overlayGroup
    g.clear()
    if (!overlayPolylines || overlayPolylines.length === 0) return
    for (const ov of overlayPolylines) {
      const pts = ov.pts || []
      const pos = []
      for (let i = 0; i < pts.length - 1; i++) {
        pos.push(pts[i][0], pts[i][1], 1.5, pts[i + 1][0], pts[i + 1][1], 1.5)
      }
      const geo = new THREE.BufferGeometry()
      geo.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3))
      const mat = new THREE.LineBasicMaterial({ color: ov.color || '#4de1c1', depthTest: false })
      const mesh = new THREE.LineSegments(geo, mat); mesh.renderOrder = 9; g.add(mesh)
    }
  }, [overlayPolylines])

  return <div ref={mountRef} className="viewer-canvas" />
})

export default Viewer
