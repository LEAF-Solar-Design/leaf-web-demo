import { useEffect, useRef, useState } from 'react'
import { getAuthorStage, getAuthorStageSource, postReview, reviewContext, withdrawAuthorStage } from '../../api.js'
import { postMessage } from '../../converse.js'
import { track } from '../../telemetry.js'
import { capabilityLabel } from './PlanCard.jsx'
import DeployTarget from './DeployTarget.jsx'
import './inlineAuthoring.css'

const notReported = 'Not reported'
const stagedPollWindow = 30 * 60 * 1000
const string = (value) => typeof value === 'string' ? value : ''
const terminalPublicationStates = ['published', 'denied', 'superseded', 'rolled_back']
const closedPublicationStates = [...terminalPublicationStates, 'publishing']
const publishLabels = {
  rolled_back: 'Rolled back from your plugin catalog',
  superseded: 'Not published: another change was published first',
  denied: 'Not published: changes were requested',
  publishing: 'Publishing...',
  awaiting_approval: 'Waiting for approval',
}

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

function attemptLabel(attempt, { limit } = {}) {
  const diagnostics = Array.isArray(attempt.diagnostics) ? attempt.diagnostics.map(diagnostic).filter(Boolean) : []
  const status = resultLabel(attempt)
  const label = ['failed', 'compile_failed'].includes(status) ? 'did not compile' : status === 'accept' ? 'passed' : status
  const shown = limit == null ? diagnostics : diagnostics.slice(0, limit)
  if (shown.length < diagnostics.length) shown.push(`and ${diagnostics.length - shown.length} more`)
  const entities = attempt.source_decoded?.entities
  const decoded = Number.isInteger(entities) && entities >= 1 ? `, Studio decoded HTML escapes in the source (${entities})` : ''
  return `Attempt ${attempt.attempt}: ${[label || notReported, ...shown].join(', ')}${decoded}`
}

// The stage read reports the plugin's latest receipt as { last_event, at, plugin_version, diagnostics }.
const branchEvents = { synced: 'Synced', compile_failed: 'Compile · refused', ran: 'Ran', run_failed: 'Run failed' }

function branchSummary(report) {
  const lines = []
  const event = branchEvents[string(report?.last_event)]
  if (event) {
    const codes = Array.isArray(report.diagnostics) ? report.diagnostics.map(diagnostic).filter(Boolean) : []
    lines.push([event, ...codes].join(', '))
  }
  if (typeof report?.synced === 'boolean') lines.push(report.synced ? 'Synced' : 'Not synced')
  if (resultLabel(report?.compile)) lines.push(`Compile · ${resultLabel(report.compile)}`)
  const run = report?.ran === true ? 'Ran' : resultLabel(report?.run)
  const summary = string(report?.run?.summary || report?.summary)
  if (run || summary) lines.push([run, summary].filter(Boolean).join(' · '))
  return lines.join(' · ')
}

function DecisionTime({ at }) {
  const date = new Date(at)
  return at && Number.isFinite(date.getTime()) ? <>, <time dateTime={date.toISOString()}>
    {date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit', second: '2-digit' })}</time></> : null
}

function reviewPlan(stage, proposal) {
  const reported = stage?.plan?.plan || stage?.plan
  for (const plan of [reported, proposal?.plan]) {
    if (string(plan?.summary) && Array.isArray(plan.behavior) && Array.isArray(plan.capabilities)) return plan
  }
  const manifest = stage?.result?.branch_package || stage?.result?.tool
  if (!string(manifest?.name) || !string(manifest.description) || !Array.isArray(manifest.capabilities)) return null
  return {
    title: [manifest.name, string(manifest.version)].filter(Boolean).join(' · '),
    summary: manifest.description,
    capabilities: manifest.capabilities,
    change_kind: stage.change_kind,
    target_tool_name: stage.target_tool_name || manifest.name,
  }
}

function ReviewDetails({ plan, files, source, loadingSource, compile }) {
  const capabilities = plan?.capabilities || []
  const risks = capabilities.filter((cap) => cap === 'drawing.write' || /^(file|network)/.test(cap)).map(capabilityLabel)
  if (plan?.change_kind === 'revise') risks.push(`Revises the existing tool ${plan.target_tool_name}`)
  const lines = source?.text ? source.text.replace(/\r?\n$/, '').split('\n').length : 0
  return <div className="inline-review-details">
    <p>{plan?.summary || 'Build context not reported'}</p>
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
  const [decision, setDecision] = useState(null)
  const [editing, setEditing] = useState(false)
  const [comment, setComment] = useState('')
  const [busy, setBusy] = useState(false)
  const [retried, setRetried] = useState(false)
  const [readError, setReadError] = useState('')
  const [reviewError, setReviewError] = useState('')
  const [actionError, setActionError] = useState('')
  const [refresh, setRefresh] = useState(0)
  const [withdrawing, setWithdrawing] = useState(false)
  const [withdrawMessage, setWithdrawMessage] = useState('')
  const [withdrawClosed, setWithdrawClosed] = useState(false)
  const pollAfter = useRef(0)
  const pollUntil = useRef(Infinity)
  const pollStopped = useRef(false)
  const forceStageRead = useRef(false)
  const refreshStage = () => {
    forceStageRead.current = true
    pollAfter.current = 0
    setRefresh(n => n + 1)
  }
  const denied = context?.review?.decision === 'request_changes'
  useEffect(() => {
    if (!visible) return
    const controller = new AbortController()
    const { signal } = controller
    let timer
    const poll = async () => {
      if (!forceStageRead.current && (pollStopped.current || denied || Date.now() >= pollUntil.current)) return
      forceStageRead.current = false
      let delay = 2000
      try {
        const next = await getAuthorStage(changeSetId, signal)
        if (signal.aborted) return
        if (next?.change_set_id !== changeSetId || (next.receipt && next.receipt.change_set_id !== changeSetId)) {
          setStage(null)
          setReadError('The stage response did not match this build.')
          return
        }
        setReadError('')
        setStage(next)
        const reported = next.compile_attempts || next.compile?.attempts
        if (Array.isArray(reported)) setAttempts(previous => {
          const merged = new Map(previous.map(row => [row.attempt, row]))
          // The stage read numbers attempts as `n`; older reports used `attempt`.
          for (const row of reported) {
            const number = Number.isInteger(row?.attempt) ? row.attempt : row?.n
            if (Number.isInteger(number)) merged.set(number, { ...row, attempt: number })
          }
          return [...merged.values()].sort((a, b) => a.attempt - b.attempt)
        })
        const published = [next.status, next.publication?.state, decision?.status].includes('published')
        pollStopped.current = next.status === 'failed' || denied || next.review?.decision === 'request_changes'
          || ['denied', 'superseded', 'rolled_back'].includes(next.publication?.state)
          || [next.status, decision?.status].includes('denied')
          || (published && !!branchSummary(next.branch))
        if (pollStopped.current) return
        if (next.status === 'staged' || published) {
          const stagedAt = new Date(next.staged_at ?? next.updated_at ?? Date.now()).getTime()
          const since = Number.isFinite(stagedAt) ? Math.min(Date.now(), stagedAt) : Date.now()
          pollUntil.current = Math.min(pollUntil.current, since + stagedPollWindow)
        }
        delay = Math.max(2000, Number(next.retry_after_ms) || 0)
      } catch (e) {
        if (signal.aborted) return
        setReadError('Could not read build status.')
        if ([401, 403, 404].includes(e.status)) return
      }
      pollAfter.current = Date.now() + delay
      if (pollAfter.current < pollUntil.current) timer = setTimeout(poll, delay)
    }
    const remaining = pollAfter.current - Date.now()
    if (remaining > 0) timer = setTimeout(poll, remaining)
    else void poll()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [changeSetId, visible, denied, decision, refresh])

  const receipt = stage?.receipt
  const reportedReview = stage?.review?.decision
  const reportedPublication = stage?.publication?.state
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
        setContext({ ...view, commit: receipt.staged_commit })
        setReviewError('')
        if (view?.viewer_may_approve === true && !view.review && !reportedReview
          && !closedPublicationStates.includes(reportedPublication)) {
          const file = await getAuthorStageSource(changeSetId, receipt.staged_commit, signal).catch(() => null)
          if (signal.aborted) return
          setSource({ commit: receipt.staged_commit, file })
        }
      } catch {
        if (!signal.aborted) {
          setContext(null)
          setReviewError('Could not read review status.')
        }
      }
    }
    void read()
    return () => controller.abort()
  }, [changeSetId, receipt?.staged_commit, visible, decision, refresh, reportedReview, reportedPublication])

  const plan = reviewPlan(stage, proposal)
  const review = decision || (reportedReview ? stage.review : context?.review)
  const mayApprove = context?.viewer_may_approve === true && context.commit === receipt?.staged_commit
    && !review && !!receipt?.catalog_digest && !closedPublicationStates.includes(reportedPublication)
    && !['published', 'denied'].includes(stage?.status)
  const sourceReady = !!source && source.commit === receipt?.staged_commit
  const approvalReady = Boolean(plan && sourceReady)
  const self = context?.approval_kind_if_approved === 'self'
  const latest = attempts.at(-1)
  const studioTest = stage?.test_run || stage?.result?.test_run
  const compile = latest ? attemptLabel(latest) : resultLabel(studioTest)
  const files = receipt?.package_files || stage?.result?.package_files || [stage?.result?.tool?.entry].filter(Boolean)
  const publicationState = terminalPublicationStates.includes(reportedPublication) ? reportedPublication
    : decision?.status === 'published' ? 'published' : reportedPublication || decision?.status || stage?.status
  const publication = reportedPublication === 'published' ? stage.publication : decision
  const packageDigest = string(publication?.package_digest)
  const packageVersion = string(publication?.version)
  const validPackage = packageDigest.length === 64 && /^[0-9a-f]{64}$/.test(packageDigest)
    && packageVersion.trim() === packageVersion && /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.test(packageVersion)
  const failedCompile = ['refused', 'failed', 'compile_failed'].includes(resultLabel(latest))
    || /compile/.test(string(stage?.error?.reason_code))
  const published = publicationState === 'published'
  // A self approval is a one-click deploy: approving publishes the tool to the tenant catalog.
  const selfDeploy = review
    ? review.decision === 'approve' && (review.approval_kind === 'self' || (!!decision && self))
    : mayApprove && self
  const checksPassed = approvalReady && !failedCompile && stage?.status !== 'failed'
  const decide = async (choice) => {
    if (!visible || !mayApprove || busy || (choice === 'approve' && !approvalReady)) return
    setBusy(true)
    setActionError('')
    try {
      const result = await postReview({ change_set_id: changeSetId, staged_commit: receipt.staged_commit,
        catalog_digest: receipt.catalog_digest, decision: choice,
        ...(choice === 'request_changes' && comment.trim() ? { comment: comment.trim() } : {}) })
      if (result?.change_set_id !== changeSetId || result.decision !== choice) throw new Error()
      setDecision(result)
      refreshStage()
      track('review.decided', { decision: choice,
        approval_kind: ['self', 'independent'].includes(result.approval_kind) ? result.approval_kind : undefined })
    } catch (e) {
      setActionError(e.secretRefused ? e.message : 'Could not complete the review. Check the current decision and try again.')
      setContext(null)
      refreshStage()
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
  const queue = stage?.status === 'queued' && stage.queue && typeof stage.queue === 'object'
    && !Array.isArray(stage.queue) && Number.isInteger(stage.queue.ahead) && stage.queue.ahead >= 0 ? stage.queue : null
  const waiting = queue ? `Waiting, ${queue.ahead === 0 ? 'next in line' : `${queue.ahead} ahead`}${
    Number.isInteger(queue.estimated_wait_s) && queue.estimated_wait_s > 0
      ? ` · about ${Math.max(1, Math.ceil(queue.estimated_wait_s / 60))} min` : ''}` : ''
  const withdraw = async () => {
    if (withdrawing || withdrawClosed || stage?.status !== 'queued' || stage.withdrawable !== true) return
    setWithdrawing(true)
    setWithdrawMessage('')
    track('build.withdraw_clicked')
    try {
      const result = await withdrawAuthorStage(changeSetId)
      if (result.withdrawn) refreshStage()
      else {
        setWithdrawMessage(result.revoked ? 'Stopped. It will not start again.' : 'This build can no longer be withdrawn.')
        setWithdrawClosed(true)
      }
    } catch {
      setWithdrawMessage('Could not withdraw. Try again.')
    } finally {
      setWithdrawing(false)
    }
  }
  return <section ref={card} className="inline-authoring" aria-label="Tool authoring" data-converse-role="authoring">
    <h4>{plan?.title || 'Tool build'}</h4>
    <dl className="inline-build-rows">
      <dt>Build</dt><dd>{stage?.status === 'running' ? <><span className="dot live pulse" aria-hidden="true" /> Building
        <p>Claude is writing the C# and Studio is compiling it. This usually takes a few minutes.</p></>
        : stage?.status === 'queued' ? 'Waiting to start' : ['staged', 'published'].includes(stage?.status) ? 'Built'
          : stage?.status === 'failed' ? 'Failed' : string(stage?.status) || notReported}
        {waiting && <p>{waiting}</p>}
        {stage?.status === 'queued' && stage.withdrawable === true && !withdrawClosed
          && <button type="button" className="chip-neutral" disabled={withdrawing} onClick={withdraw}>Withdraw</button>}
        {withdrawMessage && <p>{withdrawMessage}</p>}
        {stage?.status === 'failed' && <p>{string(stage.error?.message) || string(stage.error?.reason_code)}</p>}</dd>
      <dt>Compile</dt><dd>{attempts.length > 0 && <ul>{attempts.map(row => <li key={row.attempt} title={attemptLabel(row)}>{attemptLabel(row, { limit: 3 })}</li>)}</ul>}
        {studioTest && <p>Test run · {resultLabel(studioTest) || notReported}{studioTest.summary && ` · ${studioTest.summary}`}</p>}
        {!attempts.length && !studioTest && (['queued', 'running'].includes(stage?.status) ? 'Not yet' : notReported)}
        {stage?.status === 'failed' && failedCompile && <div><p>Fixing these errors uses one author run.</p>
          <button type="button" className="chip-neutral" disabled={busy || retried || disabled} onClick={retry}>
            {retried ? 'Retry requested' : 'Fix with these errors'}</button></div>}</dd>
      <dt>{selfDeploy ? 'Deploy' : 'Review'}</dt><dd>{selfDeploy ? review ? <p>
        {published ? <>Deployed to AutoCAD<DecisionTime at={publication?.published_at ?? review.decided_at} /></>
          : publicationState === 'rolled_back' ? 'Rolled back'
            : ['superseded', 'denied'].includes(publicationState) ? 'Not deployed' : 'Deploying to AutoCAD...'}</p> : <>
        {checksPassed ? <div className="inline-actions">
          <button type="button" className="chip-act" disabled={busy || !visible}
            onClick={() => { track('deploy.clicked'); void decide('approve') }}>{busy ? 'Deploying...' : 'Deploy to AutoCAD'}</button>
        </div> : <p>Deploy is available once the checks pass.</p>}
        <details><summary>What will be deployed</summary>
          <ReviewDetails plan={plan} files={files} source={sourceReady && source.file} loadingSource={!sourceReady} compile={compile} />
        </details></> : review ? <p>
        {review.decision === 'approve' ? 'Approved' : 'Changes requested'} by {decision
          ? `you (${review.approver_role})` : review.approver_role}<DecisionTime at={review.decided_at} />
      </p> : mayApprove ? <>
        <ReviewDetails plan={plan} files={files} source={sourceReady && source.file} loadingSource={!sourceReady} compile={compile} />
        <div className="inline-actions">
          <button type="button" className="chip-act" disabled={busy || !approvalReady || !visible} onClick={() => decide('approve')}>
            Approve</button>
          <button type="button" className="chip-neutral" disabled={busy} aria-expanded={editing}
            onClick={() => setEditing(!editing)}>Request changes</button>
          {editing && <div className="inline-comment"><label>Review comment<textarea maxLength={500} value={comment} disabled={busy}
            onChange={e => setComment(e.target.value)} /></label>
            <button type="button" className="chip-act" disabled={busy || !comment.trim()}
              onClick={() => decide('request_changes')}>Send change request</button></div>}
        </div></> : ['queued', 'running'].includes(stage?.status) ? 'Not yet' : notReported}</dd>
      <dt>Publish</dt><dd>{published
        ? <>Published to your plugin catalog<DecisionTime at={publication?.published_at ?? decision?.decided_at
          ?? (stage?.status === 'published' ? stage.updated_at : null)} />
          {validPackage && <span title={packageDigest}> · v{packageVersion} · {packageDigest.slice(0, 12)}{' '}
            <button type="button" className="chip-neutral" aria-label="Copy package digest" title={packageDigest}
              onClick={() => navigator.clipboard?.writeText(packageDigest)?.catch(() => {})}>Copy</button>
          </span>}</>
        : publishLabels[publicationState] || (['queued', 'running', 'staged'].includes(stage?.status) ? 'Not yet' : notReported)}</dd>
      <dt>AutoCAD</dt><dd>{branchSummary(stage?.branch) || (published ? 'Waiting for AutoCAD to pick it up' : 'Not deployed yet')}</dd>
      {published && <DeployTarget />}
    </dl>
    {(readError || reviewError) && <p role="alert">{readError || reviewError} <button type="button" className="chip-neutral"
      onClick={refreshStage}>Check again</button></p>}
    {actionError && <p role="alert">{actionError}</p>}
  </section>
}
