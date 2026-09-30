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
import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { flushSync } from 'react-dom'

import CockpitIcon from './CockpitIcon.jsx'
import LiveRegion, { HIDE_WITH_STYLE } from '../components/LiveRegion.jsx'
import { layerBounds } from '../lib/viewHistory.js'

const ZOOM_IN = 1.25
const ZOOM_OUT = 0.8

// Fixed-precision, sign-stable formatting for a drawing-unit coordinate:
// tabular in the status bar, never scientific notation, never "-0.00".
export function formatCoordinate(value) {
  if (!Number.isFinite(value)) return '·'
  const fixed = value.toFixed(2)
  return fixed === '-0.00' ? '0.00' : fixed
}

// "1px = 0.42u": drawing units per screen pixel, three significant digits.
export function formatScale(worldPerPixel) {
  if (!Number.isFinite(worldPerPixel) || worldPerPixel <= 0) return '·'
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

export const BACK_UNAVAILABLE = 'There is no earlier view to go back to'

export function backAnnouncement(selectedHandle) {
  return selectedHandle ? `Back to your previous view, ${selectedHandle} selected` : 'Back to your previous view'
}

// The share of the safe rectangle a layer fills when Up frames it.
const UP_LAYER_SHARE = 0.8

/**
 * Back and Up for the drawing viewer (S3, navigation rules R4 and R13).
 *
 * Back returns to exactly where the user was: the camera, the selection and
 * the visible layers from one snapshot. Up goes one level toward the whole
 * drawing: from a selected entity to its layer's bounds, else to the whole
 * drawing. The caller owns the ONE bounded history (lib/viewHistory.js) and a
 * size counter, so every consumer re-renders when the size changes.
 *
 * Callbacks are stable: they read the latest selection, layers and intake
 * from a ref written on each render, so a jump never pushes a stale snapshot.
 */
export function useViewNavigation({
  viewerRef, history, setHistorySize,
  selectedHandle = null, selectedLayer = null, setSelectedHandle,
  visibleLayers = null, setVisibleLayers, intake = null,
  navigationSource = null,
}) {
  const latest = useRef(null)
  latest.current = { history, setHistorySize, selectedHandle, selectedLayer, setSelectedHandle, visibleLayers, setVisibleLayers, intake, navigationSource }
  const scope = useRef(undefined)
  const [announcement, setAnnouncement] = useState('')
  const frameRef = useRef(0)
  useEffect(() => () => {
    if (frameRef.current && typeof window !== 'undefined') window.cancelAnimationFrame(frameRef.current)
  }, [])

  // Clear, then set again on the next frame: a repeated message is a real
  // mutation, so a screen reader announces it again.
  const announce = useCallback((text) => {
    setAnnouncement('')
    if (typeof window === 'undefined' || typeof window.requestAnimationFrame !== 'function') {
      setAnnouncement(text)
      return
    }
    if (frameRef.current) window.cancelAnimationFrame(frameRef.current)
    frameRef.current = window.requestAnimationFrame(() => {
      frameRef.current = 0
      setAnnouncement(text)
    })
  }, [])

  const syncScope = useCallback(() => {
    const now = latest.current
    const source = now.navigationSource?.current
    if (source && scope.current !== source.drawingKey) {
      scope.current = source.drawingKey
      now.history?.clear()
      now.setHistorySize?.(0)
    }
    return source
  }, [])

  const capture = useCallback(() => {
    const source = syncScope(), now = latest.current
    const viewer = viewerRef.current
    const current = typeof viewer?.getPose === 'function' ? viewer.getPose() : null
    const pose = current ? { ...current, target: [...(current.target || [])], position: current.position && [...current.position] } : null
    return { pose, selectedHandle: now.selectedHandle, visibleLayers: { ...now.visibleLayers }, ...source?.capture() }
  }, [syncScope, viewerRef])

  const commit = useCallback((snapshot) => {
    const now = latest.current
    const pushed = now.history?.push(snapshot) ?? false
    if (pushed) now.setHistorySize?.(now.history.size())
    return pushed
  }, [])

  const pushView = useCallback(() => commit(capture()), [capture, commit])

  const jump = useCallback((id) => {
    const source = syncScope(), now = latest.current
    if (!source) return { ok: false, reason: 'viewer not ready' }
    if (!source.isVisible(id, now.visibleLayers)) return { ok: false, reason: 'This object is hidden by its layers.' }
    const snapshot = capture()
    const result = source.focus(id)
    if (result.ok) commit(snapshot)
    return result
  }, [capture, commit, syncScope])

  const fit = useCallback(() => {
    pushView()
    viewerRef.current?.setView?.('home')
  }, [pushView, viewerRef])

  const back = useCallback(() => {
    const source = syncScope()
    const now = latest.current
    const snap = now.history?.pop() ?? null
    if (!snap) return false
    now.setHistorySize?.(now.history.size())
    if (source && snap.drawingKey !== source.drawingKey) return false
    // Commit visibility before focus frames, then let the saved camera win.
    flushSync(() => now.setVisibleLayers?.({ ...snap.visibleLayers }))
    const restoredSelection = source?.restore(snap)
    if (!restoredSelection) now.setSelectedHandle?.(snap.selectedHandle)
    const viewer = viewerRef.current
    const { pose } = snap
    // Restore the SCALE, not the raw zoom: Fit and resize recompute the
    // frustum, so the same zoom number can mean a different scale now.
    const current = typeof viewer?.getPose === 'function' ? viewer.getPose() : null
    const scaled = current && pose.worldPerPixel > 0 ? current.zoom * current.worldPerPixel / pose.worldPerPixel : NaN
    const zoom = Number.isFinite(scaled) && scaled > 0 ? scaled : pose.zoom
    viewer?.setView?.({ center: { x: pose.target[0], y: pose.target[1] }, zoom })
    announce(backAnnouncement(snap.selectedHandle))
    return true
  }, [announce, syncScope, viewerRef])

  const up = useCallback(() => {
    const source = syncScope()
    const parent = source?.parent()
    if (parent) {
      const result = jump(parent.id)
      announce(result.ok ? `Showing ${parent.name}` : result.reason)
      return
    }
    const now = latest.current
    const viewer = viewerRef.current
    if (now.selectedHandle && now.visibleLayers?.[now.selectedLayer] === false) {
      announce('This object is hidden by its layers.')
      return
    }
    pushView()
    if (now.selectedHandle) {
      const bounds = layerBounds(now.intake, now.selectedLayer)
      if (bounds && typeof viewer?.frame === 'function' && viewer.frame(bounds, UP_LAYER_SHARE) === true) {
        announce(`Showing layer ${now.selectedLayer}`)
        return
      }
    }
    viewer?.setView?.('home')
    announce('Showing the whole drawing')
  }, [announce, jump, pushView, syncScope, viewerRef])

  return { pushView, fit, back, up, jump, syncScope, announcement }
}

export function ViewCluster({ viewerRef, onFit = null, canBack = false, onBack = null, onUp = null, announcement = '' }) {
  const backReasonId = useId()
  const fit = typeof onFit === 'function' ? onFit : () => viewerRef.current?.setView?.('home')
  return (
    <>
      {/* The viewport strip: fit / back / up / zoom, then the view mode the
          Viewer actually renders (2D wireframe; there is no other mode, so
          it is a readout, never a fake dropdown). */}
      <div className="cockpit-view" role="toolbar" aria-label="View" data-testid="cockpit-view">
        <button type="button" onClick={fit} aria-label="Fit drawing to view" title="Fit to view">
          <CockpitIcon id="fit" fallback="Fit" size="strip" />
        </button>
        {typeof onBack === 'function' ? (
          // aria-disabled, never `disabled`: an empty Back stays in the Tab
          // order and keeps focus, and says why it does nothing.
          <button
            type="button"
            className="cockpit-view-word"
            data-view="back"
            aria-label="Back to the previous view"
            aria-disabled={canBack ? undefined : 'true'}
            aria-describedby={canBack ? undefined : backReasonId}
            title={canBack ? 'Back to the previous view' : BACK_UNAVAILABLE}
            onClick={() => { if (canBack) onBack() }}
          >
            <span className="ci-word" aria-hidden="true">Back</span>
          </button>
        ) : null}
        {typeof onUp === 'function' ? (
          <button type="button" className="cockpit-view-word" data-view="up" aria-label="Up one level" title="Up one level" onClick={() => onUp()}>
            <span className="ci-word" aria-hidden="true">Up</span>
          </button>
        ) : null}
        {typeof onBack === 'function' ? <span id={backReasonId} hidden>{BACK_UNAVAILABLE}</span> : null}
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
      {typeof onBack === 'function' || typeof onUp === 'function' ? (
        <LiveRegion role="status" visuallyHidden={HIDE_WITH_STYLE} data-testid="cockpit-view-live">{announcement}</LiveRegion>
      ) : null}
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
export function useCursorReadout(ground, viewerRef, refs, canvasSelector = null) {
  useEffect(() => {
    if (!ground || typeof window === 'undefined') return undefined
    let frame = 0
    let last = null
    let offCanvas = false
    const write = () => {
      frame = 0
      const viewer = viewerRef.current
      if (!viewer || !last) return
      const point = typeof viewer.unproject === 'function' ? viewer.unproject(last.x, last.y) : null
      if (refs.x.current) refs.x.current.textContent = point ? formatCoordinate(point.x) : '·'
      if (refs.y.current) refs.y.current.textContent = point ? formatCoordinate(point.y) : '·'
      const pose = typeof viewer.getPose === 'function' ? viewer.getPose() : null
      if (refs.scale.current) refs.scale.current.textContent = formatScale(pose?.worldPerPixel)
    }
    const onMove = (event) => {
      const target = event.target
      if (canvasSelector != null && !(target instanceof Element && ground.contains(target) && ground.contains(target.closest(canvasSelector)))) {
        if (!offCanvas) onLeave()
        offCanvas = true
        return
      }
      offCanvas = false
      last = { x: event.clientX, y: event.clientY }
      if (!frame) frame = window.requestAnimationFrame(write)
    }
    const onLeave = () => {
      last = null
      if (refs.x.current) refs.x.current.textContent = '·'
      if (refs.y.current) refs.y.current.textContent = '·'
    }
    ground.addEventListener('pointermove', onMove, { passive: true })
    ground.addEventListener('pointerleave', onLeave)
    return () => {
      if (frame) window.cancelAnimationFrame(frame)
      ground.removeEventListener('pointermove', onMove)
      ground.removeEventListener('pointerleave', onLeave)
    }
  }, [ground, viewerRef, refs, canvasSelector])
}

// The status bar's left end (W4e slice I): the reference's Model tab, the
// drawing's name tab, and +. Model is the only space this viewer has, so the
// tab is a readout; the name tab is the same drawing the document band
// shows; + opens the project board inside the current workspace profile.
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

// A status-bar REGION (P1 studio-shell pass).
//
// The reference's bar is not a run of equal cells: the document tabs own the
// left end and the drafting instruments own the right, with a gap between,
// and ours carries a third group the reference has no equivalent for — the
// honesty signals (backend, solver, catalog fold, build). Flex `order` alone
// arranged those three into ONE continuous strip: measured on the studio at
// 1512x950 before this pass, every segment from x=505 to x=1504 was an
// unbroken run of identical 1px #383838-separated cells on one transparent
// ground, so a coordinate readout and a build hash read as neighbours.
//
// So each group is a real element here, and the boundary is a region edge
// with its own ground. `on` is the gate: OFF, this renders a fragment and
// contributes NO element, which is how the old shell's flat status bar (and
// every non-drafting surface) keeps its DOM byte-identical. Nothing is
// conditional inside the region, so the branch is one comparison per render.
export function FootRegion({ on, name, children }) {
  if (!on) return <>{children}</>
  return (
    <span className={`foot-region foot-region-${name}`} data-testid={`foot-region-${name}`}>
      {children}
    </span>
  )
}

const STATUS_TOGGLES = Object.freeze([
  { id: 'snap', label: 'Snap mode', icon: 'snap', effect: 'moves the cursor in fixed steps' },
  { id: 'grid', label: 'Grid display', icon: 'grid', effect: 'shows a reference grid behind the drawing' },
  { id: 'ortho', label: 'Ortho mode', icon: 'ortho', live: true, effect: 'locks drawing to horizontal and vertical' },
  { id: 'polar', label: 'Polar tracking', icon: 'polar', effect: 'guides the cursor along set angles' },
  { id: 'osnap', label: 'Object snap', icon: 'osnap', live: true, effect: 'locks the cursor onto existing geometry, like endpoints and midpoints' },
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
// mirrors real ORTHO and OSNAP through StatusModesBridge window events;
// snap, grid and polar stay disabled. Fullscreen is also real.
export function StatusToggles() {
  const [state, setState] = useState({ live: false, ortho: false, osnap: true })
  useEffect(() => {
    if (typeof window === 'undefined') return undefined
    const onModes = ({ detail }) => {
      if (!detail || typeof detail !== 'object' || typeof detail.live !== 'boolean') return
      if (detail.live && (typeof detail.ortho !== 'boolean' || typeof detail.osnap !== 'boolean')) return
      setState(detail.live ? { live: true, ortho: detail.ortho, osnap: detail.osnap } : { live: false, ortho: false, osnap: true })
    }
    window.addEventListener('cockpit:modes', onModes)
    window.dispatchEvent(new CustomEvent('cockpit:modes-request'))
    return () => window.removeEventListener('cockpit:modes', onModes)
  }, [])
  return (
    <span className="cockpit-status-toggles" role="toolbar" aria-label="Drafting settings" data-testid="cockpit-status-toggles">
      {STATUS_TOGGLES.map((t) => t.live && state.live ? (
        <button
          key={t.id + '-live'}
          type="button"
          data-toggle={t.id}
          aria-pressed={state[t.id]}
          title={`${t.label} ${state[t.id] ? 'on' : 'off'} (${t.id === 'ortho' ? 'F8' : 'F3'}). ${t.effect[0].toUpperCase() + t.effect.slice(1)}.`}
          aria-label={t.label}
          onClick={() => {
            if (typeof window !== 'undefined') window.dispatchEvent(new CustomEvent('cockpit:mode-toggle', { detail: { id: t.id } }))
          }}
        >
          <CockpitIcon id={t.icon} fallback={t.label} size="strip" />
        </button>
      ) : (
        <button
          key={t.id}
          type="button"
          data-toggle={t.id}
          disabled
          title={`${t.label}: ${t.effect}. ${TOGGLE_REASON[0].toUpperCase() + TOGGLE_REASON.slice(1)}.`}
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

export function CockpitStatus({ ground, viewerRef, shown = null, selectedHandle = null, canvasSelector = null }) {
  const refs = useRef({ x: { current: null }, y: { current: null }, scale: { current: null } }).current
  useCursorReadout(ground, viewerRef, refs, canvasSelector)
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
