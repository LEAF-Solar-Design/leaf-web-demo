import { useEffect, useRef, useState } from 'react'
import { getAuthorStage, getAuthorStageSource, postReview, reviewContext } from '../../api.js'
import { postMessage } from '../../converse.js'
import { track } from '../../telemetry.js'
import { capabilityLabel } from './PlanCard.jsx'
import './inlineAuthoring.css'

const notReported = 'Not reported'
const terminal = new Set(['staged', 'failed', 'published'])
const string = (value) => typeof value === 'string' ? value : ''

function useCardVisibility(ref) {
  const [inView, setInView] = useState(() => typeof IntersectionObserver === 'undefined')
  const [pageVisible, setPageVisible] = useState(() => !document.hidden)
  useEffect(() => {
    const visibility = () => setPageVisible(!document.hidden)
    document.addEventListener('visibilitychange', visibility)
    const observer = typeof IntersectionObserver === 'undefined' ? null
      : new IntersectionObserver(([entry]) => setInView(entry.isIntersecting))
    observer?.observe(ref.current)
    return () => {
      observer?.disconnect()
      document.removeEventListener('visibilitychange', visibility)
    }
  }, [ref])
  return inView && pageVisible
}

function diagnostic(value) {
  if (typeof value === 'string') return value
  const location = [string(value?.file), value?.line].filter(Boolean).join(':')
  return [string(value?.code), location, string(value?.message)].filter(Boolean).join(' ')
}

function resultLabel(report) {
  return string(report) || string(report?.status) || string(report?.decision)
    || (report?.ok === true ? 'passed' : report?.ok === false ? 'refused' : '')
}

function attemptLabel(attempt) {
  const diagnostics = Array.isArray(attempt.diagnostics) ? attempt.diagnostics.map(diagnostic).filter(Boolean) : []
  const status = resultLabel(attempt)
  const label = ['failed', 'compile_failed'].includes(status) ? 'refused' : status
  return `Attempt ${attempt.attempt}: ${[label || notReported, ...diagnostics].join(', ')}`
}

function BranchReport({ report }) {
  const lines = []
  if (typeof report?.synced === 'boolean') lines.push(report.synced ? 'Synced' : 'Not synced')
  if (resultLabel(report?.compile)) lines.push(`Compile · ${resultLabel(report.compile)}`)
  const run = report?.ran === true ? 'Ran' : resultLabel(report?.run)
  const summary = string(report?.run?.summary || report?.summary)
  if (run || summary) lines.push([run, summary].filter(Boolean).join(' · '))
  return lines.length ? lines.join(' · ') : 'Branch has not reported yet'
}

function DecisionTime({ at }) {
  const date = new Date(at)
  return at && Number.isFinite(date.getTime()) ? <>, <time dateTime={date.toISOString()}>
    {date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}</time></> : null
}

function ReviewDetails({ plan, files, source, loadingSource, compile }) {
  const capabilities = plan?.capabilities || []
  const risks = capabilities.filter((cap) => cap === 'drawing.write' || /^(file|network)/.test(cap)).map(capabilityLabel)
  if (plan?.change_kind === 'revise') risks.push(`Revises the existing tool ${plan.target_tool_name}`)
  const lines = source?.text ? source.text.replace(/\r?\n$/, '').split('\n').length : 0
  return <div className="inline-review-details">
    <p>{plan?.summary || 'Plan summary not reported'}</p>
    {plan?.behavior?.length > 0 && <ol>{plan.behavior.map((step, i) => <li key={i}>{step}</li>)}</ol>}
    <dl><dt>Files</dt><dd>{files.length ? files.join(', ') : notReported}</dd>
      <dt>Capabilities</dt><dd>{capabilities.map(capabilityLabel).join(', ') || notReported}</dd>
      <dt>Compile result</dt><dd>{compile || notReported}</dd>
      <dt>Source</dt><dd>{source ? <details>
        <summary>{source.path} · {lines} {lines === 1 ? 'line' : 'lines'}</summary>
        <pre><code>{source.text}</code></pre>
      </details> : loadingSource ? 'Reading source…' : 'source not available'}</dd>
      <dt>Risk</dt><dd>{risks.join(' · ') || (capabilities.length ? capabilities.join(', ') : notReported)}</dd>
    </dl>
  </div>
}

export default function AuthoringCard({ sessionId, changeSetId, proposal, disabled, active = true, onRetry }) {
  const card = useRef(null)
  const inView = useCardVisibility(card)
  const visible = active && inView
  const [stage, setStage] = useState(null)
  const [attempts, setAttempts] = useState([])
  const [context, setContext] = useState(null)
  const [source, setSource] = useState(null)
  const [readingSource, setReadingSource] = useState(false)
  const [decision, setDecision] = useState(null)
  const [editing, setEditing] = useState(false)
  const [comment, setComment] = useState('')
  const [busy, setBusy] = useState(false)
  const [retried, setRetried] = useState(false)
  const [readError, setReadError] = useState('')
  const [actionError, setActionError] = useState('')
  const [refresh, setRefresh] = useState(0)
  const pollAfter = useRef(0)
  const stageDone = terminal.has(stage?.status)
  useEffect(() => {
    if (!visible || stageDone) return
    const controller = new AbortController()
    const { signal } = controller
    let timer
    const poll = async () => {
      let delay = 2000
      try {
        const next = await getAuthorStage(changeSetId, signal)
        if (signal.aborted) return
        if (next?.change_set_id !== changeSetId || (next.receipt && next.receipt.change_set_id !== changeSetId)) {
          setReadError('The stage response did not match this build.')
          return
        }
        setReadError('')
        setStage(next)
        const reported = next.compile_attempts || next.compile?.attempts
        if (Array.isArray(reported)) setAttempts(previous => {
          const merged = new Map(previous.map(row => [row.attempt, row]))
          for (const row of reported) if (Number.isInteger(row?.attempt)) merged.set(row.attempt, row)
          return [...merged.values()].sort((a, b) => a.attempt - b.attempt)
        })
        if (terminal.has(next.status)) return
        delay = Math.max(2000, Number(next.retry_after_ms) || 0)
      } catch (e) {
        if (signal.aborted) return
        setReadError('Could not read build status.')
        if ([401, 403, 404].includes(e.status)) return
      }
      pollAfter.current = Date.now() + delay
      timer = setTimeout(poll, delay)
    }
    const remaining = pollAfter.current - Date.now()
    if (remaining > 0) timer = setTimeout(poll, remaining)
    else void poll()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [changeSetId, visible, stageDone, refresh])

  const receipt = stage?.receipt
  useEffect(() => {
    if (!visible || !receipt?.staged_commit || decision) return
    const controller = new AbortController()
    const { signal } = controller
    const read = async () => {
      setContext(null)
      setSource(null)
      try {
        const view = await reviewContext(changeSetId, signal)
        if (signal.aborted) return
        setContext(view)
        setReadError('')
        if (view?.viewer_may_approve === true && !view.review) {
          setReadingSource(true)
          const file = await getAuthorStageSource(changeSetId, receipt.staged_commit, signal).catch(() => null)
          if (signal.aborted) return
          setSource(file)
          setReadingSource(false)
        }
      } catch {
        if (!signal.aborted) {
          setContext(null)
          setReadError('Could not read review status.')
        }
      }
    }
    void read()
    return () => controller.abort()
  }, [changeSetId, receipt?.staged_commit, visible, decision, refresh])

  const plan = proposal?.plan
  const review = decision || context?.review
  const mayApprove = context?.viewer_may_approve === true && !review && !!receipt?.catalog_digest
  const self = context?.approval_kind_if_approved === 'self'
  const latest = attempts.at(-1)
  const studioTest = stage?.test_run || stage?.result?.test_run
  const compile = latest ? attemptLabel(latest) : resultLabel(studioTest)
  const files = receipt?.package_files || stage?.result?.package_files || [stage?.result?.tool?.entry].filter(Boolean)
  const publication = decision?.status === 'published' ? decision : stage?.publication
  const failedCompile = ['refused', 'failed', 'compile_failed'].includes(resultLabel(latest))
    || /compile/.test(string(stage?.error?.reason_code))
  const decide = async (choice) => {
    if (!visible || !mayApprove || busy) return
    setBusy(true)
    setActionError('')
    try {
      const result = await postReview({ change_set_id: changeSetId, staged_commit: receipt.staged_commit,
        catalog_digest: receipt.catalog_digest, decision: choice,
        ...(choice === 'request_changes' && comment.trim() ? { comment: comment.trim() } : {}) })
      if (result?.change_set_id !== changeSetId || result.decision !== choice) throw new Error()
      setDecision(result)
      track('review.decided', { decision: choice,
        approval_kind: ['self', 'independent'].includes(result.approval_kind) ? result.approval_kind : undefined })
    } catch (e) {
      setActionError(e.secretRefused ? e.message : 'Could not complete the review. Check the current decision and try again.')
      setContext(null)
      setRefresh(n => n + 1)
    } finally {
      setBusy(false)
    }
  }
  const retry = async () => {
    if (busy || retried || disabled) return
    setBusy(true)
    setActionError('')
    try {
      // Plan approvals are single use. A retry requires the same plan digest
      // and a fresh confirmation, even though it names the failed change set.
      const retryTarget = JSON.stringify({ retry_of_change_set_id: changeSetId })
      const report = JSON.stringify(compile || string(stage?.error?.message) || notReported)
      const result = await postMessage(sessionId, { text: `Fix these compile errors. Re-propose the same build plan for approval, then retry author_tool with the new plan_confirmation_id and ${retryTarget}. Compiler report: ${report}` })
      setRetried(true)
      if (result?.turn_id) onRetry?.(result.turn_id)
    } catch (e) {
      setActionError(e.secretRefused ? e.message : 'Could not send the retry. Try again.')
    } finally {
      setBusy(false)
    }
  }
  return <section ref={card} className="inline-authoring" aria-label="Tool authoring" data-converse-role="authoring">
    <h4>{plan?.title || 'Tool build'}</h4>
    <dl className="inline-build-rows">
      <dt>Build</dt><dd>{stage?.status === 'running' ? 'authoring' : string(stage?.status) || notReported}
        {stage?.status === 'failed' && <p>{string(stage.error?.message) || string(stage.error?.reason_code)}</p>}</dd>
      <dt>Compile</dt><dd>{attempts.length > 0 && <ul>{attempts.map(row => <li key={row.attempt}>{attemptLabel(row)}</li>)}</ul>}
        {studioTest && <p>Test run · {resultLabel(studioTest) || notReported}{studioTest.summary && ` · ${studioTest.summary}`}</p>}
        {!attempts.length && !studioTest && notReported}
        {stage?.status === 'failed' && failedCompile && <div><p>Fixing these errors uses one author run.</p>
          <button type="button" className="chip-neutral" disabled={busy || retried || disabled} onClick={retry}>
            {retried ? 'Retry requested' : 'Fix with these errors'}</button></div>}</dd>
      <dt>Review</dt><dd>{review ? <p>
        {review.decision === 'approve' ? 'Approved' : 'Changes requested'} by {decision
          ? `you (${review.approver_role})` : review.approver_role}<DecisionTime at={review.decided_at} />
      </p> : mayApprove ? <>
        <ReviewDetails plan={plan} files={files} source={source} loadingSource={readingSource} compile={compile} />
        {self && <p>This will be recorded as a self-approval.</p>}
        <div className="inline-actions">
          <button type="button" className="chip-act" disabled={busy} onClick={() => decide('approve')}>
            {self ? 'Approve your own tool' : 'Approve'}</button>
          <button type="button" className="chip-neutral" disabled={busy} aria-expanded={editing}
            onClick={() => setEditing(!editing)}>Request changes</button>
          {editing && <div className="inline-comment"><label>Review comment<textarea maxLength={500} value={comment} disabled={busy}
            onChange={e => setComment(e.target.value)} /></label>
            <button type="button" className="chip-act" disabled={busy || !comment.trim()}
              onClick={() => decide('request_changes')}>Send change request</button></div>}
        </div></> : notReported}</dd>
      <dt>Publish</dt><dd>{publication?.status === 'published' || stage?.status === 'published'
        ? <>Published to the internal dev catalog<DecisionTime at={publication?.published_at || publication?.decided_at || stage?.updated_at} /></>
        : notReported}</dd>
      <dt>Branch</dt><dd><BranchReport report={stage?.branch} /></dd>
    </dl>
    {readError && <p role="alert">{readError} <button type="button" className="chip-neutral"
      onClick={() => setRefresh(n => n + 1)}>Check again</button></p>}
    {actionError && <p role="alert">{actionError}</p>}
  </section>
}
