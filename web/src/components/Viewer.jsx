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

function configureControls(controls, enabled, rotate) {
  controls.enabled = enabled !== false
  controls.enableRotate = rotate === true
  controls.screenSpacePanning = true
  controls.mouseButtons = rotate ? {
    LEFT: THREE.MOUSE.ROTATE,
    MIDDLE: THREE.MOUSE.DOLLY,
    RIGHT: THREE.MOUSE.PAN,
  } : {
    LEFT: THREE.MOUSE.PAN,
    MIDDLE: THREE.MOUSE.DOLLY,
    RIGHT: THREE.MOUSE.PAN,
  }
  controls.zoomSpeed = 1.3
  controls.rotateSpeed = 0.65
  controls.panSpeed = 0.8
}

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

// Additive site-stage props (Website One Surface; defaults preserve the
// console's behavior EXACTLY when absent):
//   background      : 'transparent' -> renderer alpha:true + scene.background
//                     null (the stage grid shows through). Default: today's
//                     opaque --cv-bg scene, byte-identical.
//   controlsEnabled : false disables OrbitControls (the landing stage is a
//                     static hero, not an editor). Default true.
//   rotateEnabled   : true changes left-drag from pan to orbit. Default false,
//                     preserving the top-down CAD editor.
//   panelSculpture  : true extrudes panels in display space as a curved relief.
//                     It never changes intake coordinates or evidence.
//   stringRoutes    : [{id, color?, pts:[[x,y],...]}] world-coord routes drawn
//                     as THREE.Line overlays and animated in the existing rAF
//                     loop — sequential draw-on at ~650 world-units/s with a
//                     glowing head sprite, 2.6s hold at full, loop. Under
//                     prefers-reduced-motion they render fully drawn, static.
//   onGlError       : optional callback fired when WebGL is unavailable so a
//                     host can mount its own 2D fallback.
const STRING_SPEED = 650 // world units / second
const STRING_GAP_S = 0.35
const STRING_HOLD_S = 2.6

function makeGlowTexture() {
  const c = document.createElement('canvas')
  c.width = c.height = 64
  const ctx = c.getContext('2d')
  const grad = ctx.createRadialGradient(32, 32, 0, 32, 32, 32)
  grad.addColorStop(0, 'rgba(255,255,255,1)')
  grad.addColorStop(0.25, 'rgba(255,255,255,.85)')
  grad.addColorStop(1, 'rgba(255,255,255,0)')
  ctx.fillStyle = grad
  ctx.fillRect(0, 0, 64, 64)
  return new THREE.CanvasTexture(c)
}

const Viewer = forwardRef(function Viewer(
  {
    intake, colorForLayer, visibleLayers,
    highlightHandles, markers, overlayPolylines,
    selectedHandle, onSelectEntity, pendingEdit,
    background, controlsEnabled = true, rotateEnabled = false,
    panelSculpture = false, stringRoutes, onGlError,
  },
  ref,
) {
  const mountRef = useRef(null)
  const stateRef = useRef(null)
  // Latest onSelectEntity kept in a ref so the (one-time) pointer handler never
  // fires a stale closure.
  const onSelectRef = useRef(onSelectEntity)
  onSelectRef.current = onSelectEntity
  const onGlErrorRef = useRef(onGlError)
  onGlErrorRef.current = onGlError
  // controlsEnabled read via ref inside the scene build (and synced by its own
  // effect) so toggling it never forces a scene rebuild.
  const controlsEnabledRef = useRef(controlsEnabled)
  controlsEnabledRef.current = controlsEnabled
  const rotateEnabledRef = useRef(rotateEnabled)
  rotateEnabledRef.current = rotateEnabled

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

    const transparent = background === 'transparent'
    const scene = new THREE.Scene()
    scene.background = transparent ? null : new THREE.Color(tokens.bg)

    const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, -1000, 1000)
    let renderer
    try {
      renderer = transparent
        ? new THREE.WebGLRenderer({ antialias: true, alpha: true })
        : new THREE.WebGLRenderer({ antialias: true })
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn('[CADViewport] WebGL unavailable:', e?.message || e)
      setGlError(true)
      if (onGlErrorRef.current) onGlErrorRef.current(e)
      return
    }
    setGlError(false)
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    renderer.setSize(width, height)
    mount.appendChild(renderer.domElement)

    const controls = new OrbitControls(camera, renderer.domElement)
    configureControls(
      controls,
      controlsEnabledRef.current,
      rotateEnabledRef.current,
    )

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
    const sculpture = panelSculpture === true
    const sculptureDepth = sculpture ? dataSpan * 0.24 : 0
    const panelThickness = sculpture ? Math.max(dataSpan * 0.006, 0.45) : 0
    let minDisplayZ = Infinity
    let maxDisplayZ = -Infinity

    const displayZ = (pl) => {
      const pts = pl.pts || []
      const baseZ = pts.length
        ? pts.reduce((sum, point) => sum + (Number(point[2]) || 0), 0) / pts.length
        : 0
      if (!sculpture) return 0
      if (pts.length === 0) return baseZ
      const px = pts.reduce((sum, point) => sum + point[0], 0) / pts.length
      const py = pts.reduce((sum, point) => sum + point[1], 0) / pts.length
      const nx = (px - cx) / Math.max(dataW / 2, 1)
      const ny = (py - cy) / Math.max(dataH / 2, 1)
      const dome = Math.sqrt(Math.max(0, 1 - Math.min(1, nx * nx * 0.82 + ny * ny * 0.18)))
      const facets = 0.04 * Math.sin(px * 0.48) * Math.cos(py * 0.31)
      return baseZ + sculptureDepth * Math.max(0.12, dome + facets)
    }

    if (sculpture) {
      scene.add(new THREE.HemisphereLight(0xd8efff, 0x17202a, 1.9))
      const keyLight = new THREE.DirectionalLight(0xffffff, 2.8)
      keyLight.position.set(cx + dataSpan, cy - dataSpan * 0.4, dataSpan * 1.4)
      scene.add(keyLight)
      const rimLight = new THREE.DirectionalLight(0x39ff88, 1.15)
      rimLight.position.set(cx - dataSpan, cy + dataSpan * 0.5, dataSpan * 0.6)
      scene.add(rimLight)
    }

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
        const topZ = displayZ(pl)
        const bottomZ = topZ - panelThickness
        minDisplayZ = Math.min(minDisplayZ, bottomZ)
        maxDisplayZ = Math.max(maxDisplayZ, topZ)
        pickIndex.set(pl.handle, { kind: 'poly', layer: pl.layer, pts })
        // fan-triangulate (panels are convex quads) for a subtle fill
        for (let i = 1; i < pts.length - 1; i++) {
          fillPos.push(pts[0][0], pts[0][1], topZ)
          fillPos.push(pts[i][0], pts[i][1], topZ)
          fillPos.push(pts[i + 1][0], pts[i + 1][1], topZ)
          triHandles.push(pl.handle)
        }
        if (sculpture) {
          // Bottom and four walls turn each unchanged panel into a thin tile.
          // The depth is a display-space treatment, not drawing geometry.
          for (let i = 1; i < pts.length - 1; i++) {
            fillPos.push(pts[0][0], pts[0][1], bottomZ)
            fillPos.push(pts[i + 1][0], pts[i + 1][1], bottomZ)
            fillPos.push(pts[i][0], pts[i][1], bottomZ)
            triHandles.push(pl.handle)
          }
          for (let i = 0; i < pts.length; i++) {
            const a = pts[i]
            const b = pts[(i + 1) % pts.length]
            fillPos.push(a[0], a[1], bottomZ, b[0], b[1], bottomZ, b[0], b[1], topZ)
            fillPos.push(a[0], a[1], bottomZ, b[0], b[1], topZ, a[0], a[1], topZ)
            triHandles.push(pl.handle, pl.handle)
            linePos.push(a[0], a[1], bottomZ, a[0], a[1], topZ)
          }
        }
        // border segments
        for (let i = 0; i < pts.length; i++) {
          const a = pts[i]
          const b = pts[(i + 1) % pts.length]
          if (i === pts.length - 1 && !pl.closed) break
          linePos.push(a[0], a[1], topZ + 0.05, b[0], b[1], topZ + 0.05)
        }
      }

      const fillGeom = new THREE.BufferGeometry()
      fillGeom.setAttribute('position', new THREE.Float32BufferAttribute(fillPos, 3))
      const fillMat = sculpture
        ? new THREE.MeshStandardMaterial({
          color: col, roughness: 0.72, metalness: 0.14,
          emissive: col, emissiveIntensity: 0.18, flatShading: true,
          transparent: true, opacity: 0.82, side: THREE.DoubleSide,
        })
        : new THREE.MeshBasicMaterial({
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
    const stringGroup = new THREE.Group(); scene.add(stringGroup)

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
      const margin = sculpture ? 1.28 : 1.08
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
      const targetZ = isFinite(minDisplayZ) && isFinite(maxDisplayZ)
        ? (minDisplayZ + maxDisplayZ) / 2
        : 0
      if (sculpture && rotateEnabledRef.current) {
        camera.position.set(
          cx + dataSpan * 0.42,
          cy - dataSpan * 0.2,
          targetZ + dataSpan * 1.15,
        )
      } else {
        camera.position.set(cx, cy, 100)
      }
      camera.zoom = 1
      camera.updateProjectionMatrix()
      controls.target.set(cx, cy, targetZ)
      controls.update()
    }
    fitToBounds()
    const recordCameraPose = () => {
      mount.dataset.cameraPosition = camera.position
        .toArray()
        .map((value) => Number(value.toFixed(4)))
        .join(',')
      mount.dataset.cameraTarget = controls.target
        .toArray()
        .map((value) => Number(value.toFixed(4)))
        .join(',')
    }
    recordCameraPose()
    controls.addEventListener('change', recordCameraPose)

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
    function animate() {
      raf = requestAnimationFrame(animate)
      controls.update()
      // string-route draw-on animation (site stage); no-op when absent
      const sa = stateRef.current && stateRef.current.stringAnim
      if (sa) sa.tick()
      renderer.render(scene, camera)
    }
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
      stringGroup, stringAnim: null,
      fitToBounds, dataSpan, tokens,
    }

    const depthSpan = isFinite(minDisplayZ) && isFinite(maxDisplayZ)
      ? maxDisplayZ - minDisplayZ
      : 0
    mount.dataset.viewMode = sculpture ? 'panel-sculpture' : 'flat'
    mount.dataset.depthSpan = depthSpan.toFixed(3)

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
        cameraPose() {
          return {
            position: camera.position.toArray().map((value) => Number(value.toFixed(4))),
            target: controls.target.toArray().map((value) => Number(value.toFixed(4))),
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
      controls.removeEventListener('change', recordCameraPose)
      controls.dispose()
      renderer.dispose()
      if (renderer.domElement.parentNode) renderer.domElement.parentNode.removeChild(renderer.domElement)
      scene.traverse((o) => { o.geometry?.dispose?.(); o.material?.dispose?.() })
      delete mount.dataset.viewMode
      delete mount.dataset.depthSpan
      delete mount.dataset.cameraPosition
      delete mount.dataset.cameraTarget
      delete mount.__cadviewer
      stateRef.current = null
    }
  }, [activeIntake, colorForLayer, background, panelSculpture])

  useImperativeHandle(ref, () => ({
    fit: () => stateRef.current?.fitToBounds(),
    // Rebuild scene geometry from a new intake version (e.g. a backend push of
    // the next drawing version). Disposes old geometry and clears the pending
    // ghost as part of the rebuild.
    applyVersion: (newIntake) => setInternalIntake(newIntake),
    // Project a world point to a client pixel (production twin of the DEV
    // mount.__cadviewer hook — used by the site layer and automated checks).
    project: (wx, wy) => {
      const s = stateRef.current
      if (!s) return null
      const v = new THREE.Vector3(wx, wy, 0).project(s.camera)
      const rect = s.renderer.domElement.getBoundingClientRect()
      return {
        x: rect.left + (v.x * 0.5 + 0.5) * rect.width,
        y: rect.top + (-v.y * 0.5 + 0.5) * rect.height,
      }
    },
  }), [])

  // --- controls enable/disable (site stage: static hero) --------------------
  useEffect(() => {
    const s = stateRef.current
    if (s) configureControls(s.controls, controlsEnabled, rotateEnabled)
  }, [controlsEnabled, rotateEnabled, buildTick])

  // --- string routes (site stage draw-on animation) --------------------------
  // [{id, color?, pts:[[x,y],...]}] -> THREE.Line per route in stringGroup.
  // Animated inside the EXISTING rAF loop (stateRef.stringAnim.tick): strings
  // draw on sequentially at ~650 world-units/s via setDrawRange, with the
  // in-flight vertex interpolated for a smooth head + a glowing head sprite;
  // 2.6s hold at full, then loop. prefers-reduced-motion -> fully drawn, no
  // animation. No routes -> everything here is a no-op (byte-compatible).
  useEffect(() => {
    const s = stateRef.current
    if (!s) return undefined
    const g = s.stringGroup
    s.stringAnim = null
    g.clear()
    if (!stringRoutes || stringRoutes.length === 0) return undefined

    const disposables = []
    const entries = []
    for (const rt of stringRoutes) {
      const pts = rt.pts || []
      if (pts.length < 2) continue
      const pos = new Float32Array(pts.length * 3)
      for (let i = 0; i < pts.length; i++) {
        pos[i * 3] = pts[i][0]; pos[i * 3 + 1] = pts[i][1]; pos[i * 3 + 2] = 4
      }
      const geo = new THREE.BufferGeometry()
      geo.setAttribute('position', new THREE.BufferAttribute(pos, 3))
      const mat = new THREE.LineBasicMaterial({
        color: rt.color || s.tokens.overlay, transparent: true, opacity: 0.95, depthTest: false,
      })
      const line = new THREE.Line(geo, mat)
      line.renderOrder = 14
      g.add(line)
      disposables.push(geo, mat)
      const cum = [0]; let len = 0
      for (let i = 1; i < pts.length; i++) {
        len += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
        cum.push(len)
      }
      entries.push({ geo, pos, orig: pos.slice(), n: pts.length, cum, len, dirty: null })
    }
    if (entries.length === 0) return undefined

    const reduced = typeof window !== 'undefined' && window.matchMedia &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches
    if (reduced) {
      for (const e of entries) e.geo.setDrawRange(0, e.n)
      return () => { if (stateRef.current) stateRef.current.stringGroup.clear(); disposables.forEach((d) => d.dispose()) }
    }

    // glowing head sprite (one, repositioned onto the active string)
    const headTex = makeGlowTexture()
    const headMat = new THREE.SpriteMaterial({
      map: headTex, transparent: true, depthTest: false, blending: THREE.AdditiveBlending,
    })
    const head = new THREE.Sprite(headMat)
    const hsz = s.dataSpan * 0.02
    head.scale.set(hsz, hsz, 1)
    head.renderOrder = 15
    head.visible = false
    g.add(head)
    disposables.push(headTex, headMat)

    for (const e of entries) e.geo.setDrawRange(0, 0)
    const restore = (e) => {
      if (e.dirty == null) return
      const i = e.dirty
      e.pos[i * 3] = e.orig[i * 3]; e.pos[i * 3 + 1] = e.orig[i * 3 + 1]
      e.geo.attributes.position.needsUpdate = true
      e.dirty = null
    }
    const total = entries.reduce((a, e) => a + e.len / STRING_SPEED + STRING_GAP_S, 0) + STRING_HOLD_S
    const t0 = performance.now()
    s.stringAnim = {
      tick() {
        const t = ((performance.now() - t0) / 1000) % total
        let acc = 0
        let headOn = false
        for (const e of entries) {
          const dur = e.len / STRING_SPEED
          if (t >= acc + dur + STRING_GAP_S) {
            restore(e)
            e.geo.setDrawRange(0, e.n)
          } else if (t > acc) {
            const dist = Math.min((t - acc) * STRING_SPEED, e.len)
            let i = 0
            while (i < e.n - 2 && e.cum[i + 1] <= dist) i++
            const span = e.cum[i + 1] - e.cum[i] || 1
            const k = Math.min(Math.max((dist - e.cum[i]) / span, 0), 1)
            const ax = e.orig[i * 3], ay = e.orig[i * 3 + 1]
            const bx = e.orig[(i + 1) * 3], by = e.orig[(i + 1) * 3 + 1]
            const hx = ax + (bx - ax) * k, hy = ay + (by - ay) * k
            restore(e)
            e.pos[(i + 1) * 3] = hx; e.pos[(i + 1) * 3 + 1] = hy
            e.dirty = i + 1
            e.geo.attributes.position.needsUpdate = true
            e.geo.setDrawRange(0, i + 2)
            if (dist < e.len) { head.position.set(hx, hy, 4.5); head.visible = true; headOn = true }
          } else {
            restore(e)
            e.geo.setDrawRange(0, 0)
          }
          acc += dur + STRING_GAP_S
        }
        if (!headOn) head.visible = false
      },
    }

    return () => {
      if (stateRef.current) {
        stateRef.current.stringAnim = null
        stateRef.current.stringGroup.clear()
      }
      disposables.forEach((d) => d.dispose())
    }
  }, [stringRoutes, buildTick])

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
