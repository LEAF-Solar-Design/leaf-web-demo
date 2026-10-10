/**
 * W4f slice A1: the drawing answers the prompts.
 *
 * With a command armed whose operands are points (LINE, CIRCLE, ARC, PLINE,
 * MOVE ...), a click on the drawing ground is unprojected through the ONE
 * viewer's `unproject` and written into the operand fields the way typing
 * would (pointPicking.js decides which: a point, a radius from the picked
 * centre, a displacement from a base, a polyline append), the caret moves to
 * the next step, and the viewer's rubber band follows the cursor from what
 * was picked. Esc / a run / a new arming reset the sequence. The host is told
 * when picking is live (`onPicking`) so the console's own click-to-select
 * stands aside for those clicks.
 *
 * A consumer inside the ONE EngineSessionProvider: no boundary, no worker
 * path, no React state on the pointer path (a ref machine + rAF for the
 * ghost). Listens on the GROUND node, the same node the cursor readout uses,
 * so only pointer traffic that reaches the drawing counts.
 */
import { useEffect, useLayoutEffect, useRef } from 'react'

import { useEngineSessionContext } from './EngineSessionProvider.jsx'
import { applyPick, buildSnapIndex, currentStep, ghostFor, orthoAnchor, orthoPoint, snapPoint, startPicking, wantsPick } from './pointPicking.js'
import { isCompositionKey } from '../lib/keyComposition.js'

const CLICK_MOVE_PX = 5
const CLICK_MAX_MS = 500
// W4f-5: the object-snap reach, in screen pixels (the reference's aperture).
const SNAP_PX = 10
// Operand key -> the prompt field's accessible name suffix ("ribbon <label>").
const FIELD_LABEL = Object.freeze({ x: 'x', y: 'y', x2: 'x2', y2: 'y2', r: 'r', pts: 'points', dx: 'dx', dy: 'dy', members: 'members' })

function focusField(key) {
  if (typeof document === 'undefined') return
  const label = FIELD_LABEL[key]
  const el = label ? document.querySelector(`#cockpit-prompt [aria-label="ribbon ${label}"]`) : null
  el?.focus()
}

function focusRun() {
  if (typeof document === 'undefined') return
  document.querySelector('#cockpit-prompt [data-testid="cockpit-prompt-run"]')?.focus()
}

export default function CanvasPointPicker({ viewerRef = null, ground = null, onPicking = null, canvasSelector = null }) {
  const canvasSelectorRef = useRef(canvasSelector)
  canvasSelectorRef.current = canvasSelector
  const { session, inputs, setInput, armed, ortho, setOrtho, osnap, setOsnap, snapModes, setSnapLimited, highlightedIds } = useEngineSessionContext()
  useEffect(() => {
    viewerRef?.current?.setHighlight?.(Array.from(highlightedIds || []))
    return () => viewerRef?.current?.setHighlight?.([])
  }, [viewerRef, highlightedIds])
  // W4f-4: ORTHO (F8) constrains the cursor to the axis of the larger delta
  // from the last point, for the pick and the rubber band alike. W4f-5:
  // OSNAP (F3) lands the cursor on the document's geometry within SNAP_PX
  // (B1b: the kinds the provider's snapModes select), and wins over ORTHO
  // when it finds one. Both read through refs so the pointer path allocates
  // nothing and re-binds nothing.
  const orthoRef = useRef(ortho)
  orthoRef.current = ortho
  const setOrthoRef = useRef(setOrtho)
  setOrthoRef.current = setOrtho
  const osnapRef = useRef(osnap)
  osnapRef.current = osnap
  const setOsnapRef = useRef(setOsnap)
  setOsnapRef.current = setOsnap
  // B1b: the selected snap modes (the provider's mask) and the query's own
  // diagnostics, one reused record, so a query allocates nothing for them.
  const snapModesRef = useRef(snapModes)
  snapModesRef.current = snapModes
  const diagnosticsRef = useRef({ invalid: false, truncated: false, localOverflow: false, admitted: 0, omitted: 0, pairs: 0 })
  // Whether the last query near the cursor met a bound. Sent to the provider
  // only when it CHANGES, so repeated moves cost no React update.
  const setSnapLimitedRef = useRef(setSnapLimited)
  setSnapLimitedRef.current = setSnapLimited
  const limitedRef = useRef(false)
  const reportLimited = useRef((next) => {
    if (limitedRef.current === next) return
    limitedRef.current = next
    setSnapLimitedRef.current?.(next)
  }).current
  // The ground listener's redraw (or null before it binds): a mode, master,
  // anchor or index change re-runs the stationary cursor's preview.
  const feedbackRef = useRef(null)
  useEffect(() => {
    if (typeof window === 'undefined') return undefined
    const onKey = (event) => {
      if (isCompositionKey(event)) return
      if (event.defaultPrevented) return
      if (event.key === 'F8') { event.preventDefault(); setOrthoRef.current(!orthoRef.current); return }
      if (event.key === 'F3') { event.preventDefault(); setOsnapRef.current(!osnapRef.current) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
  // The snap candidates, packed once per document change (never per frame).
  const snapIndex = useRef(null)
  useEffect(() => { snapIndex.current = buildSnapIndex(session.entities) }, [session.entities])
  const armedOp = armed ? armed.op : ''
  const groupPickDone = armedOp === 'group' && (!!inputs.membersDone || !!inputs.groupName)
  const blockPickDone = armedOp === 'createBlock' && !!inputs.membersDone
  // W4f-3: the chain point a continued command starts from (LINE's next
  // segment), keyed as a string so the sequence restarts only when it moves.
  const armedFrom = armed && armed.from ? armed.from : null
  const fromKey = armedFrom ? `${armedFrom[0]},${armedFrom[1]}` : ''
  const fromRef = useRef(armedFrom)
  fromRef.current = armedFrom
  const inputsRef = useRef(inputs)
  inputsRef.current = inputs
  const busyRef = useRef(session.busy)
  busyRef.current = session.busy
  // W4g-6: an edge pick resolves against the entity list, minus the selection.
  const entitiesRef = useRef(session.entities)
  entitiesRef.current = session.entities
  const selectedRef = useRef(session.selectedId)
  selectedRef.current = session.selectedId
  const onPickingRef = useRef(onPicking)
  onPickingRef.current = onPicking
  const machine = useRef(null)
  // A fresh sequence on every arming and after every applied edit (the next
  // segment starts clean, or from the chain point); cleared when nothing is
  // armed.
  const entities = session.entities
  useEffect(() => {
    machine.current = armedOp && !groupPickDone ? startPicking(armedOp, fromRef.current) : null
    if (blockPickDone && machine.current) machine.current.step = 1
    const live = !!(machine.current && machine.current.sequence)
    onPickingRef.current?.(live)
    viewerRef?.current?.setRubberBand?.(null)
    // A new anchor, a new entity list (or no sequence at all) re-reads the
    // snap under a stationary cursor at once, in the same effect that gives
    // the click its new machine, so a held frame can never leave the marker
    // on a point the click no longer resolves (Astra round two, B1b);
    // without a sequence the marker and limit clear.
    feedbackRef.current?.redrawNow()
    return () => { onPickingRef.current?.(false) }
  }, [armed, armedOp, fromKey, entities, viewerRef, groupPickDone, blockPickDone])

  useEffect(() => {
    if (!ground || typeof window === 'undefined') return undefined
    let down = null
    let frame = 0
    let last = null
    let offCanvas = false
    const onCanvas = (event) => {
      const selector = canvasSelectorRef.current
      if (selector == null) return true
      const target = event.target
      return target instanceof Element && ground.contains(target) && ground.contains(target.closest(selector))
    }
    const viewer = () => viewerRef?.current
    // The snap under the cursor, if any, ONE query for hover and click alike:
    // the raw cursor, the aperture in world units one extra unproject SNAP_PX
    // to the right (zoom-aware), the selected modes, and the anchor the
    // sequence measures from (none on a first point, so perpendicular and
    // tangent find nothing there). An index with no fixed points can still
    // hold insertion points, so its size is not a gate. The query's
    // diagnostics are read on a hit AND a miss.
    const snapAt = (v, m, cx, cy, p) => {
      const index = snapIndex.current
      if (!osnapRef.current || !p || !index) { reportLimited(false); return null }
      const q = v.unproject(cx + SNAP_PX, cy)
      const tol = q ? Math.abs(q.x - p.x) : 0
      const diagnostics = diagnosticsRef.current
      const hit = snapPoint(index, p.x, p.y, tol, { modes: snapModesRef.current, anchor: orthoAnchor(m), diagnostics })
      reportLimited(!!(diagnostics.truncated || diagnostics.localOverflow))
      if (!hit) return null
      hit.tol = tol
      return hit
    }
    // The marker is redrawn only when its point, kind or size changes.
    let markerShown = false
    let markerX = NaN
    let markerY = NaN
    let markerKind = ''
    let markerSize = NaN
    const showMarker = (v, hit) => {
      if (hit) {
        if (!markerShown || hit.x !== markerX || hit.y !== markerY || hit.kind !== markerKind || hit.tol !== markerSize) {
          v.setSnapMarker?.({ x: hit.x, y: hit.y, kind: hit.kind }, hit.tol)
          markerShown = true; markerX = hit.x; markerY = hit.y; markerKind = hit.kind; markerSize = hit.tol
        }
      } else if (markerShown) {
        v.setSnapMarker?.(null)
        markerShown = false; markerX = NaN; markerY = NaN; markerKind = ''; markerSize = NaN
      }
    }
    // No snap feedback: the marker goes and the limitation clears.
    const clearFeedback = (v) => {
      if (v) showMarker(v, null)
      reportLimited(false)
    }
    const draw = () => {
      frame = 0
      const v = viewer()
      const m = machine.current
      if (!v) return
      if (!m || !m.sequence || !last) { clearFeedback(v); return }
      if (typeof v.unproject !== 'function') return
      const p = v.unproject(last.x, last.y)
      // No allocation on this per-frame path with both modes off; on, the
      // constrained pair or the snap hit is the price of the mode (kimi
      // note on #982).
      let gx = p ? p.x : NaN
      let gy = p ? p.y : NaN
      // An edge step names an ENTITY: its cursor stays raw (no snap, no
      // ORTHO), the same as its click.
      const edgeStep = currentStep(m)?.kind === 'edge'
      const hit = edgeStep ? null : snapAt(v, m, last.x, last.y, p)
      if (edgeStep) reportLimited(false)
      showMarker(v, hit)
      if (hit) { gx = hit.x; gy = hit.y } else if (p && !edgeStep && orthoRef.current) { const q = orthoPoint(m, gx, gy); gx = q[0]; gy = q[1] }
      // W4g-7b: an armed INSERT reads its typed name, scale and rotation, and
      // the document's block catalogue, so the ghost can be the definition's
      // own bounding box; every other op ignores the extra arguments.
      const ghost = p ? ghostFor(m, gx, gy, inputsRef.current, entitiesRef.current.blocks) : null
      v.setRubberBand?.(ghost ? ghost.pts : null, !!ghost?.closed)
    }
    const onMove = (event) => {
      if (!onCanvas(event)) {
        if (!offCanvas) onLeave()
        offCanvas = true
        return
      }
      offCanvas = false
      if (!machine.current?.sequence) return
      last = { x: event.clientX, y: event.clientY }
      if (!frame) frame = window.requestAnimationFrame(draw)
    }
    const onDown = (event) => {
      if (!onCanvas(event)) { down = null; return }
      if (event.button !== 0 || !machine.current?.sequence) return
      down = { x: event.clientX, y: event.clientY, t: performance.now() }
    }
    const acceptPoint = (m, px, py, context = null, typed = false) => {
      if (busyRef.current) return false
      const step = currentStep(m)
      const { state, writes } = applyPick(m, px, py, inputsRef.current, context)
      if (!writes.length && state === m) return false
      machine.current = state
      for (const [key, value] of writes) setInput(key, value)
      // Preserve click-only Run behavior; a bar entry submits a mixed LINE.
      state.barPoint = typed || m.barPoint
      const detail = { op: m.op, key: step.keys?.[0] || step.key, run: !!state.barPoint, handled: false }
      window.dispatchEvent(new CustomEvent('cockpit:picked', { detail }))
      // Keep the endpoint and ghost anchor until the store accepts the run.
      // A refusal then lets either surface correct the same endpoint.
      const runLine = detail.handled && detail.run && m.op === 'createLine' && m.step === 1
      if (runLine) machine.current = { ...m, barPoint: state.barPoint }
      const nextStep = currentStep(machine.current)
      window.requestAnimationFrame(() => {
        const focus = { handled: false, complete: !nextStep }
        window.dispatchEvent(new CustomEvent('cockpit:focus-step', { detail: focus }))
        if (focus.handled) return
        if (nextStep) focusField(nextStep.keys ? nextStep.keys[0] : nextStep.key)
        else if (state.op === 'createBlock') document.querySelector('#cockpit-prompt [aria-label="ribbon block name"]')?.focus()
        else focusRun()
      })
      draw()
      return true
    }
    const onPoint = (event) => {
      const m = machine.current
      const detail = event.detail
      if (!detail || !m?.sequence) return
      if (!Array.isArray(detail.point) || detail.point.length !== 2) return
      const index = m.sequence.findIndex((step) => step.kind === 'point' && step.keys[0] === detail.key)
      if (index < 0) return
      if (index > m.step) {
        detail.handled = true
        detail.refusal = 'Start a drawing command before entering a point.'
        return
      }
      // Replacing an earlier point discards its dependent picks and anchor.
      const rewound = index < m.step
        ? { ...m, step: index, picked: m.picked.slice(0, index), base: index === 0 ? null : m.base }
        : m
      detail.handled = acceptPoint(rewound, detail.point[0], detail.point[1], null, true)
    }
    const onUp = (event) => {
      if (!onCanvas(event)) { down = null; return }
      if (!down) return
      const moved = Math.hypot(event.clientX - down.x, event.clientY - down.y)
      const dt = performance.now() - down.t
      down = null
      if (moved >= CLICK_MOVE_PX || dt >= CLICK_MAX_MS) return
      const v = viewer()
      const m = machine.current
      if (!v || !m || !wantsPick(m) || typeof v.unproject !== 'function') return
      // BLOCK owns this click before the viewer's target listener can
      // replace the selection that anchors the member list.
      if (m.op === 'createBlock') event.stopPropagation()
      if (m.op === 'group' && inputsRef.current.groupName) return
      const p = v.unproject(event.clientX, event.clientY)
      if (!p) return
      // W4g-6: an edge step names an ENTITY, so the raw click (no snap, no
      // ORTHO) resolves against the entity list within the same aperture
      // the object snap uses, one extra unproject SNAP_PX to the right.
      const edgeStep = currentStep(m)?.kind === 'edge'
      const apertureStep = currentStep(m)?.aperture === true
      const hit = edgeStep ? null : snapAt(v, m, event.clientX, event.clientY, p)
      if (edgeStep) reportLimited(false)
      const [px, py] = hit ? [hit.x, hit.y] : (!edgeStep && orthoRef.current ? orthoPoint(m, p.x, p.y) : [p.x, p.y])
      let edgeCtx = null
      if (edgeStep) {
        const q = v.unproject(event.clientX + SNAP_PX, event.clientY)
        // W4g-6d: FILLET / CHAMFER on a polyline may name the selection
        // itself (its own corner), so the selection stays pickable then;
        // every other edge pick ignores that hit after resolving it.
        const selfCorner = (m.op === 'fillet' || m.op === 'chamfer')
          && String((entitiesRef.current || []).find((e) => e && e.id === selectedRef.current)?.type || '').toUpperCase() === 'LWPOLYLINE'
        edgeCtx = { entities: entitiesRef.current, tol: q ? Math.abs(q.x - p.x) : 0, exceptId: selfCorner ? null : selectedRef.current }
      }
      let apertureCtx = null
      if (apertureStep) {
        const q = v.unproject(event.clientX + SNAP_PX, event.clientY)
        const tol = q ? Math.abs(q.x - p.x) : 0
        apertureCtx = { tol }
      }
      acceptPoint(m, px, py, edgeStep ? edgeCtx : apertureCtx)
    }
    const onLeave = () => { last = null; const v = viewer(); v?.setRubberBand?.(null); clearFeedback(v) }
    // The stationary cursor's preview again, through the same frame path; with
    // no cursor on the ground or no sequence, the feedback just clears.
    const redraw = () => {
      if (!last || !machine.current?.sequence) { clearFeedback(viewer()); return }
      if (!frame) frame = window.requestAnimationFrame(draw)
    }
    // The same feedback at once, for a change the next click reads immediately
    // (a mode or the master): a pending frame is dropped and drawn now, so the
    // marker never shows a mask the click no longer uses (Astra round one, B1b).
    const redrawNow = () => {
      if (frame) window.cancelAnimationFrame(frame)
      frame = 0
      if (!last || !machine.current?.sequence) { clearFeedback(viewer()); return }
      draw()
    }
    feedbackRef.current = { redraw, redrawNow }
    const onCancel = () => {
      down = null
      if (frame) window.cancelAnimationFrame(frame)
      frame = 0
      onLeave()
    }
    const onWindowUp = (event) => { if (!onCanvas(event)) down = null }
    const onBlockUp = (event) => {
      if (machine.current?.op === 'createBlock') onUp(event)
    }
    window.addEventListener('cockpit:pick-point', onPoint)
    ground.addEventListener('pointermove', onMove, { passive: true })
    ground.addEventListener('pointerdown', onDown)
    ground.addEventListener('pointerup', onBlockUp, true)
    ground.addEventListener('pointerup', onUp)
    ground.addEventListener('pointerleave', onLeave)
    ground.addEventListener('pointercancel', onCancel)
    window.addEventListener('pointercancel', onCancel)
    if (canvasSelector != null) window.addEventListener('pointerup', onWindowUp, true)
    return () => {
      if (frame) window.cancelAnimationFrame(frame)
      window.removeEventListener('cockpit:pick-point', onPoint)
      ground.removeEventListener('pointermove', onMove)
      ground.removeEventListener('pointerdown', onDown)
      ground.removeEventListener('pointerup', onBlockUp, true)
      ground.removeEventListener('pointerup', onUp)
      ground.removeEventListener('pointerleave', onLeave)
      ground.removeEventListener('pointercancel', onCancel)
      window.removeEventListener('pointercancel', onCancel)
      if (canvasSelector != null) window.removeEventListener('pointerup', onWindowUp, true)
      if (feedbackRef.current?.redraw === redraw) feedbackRef.current = null
      viewer()?.setRubberBand?.(null)
      viewer()?.setSnapMarker?.(null)
      reportLimited(false)
    }
  }, [ground, viewerRef, setInput, canvasSelector])
  // B1b: a mode, master or ORTHO change re-reads a stationary cursor (the
  // master off clears the marker and the limitation; the selection itself is
  // kept; ORTHO moves the ghost of an unsnapped cursor, Astra round two).
  // A layout effect: the marker is redrawn in the same commit as the mask the
  // click handler reads, before any further input event can arrive.
  useLayoutEffect(() => {
    if (!osnap) reportLimited(false)
    feedbackRef.current?.redrawNow()
  }, [osnap, snapModes, ortho, reportLimited])
  // B1b: a camera change (zoom, pan, fit) moves the drawing under a still
  // cursor. The Viewer's camera channel re-reads it as soon as a snapshot is
  // delivered, so the marker and the ghost follow the camera the next click
  // unprojects through (Astra round two, B1b). Subscribed the way
  // DrawingCameraControls does: again whenever the viewer behind the ref
  // changes, never twice for one viewer, and released on unmount.
  const cameraSubscription = useRef(null)
  useEffect(() => {
    const api = viewerRef?.current || null
    if (cameraSubscription.current && cameraSubscription.current.api === api) return
    cameraSubscription.current?.off?.()
    const owner = { api, off: null }
    cameraSubscription.current = owner
    const off = api?.subscribeCamera?.(() => {
      if (cameraSubscription.current === owner) feedbackRef.current?.redrawNow()
    })
    owner.off = typeof off === 'function' ? off : null
  })
  useEffect(() => () => {
    const previous = cameraSubscription.current
    cameraSubscription.current = null
    previous?.off?.()
  }, [])
  return null
}
