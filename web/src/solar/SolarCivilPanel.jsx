import React, { useEffect, useRef, useState } from 'react'
import { CIVIL_OPERATIONS, civilSummary, parseCivilDrafts, validateCivilOperation, validateCivilView } from './solarCivilModel.js'
import { civilReason } from './solarCivilClient.js'
import { labelOf } from './solarReadResultModel.js'

const UNKNOWN = 'The publication outcome is unknown, so refresh before retrying the original request.'
const UNAVAILABLE = 'The current physical state could not be read, so publishing stays off.'
const PENDING = 'A terrain operation is in progress, so wait for it to finish.'
const LOST = ['TERRAIN_CLIENT_TIMEOUT', 'TERRAIN_CLIENT_NETWORK', 'TERRAIN_CLIENT_ABORTED', 'TERRAIN_CLIENT_RESPONSE_INVALID']

export default function SolarCivilPanel(props) {
  return <CivilScope key={JSON.stringify([props.drawingId ?? null, props.projectId ?? null])} {...props} />
}

function CivilScope({ drawingId, projectId = null, client, headSignal, disabled = false, onPhysicalHeadChanged }) {
  const [phase, setPhase] = useState(drawingId ? 'loading' : 'no-drawing')
  const [view, setView] = useState(null)
  const [outcome, setOutcome] = useState(null)
  const [refusal, setRefusal] = useState(null)
  const [announcement, setAnnouncement] = useState('')
  const [fieldError, setFieldError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [drafts, setDrafts] = useState({ operation: 'frame-generate', boundary: '', preset: '', pileTemplate: '', drawingUnits: '', mode: 'Auto', value: '' })
  const alive = useRef(false)
  const epoch = useRef(0)
  const readGeneration = useRef(0)
  const readController = useRef(null)
  const lock = useRef(null)
  const runButton = useRef(null)
  const refreshButton = useRef(null)
  const focusAfter = useRef(null)
  const callback = useRef(onPhysicalHeadChanged)
  callback.current = onPhysicalHeadChanged

  useEffect(() => {
    const target = focusAfter.current
    if (!busy && target) {
      focusAfter.current = null
      if (document.activeElement === target || document.activeElement === document.body) target.focus()
    }
  }, [busy])

  async function read(refresh = false, preserve = false) {
    const generation = ++readGeneration.current
    const at = epoch.current
    readController.current?.abort()
    const controller = new AbortController()
    readController.current = controller
    setView(null)
    setPhase(refresh ? 'refreshing' : 'loading')
    if (!preserve) setRefusal(null)
    let answer
    try { answer = await client?.getCivil({ drawingId, projectId, signal: controller.signal }) } catch { answer = null }
    if (!alive.current || generation !== readGeneration.current || at !== epoch.current) return null
    const value = answer?.ok ? validateCivilView(answer.value, { drawingId, projectId }) : null
    if (value === null) {
      setPhase('refused')
      if (!preserve) setRefusal(UNAVAILABLE)
      return null
    }
    setView(value)
    setPhase('ready')
    return value
  }

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
      epoch.current++
      readController.current?.abort()
      lock.current?.controller.abort()
    }
  }, [])

  useEffect(() => {
    epoch.current++
    setOutcome(null)
    setAnnouncement('')
    if (drawingId) void read(false)
    else { setPhase('no-drawing'); setView(null) }
    return () => { readController.current?.abort() }
  }, [drawingId, projectId, headSignal, client])

  async function refresh() {
    if (lock.current || !drawingId) return
    const focus = document.activeElement === refreshButton.current
    const value = await read(true)
    if (alive.current && focus && (document.activeElement === document.body || document.activeElement === refreshButton.current)) refreshButton.current?.focus()
    if (alive.current && value) setAnnouncement('ready')
  }

  async function submit(event) {
    event.preventDefault()
    if (lock.current || disabled || view === null || !drawingId) return
    const parsed = parseCivilDrafts(drafts)
    if (!parsed.ok) { setFieldError(parsed); setRefusal(civilReason(parsed.code)); return }
    if (view.head === null && drafts.operation !== 'frame-generate') {
      setRefusal(civilReason('FRAMES_PILES_STATE_REQUIRED')); return
    }
    const body = { operation: drafts.operation, expected_head: view.head?.state.artifact_id ?? null, ...parsed.fields }
    const token = { epoch: epoch.current, controller: new AbortController() }
    lock.current = token
    const target = runButton.current
    const restore = document.activeElement === target
    setBusy(true); setPhase('pending'); setRefusal(null); setFieldError(null); setOutcome(null); setAnnouncement(PENDING)
    let answer
    try { answer = await client?.runCivilOperation({ drawingId, projectId, body, signal: token.controller.signal }) }
    catch { answer = { ok: false, code: 'TERRAIN_CLIENT_NETWORK' } }
    if (!alive.current || lock.current !== token) return
    if (token.epoch !== epoch.current) { lock.current = null; setBusy(false); return }
    const value = answer?.ok ? validateCivilOperation(answer.value, { drawingId, projectId, body }) : null
    if (value !== null) {
      setOutcome(value); setAnnouncement(value.outcome)
      if (value.created) {
        try { callback.current?.(value) } catch { /* An observer cannot replace the answer. */ }
      }
      if (alive.current && token.epoch === epoch.current) await read(true, true)
    } else {
      const code = answer?.ok ? 'TERRAIN_CLIENT_RESPONSE_INVALID' : answer?.code ?? 'TERRAIN_CLIENT_RESPONSE_INVALID'
      setPhase('refused')
      if (LOST.includes(code)) { setView(null); setRefusal(UNKNOWN); setAnnouncement(UNKNOWN) }
      else {
        setRefusal(civilReason(code)); setAnnouncement(civilReason(code))
        if (['FRAMES_PILES_STALE_BASE', 'PHYSICAL_HEAD_CONFLICT'].includes(code)) await read(true, true)
      }
    }
    if (!alive.current || lock.current !== token) return
    lock.current = null; setBusy(false)
    if (token.epoch === epoch.current && restore && (document.activeElement === target || document.activeElement === document.body)) focusAfter.current = target
  }

  const locked = busy || disabled
  const confirmed = civilSummary(view)
  const last = civilSummary(outcome)
  const summary = { ...confirmed, outcome: last.outcome, summary: last.summary }
  const operation = drafts.operation
  const update = (key) => (event) => { setDrafts((v) => ({ ...v, [key]: event.target.value })); setFieldError(null) }
  const errorFor = (key) => fieldError?.field === key
  const jsonField = (key, draftKey = key) => <div key={key}>
    <label htmlFor={`civil-${key}`}>{labelOf(key)}</label>
    <textarea id={`civil-${key}`} value={drafts[draftKey]} onChange={update(draftKey)} disabled={locked}
      aria-invalid={errorFor(key) || undefined} aria-describedby={errorFor(key) ? 'civil-field-error' : undefined} />
  </div>
  if (!drawingId) return <section><p>Open a drawing to view terrain previews</p></section>
  return <section aria-label="Civil operations" data-state={phase}>
    {phase === 'loading' && <p>Reading the current physical state.</p>}
    {phase === 'refreshing' && <p>Refreshing the current physical state.</p>}
    {disabled && <p>A run is in progress, so wait for it to finish.</p>}
    <form onSubmit={submit}>
      <label htmlFor="civil-operation">{labelOf('operation')}</label>
      <select id="civil-operation" value={operation} onChange={update('operation')} disabled={locked}>
        {CIVIL_OPERATIONS.map((op) => <option key={op} value={op}>{labelOf(op.replaceAll('-', ' '))}</option>)}
      </select>
      {['frame-generate', 'grade-pad'].includes(operation) && jsonField('boundary')}
      {['frame-generate', 'piling-generate', 'pile-length-range-check'].includes(operation) && jsonField('preset')}
      {operation === 'piling-generate' && jsonField('pile_template', 'pileTemplate')}
      {operation === 'frame-generate' && <div>
        <label htmlFor="civil-units">{labelOf('drawing_units')}</label>
        <select id="civil-units" value={drafts.drawingUnits} onChange={update('drawingUnits')} disabled={locked}
          aria-invalid={errorFor('drawing_units') || undefined} aria-describedby={errorFor('drawing_units') ? 'civil-field-error' : undefined}>
          <option value="">Choose drawing units</option><option value="m">m</option><option value="ft">ft</option>
        </select>
      </div>}
      {operation === 'grade-pad' && <>
        <label htmlFor="civil-mode">{labelOf('mode')}</label>
        <select id="civil-mode" value={drafts.mode} onChange={update('mode')} disabled={locked}
          aria-invalid={errorFor('mode') || undefined} aria-describedby={errorFor('mode') ? 'civil-field-error' : undefined}>
          {['Auto', 'Manual', 'Clearance'].map((mode) => <option key={mode}>{mode}</option>)}
        </select>
        <label htmlFor="civil-value">{labelOf('value_du')}</label>
        <input id="civil-value" inputMode="decimal" value={drafts.value} onChange={update('value')} disabled={locked}
          aria-invalid={errorFor('value_du') || undefined} aria-describedby={errorFor('value_du') ? 'civil-field-error' : undefined} />
      </>}
      {fieldError && <p id="civil-field-error">{civilReason(fieldError.code)}</p>}
      <button ref={runButton} type="submit" disabled={locked || view === null}>Run</button>
      <button ref={refreshButton} type="button" onClick={refresh} disabled={busy}>Refresh physical state</button>
    </form>
    {refusal && <p role="alert">{refusal}</p>}
    <p role="status" aria-live="polite">{announcement}</p>
    {summary.outcome && <p>{labelOf('outcome')}: {summary.outcome}</p>}
    <dl>{summary.counts.map((row) => <React.Fragment key={`count:${row.key}`}>
      <dt>{row.label}</dt><dd>{row.text}</dd>
    </React.Fragment>)}{summary.summary.map((row) => <React.Fragment key={`summary:${row.key}`}>
      <dt>{row.label}</dt><dd>{row.text}</dd>
    </React.Fragment>)}</dl>
    {summary.standing.map((row) => <section key={row.entity} aria-label={`${labelOf(row.entity)} standing`}>
      <p>{labelOf(row.entity)}: {row.state}</p><dl><dt>Checked</dt><dd>{row.checked}</dd><dt>Stale</dt><dd>{row.stale}</dd></dl>
    </section>)}
  </section>
}
