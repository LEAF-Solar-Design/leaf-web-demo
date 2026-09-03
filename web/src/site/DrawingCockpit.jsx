// The drawing cockpit (W4b, re-seated in W4e): the instrument chrome that
// touches the drawing under the studio shell, in the reference CAD grammar
// (the viewport strip at the canvas's top-left, the view cube at its
// top-right; live cursor coordinates, scale, counts, and selection in the
// status bar). Both pieces are built on the Viewer's existing ref surface
// (setView / getPose / unproject, W3 pre-work #890) and render ONLY under the
// rail (App guards each with `studioGround &&`), so the old shell never
// carries them.
//
// Two-material rule: this is chrome ON the drawing, so it is dark glass, the
// legend's material, never paper. The cursor readout is a rAF-throttled DOM
// write, never React state: pointer-rate re-renders were risk R11 in the
// convergence plan.
import { useEffect, useRef } from 'react'

import CockpitIcon from './CockpitIcon.jsx'

const ZOOM_IN = 1.25
const ZOOM_OUT = 0.8

// Fixed-precision, sign-stable formatting for a drawing-unit coordinate:
// tabular in the status bar, never scientific notation, never "-0.00".
export function formatCoordinate(value) {
  if (!Number.isFinite(value)) return '—'
  const fixed = value.toFixed(2)
  return fixed === '-0.00' ? '0.00' : fixed
}

// "1px = 0.42u": drawing units per screen pixel, three significant digits.
export function formatScale(worldPerPixel) {
  if (!Number.isFinite(worldPerPixel) || worldPerPixel <= 0) return '—'
  return `1px = ${Number(worldPerPixel.toPrecision(3))}u`
}

// Zoom by a factor around the current pose; a missing viewer or pose is a
// no-op (the drawing is not laid out yet), never a throw.
export function zoomViewer(viewer, factor) {
  if (!viewer || typeof viewer.getPose !== 'function' || typeof viewer.setView !== 'function') return false
  const pose = viewer.getPose()
  if (!pose || !Number.isFinite(pose.zoom) || pose.zoom <= 0) return false
  return viewer.setView({ zoom: pose.zoom * factor })
}

export function ViewCluster({ viewerRef }) {
  return (
    <>
      {/* The viewport strip: fit / zoom as icons, then the view mode the
          Viewer actually renders (2D wireframe; there is no other mode, so
          it is a readout, never a fake dropdown). */}
      <div className="cockpit-view" role="toolbar" aria-label="View" data-testid="cockpit-view">
        <button type="button" onClick={() => viewerRef.current?.setView?.('home')} aria-label="Fit drawing to view" title="Fit to view">
          <CockpitIcon id="fit" fallback="Fit" size="strip" />
        </button>
        <button type="button" onClick={() => zoomViewer(viewerRef.current, ZOOM_IN)} aria-label="Zoom in" title="Zoom in">
          <CockpitIcon id="zoom-in" fallback="+" size="strip" />
        </button>
        <button type="button" onClick={() => zoomViewer(viewerRef.current, ZOOM_OUT)} aria-label="Zoom out" title="Zoom out">
          <CockpitIcon id="zoom-out" fallback="−" size="strip" />
        </button>
        <span className="cockpit-view-mode" aria-label="View mode">
          <CockpitIcon id="wireframe" fallback="WF" size="strip" />
          Wireframe 2D
        </span>
      </div>
      {/* The view cube: TOP is the only view this 2D viewer has, so the cube
          is a readout of that fact with the compass around it. */}
      <div className="cockpit-cube-wrap" aria-hidden="true">
        <span className="cockpit-cube-n">N</span>
        <span className="cockpit-cube-w">W</span>
        <span className="cockpit-cube"><span>TOP</span></span>
        <span className="cockpit-cube-e">E</span>
        <span className="cockpit-cube-s">S</span>
      </div>
      <div className="cockpit-cube-wcs" aria-hidden="true"><span>WCS</span><span>▾</span></div>
    </>
  )
}

// Live cursor coordinates over the drawing ground. Listens on the GROUND
// node (the portal target) so only pointer traffic that actually reaches the
// drawing through the console's punch-through counts; over a painted pane
// the readout simply holds, and leaving the ground clears it.
export function useCursorReadout(ground, viewerRef, refs) {
  useEffect(() => {
    if (!ground || typeof window === 'undefined') return undefined
    let frame = 0
    let last = null
    const write = () => {
      frame = 0
      const viewer = viewerRef.current
      if (!viewer || !last) return
      const point = typeof viewer.unproject === 'function' ? viewer.unproject(last.x, last.y) : null
      if (refs.x.current) refs.x.current.textContent = point ? formatCoordinate(point.x) : '—'
      if (refs.y.current) refs.y.current.textContent = point ? formatCoordinate(point.y) : '—'
      const pose = typeof viewer.getPose === 'function' ? viewer.getPose() : null
      if (refs.scale.current) refs.scale.current.textContent = formatScale(pose?.worldPerPixel)
    }
    const onMove = (event) => {
      last = { x: event.clientX, y: event.clientY }
      if (!frame) frame = window.requestAnimationFrame(write)
    }
    const onLeave = () => {
      last = null
      if (refs.x.current) refs.x.current.textContent = '—'
      if (refs.y.current) refs.y.current.textContent = '—'
    }
    ground.addEventListener('pointermove', onMove, { passive: true })
    ground.addEventListener('pointerleave', onLeave)
    return () => {
      if (frame) window.cancelAnimationFrame(frame)
      ground.removeEventListener('pointermove', onMove)
      ground.removeEventListener('pointerleave', onLeave)
    }
  }, [ground, viewerRef, refs])
}

// The status bar's left end (W4e slice I): the reference's Model tab, the
// drawing's name tab, and +. Model is the only space this viewer has, so the
// tab is a readout; the name tab is the same drawing the document band
// shows; + opens the project board (a real surface switch).
export function StatusTabs({ name = '', onStart = null }) {
  return (
    <span className="cockpit-status-tabs" data-testid="cockpit-status-tabs">
      <span className="foot-model-tab" aria-label="Model space">Model</span>
      {name ? <span className="foot-doc-tab">{name}</span> : null}
      {onStart ? (
        <button type="button" className="foot-doc-add" aria-label="Open the project board" title="Project board" onClick={onStart}>+</button>
      ) : null}
    </span>
  )
}

const STATUS_TOGGLES = Object.freeze([
  { id: 'snap', label: 'Snap mode', icon: 'snap' },
  { id: 'grid', label: 'Grid display', icon: 'grid' },
  { id: 'ortho', label: 'Ortho mode', icon: 'ortho' },
  { id: 'polar', label: 'Polar tracking', icon: 'polar' },
  { id: 'osnap', label: 'Object snap', icon: 'osnap' },
])
const TOGGLE_REASON = 'not in the browser viewer yet'

function requestFullscreen() {
  if (typeof document === 'undefined') return false
  const root = document.documentElement
  if (document.fullscreenElement) { document.exitFullscreen?.(); return true }
  if (typeof root.requestFullscreen === 'function') { root.requestFullscreen().catch(() => {}); return true }
  return false
}

// The status bar's right end: the reference's drafting toggles. This viewer
// has no snap, grid, ortho, polar or object-snap state, so each is present,
// disabled, and says so; fullscreen is the one that is real.
export function StatusToggles() {
  return (
    <span className="cockpit-status-toggles" role="toolbar" aria-label="Drafting settings" data-testid="cockpit-status-toggles">
      {STATUS_TOGGLES.map((t) => (
        <button
          key={t.id}
          type="button"
          data-toggle={t.id}
          disabled
          title={`${t.label}: ${TOGGLE_REASON}`}
          aria-label={`${t.label} (unavailable: ${TOGGLE_REASON})`}
        >
          <CockpitIcon id={t.icon} fallback={t.label} size="strip" />
        </button>
      ))}
      <button type="button" data-toggle="fullscreen" title="Fullscreen" aria-label="Toggle fullscreen" onClick={requestFullscreen}>
        <CockpitIcon id="fullscreen" fallback="Full" size="strip" />
      </button>
    </span>
  )
}

export function CockpitStatus({ ground, viewerRef, shown = null, selectedHandle = null }) {
  const refs = useRef({ x: { current: null }, y: { current: null }, scale: { current: null } }).current
  useCursorReadout(ground, viewerRef, refs)
  return (
    <span className="cockpit-status" data-testid="cockpit-status">
      <span className="cockpit-coord">X <b ref={(el) => { refs.x.current = el }}>—</b></span>
      <span className="cockpit-coord">Y <b ref={(el) => { refs.y.current = el }}>—</b></span>
      <span className="cockpit-scale"><b ref={(el) => { refs.scale.current = el }}>—</b></span>
      {shown && (
        <span className="cockpit-count">{shown.polylines.length} entities · {shown.layers.length} layers</span>
      )}
      <span className="cockpit-sel" data-selected={selectedHandle ? 'true' : 'false'}>
        {selectedHandle ? `sel ${selectedHandle}` : 'no selection'}
      </span>
    </span>
  )
}
