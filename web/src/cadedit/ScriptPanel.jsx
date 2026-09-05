// W4g-7a SCRIPT: the reference's .scr in the browser. A script is command
// words with their operands, one per line, run in order through the SAME
// parser the command line uses and the SAME prompt grammar the ribbon shows
// (script.js), each line costing exactly what the drafter's own run of that
// command costs: one engine round trip and one undo step. The first line the
// store or the engine refuses stops the script with its number and the
// refusal's own sentence; the lines before it stay applied (each is its own
// undo step, as in the reference).
//
// The runner is a small state machine over the session: a line is dispatched
// through the store's own actions, then the session is watched until the
// engine has answered (busy rose and fell) or the store refused before any
// post (its status changed with an error kind), bounded by LINE_BUDGET_MS so
// a reply that never comes cannot hang the script.
import { useCallback, useEffect, useRef, useState } from 'react'

import { clipboardReason, drawReason, modifyReason } from '../lib/actionRegistry.js'
import { parseDrawingCommand } from '../lib/commandWords.js'
import { PROMPTS } from './EngineRibbonClusters.jsx'
import { useEngineSessionContext } from './EngineSessionProvider.jsx'
import { buildCreatePayload, buildEditPayload } from './engineSession.js'
import { resolvePromptInputs } from './promptInputs.js'
import { BARE_OPS, MAX_SCRIPT_CHARS, parseScript } from './script.js'

/** The longest one line may wait for the engine's answer. */
export const LINE_BUDGET_MS = 60_000

// The registry's own rule (actionRegistry engineOp.when): a draw create by
// the Draw ladder, PASTE by the clipboard ladder (it needs a record, not a
// selection), and every other op, COPY and CUT included, by the Modify
// ladder (they need a selection; kimi, #1049).
function gateFor(group, op, session, reach) {
  if (group === 'draw') return drawReason(session, reach)
  if (op === 'pasteClip') return clipboardReason(session, reach)
  return modifyReason(session, reach)
}

export default function ScriptPanel() {
  const { session, inputs, reach } = useEngineSessionContext()
  const [text, setText] = useState('')
  const [report, setReport] = useState({ phase: 'idle', text: '' })
  // The latest session and inputs, read by the runner between renders.
  const sessionRef = useRef(session)
  sessionRef.current = session
  const inputsRef = useRef(inputs)
  inputsRef.current = inputs
  const reachRef = useRef(reach)
  reachRef.current = reach
  // The run in progress: its lines, the line awaiting the engine, and what
  // the session read when that line was dispatched.
  const runRef = useRef(null)
  const timerRef = useRef(null)

  const stop = useCallback((phase, sentence) => {
    if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null }
    runRef.current = null
    setReport({ phase, text: sentence })
  }, [])

  const dispatch = useCallback((run, index) => {
    const line = run.lines[index]
    const current = sessionRef.current
    const { actions } = current
    // UNDO and REDO are session steps, not group ops: their gate is the depth.
    const gate = line.op === 'undo' || line.op === 'redo'
      ? (current.busy ? drawReason(current, reachRef.current) : (line.op === 'undo' ? current.undoDepth : current.redoDepth) > 0 ? '' : `nothing to ${line.op}`)
      : gateFor(line.group, line.op, current, reachRef.current)
    if (gate) { stop('stopped', `Script stopped at line ${line.line}: ${line.verb} is unavailable (${gate}).`); return }
    const before = { status: current.status, count: current.entityCount, undo: current.undoDepth, redo: current.redoDepth, clipboard: current.clipboard }
    run.awaiting = { index, sawBusy: false, answered: false, before, at: Date.now() }
    if (BARE_OPS.has(line.op)) {
      if (line.op === 'undo') actions.undo()
      else if (line.op === 'redo') actions.redo()
      else if (line.op === 'copyClip') {
        // COPY touches no engine op: it is answered the moment it returns,
        // whether or not its status sentence differs from the last one (the
        // same selection copied twice reads the same sentence; kimi, #1046).
        actions.copyToClipboard(false)
        run.awaiting.answered = true
      } else if (line.op === 'cutClip') actions.copyToClipboard(true)
      else actions.applyEdit(line.op, {})
    } else {
      const prompt = PROMPTS[line.op]
      const merged = { ...inputsRef.current, ...line.inputs }
      const { effective, expressionRefusal, waitingStep } = resolvePromptInputs(prompt, merged, null)
      if (expressionRefusal) { stop('stopped', `Script stopped at line ${line.line}: ${expressionRefusal}`); return }
      if (waitingStep) { stop('stopped', `Script stopped at line ${line.line}: ${line.verb} still needs "${waitingStep.ask}"`); return }
      if (line.op !== 'pasteClip') {
        const checked = line.group === 'draw'
          ? buildCreatePayload(line.op, effective)
          : buildEditPayload(line.op, current.selectedId, effective)
        if (checked.refusal) { stop('stopped', `Script stopped at line ${line.line}: ${checked.refusal}`); return }
      }
      if (line.group === 'draw') actions.create(line.op, effective)
      else if (line.op === 'pasteClip') actions.pasteFromClipboard(effective)
      else actions.applyEdit(line.op, effective)
    }
    if (timerRef.current) clearTimeout(timerRef.current)
    timerRef.current = setTimeout(() => {
      if (runRef.current === run && run.awaiting && run.awaiting.index === index) {
        stop('stopped', `Script stopped at line ${line.line}: the engine did not answer within ${LINE_BUDGET_MS / 1000} s.`)
      }
    }, LINE_BUDGET_MS)
    // The source line number (what the drafter sees in the box) and the
    // command's place in the run (comments and blanks do not count).
    setReport({ phase: 'running', text: `Running line ${line.line}: ${line.verb} (${index + 1} of ${run.lines.length})...` })
  }, [stop])

  // Watch the session for the awaited line's answer, then advance or stop.
  useEffect(() => {
    const run = runRef.current
    if (!run || !run.awaiting) return
    const { index, before } = run.awaiting
    const line = run.lines[index]
    if (session.busy) { run.awaiting.sawBusy = true; return }
    const changed = session.status !== before.status || session.entityCount !== before.count
      || session.undoDepth !== before.undo || session.redoDepth !== before.redo || session.clipboard !== before.clipboard
    if (!run.awaiting.sawBusy && !changed && !run.awaiting.answered) return
    if (session.errorKind) { stop('stopped', `Script stopped at line ${line.line}: ${session.status}`); return }
    run.awaiting = null
    const next = index + 1
    if (next >= run.lines.length) { stop('done', `Script ran ${run.lines.length} command${run.lines.length === 1 ? '' : 's'}.`); return }
    dispatch(run, next)
  }, [session, dispatch, stop])

  useEffect(() => () => { if (timerRef.current) clearTimeout(timerRef.current) }, [])

  const runScript = () => {
    if (runRef.current) return
    const parsed = parseScript(text, parseDrawingCommand, PROMPTS)
    if (parsed.refusal) { setReport({ phase: 'stopped', text: `Script stopped before running: ${parsed.refusal}.` }); return }
    if (!parsed.lines.length) { setReport({ phase: 'stopped', text: 'Script stopped before running: no command lines.' }); return }
    const run = { lines: parsed.lines, awaiting: null }
    runRef.current = run
    dispatch(run, 0)
  }
  const onFile = (event) => {
    const file = event.target.files && event.target.files[0]
    // Choosing the same file again must read it again (an edited .scr under
    // the same name): the input forgets its value once the file is taken.
    event.target.value = ''
    if (!file) return
    if (file.size > MAX_SCRIPT_CHARS) { setReport({ phase: 'stopped', text: `Script stopped before running: the file is longer than ${MAX_SCRIPT_CHARS} characters.` }); return }
    file.text().then((content) => setText(content)).catch(() => setReport({ phase: 'stopped', text: 'Script stopped before running: the file could not be read.' }))
  }
  const gate = drawReason(session, reach)
  const running = report.phase === 'running'
  const hold = running ? 'a script is running' : gate || (!text.trim() ? 'enter or choose a script' : '')
  return (
    <div className="ribbon-cluster-tools cockpit-script" data-testid="cockpit-script">
      <textarea
        className="cp-input cp-script"
        aria-label="ribbon script"
        placeholder="line 0,0 10,10"
        rows={3}
        value={text}
        onChange={(event) => setText(event.target.value.slice(0, MAX_SCRIPT_CHARS))}
        disabled={running}
        spellCheck={false}
      />
      <span className="cp-actions">
        <label className="cp-field">
          <input type="file" accept=".scr,.txt" aria-label="Script file" onChange={onFile} disabled={running} />
        </label>
        <button
          type="button"
          className="cp-run"
          data-testid="cockpit-script-run"
          disabled={!!hold}
          title={hold || 'Run the script, one command per line'}
          aria-label={hold ? `Run script (unavailable: ${hold})` : 'Run script'}
          onClick={runScript}
        >
          Run script
        </button>
      </span>
      <span className="cp-note" data-testid="cockpit-script-status" data-phase={report.phase}>{report.text}</span>
    </div>
  )
}
