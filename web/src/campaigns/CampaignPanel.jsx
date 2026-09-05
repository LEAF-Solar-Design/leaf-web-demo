import { useEffect, useRef, useState } from 'react'
import useCampaigns from './useCampaigns.js'
import './campaigns.css'

const conflictMessage = 'This question already has a different recorded answer. Reload to see it.'
const messageOf = error => error?.code === 'answer_conflict' ? conflictMessage : error?.message || String(error)
const statusWords = { accepted: 'Accepted, not running', running: 'Running', succeeded: 'Succeeded', failed: 'Failed', cancelled: 'Cancelled' }

function Alert({ error, onReload, retry = 'Reload' }) {
  const ref = useRef(null)
  const refresh = useAction()
  useEffect(() => { if (error) ref.current?.focus() }, [error])
  if (!error) return null
  return <div className="campaign-error" role="alert" tabIndex={-1} ref={ref}>
    <p>{messageOf(error)}</p>
    {onReload && <button type="button" className="chip-act" disabled={refresh.busy} aria-busy={refresh.busy} onClick={() => refresh.run(onReload, 'Campaigns reloaded.')}>{retry}</button>}
    {refresh.outcome && <span role="status">{refresh.outcome}</span>}
  </div>
}

function useAction() {
  const [error, setError] = useState(null)
  const [outcome, setOutcome] = useState('')
  const [busy, setBusy] = useState(false)
  const active = useRef(false)
  const lock = useRef(false)
  useEffect(() => { active.current = true; return () => { active.current = false } }, [])
  async function run(operation, success) {
    if (lock.current) return
    lock.current = true
    setBusy(true)
    setError(null)
    setOutcome('')
    try {
      const result = await operation()
      if (active.current && result != null) setOutcome(success)
    } catch (failure) {
      if (active.current) setError(failure)
    } finally {
      lock.current = false
      if (active.current) setBusy(false)
    }
  }
  return { error, outcome, busy, run }
}

function check(value, field, max) {
  if (!value.trim() || value.length > max) {
    throw Object.assign(new Error(`${field[0].toUpperCase() + field.slice(1)} must contain 1 to ${max} characters.`), { invalidField: field })
  }
}

function SubmitForm({ campaign }) {
  const [title, setTitle] = useState('')
  const [prompt, setPrompt] = useState('')
  const action = useAction()
  const busy = action.busy || !!campaign.pending.submit
  const field = action.error?.invalidField
  return <form className="panel-sub" noValidate onSubmit={event => {
    event.preventDefault()
    action.run(() => {
      check(title, 'title', 200)
      check(prompt, 'prompt', 32768)
      return campaign.submit({ title, prompt })
    }, 'Campaign recorded.')
  }}>
    <h3>Submit a campaign</h3>
    <label>Title<input value={title} maxLength={200} aria-invalid={field === 'title'} onChange={event => setTitle(event.target.value)} /></label>
    {field === 'title' && <Alert error={action.error} />}
    <label>Prompt<textarea value={prompt} maxLength={32768} aria-invalid={field === 'prompt'} onChange={event => setPrompt(event.target.value)} /></label>
    <span className="dim" aria-live="polite">{32768 - prompt.length} characters remaining</span>
    {field === 'prompt' && <Alert error={action.error} />}
    <button type="submit" className="btn primary" disabled={busy} aria-busy={busy}>Submit campaign</button>
    {field !== 'title' && field !== 'prompt' && <Alert error={action.error} onReload={campaign.refetch} />}
    <span role="status">{action.outcome}</span>
  </form>
}

function AskForm({ campaign }) {
  const [prompt, setPrompt] = useState('')
  const action = useAction()
  const busy = action.busy || !!campaign.pending.ask
  return <form noValidate onSubmit={event => {
    event.preventDefault()
    action.run(() => {
      check(prompt, 'question', 4096)
      return campaign.ask({ prompt })
    }, 'Question recorded.')
  }}>
    <label>Follow-up question<textarea maxLength={4096} value={prompt} onChange={event => setPrompt(event.target.value)} /></label>
    <button type="submit" className="chip-act" disabled={busy} aria-busy={busy}>Ask</button>
    <Alert error={action.error} onReload={campaign.refetch} />
    <span role="status">{action.outcome}</span>
  </form>
}

function AnswerForm({ question, campaign }) {
  const [draft, setDraft] = useState('')
  const action = useAction()
  const busy = action.busy || !!campaign.pending[`answer:${question.question_id}`]
  return <form noValidate onSubmit={event => {
    event.preventDefault()
    action.run(() => {
      check(draft, 'answer', 8192)
      return campaign.answer(question.question_id, draft)
    }, 'Answer recorded.')
  }}>
    <label>Answer<textarea maxLength={8192} value={draft} onChange={event => setDraft(event.target.value)} /></label>
    <button type="submit" className="chip-act" disabled={busy} aria-busy={busy}>Record answer</button>
    <Alert error={action.error} onReload={campaign.refetch} />
    <span role="status">{action.outcome}</span>
  </form>
}

function SignedInPanel({ projectId, projectName }) {
  const campaign = useCampaigns(projectId, { enabled: true })
  const selected = campaign.selected
  return <>
    <p className="dim">{projectName ? `${projectName} / Campaign` : 'Project / Campaign'}</p>
    <SubmitForm key={campaign.selectedId || 'new'} campaign={campaign} />
    {campaign.status === 'loading' && <div role="status" aria-label="Loading campaigns">
      <div className="skeleton-stack" aria-hidden="true"><div className="skeleton-row" /><div className="skeleton-row" /></div>
    </div>}
    {campaign.campaigns.length > 0 && <label>Active campaign
      <select aria-label="Active campaign" aria-busy={campaign.refreshing} value={campaign.selectedId || ''} onChange={event => campaign.select(event.target.value)}>
        {campaign.campaigns.map(row => <option key={row.campaign_id} value={row.campaign_id}>{row.title}</option>)}
      </select>
    </label>}
    {selected && <div className="panel-sub campaign-status" data-state={selected.status}>
      <h3>{selected.title}</h3>
      <p role="status">{statusWords[selected.status] || selected.status}</p>
      {selected.dispatch?.available === false && <p role="status">The build fleet is not connected yet.</p>}
      {selected.dispatch?.available === true && <p role="status">Build fleet available</p>}
    </div>}
    {selected && <div className="panel-sub" key={campaign.selectedId}>
      <h3>Questions</h3>
      <ul className="campaign-questions">
        {campaign.questions.map(question => {
          const recorded = campaign.answers[question.question_id]
          const text = typeof recorded === 'string' ? recorded : recorded?.answer
          return <li key={question.question_id} data-state={question.status}>
            <p>{question.prompt}</p>
            {question.status === 'answered'
              ? <p role="status" className="campaign-answer">{text ?? 'The recorded answer text is unavailable. Reload to retrieve it.'}</p>
              : <AnswerForm question={question} campaign={campaign} />}
          </li>
        })}
      </ul>
      <AskForm campaign={campaign} />
    </div>}
    {campaign.refreshing && <p role="status">Updating campaigns…</p>}
    {campaign.status === 'ready' && campaign.campaigns.length === 0 && <p role="status">No campaigns yet.</p>}
    {campaign.error && campaign.errorAction === 'load' && <div className="project-lifecycle-stale">
      <Alert error={campaign.error} onReload={campaign.refetch} retry="Try again" />
    </div>}
  </>
}

export default function CampaignPanel({ projectId, projectName, signedIn, enabled = true }) {
  if (!enabled || !projectId) return null
  return <section className="campaign-panel" aria-label="Campaign">
    {signedIn ? <SignedInPanel key={projectId} projectId={projectId} projectName={projectName} />
      : <p role="status">Sign in to submit a campaign.</p>}
  </section>
}
