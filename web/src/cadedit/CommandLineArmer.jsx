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
import { useEffect } from 'react'

import { COCKPIT_COMMAND_EVENT } from '../lib/commandWords.js'

import { PROMPTS, modifyReason } from './EngineRibbonClusters.jsx'
import { useEngineSessionContext } from './EngineSessionProvider.jsx'

const GROUPS = new Set(['draw', 'modify'])
// Ops with no operands run the moment the word arrives: delete on a live
// selection, undo/redo on the engine's own history (W4f slice F).
const RUN_ON_ARRIVAL = new Set(['delete', 'undo', 'redo'])

/** The event detail is a command the engine can take: { group, op } and nothing surprising. */
export function acceptsCommand(detail) {
  if (!detail || typeof detail !== 'object') return false
  const { group, op } = detail
  if (!GROUPS.has(group) || typeof op !== 'string') return false
  return Object.prototype.hasOwnProperty.call(PROMPTS, op) || RUN_ON_ARRIVAL.has(op)
}

export default function CommandLineArmer() {
  const { session, inputs, setArmed } = useEngineSessionContext()
  const { applyEdit, undo, redo } = session.actions
  useEffect(() => {
    if (typeof window === 'undefined') return undefined
    const onCommand = (event) => {
      const detail = event?.detail
      if (!acceptsCommand(detail)) return
      if (detail.op === 'undo') { undo(); return }
      if (detail.op === 'redo') { redo(); return }
      if (RUN_ON_ARRIVAL.has(detail.op)) {
        // ERASE with a live selection runs, as the ribbon's Delete does; with
        // nothing to act on it arms nothing (the ribbon's note names why).
        if (!modifyReason(session)) applyEdit(detail.op, inputs)
        return
      }
      setArmed({ group: detail.group, op: detail.op })
    }
    window.addEventListener(COCKPIT_COMMAND_EVENT, onCommand)
    return () => window.removeEventListener(COCKPIT_COMMAND_EVENT, onCommand)
  }, [session, inputs, setArmed, applyEdit, undo, redo])
  return null
}
