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
import { useEffect, useRef } from 'react'

import { useEngineSessionContext } from './EngineSessionProvider.jsx'
import { applyPick, buildSnapIndex, currentStep, ghostFor, orthoPoint, snapPoint, startPicking, wantsPick } from './pointPicking.js'

const CLICK_MOVE_PX = 5
const CLICK_MAX_MS = 500
// W4f-5: the object-snap reach, in screen pixels (the reference's aperture).
const SNAP_PX = 10
// Operand key -> the prompt field's accessible name suffix ("ribbon <label>").
const FIELD_LABEL = Object.freeze({ x: 'x', y: 'y', x2: 'x2', y2: 'y2', r: 'r', pts: 'points', dx: 'dx', dy: 'dy' })

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

export default function CanvasPointPicker({ viewerRef = null, ground = null, onPicking = null }) {
  const { session, inputs, setInput, armed, ortho, setOrtho, osnap, setOsnap } = useEngineSessionContext()
  // W4f-4: ORTHO (F8) constrains the cursor to the axis of the larger delta
  // from the last point, for the pick and the rubber band alike. W4f-5:
  // OSNAP (F3) lands the cursor on the document's endpoints, midpoints and
  // centres within SNAP_PX, and wins over ORTHO when it finds one. Both read
  // through refs so the pointer path allocates nothing and re-binds nothing.
  const orthoRef = useRef(ortho)
  orthoRef.current = ortho
  const setOrthoRef = useRef(setOrtho)
  setOrthoRef.current = setOrtho
  const osnapRef = useRef(osnap)
  osnapRef.current = osnap
  const setOsnapRef = useRef(setOsnap)
  setOsnapRef.current = setOsnap
  useEffect(() => {
    if (typeof window === 'undefined') return undefined
    const onKey = (event) => {
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
  // W4f-3: the chain point a continued command starts from (LINE's next
  // segment), keyed as a string so the sequence restarts only when it moves.
  const armedFrom = armed && armed.from ? armed.from : null
  const fromKey = armedFrom ? `${armedFrom[0]},${armedFrom[1]}` : ''
  const fromRef = useRef(armedFrom)
  fromRef.current = armedFrom
  const inputsRef = useRef(inputs)
  inputsRef.current = inputs
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
    machine.current = armedOp ? startPicking(armedOp, fromRef.current) : null
    const live = !!(machine.current && machine.current.sequence)
    onPickingRef.current?.(live)
    viewerRef?.current?.setRubberBand?.(null)
    return () => { onPickingRef.current?.(false) }
  }, [armedOp, fromKey, entities, viewerRef])

  useEffect(() => {
    if (!ground || typeof window === 'undefined') return undefined
    let down = null
    let frame = 0
    let last = null
    const viewer = () => viewerRef?.current
    // The snap under the cursor, if any: the aperture in world units is one
    // extra unproject SNAP_PX to the right (zoom-aware), the search is one
    // linear pass over the packed candidates. The marker is redrawn only
    // when the snapped point changes.
    let snapX = NaN
    let snapY = NaN
    let markerShown = false
    const snapAt = (v, m, cx, cy, p) => {
      if (!osnapRef.current || !p || !snapIndex.current?.n) return null
      const q = v.unproject(cx + SNAP_PX, cy)
      const tol = q ? Math.abs(q.x - p.x) : 0
      const hit = snapPoint(snapIndex.current, p.x, p.y, tol)
      if (!hit) return null
      hit.tol = tol
      return hit
    }
    const showMarker = (v, hit) => {
      if (hit) {
        if (!markerShown || hit.x !== snapX || hit.y !== snapY) {
          v.setSnapMarker?.({ x: hit.x, y: hit.y }, hit.tol)
          snapX = hit.x; snapY = hit.y; markerShown = true
        }
      } else if (markerShown) {
        v.setSnapMarker?.(null)
        markerShown = false; snapX = NaN; snapY = NaN
      }
    }
    const draw = () => {
      frame = 0
      const v = viewer()
      const m = machine.current
      if (!v || !m || !m.sequence || !last || typeof v.unproject !== 'function') return
      const p = v.unproject(last.x, last.y)
      // No allocation on this per-frame path with both modes off; on, the
      // constrained pair or the snap hit is the price of the mode (kimi
      // note on #982).
      let gx = p ? p.x : NaN
      let gy = p ? p.y : NaN
      const hit = snapAt(v, m, last.x, last.y, p)
      showMarker(v, hit)
      if (hit) { gx = hit.x; gy = hit.y } else if (p && orthoRef.current) { const q = orthoPoint(m, gx, gy); gx = q[0]; gy = q[1] }
      const ghost = p ? ghostFor(m, gx, gy) : null
      v.setRubberBand?.(ghost ? ghost.pts : null, !!ghost?.closed)
    }
    const onMove = (event) => {
      if (!machine.current?.sequence) return
      last = { x: event.clientX, y: event.clientY }
      if (!frame) frame = window.requestAnimationFrame(draw)
    }
    const onDown = (event) => {
      if (event.button !== 0 || !machine.current?.sequence) return
      down = { x: event.clientX, y: event.clientY, t: performance.now() }
    }
    const onUp = (event) => {
      if (!down) return
      const moved = Math.hypot(event.clientX - down.x, event.clientY - down.y)
      const dt = performance.now() - down.t
      down = null
      if (moved >= CLICK_MOVE_PX || dt >= CLICK_MAX_MS) return
      const v = viewer()
      const m = machine.current
      if (!v || !m || !wantsPick(m) || typeof v.unproject !== 'function') return
      const p = v.unproject(event.clientX, event.clientY)
      if (!p) return
      // W4g-6: an edge step names an ENTITY, so the raw click (no snap, no
      // ORTHO) resolves against the entity list within the same aperture
      // the object snap uses, one extra unproject SNAP_PX to the right.
      const edgeStep = currentStep(m)?.kind === 'edge'
      const apertureStep = currentStep(m)?.aperture === true
      const hit = edgeStep ? null : snapAt(v, m, event.clientX, event.clientY, p)
      const [px, py] = hit ? [hit.x, hit.y] : (!edgeStep && orthoRef.current ? orthoPoint(m, p.x, p.y) : [p.x, p.y])
      let edgeCtx = null
      if (edgeStep) {
        const q = v.unproject(event.clientX + SNAP_PX, event.clientY)
        // W4g-6d: FILLET / CHAMFER on a polyline may name the selection
        // itself (its own corner), so the selection stays pickable then;
        // every other edge pick still skips it.
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
      const { state, writes } = applyPick(m, px, py, inputsRef.current, edgeStep ? edgeCtx : apertureCtx)
      if (!writes.length && state === m) return
      machine.current = state
      for (const [key, value] of writes) setInput(key, value)
      const nextStep = wantsPick(state) ? state.sequence[Math.min(state.step, state.sequence.length - 1)] : null
      // The caret moves after React has painted the writes: a Run button
      // that was disabled a moment ago (an empty or refused operand, W4f-6)
      // only takes focus once the render has enabled it.
      window.requestAnimationFrame(() => {
        if (nextStep) focusField(nextStep.keys ? nextStep.keys[0] : nextStep.key)
        else focusRun()
      })
      draw()
    }
    const onLeave = () => { last = null; const v = viewer(); v?.setRubberBand?.(null); if (v) showMarker(v, null) }
    ground.addEventListener('pointermove', onMove, { passive: true })
    ground.addEventListener('pointerdown', onDown)
    ground.addEventListener('pointerup', onUp)
    ground.addEventListener('pointerleave', onLeave)
    return () => {
      if (frame) window.cancelAnimationFrame(frame)
      ground.removeEventListener('pointermove', onMove)
      ground.removeEventListener('pointerdown', onDown)
      ground.removeEventListener('pointerup', onUp)
      ground.removeEventListener('pointerleave', onLeave)
      viewer()?.setRubberBand?.(null)
      viewer()?.setSnapMarker?.(null)
    }
  }, [ground, viewerRef, setInput])
  return null
}
