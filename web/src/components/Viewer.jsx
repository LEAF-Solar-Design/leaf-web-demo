import { useEffect, useRef, useState, forwardRef, useImperativeHandle } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'

// Edit-capable top-down CADViewport (CONTRACT §1/§3). Plain three.js with an
// orthographic camera + OrbitControls locked to pan+zoom (no rotate). Renders:
//   - LWPOLYLINEs  : per-layer merged fill + border (unchanged fast path)
//   - INSERTs       : a block glyph at pt, rotated by rot (deg), scaled by scale
//   - 3DFACEs       : filled triangle-pair surfaces
// Adds click-to-pick (raycast -> DWG handle -> onSelectEntity), a user-driven
// selection highlight (independent of Result overlays), an optimistic
// pending-edit ghost overlay, and a version-apply hook (applyVersion) that
// rebuilds geometry from a new intake and clears the ghost.
//
// Colors come from the calm --cv-* CSS variables (read once per build) so the
// scene stays in sync with the design tokens in styles.css.

const CLICK_MOVE_PX = 5      // pointer travel under this = click (not a pan/drag)
const CLICK_MAX_MS = 500

function readTokens() {
  const cs = getComputedStyle(document.documentElement)
  const v = (name, fallback) => {
    const raw = cs.getPropertyValue(name).trim()
    return raw || fallback
  }
  return {
    // Fallbacks mirror the committed dark values in styles.css (:root --cv-*)
    // so a failed variable resolve still renders the dark scheme, never the
    // retired light "paper" palette.
    bg: v('--cv-bg', '#0f0f11'),
    insert: v('--cv-insert', '#4f83c2'),
    face: v('--cv-face', '#4b8f68'),
    select: v('--cv-select', '#e7b643'),
    ghost: v('--cv-ghost', '#6b7280'),
    ghostRemove: v('--cv-ghost-remove', '#a05555'),
    danger: v('--cv-danger', '#e24947'),
    // Default color for result-overlay polylines when the envelope carries
    // none — info-blue family, off the accent/status reds and greens. The
    // token itself lives in styles.css; the fallback is the faithful hex of
    // oklch(0.72 0.12 230).
    overlay: v('--cv-overlay', '#43b2e1'),
  }
}

const Viewer = forwardRef(function Viewer(
  {
    intake, colorForLayer, visibleLayers,
    highlightHandles, markers, overlayPolylines,
    selectedHandle, onSelectEntity, pendingEdit,
  },
  ref,
) {
  const mountRef = useRef(null)
  const stateRef = useRef(null)
  // Latest onSelectEntity kept in a ref so the (one-time) pointer handler never
  // fires a stale closure.
  const onSelectRef = useRef(onSelectEntity)
  onSelectRef.current = onSelectEntity

  // Internal override intake set by the imperative applyVersion(). Drives a
  // rebuild without the parent having to swap the `intake` prop. Reset whenever
  // a genuinely new `intake` prop arrives (fixture/drawing switch).
  const [internalIntake, setInternalIntake] = useState(null)
  useEffect(() => { setInternalIntake(null) }, [intake])
  const activeIntake = internalIntake || intake

  // Bumped at the end of each scene build so the dynamic-overlay effects
  // (highlight/markers/overlay/selection/pending) re-apply against the fresh
  // scene even when their own props did not change.
  const [buildTick, setBuildTick] = useState(0)
  // Set when the browser cannot provide a WebGL context — degrade to a calm
  // message instead of crashing the React tree.
  const [glError, setGlError] = useState(false)

  // --- scene build (re-runs on activeIntake / colorForLayer change) --------
  useEffect(() => {
    if (!activeIntake) return
    const mount = mountRef.current
    const width = mount.clientWidth
    const height = mount.clientHeight
    const tokens = readTokens()

    const scene = new THREE.Scene()
    scene.background = new THREE.Color(tokens.bg)

    const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, -1000, 1000)
    let renderer
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true })
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn('[CADViewport] WebGL unavailable:', e?.message || e)
      setGlError(true)
      return
    }
    setGlError(false)
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

    const polylines = activeIntake.polylines || []
    const inserts = activeIntake.inserts || []
    const faces3d = activeIntake.faces3d || []

    // bounds — include polylines, insert points, and face corners so the
    // whole drawing (not just polylines) is framed.
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
    const grow = (x, y) => {
      if (x < minX) minX = x
      if (y < minY) minY = y
      if (x > maxX) maxX = x
      if (y > maxY) maxY = y
    }
    for (const pl of polylines) for (const p of pl.pts) grow(p[0], p[1])
    for (const ins of inserts) if (ins.pt) grow(ins.pt[0], ins.pt[1])
    for (const f of faces3d) for (const p of [f.p1, f.p2, f.p3, f.p4]) if (p) grow(p[0], p[1])
    if (!isFinite(minX)) { minX = -1; minY = -1; maxX = 1; maxY = 1 }
    const dataW = maxX - minX || 1
    const dataH = maxY - minY || 1
    const cx = (minX + maxX) / 2
    const cy = (minY + maxY) / 2
    const dataSpan = Math.max(dataW, dataH)

    // pickIndex: handle -> geometry descriptor (for selection highlight redraw)
    const pickIndex = new Map()
    // pickables: flat meshes the raycaster tests (carry handle attribution)
    const pickables = []

    // --- polylines: per-layer merged fill + border --------------------------
    const byLayer = new Map()
    for (const pl of polylines) {
      if (!byLayer.has(pl.layer)) byLayer.set(pl.layer, [])
      byLayer.get(pl.layer).push(pl)
    }

    const layerGroups = new Map()
    for (const [layer, polys] of byLayer) {
      const col = new THREE.Color(colorForLayer(layer))
      const group = new THREE.Group()

      const fillPos = []
      const linePos = []
      const triHandles = [] // triangle index -> polyline handle (fill pick map)
      for (const pl of polys) {
        const pts = pl.pts
        pickIndex.set(pl.handle, { kind: 'poly', layer: pl.layer, pts })
        // fan-triangulate (panels are convex quads) for a subtle fill
        for (let i = 1; i < pts.length - 1; i++) {
          fillPos.push(pts[0][0], pts[0][1], 0)
          fillPos.push(pts[i][0], pts[i][1], 0)
          fillPos.push(pts[i + 1][0], pts[i + 1][1], 0)
          triHandles.push(pl.handle)
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
      fillMesh.userData = { kind: 'polyfill', triHandles }
      group.add(fillMesh)
      pickables.push(fillMesh)

      const lineGeom = new THREE.BufferGeometry()
      lineGeom.setAttribute('position', new THREE.Float32BufferAttribute(linePos, 3))
      const lineMat = new THREE.LineBasicMaterial({ color: col, transparent: true, opacity: 0.9 })
      const lines = new THREE.LineSegments(lineGeom, lineMat)
      group.add(lines)

      scene.add(group)
      layerGroups.set(layer, group)
    }

    // --- inserts: block glyph at pt, rotated by rot (deg), scaled by scale ---
    const insertColor = new THREE.Color(tokens.insert)
    const insertGroup = new THREE.Group()
    const hs = dataSpan * 0.02 // base half-size of the glyph
    let nInserts = 0
    for (const ins of inserts) {
      if (!ins.pt) continue
      const geom = new THREE.PlaneGeometry(hs * 2, hs * 2)
      const mat = new THREE.MeshBasicMaterial({
        color: insertColor, transparent: true, opacity: 0.18, depthWrite: false,
        side: THREE.DoubleSide,
      })
      const mesh = new THREE.Mesh(geom, mat)
      mesh.position.set(ins.pt[0], ins.pt[1], 2)
      mesh.rotation.z = THREE.MathUtils.degToRad(ins.rot || 0)
      const sx = ins.scale?.[0] ?? 1
      const sy = ins.scale?.[1] ?? 1
      mesh.scale.set(sx || 1, sy || 1, 1)
      mesh.userData = { kind: 'insert', handle: ins.handle }
      // outline square + a tick toward local +X so rotation reads visually
      const o = hs
      const outline = [
        -o, -o, 0.1, o, -o, 0.1, o, -o, 0.1, o, o, 0.1,
        o, o, 0.1, -o, o, 0.1, -o, o, 0.1, -o, -o, 0.1,
        0, 0, 0.1, o, 0, 0.1,
      ]
      const olg = new THREE.BufferGeometry()
      olg.setAttribute('position', new THREE.Float32BufferAttribute(outline, 3))
      const olm = new THREE.LineBasicMaterial({ color: insertColor, transparent: true, opacity: 0.95 })
      mesh.add(new THREE.LineSegments(olg, olm))
      insertGroup.add(mesh)
      pickables.push(mesh)
      nInserts++
      pickIndex.set(ins.handle, { kind: 'insert', layer: ins.layer, pt: ins.pt, rot: ins.rot || 0, scale: [sx || 1, sy || 1], hs })
    }
    scene.add(insertGroup)

    // --- 3DFACEs: filled triangle-pair surfaces -----------------------------
    const faceColor = new THREE.Color(tokens.face)
    const faceGroup = new THREE.Group()
    let nFaces = 0
    for (const f of faces3d) {
      const ring = [f.p1, f.p2, f.p3, f.p4].filter(Boolean)
      if (ring.length < 3) continue
      const pos = []
      pos.push(ring[0][0], ring[0][1], 0.5, ring[1][0], ring[1][1], 0.5, ring[2][0], ring[2][1], 0.5)
      if (ring[3]) pos.push(ring[0][0], ring[0][1], 0.5, ring[2][0], ring[2][1], 0.5, ring[3][0], ring[3][1], 0.5)
      const g = new THREE.BufferGeometry()
      g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3))
      const m = new THREE.MeshBasicMaterial({
        color: faceColor, transparent: true, opacity: 0.24, depthWrite: false, side: THREE.DoubleSide,
      })
      const mesh = new THREE.Mesh(g, m)
      mesh.userData = { kind: 'face', handle: f.handle }
      faceGroup.add(mesh)
      pickables.push(mesh)
      nFaces++
      // border
      const bpos = []
      for (let i = 0; i < ring.length; i++) {
        const a = ring[i], b = ring[(i + 1) % ring.length]
        bpos.push(a[0], a[1], 0.6, b[0], b[1], 0.6)
      }
      const bg = new THREE.BufferGeometry()
      bg.setAttribute('position', new THREE.Float32BufferAttribute(bpos, 3))
      const bm = new THREE.LineBasicMaterial({ color: faceColor, transparent: true, opacity: 0.9 })
      faceGroup.add(new THREE.LineSegments(bg, bm))
      pickIndex.set(f.handle, { kind: 'face', layer: f.layer, pts: ring })
    }
    scene.add(faceGroup)

    // dynamic overlay containers (rebuilt on prop change)
    const highlightGroup = new THREE.Group(); scene.add(highlightGroup)
    const markerGroup = new THREE.Group(); scene.add(markerGroup)
    const overlayGroup = new THREE.Group(); scene.add(overlayGroup)
    const selectionGroup = new THREE.Group(); scene.add(selectionGroup)
    const pendingGroup = new THREE.Group(); scene.add(pendingGroup)

    // rendered-count assertion (acceptance: rendered == intake lengths)
    // eslint-disable-next-line no-console
    console.log(
      `[CADViewport] rendered ${polylines.length} polylines (intake ${polylines.length}), ` +
      `${nInserts} inserts (intake ${inserts.length}), ` +
      `${nFaces} faces3d (intake ${faces3d.length})`,
    )

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

    // --- picking: click (not drag) -> raycast -> handle ---------------------
    const raycaster = new THREE.Raycaster()
    const ndc = new THREE.Vector2()
    let down = null // { x, y, t }
    function onPointerDown(e) {
      if (e.button !== 0) return // only left-click selects
      down = { x: e.clientX, y: e.clientY, t: performance.now() }
    }
    function onPointerUp(e) {
      if (!down) return
      const moved = Math.hypot(e.clientX - down.x, e.clientY - down.y)
      const dt = performance.now() - down.t
      const wasClick = moved < CLICK_MOVE_PX && dt < CLICK_MAX_MS
      down = null
      if (!wasClick) return
      const rect = renderer.domElement.getBoundingClientRect()
      ndc.x = ((e.clientX - rect.left) / rect.width) * 2 - 1
      ndc.y = -((e.clientY - rect.top) / rect.height) * 2 + 1
      raycaster.setFromCamera(ndc, camera)
      const hits = raycaster.intersectObjects(pickables, false)
      let handle = null
      for (const hit of hits) {
        const ud = hit.object.userData
        // respect layer visibility — don't pick hidden geometry
        const grp = hit.object.parent
        if (grp && grp.visible === false) continue
        if (ud.kind === 'polyfill') {
          handle = ud.triHandles[hit.faceIndex] ?? null
        } else if (ud.handle) {
          handle = ud.handle
        }
        if (handle != null) break
      }
      const cb = onSelectRef.current
      if (cb) cb(handle)
    }
    const dom = renderer.domElement
    dom.addEventListener('pointerdown', onPointerDown)
    dom.addEventListener('pointerup', onPointerUp)

    let raf
    function animate() { raf = requestAnimationFrame(animate); controls.update(); renderer.render(scene, camera) }
    animate()

    function onResize() {
      const w = mount.clientWidth, h = mount.clientHeight
      renderer.setSize(w, h)
      applyFrustum()
    }
    const ro = new ResizeObserver(onResize)
    ro.observe(mount)

    stateRef.current = {
      scene, camera, renderer, controls, layerGroups, pickIndex,
      highlightGroup, markerGroup, overlayGroup, selectionGroup, pendingGroup,
      fitToBounds, dataSpan, tokens,
    }

    // DEV-only test hook: project a world point to a client pixel so automated
    // verification can dispatch a real click on a known entity. Stripped from
    // production builds (dead-code eliminated when import.meta.env.DEV is false).
    if (import.meta.env.DEV) {
      mount.__cadviewer = {
        project(wx, wy) {
          const v = new THREE.Vector3(wx, wy, 0).project(camera)
          const rect = renderer.domElement.getBoundingClientRect()
          return {
            x: rect.left + (v.x * 0.5 + 0.5) * rect.width,
            y: rect.top + (-v.y * 0.5 + 0.5) * rect.height,
          }
        },
      }
    }

    // let the dynamic-overlay effects re-apply against this fresh scene
    setBuildTick((t) => t + 1)

    return () => {
      cancelAnimationFrame(raf)
      ro.disconnect()
      dom.removeEventListener('pointerdown', onPointerDown)
      dom.removeEventListener('pointerup', onPointerUp)
      controls.dispose()
      renderer.dispose()
      if (renderer.domElement.parentNode) renderer.domElement.parentNode.removeChild(renderer.domElement)
      scene.traverse((o) => { o.geometry?.dispose?.(); o.material?.dispose?.() })
      stateRef.current = null
    }
  }, [activeIntake, colorForLayer])

  useImperativeHandle(ref, () => ({
    fit: () => stateRef.current?.fitToBounds(),
    // Rebuild scene geometry from a new intake version (e.g. a backend push of
    // the next drawing version). Disposes old geometry and clears the pending
    // ghost as part of the rebuild.
    applyVersion: (newIntake) => setInternalIntake(newIntake),
  }), [])

  // --- layer visibility ----------------------------------------------------
  useEffect(() => {
    const s = stateRef.current
    if (!s) return
    for (const [layer, group] of s.layerGroups) {
      group.visible = visibleLayers ? visibleLayers[layer] !== false : true
    }
  }, [visibleLayers, buildTick])

  // --- result highlight handles (CONTRACT §3, result-driven) ---------------
  useEffect(() => {
    const s = stateRef.current
    if (!s) return
    const g = s.highlightGroup
    g.clear()
    if (!highlightHandles || highlightHandles.length === 0) return
    const fillPos = [], linePos = []
    for (const h of highlightHandles) {
      const d = s.pickIndex.get(h)
      if (!d || d.kind !== 'poly') continue
      const pts = d.pts
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
    const fm = new THREE.MeshBasicMaterial({ color: s.tokens.select, transparent: true, opacity: 0.35, depthTest: false, depthWrite: false })
    const fMesh = new THREE.Mesh(fg, fm); fMesh.renderOrder = 10; g.add(fMesh)

    const lg = new THREE.BufferGeometry()
    lg.setAttribute('position', new THREE.Float32BufferAttribute(linePos, 3))
    const lm = new THREE.LineBasicMaterial({ color: s.tokens.select, transparent: true, opacity: 1, depthTest: false })
    const lMesh = new THREE.LineSegments(lg, lm); lMesh.renderOrder = 11; g.add(lMesh)
  }, [highlightHandles, buildTick])

  // --- user selection highlight (picking-driven, independent of results) ---
  useEffect(() => {
    const s = stateRef.current
    if (!s) return
    const g = s.selectionGroup
    g.clear()
    if (!selectedHandle) return
    const d = s.pickIndex.get(selectedHandle)
    if (!d) return
    const col = s.tokens.select
    const z = 5

    if (d.kind === 'poly' || d.kind === 'face') {
      const pts = d.pts
      const fillPos = [], linePos = []
      for (let i = 1; i < pts.length - 1; i++) {
        fillPos.push(pts[0][0], pts[0][1], z)
        fillPos.push(pts[i][0], pts[i][1], z)
        fillPos.push(pts[i + 1][0], pts[i + 1][1], z)
      }
      for (let i = 0; i < pts.length; i++) {
        const a = pts[i], b = pts[(i + 1) % pts.length]
        linePos.push(a[0], a[1], z + 0.1, b[0], b[1], z + 0.1)
      }
      const fg = new THREE.BufferGeometry()
      fg.setAttribute('position', new THREE.Float32BufferAttribute(fillPos, 3))
      const fm = new THREE.MeshBasicMaterial({ color: col, transparent: true, opacity: 0.28, depthTest: false, depthWrite: false })
      const fMesh = new THREE.Mesh(fg, fm); fMesh.renderOrder = 20; g.add(fMesh)
      const lg = new THREE.BufferGeometry()
      lg.setAttribute('position', new THREE.Float32BufferAttribute(linePos, 3))
      const lm = new THREE.LineBasicMaterial({ color: col, transparent: true, opacity: 1, depthTest: false })
      const lMesh = new THREE.LineSegments(lg, lm); lMesh.renderOrder = 21; g.add(lMesh)
    } else if (d.kind === 'insert') {
      // square outline slightly larger than the glyph, honoring rot/scale
      const o = d.hs * 1.35
      const local = [
        -o, -o, 0, o, -o, 0, o, -o, 0, o, o, 0,
        o, o, 0, -o, o, 0, -o, o, 0, -o, -o, 0,
      ]
      const lg = new THREE.BufferGeometry()
      lg.setAttribute('position', new THREE.Float32BufferAttribute(local, 3))
      const lm = new THREE.LineBasicMaterial({ color: col, transparent: true, opacity: 1, depthTest: false })
      const lMesh = new THREE.LineSegments(lg, lm)
      lMesh.position.set(d.pt[0], d.pt[1], z)
      lMesh.rotation.z = THREE.MathUtils.degToRad(d.rot)
      lMesh.scale.set(d.scale[0], d.scale[1], 1)
      lMesh.renderOrder = 21
      g.add(lMesh)
    }
  }, [selectedHandle, buildTick])

  // --- markers (result-driven) ---------------------------------------------
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
      pos.push(x - r, y, 2, x, y + r, 2)
      pos.push(x, y + r, 2, x + r, y, 2)
      pos.push(x + r, y, 2, x, y - r, 2)
      pos.push(x, y - r, 2, x - r, y, 2)
    }
    const geo = new THREE.BufferGeometry()
    geo.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3))
    const mat = new THREE.LineBasicMaterial({ color: s.tokens.danger, depthTest: false })
    const mesh = new THREE.LineSegments(geo, mat); mesh.renderOrder = 12; g.add(mesh)
  }, [markers, buildTick])

  // --- overlay polylines (result-driven) -----------------------------------
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
      const mat = new THREE.LineBasicMaterial({ color: ov.color || s.tokens.overlay, depthTest: false })
      const mesh = new THREE.LineSegments(geo, mat); mesh.renderOrder = 9; g.add(mesh)
    }
  }, [overlayPolylines, buildTick])

  // --- pending-edit ghost overlay (optimistic, pre-commit) -----------------
  useEffect(() => {
    const s = stateRef.current
    if (!s) return
    const g = s.pendingGroup
    g.clear()
    if (!pendingEdit) return
    const addedCol = s.tokens.ghost
    const removeCol = s.tokens.ghostRemove
    const z = 3

    // added: dashed low-opacity ghost of the proposed new geometry
    const addPos = []
    for (const ent of pendingEdit.added || []) {
      const pts = ent.pts
      if (!pts) continue
      const n = ent.closed === false ? pts.length - 1 : pts.length
      for (let i = 0; i < n; i++) {
        const a = pts[i], b = pts[(i + 1) % pts.length]
        addPos.push(a[0], a[1], z, b[0], b[1], z)
      }
    }
    if (addPos.length) {
      const geo = new THREE.BufferGeometry()
      geo.setAttribute('position', new THREE.Float32BufferAttribute(addPos, 3))
      const mat = new THREE.LineDashedMaterial({
        color: addedCol, transparent: true, opacity: 0.85, depthTest: false,
        dashSize: s.dataSpan * 0.02, gapSize: s.dataSpan * 0.012,
      })
      const mesh = new THREE.LineSegments(geo, mat)
      mesh.computeLineDistances()
      mesh.renderOrder = 15
      g.add(mesh)
    }

    // removed: dashed "struck-out" outline over the entity being deleted
    const remPos = []
    for (const h of pendingEdit.removed || []) {
      const d = s.pickIndex.get(h)
      if (!d || !d.pts) continue
      const pts = d.pts
      for (let i = 0; i < pts.length; i++) {
        const a = pts[i], b = pts[(i + 1) % pts.length]
        remPos.push(a[0], a[1], z, b[0], b[1], z)
      }
      // an X across the bbox to read as "removed"
      let mnx = Infinity, mny = Infinity, mxx = -Infinity, mxy = -Infinity
      for (const p of pts) {
        if (p[0] < mnx) mnx = p[0]; if (p[1] < mny) mny = p[1]
        if (p[0] > mxx) mxx = p[0]; if (p[1] > mxy) mxy = p[1]
      }
      remPos.push(mnx, mny, z, mxx, mxy, z)
      remPos.push(mnx, mxy, z, mxx, mny, z)
    }
    if (remPos.length) {
      const geo = new THREE.BufferGeometry()
      geo.setAttribute('position', new THREE.Float32BufferAttribute(remPos, 3))
      const mat = new THREE.LineDashedMaterial({
        color: removeCol, transparent: true, opacity: 0.9, depthTest: false,
        dashSize: s.dataSpan * 0.018, gapSize: s.dataSpan * 0.01,
      })
      const mesh = new THREE.LineSegments(geo, mat)
      mesh.computeLineDistances()
      mesh.renderOrder = 15
      g.add(mesh)
    }
  }, [pendingEdit, buildTick])

  return (
    <div ref={mountRef} className="viewer-canvas">
      {glError && (
        <div className="viewer-fallback">
          3D preview needs WebGL, which is unavailable in this browser.
        </div>
      )}
    </div>
  )
})

export default Viewer
