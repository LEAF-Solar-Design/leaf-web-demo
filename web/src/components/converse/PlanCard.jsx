import { useEffect, useRef, useState } from 'react'
import { resolveApproval } from '../../converse.js'
import { track } from '../../telemetry.js'
import './inlineAuthoring.css'

export const capabilityLabel = (value) => value === 'drawing.write' ? 'Changes your drawing' : value

function Items({ title, items, ordered }) {
  const List = ordered ? 'ol' : 'ul'
  return <div><h5>{title}</h5>{items?.length
    ? <List>{items.map((item, i) => <li key={i}>{item}</li>)}</List> : <p>None</p>}</div>
}

export default function PlanCard({ sessionId, proposal, replacedBy, expiresAt, resolved, pending, disabled, onResumed }) {
  const { confirmation_id: id, revision, plan } = proposal
  const [editing, setEditing] = useState(false)
  const [feedback, setFeedback] = useState('')
  const [busy, setBusy] = useState(false)
  const [recorded, setRecorded] = useState(null)
  const [done, setDone] = useState(null)
  const [expired, setExpired] = useState(false)
  const [error, setError] = useState('')
  const lastDeadline = useRef(NaN)
  const suppliedDeadline = typeof expiresAt === 'number'
    ? (expiresAt < 1e12 ? expiresAt * 1000 : expiresAt) : Date.parse(expiresAt)
  if (Number.isFinite(suppliedDeadline)) lastDeadline.current = suppliedDeadline
  const deadline = lastDeadline.current
  useEffect(() => {
    if (!Number.isFinite(deadline)) return
    const remaining = deadline - Date.now()
    if (remaining <= 0) { setExpired(true); return }
    setExpired(false)
    const timer = setTimeout(() => setExpired(true), remaining)
    return () => clearTimeout(timer)
  }, [deadline])
  const live = useRef(false)
  useEffect(() => {
    live.current = !replacedBy && !expired
    return () => { live.current = false }
  }, [replacedBy, expired])
  const resume = recorded || (pending?.resume_required ? { approved: pending.approved } : null)
  const decision = done ?? (!resume ? resolved?.approved : null)
  const decide = async (approved, note) => {
    if (busy || disabled || !live.current || Date.now() >= deadline || decision != null) return
    setBusy(true)
    setError('')
    try {
      const result = await resolveApproval(id, sessionId, approved, !!resume, {
        feedback: note,
        onRecorded: () => setRecorded({ approved, feedback: note }),
        isCurrent: () => live.current && !(Date.now() >= deadline),
      })
      if (result.held) return
      setDone(approved)
      if (live.current && result.turn_id) onResumed?.(result.turn_id, approved)
      if (approved) track('plan.approved', { revision })
      else track(note ? 'plan.revised' : 'plan.cancelled')
    } catch (e) {
      if (e.status === 410) setExpired(true)
      else setError(e.secretRefused ? e.message : 'Could not complete the plan decision. Try again.')
    } finally {
      setBusy(false)
    }
  }
  const state = replacedBy ? `Replaced by revision ${replacedBy}` : decision != null
    ? (decision ? 'Approved' : 'Denied') : expired ? 'Expired, ask to re-propose' : null
  return <section className="inline-authoring" aria-label="Build plan" data-converse-role="plan">
    <header><small>Plan · Revision {revision}</small><h4>{plan.title}</h4>
      <p>{plan.target === 'branch' ? 'Branch tool, runs inside AutoCAD' : 'Studio tool'}</p>
      <p>{plan.change_kind === 'revise' ? `Revise ${plan.target_tool_name}` : `New tool ${plan.tool_name}`}</p>
    </header>
    <p>{plan.summary}</p>
    <Items title="Behavior" items={plan.behavior} ordered />
    <div><h5>Inputs</h5>{plan.inputs?.length ? <table aria-label="Inputs">
      <thead><tr>{['Name', 'Type', 'Default', 'Description'].map((name) => <th key={name}>{name}</th>)}</tr></thead>
      <tbody>{plan.inputs.map((input) => <tr key={input.name}><td>{input.name}</td><td>{input.type}</td>
        <td>{String(input.default ?? '—')}</td><td>{input.description}</td></tr>)}</tbody></table> : <p>None</p>}</div>
    <div><h5>Output</h5><p>{plan.output}</p></div>
    <Items title="Capabilities" items={plan.capabilities?.map(capabilityLabel)} />
    <div><h5>Undo</h5><p>{plan.undo || 'None specified'}</p></div>
    <Items title="Assumptions" items={plan.assumptions} />
    <Items title="Acceptance checks" items={plan.acceptance} />
    <Items title="Your answers" items={Object.entries(plan.answers || {}).map(([key, answer]) =>
      `${key} · ${answer.other ?? answer.labels?.join(', ')}`)} />
    {state ? <p role="status">{state}</p> : resume
      ? <button type="button" className="chip-act" disabled={busy || disabled}
        onClick={() => decide(resume.approved, resume.feedback)}>Resume {resume.approved ? 'approval' : 'denial'}</button>
      : <div className="inline-actions">
        <button type="button" className="chip-act" disabled={busy || disabled} onClick={() => decide(true)}>Approve and build</button>
        <button type="button" className="chip-neutral" disabled={busy || disabled} aria-expanded={editing}
          onClick={() => setEditing(!editing)}>Keep planning</button>
        <button type="button" className="chip-neutral" disabled={busy || disabled} onClick={() => decide(false)}>Cancel</button>
        {editing && <div className="inline-comment"><label>What should change?
          <textarea maxLength={1000} value={feedback} disabled={busy || disabled} onChange={(e) => setFeedback(e.target.value)} /></label>
          <button type="button" className="chip-act" disabled={!feedback.trim() || busy || disabled}
            onClick={() => decide(false, feedback.trim())}>Send feedback</button>
        </div>}
      </div>}
    {error && !state && <p role="alert">{error}</p>}
  </section>
}
