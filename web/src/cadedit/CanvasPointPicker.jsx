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
import { applyPick, ghostFor, orthoPoint, startPicking, wantsPick } from './pointPicking.js'

const CLICK_MOVE_PX = 5
const CLICK_MAX_MS = 500
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
  const { session, inputs, setInput, armed, ortho, setOrtho } = useEngineSessionContext()
  // W4f-4: ORTHO (F8) constrains the cursor to the axis of the larger delta
  // from the last point, for the pick and the rubber band alike. Read
  // through refs so the pointer path allocates nothing and re-binds nothing.
  const orthoRef = useRef(ortho)
  orthoRef.current = ortho
  const setOrthoRef = useRef(setOrtho)
  setOrthoRef.current = setOrtho
  useEffect(() => {
    if (typeof window === 'undefined') return undefined
    const onKey = (event) => {
      if (event.key !== 'F8' || event.defaultPrevented) return
      event.preventDefault()
      setOrthoRef.current(!orthoRef.current)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
  const armedOp = armed ? armed.op : ''
  // W4f-3: the chain point a continued command starts from (LINE's next
  // segment), keyed as a string so the sequence restarts only when it moves.
  const armedFrom = armed && armed.from ? armed.from : null
  const fromKey = armedFrom ? `${armedFrom[0]},${armedFrom[1]}` : ''
  const fromRef = useRef(armedFrom)
  fromRef.current = armedFrom
  const inputsRef = useRef(inputs)
  inputsRef.current = inputs
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
    const draw = () => {
      frame = 0
      const v = viewer()
      const m = machine.current
      if (!v || !m || !m.sequence || !last || typeof v.unproject !== 'function') return
      const p = v.unproject(last.x, last.y)
      const [gx, gy] = p ? (orthoRef.current ? orthoPoint(m, p.x, p.y) : [p.x, p.y]) : [NaN, NaN]
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
      const [px, py] = orthoRef.current ? orthoPoint(m, p.x, p.y) : [p.x, p.y]
      const { state, writes } = applyPick(m, px, py, inputsRef.current)
      if (!writes.length && state === m) return
      machine.current = state
      for (const [key, value] of writes) setInput(key, value)
      const nextStep = wantsPick(state) ? state.sequence[Math.min(state.step, state.sequence.length - 1)] : null
      if (nextStep) focusField(nextStep.keys ? nextStep.keys[0] : nextStep.key)
      else focusRun()
      draw()
    }
    const onLeave = () => { last = null; viewer()?.setRubberBand?.(null) }
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
    }
  }, [ground, viewerRef, setInput])
  return null
}
