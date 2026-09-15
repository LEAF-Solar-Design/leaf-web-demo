/**
 * W4f slice B: the command line's typed words reach the engine here.
 *
 * App's catalog dispatch recognizes a bare command word (LINE, C, MOVE ...,
 * lib/commandWords.js) on the drafting surfaces and, instead of routing it as
 * natural language, fires ONE `cockpit:command` window event and clears the
 * bar. This consumer, mounted inside the ONE EngineSessionProvider like every
 * other cadedit surface, turns the event into the same thing a ribbon click
 * does: a prompted op ARMS (the command line then asks for its operands), a
 * no-operand op (delete) runs at once when its group is live. It renders
 * nothing and owns no session (consumer only: no boundary, no worker path).
 *
 * Fail closed: the event detail must carry a group from the fixed pair and an
 * op the ribbon's PROMPTS or OPS knows; anything else is ignored. Every op is
 * validated again by the store when it runs.
 */
import { useEffect, useRef, useState } from 'react'

import { DEFERRED_REASONS } from '../lib/actionRegistry.js'
import { COCKPIT_COMMAND_EVENT } from '../lib/commandWords.js'

import { PROMPTS, modifyReason, historyStepReason } from './EngineRibbonClusters.jsx'
import { useEngineSessionContext } from './EngineSessionProvider.jsx'
import { readNumber } from './engineSession.js'
import { isPointStep, resolvePromptInputs } from './promptInputs.js'
import { isPointExpression, pointExpressionRefusal, resolvePointExpression } from './pointExpression.js'

// W4g-5c: the clipboard is the third engine group whose words this gate
// admits. A group missing here is dropped SILENTLY (App has already cleared
// the bar), which is what kimi found on #1025: COPYCLIP / CUTCLIP /
// PASTECLIP were registered as words and died here.
const GROUPS = new Set(['draw', 'modify', 'clipboard', 'groups'])
// W4g-7b-05c: the four controls this crate defers. Unlike GROUPS above,
// `deferred` never arms — the word is honest, not a command, so it is
// handled before acceptsCommand rather than folded into its vocabulary.
const DEFERRED_OPS = new Set(Object.keys(DEFERRED_REASONS))
// Ops with no operands run the moment the word arrives: delete on a live
// selection, undo/redo on the engine's own history (W4f slice F).
// W4g-5c: COPYCLIP and CUTCLIP take no operands either; PASTECLIP has a
// prompt (where to put it) and arms like a draw word.
const RUN_ON_ARRIVAL = new Set(['delete', 'explode', 'undo', 'redo', 'copyClip', 'cutClip'])

/** The event detail is a command the engine can take: { group, op } and nothing surprising. */
export function acceptsCommand(detail) {
  if (!detail || typeof detail !== 'object') return false
  const { group, op } = detail
  if (!GROUPS.has(group) || typeof op !== 'string') return false
  return Object.prototype.hasOwnProperty.call(PROMPTS, op) || RUN_ON_ARRIVAL.has(op)
}

export default function CommandLineArmer() {
  const { session, inputs, setInput, armed, setArmed, refuse } = useEngineSessionContext()
  const [cursor, setCursor] = useState({ armed: null, index: 0 })
  const [runRequest, setRunRequest] = useState(null)
  const [focusRequest, setFocusRequest] = useState(0)
  const refocusing = useRef(false)
  useEffect(() => {
    if (runRequest) window.dispatchEvent(new CustomEvent('cockpit:run', { detail: runRequest }))
  }, [runRequest])
  const prompt = armed ? PROMPTS[armed.op] : null
  const index = cursor.armed === armed ? cursor.index : armed?.from ? 1 : 0
  const step = prompt?.steps[index]
  const ask = prompt ? `${prompt.verb}  ${step?.ask || 'Press Run to finish.'}` : ''
  const live = useRef(null)
  live.current = { armed, prompt, index, step, inputs, session }
  useEffect(() => {
    if (!focusRequest) return
    const current = live.current
    if (!current.prompt || current.session.busy) return
    const label = current.step?.fields[0]?.[1]
    const field = label
      ? document.querySelector(`#cockpit-prompt [aria-label="ribbon ${label}"]:not([disabled])`)
      : document.querySelector('#cockpit-prompt [data-testid="cockpit-prompt-run"]:not([disabled])')
    // Programmatic focus follows the cursor; it never chooses a new step.
    refocusing.current = true
    try { field?.focus() } finally { refocusing.current = false }
  }, [focusRequest])
  useEffect(() => {
    const publish = () => window.dispatchEvent(new CustomEvent('cockpit:armed', {
      detail: armed ? { op: armed.op, ask, step: index } : null,
    }))
    publish()
    window.addEventListener('cockpit:armed-request', publish)
    return () => window.removeEventListener('cockpit:armed-request', publish)
  }, [armed, ask, index])
  useEffect(() => () => window.dispatchEvent(new CustomEvent('cockpit:armed', { detail: null })), [])
  useEffect(() => {
    const onRefocus = (event) => {
      if (!live.current.prompt || !event.detail || event.detail.complete === true) return
      event.detail.handled = true
      setFocusRequest((request) => request + 1)
    }
    const onFocus = (event) => {
      const current = live.current
      if (refocusing.current || !current.prompt || !event.target.closest?.('#cockpit-prompt')) return
      const label = event.target.getAttribute('aria-label')
      const next = current.prompt.steps.findIndex((candidate) => candidate.fields.some(([, name]) => label === `ribbon ${name}`))
      if (next >= 0) setCursor({ armed: current.armed, index: next })
    }
    const onPoint = (event) => {
      const detail = event.detail
      const current = live.current
      if (!detail || typeof detail.text !== 'string' || !current.prompt || !current.step) return
      detail.handled = true
      if (current.session.busy) { refuse('Wait for the drawing command to finish.'); return }
      const { fields } = current.step
      const [key, label, mode = 'decimal'] = fields[0]
      const raw = detail.text.trim()
      if (isPointStep(current.step)) {
        let anchor = current.armed.from || null
        const prior = resolvePromptInputs(current.prompt, current.inputs, anchor).effective
        for (const candidate of current.prompt.steps.slice(0, current.index)) {
          if (!isPointStep(candidate)) continue
          const point = candidate.fields.map(([name]) => readNumber(prior[name]))
          if (point.every(Number.isFinite)) anchor = point
        }
        if (key === 'dx') anchor = [0, 0]
        // Command-bar polar distances, like relative pairs, measure from the last point.
        const point = resolvePointExpression(raw, anchor, { relative: true })
        if (!point) {
          setInput(key, raw)
          refuse(`${current.prompt.verb} refused: ${label}: ${pointExpressionRefusal(raw, anchor, { relative: true }) || 'enter a point using x,y.'}`)
          return
        }
        // A live picker owns the point sequence for both input surfaces.
        const pick = { point, key, handled: false }
        window.dispatchEvent(new CustomEvent('cockpit:pick-point', { detail: pick }))
        if (pick.handled) { refuse(pick.refusal || ''); return }
        fields.forEach(([name], offset) => setInput(name, String(point[offset])))
      } else {
        setInput(key, raw)
        if ((mode === 'decimal' || mode === 'decimal-default') && (isPointExpression(raw) || readNumber(raw) === null)) {
          refuse(`${current.prompt.verb} refused: ${label} needs a scalar, not a point or invalid number.`)
          return
        }
      }
      refuse('')
      const next = current.index + 1
      if (current.armed.op === 'createLine' && current.index === 1) setRunRequest({ armed: current.armed })
      else setCursor({ armed: current.armed, index: next })
    }
    const onPicked = (event) => {
      const current = live.current
      const detail = event.detail
      if (!current.prompt || detail?.op !== current.armed.op) return
      const index = current.prompt.steps.findIndex((candidate) => candidate.fields.some(([key]) => key === detail.key))
      if (index < 0) return
      detail.handled = true
      const runLine = current.armed.op === 'createLine' && index === 1
      setCursor({ armed: current.armed, index: runLine ? index : index + 1 })
      if (runLine && detail.run) setRunRequest({ armed: current.armed })
    }
    window.addEventListener('cockpit:focus-step', onRefocus)
    window.addEventListener('focusin', onFocus)
    window.addEventListener('cockpit:picked', onPicked)
    window.addEventListener('cockpit:point', onPoint)
    return () => {
      window.removeEventListener('cockpit:focus-step', onRefocus)
      window.removeEventListener('focusin', onFocus)
      window.removeEventListener('cockpit:picked', onPicked)
      window.removeEventListener('cockpit:point', onPoint)
    }
  }, [setInput, refuse])
  const { applyEdit, undo, redo, copyToClipboard } = session.actions
  useEffect(() => {
    if (typeof window === 'undefined') return undefined
    const onCommand = (event) => {
      const detail = event?.detail
      // A deferred Leader word arms nothing:
      // its reason is surfaced exactly the way a real refusal is, instead of
      // being dropped the way an out-of-contract group is below. The reason
      // must match the frozen sentence for its op, or it is dropped too —
      // never a computed string riding the event.
      if (detail && typeof detail === 'object' && detail.group === 'deferred'
          && typeof detail.op === 'string' && DEFERRED_OPS.has(detail.op)
          && detail.reason === DEFERRED_REASONS[detail.op]) {
        refuse(detail.reason)
        return
      }
      if (!acceptsCommand(detail)) return
      if (detail.op === 'undo' || detail.op === 'redo') {
        const op = detail.op
        const verb = op.toUpperCase()
        const reason = historyStepReason(session, op)
        if (reason) { refuse(`${verb} is unavailable (${reason}).`); return }
        const action = op === 'undo' ? undo : redo
        if (action() === false) refuse(`${verb} is unavailable (engine busy: wait for the current edit).`)
        return
      }
      if (detail.op === 'copyClip' || detail.op === 'cutClip') {
        // Like ERASE: on a live selection it runs, and the store's own
        // refusal sentence covers the rest (no selection, a kind that
        // cannot go on the clipboard).
        copyToClipboard(detail.op === 'cutClip')
        return
      }
      if (RUN_ON_ARRIVAL.has(detail.op)) {
        // ERASE and EXPLODE run on a live selection; otherwise surface the
        // ladder's sentence without arming a prompt.
        const reason = modifyReason(session)
        if (reason) refuse(reason)
        else applyEdit(detail.op, inputs)
        return
      }
      setArmed({ group: detail.group, op: detail.op }, { rearm: true })
    }
    window.addEventListener(COCKPIT_COMMAND_EVENT, onCommand)
    return () => window.removeEventListener(COCKPIT_COMMAND_EVENT, onCommand)
  }, [session, inputs, setArmed, applyEdit, undo, redo, copyToClipboard, refuse])
  return null
}
