import { useEffect, useRef, useState } from 'react'
import useCampaigns from './useCampaigns.js'
import './campaigns.css'

const conflictMessage = 'This question already has a different recorded answer. Reload to see it.'
const messageOf = error => error?.code === 'answer_conflict' ? conflictMessage : error?.message || String(error)
const statusWords = { accepted: 'Accepted, not running', running: 'Running', succeeded: 'Succeeded', failed: 'Failed', cancelled: 'Cancelled' }
const executionWords = value => ({ reconcile_required: 'Outcome unknown, reconciliation required', claimed: 'In progress', pending: 'Waiting' })[value]
  || String(value || '').replaceAll('_', ' ')
const nativeRelease = row => row.capability === 'campaign.native-release' || row.capability_link?.capability === 'campaign.native-release'

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

function CapabilityControls({ campaign, row }) {
  const [choice, setChoice] = useState('')
  const action = useAction()
  const tools = campaign.capabilities || []
  const selected = tools.find(tool => tool.change_set_id === choice) || tools[0]
  const link = row.capability_link
  const submission = campaign.submissions?.[row.enrollment_id]
  const recovered = campaign.invocationResults?.[row.enrollment_id]
  const invocations = [...(row.invocations || [])]
  if (recovered && !invocations.some(job => job.job_id === recovered.job_id)) invocations.push(recovered)
  const count = Number.isInteger(row.completed_uses) && row.completed_uses >= 0 && row.completed_uses <= 2
    ? row.completed_uses : null
  const complete = count === 2 && link?.state === 'completed'
  const busy = action.busy || !!campaign.pending[`enrollment:${row.enrollment_id}`]
  const waiting = invocations.some(job => !['complete', 'failed'].includes(job.status))
  const bound = ['published', 'invoked_once', 'completed'].includes(link?.state)
  return <div className="campaign-capability">
    {!bound && row.state !== 'revoked' && <>
      {selected ? <>
        <label>Published tool for {row.machine_id}<select value={selected.change_set_id} disabled={busy || !!submission}
          onChange={event => setChoice(event.target.value)}>
          {tools.map(tool => <option key={tool.change_set_id} value={tool.change_set_id}>{tool.label || tool.tool_name}</option>)}
        </select></label>
        <button type="button" className="chip-act" disabled={busy || !!submission} aria-busy={busy}
          onClick={() => action.run(() => campaign.bindPublication(row.enrollment_id, selected.change_set_id), 'Published tool bound.')}>
          Bind published tool
        </button>
      </> : <p>No published tools are available. Reload status after publication.</p>}
    </>}
    <p role="status">{count === null ? 'Verified use count unavailable.' : `Verified uses: ${count} of 2.`}
      {complete ? ' Capability complete.' : ' Capability not complete.'}</p>
    <ul>{invocations.map(job => <li key={job.job_id}>
      <p role="status">{executionWords(job.status)}{job.progress && typeof job.progress === 'string' ? `: ${executionWords(job.progress)}` : ''}</p>
      {job.reason && <p>{job.reason}</p>}
      {job.status === 'complete' && job.counted !== true && <p>Verified receipt missing or not accepted. This use is not verified.</p>}
      {job.counted === true && <p>Verified use recorded.</p>}
    </li>)}</ul>
    {submission ? <>
      <p role="status">Submission outcome unknown. Recover the existing submission before another use.</p>
      <button type="button" className="chip-act" disabled={busy} aria-busy={busy}
        onClick={() => action.run(() => campaign.invokeCapability(row.enrollment_id), 'Submission recovered. Reload status for verified progress.')}>
        Recover submission
      </button>
    </> : bound && !complete && <button type="button" className="chip-act"
      disabled={busy || waiting || row.state !== 'enabled' || !link.effective_catalog_digest || count === null || count >= 2}
      aria-busy={busy} onClick={() => action.run(() => campaign.invokeCapability(row.enrollment_id), 'Submission recorded. Awaiting verified receipt.')}>
      {count === 1 ? 'Use again' : 'Use capability'}
    </button>}
    <button type="button" className="chip-act" disabled={busy || campaign.refreshing}
      onClick={() => action.run(campaign.refetch, 'Capability status reloaded.')}>Reload status</button>
    <Alert error={action.error} />
    <span role="status">{action.outcome}</span>
  </div>
}

function EnrollmentPanel({ campaign }) {
  const [machine, setMachine] = useState('')
  const [capability, setCapability] = useState('campaign.host-enrollment')
  const action = useAction()
  const machines = campaign.allowedMachines || []
  const selected = machines.includes(machine) ? machine : machines[0] || ''
  const busy = action.busy || !!campaign.pending.enroll
  return <section className="panel-sub" aria-label="Enrollment">
    <h3>Enrollment</h3>
    <Alert error={campaign.enrollmentError} onReload={campaign.refetch} />
    <Alert error={campaign.capabilityError} onReload={campaign.refetch} />
    {campaign.recoveryUnavailable && <p role="status">Browser storage is unavailable. Retry keeps the same submission in this view, but reconnect recovery is unavailable.</p>}
    {machines.length === 0 ? <p>No campaign machines are configured. Ask your workspace operator to configure a host.</p> : <>
      <label>Registration capability<select value={capability} disabled={busy} onChange={event => setCapability(event.target.value)}>
        <option value="campaign.host-enrollment">Connect build host</option>
        <option value="campaign.native-release">Prepare native AWS release</option>
      </select></label>
      <label>Campaign machine<select value={selected} disabled={busy} onChange={event => setMachine(event.target.value)}>
        {machines.map(value => <option key={value} value={value}>{value}</option>)}
      </select></label>
      <button type="button" className="btn primary" disabled={busy || !selected} aria-busy={busy}
        onClick={() => action.run(() => capability === 'campaign.host-enrollment'
          ? campaign.enroll(selected) : campaign.enroll(selected, capability),
        capability === 'campaign.host-enrollment' ? 'Host enrollment recorded.' : 'Native release registration recorded.')}>
        {capability === 'campaign.host-enrollment' ? `Connect ${selected} to this campaign` : 'Prepare native AWS release'}
      </button>
    </>}
    <ul>{(campaign.enrollments || []).map(row => <li key={row.enrollment_id}>
      <p>{row.machine_id}: {executionWords(row.state)}</p>
      {nativeRelease(row) ? <p role="status">Native AWS release: Setup required. {row.readiness_message || 'The release executor is not connected.'}</p> : <>
        {row.capability_link?.state === 'pending_link' && <p>Capability not yet published</p>}
        <CapabilityControls campaign={campaign} row={row} />
      </>}
      {!nativeRelease(row) && row.state === 'pending' && <button type="button" className="chip-act" disabled={action.busy || !!campaign.pending[`enrollment:${row.enrollment_id}`]}
        onClick={() => action.run(() => campaign.enableEnrollment(row.enrollment_id), 'Host enrollment enabled.')}>Enable</button>}
      {row.state !== 'revoked' && <button type="button" className="chip-act" disabled={action.busy || !!campaign.pending[`enrollment:${row.enrollment_id}`]}
        onClick={() => action.run(() => campaign.revokeEnrollment(row.enrollment_id), 'Host enrollment revoked.')}>Revoke</button>}
    </li>)}</ul>
    <Alert error={action.error} onReload={campaign.refetch} />
    <span role="status">{action.outcome}</span>
  </section>
}

function SignedInPanel({ projectId, projectName }) {
  const campaign = useCampaigns(projectId, { enabled: true })
  const selected = campaign.selected
  return <>
    <p className="dim">{projectName ? `${projectName} / Campaign` : 'Project / Campaign'}</p>
    <SubmitForm key={`form:${campaign.selectedId || 'new'}`} campaign={campaign} />
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
      <p className="campaign-prompt">{selected.prompt}</p>
      <p role="status">{statusWords[selected.status] || selected.status}</p>
      {selected.dispatch?.available === false && <p role="status">The build fleet is not connected yet.</p>}
      {selected.dispatch?.available === true && <p role="status">Build fleet available</p>}
    </div>}
    {selected && <section className="panel-sub campaign-execution" aria-label="Execution">
      <h3>Execution</h3>
      {campaign.executionLoading && <p role="status">Loading execution…</p>}
      <Alert error={campaign.executionError} onReload={campaign.refetch} retry="Try again" />
      {campaign.execution && <>
        {campaign.execution.tasks.length === 0 && <p>No tasks recorded yet.</p>}
        <ul>{campaign.execution.tasks.map(task => <li key={task.task_id}>
          <h4>{task.title}</h4>
          <p>{executionWords(task.current_stage)}</p>
          <p>{executionWords(task.status)}</p>
          {task.depends_on?.length > 0 && <p>Waits for: {task.depends_on.join(', ')}</p>}
          {task.blocked_by_questions?.length > 0 && <p>Blocked by an open question</p>}
        </li>)}</ul>
        <h4>Activity</h4>
        <ul>{campaign.execution.receipts.map(receipt => <li key={receipt.receipt_id}>
          {executionWords(receipt.stage)}: {executionWords(receipt.outcome)}
          {receipt.verified === true && ' (verified)'}
          {receipt.reconciles_receipt_id && ' (reconciliation)'}
        </li>)}</ul>
        <ul>{campaign.execution.events.map(event => <li key={event.event_id}>
          {executionWords(event.event_type)}{event.created_at && <> · <time dateTime={event.created_at}>{new Date(event.created_at).toLocaleString()}</time></>}
        </li>)}</ul>
      </>}
    </section>}
    {selected && <EnrollmentPanel key={`enrollment:${campaign.selectedId}`} campaign={campaign} />}
    {selected && <div className="panel-sub" key={`questions:${campaign.selectedId}`}>
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
