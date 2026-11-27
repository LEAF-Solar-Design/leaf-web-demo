import { memo, useEffect, useId, useRef, useState } from 'react'
import { authHeaders, config, noteUnauthorized } from '../../api.js'
import { readActiveScope, subscribeActiveScope } from '../../auth/activeScope.js'
import { fetchWithBudget } from '../../fetchBudget.js'
import { canonicalJSON } from '../contract.js'
import { sha256Hex } from '../runtime/sha256.js'
import { useLabSurfaceStatus } from '../useLabSurfaceStatus.js'
import { labValueStore, useBlockValue, useRun, useRuns } from './valueStore.js'
import { cancelRun, readCorpusJobs } from './runStream.js'
import './corpusEvidence.css'
import { corpusReportDiagnostics } from './corpusReportDiagnostics.js'

const summarize = value => {
  try { return (typeof value === 'string' ? value : JSON.stringify(value) ?? 'null').slice(0, 200) }
  catch { return 'Value unavailable' }
}
const recoveryUnavailable = 'Recovery options unavailable. The plan could not be verified.'
const recoveryChanged = 'Run or account context changed. Review recovery options again.'
const recoveryTerminal = state => ['finished', 'failed', 'cancelled'].includes(state)
const recoveryScope = () => canonicalJSON([readActiveScope(), config.apiBase, config.tenant, authHeaders()])
const recoveryIdentity = run => canonicalJSON([run?.id ?? null, run?.graph_revision ?? null,
  run?.state ?? null, run?.doc_id ?? null, run?.graph_sha256 ?? null])
const recoveryReasons = {
  timeout: 'Execution timed out', resource_exhausted: 'Execution resource limit reached',
  lease_or_budget: 'Execution lease or budget ended', completed: 'Completed evaluation retained',
  missing_job: 'Authoritative job missing', duplicate_jobs: 'Multiple authoritative jobs found',
  job_receipt_changed: 'Job receipt changed', job_binding_mismatch: 'Job identity does not match',
  cleanup_unconfirmed: 'Job cleanup is unconfirmed', cancelled: 'Job was cancelled',
  job_nonterminal: 'Job is still pending or running', unknown_status: 'Job status is unknown',
  source_cancelled: 'Source run was cancelled', invalid_input: 'Input needs correction',
  access_revoked: 'Job access was revoked', runtime_unavailable: 'Required runtime is unavailable',
  verification_required: 'Additional verification is required', unknown_failure: 'Failure cause is unverified',
}
const recoveryKeys = (value, keys) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key))
const recoveryText = (value, max = 256) => typeof value === 'string' && value.length > 0 && value.length <= max
const recoveryHash = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
function verifyCleanupEvidence(evidence, block, source, refuse) {
  if (!recoveryKeys(evidence, ['schema', 'job_id', 'attempt', 'state', 'cancel', 'receipt'])
    || evidence.schema !== 'leaf.lab-corpus-cleanup-evidence.v1' || evidence.job_id !== block.job_id
    || (evidence.attempt !== null && (!Number.isSafeInteger(evidence.attempt) || evidence.attempt < 0))
    || !['unconfirmed', 'confirmed_reaped', 'confirmed_not_started'].includes(evidence.state)) refuse()
  const tag = evidence.cancel, receipt = evidence.receipt
  if (block.job_id === null && (evidence.attempt !== null || tag !== null || receipt !== null)) refuse()
  if (tag !== null && (!recoveryKeys(tag, ['run_id', 'block_id', 'attempt', 'fence', 'reason'])
    || tag.run_id !== source.run_id || tag.block_id !== block.block_id || evidence.attempt === null
    || tag.attempt !== evidence.attempt || !Number.isSafeInteger(tag.fence) || tag.fence <= 0
    || !['deadline', 'cancel'].includes(tag.reason))) refuse()
  if (evidence.state === 'unconfirmed') {
    if (receipt !== null) refuse()
    return
  }
  if (tag === null || !recoveryKeys(receipt, ['attempt', 'cancel', 'reaped', 'not_started'])
    || receipt.attempt !== evidence.attempt || canonicalJSON(receipt.cancel) !== canonicalJSON(tag)
    || typeof receipt.reaped !== 'boolean' || typeof receipt.not_started !== 'boolean'
    || receipt.reaped === receipt.not_started
    || evidence.state !== (receipt.reaped ? 'confirmed_reaped' : 'confirmed_not_started')) refuse()
}
function verifyRecoveryPlan(plan, run) {
  const refuse = () => { throw new Error(recoveryUnavailable) }
  if (!recoveryKeys(plan, ['schema', 'source', 'blocks', 'selected_failed_block_ids', 'retained_completed_block_ids',
    'unresolved_block_ids', 'draft_graph', 'execution_allowed', 'confirmation_required', 'digest'])
    || !['leaf.lab-corpus-recovery-plan.v1', 'leaf.lab-corpus-recovery-plan.v2'].includes(plan.schema) || plan.execution_allowed !== false
    || plan.confirmation_required !== true || !recoveryHash(plan.digest)) refuse()
  const source = plan.source
  if (!recoveryKeys(source, ['run_id', 'doc_id', 'graph_revision', 'graph_sha256', 'state'])
    || source.run_id !== run.id || !recoveryText(source.doc_id, 128)
    || !Number.isSafeInteger(source.graph_revision) || source.graph_revision < 1
    || (run.graph_revision != null && source.graph_revision !== run.graph_revision)
    || (run.doc_id != null && source.doc_id !== run.doc_id)
    || (run.graph_sha256 != null && source.graph_sha256 !== run.graph_sha256)
    || !recoveryHash(source.graph_sha256) || !recoveryTerminal(source.state)
    || source.state !== run.state || !Array.isArray(plan.blocks) || !plan.blocks.length || plan.blocks.length > 200) refuse()
  const membership = new Map()
  for (const [field, disposition] of [['selected_failed_block_ids', 'selected'],
    ['retained_completed_block_ids', 'retained'], ['unresolved_block_ids', 'unresolved']]) {
    if (!Array.isArray(plan[field]) || plan[field].length > 200) refuse()
    for (const id of plan[field]) {
      if (!recoveryText(id) || membership.has(id)) refuse()
      membership.set(id, disposition)
    }
  }
  const seen = new Set()
  const cleanup = plan.schema === 'leaf.lab-corpus-recovery-plan.v2'
  for (const block of plan.blocks) {
    if (!recoveryKeys(block, ['block_id', 'idempotency_key', 'job_id', 'status', 'receipt_sha256', 'disposition', 'reason_category',
      ...(cleanup ? ['cleanup_evidence'] : [])])
      || !recoveryText(block.block_id) || seen.has(block.block_id)
      || !membership.has(block.block_id) || membership.get(block.block_id) !== block.disposition
      || block.idempotency_key !== `lab:${run.id}:${block.block_id}:corpus:v1`
      || !Object.hasOwn(recoveryReasons, block.reason_category)) refuse()
    seen.add(block.block_id)
    if (block.job_id === null) {
      if (block.status !== null || block.receipt_sha256 !== null || block.disposition !== 'unresolved') refuse()
    } else if (!recoveryText(block.job_id) || !recoveryText(block.status, 64) || !recoveryHash(block.receipt_sha256)) refuse()
    if (block.disposition === 'selected' && (block.status !== 'failed'
      || !['timeout', 'resource_exhausted', 'lease_or_budget'].includes(block.reason_category))) refuse()
    if (block.disposition === 'retained' && (block.status !== 'complete' || block.reason_category !== 'completed')) refuse()
    if (block.status === 'complete' && block.disposition !== 'retained') refuse()
    if (block.disposition === 'unresolved' && ['timeout', 'resource_exhausted', 'lease_or_budget', 'completed'].includes(block.reason_category)) refuse()
    if (cleanup) verifyCleanupEvidence(block.cleanup_evidence, block, source, refuse)
  }
  if (seen.size !== membership.size) refuse()
  const draft = plan.draft_graph, selected = plan.selected_failed_block_ids
  if (!selected.length || plan.unresolved_block_ids.length) {
    if (draft !== null) refuse()
  } else if (!recoveryKeys(draft, ['schema', 'blocks', 'pipes', 'outputs'])
    || draft.schema !== 'leaf.lab-graph.v1' || !Array.isArray(draft.blocks) || draft.blocks.length !== selected.length
    || new Set(draft.blocks.map(block => block?.id)).size !== selected.length
    || draft.blocks.some(block => !selected.includes(block?.id))
    || !Array.isArray(draft.pipes) || draft.pipes.length || !draft.outputs || typeof draft.outputs !== 'object'
    || Array.isArray(draft.outputs) || Object.keys(draft.outputs).length > 200
    || Object.values(draft.outputs).some(endpoint => !selected.includes(endpoint?.node))) refuse()
  const { digest, ...unsigned } = plan
  if (sha256Hex(canonicalJSON(unsigned)) !== digest) refuse()
  return plan
}
async function readRecoveryPlan(response, signal) {
  const media = response.headers?.get('content-type')?.split(';')[0].trim().toLowerCase()
  const reader = response.body?.getReader()
  if (media !== 'application/json' || !reader) {
    void reader?.cancel().catch(() => {})
    reader?.releaseLock()
    throw new Error(recoveryUnavailable)
  }
  const cancel = () => { void reader.cancel().catch(() => {}) }
  signal.addEventListener('abort', cancel, { once: true })
  try {
    const decoder = new TextDecoder('utf-8', { fatal: true })
    let text = '', bytes = 0
    for (;;) {
      if (signal.aborted) throw new Error(recoveryUnavailable)
      const { value, done } = await reader.read()
      if (signal.aborted) throw new Error(recoveryUnavailable)
      if (done) break
      bytes += value.byteLength
      if (bytes > 524288) throw new Error(recoveryUnavailable)
      text += decoder.decode(value, { stream: true })
    }
    return JSON.parse(text + decoder.decode())
  } finally {
    signal.removeEventListener('abort', cancel)
    cancel()
    reader.releaseLock()
  }
}
function RecoveryPlan({ plan }) {
  return <section className="lab-corpus-evidence" aria-label="Recovery options preview" style={{ overflowWrap: 'anywhere' }}>
    <h4>Recovery options · read-only preview</h4>
    <p>Source document: {plan.source.doc_id} · revision {plan.source.graph_revision}</p>
    <p>Source run: {plan.source.run_id} · {plan.source.state}</p>
    <p>Graph digest: {plan.source.graph_sha256}</p>
    <p>Plan digest: {plan.digest}</p>
    <p>{plan.unresolved_block_ids.length ? 'Preparation blocked: unresolved nodes need review.'
      : plan.selected_failed_block_ids.length ? 'Preparation available for the selected failed nodes.' : 'No eligible failed nodes to prepare.'}</p>
    <p>Execution is unavailable in this preview. A later execution stage requires confirmation.</p>
    <p>Completed evaluations are retained, including verdict fail; a failed verdict is not an execution failure.</p>
    {[['selected', 'Selected failed nodes'], ['retained', 'Retained completed nodes'], ['unresolved', 'Unresolved nodes']].map(([disposition, label]) =>
      <section key={disposition} aria-label={label}><h5>{label}</h5>
        {plan.blocks.some(block => block.disposition === disposition) ? <ul>{plan.blocks.filter(block => block.disposition === disposition).map(block =>
          <li key={block.block_id}><strong>{block.block_id}</strong>
            <p>Job: {block.job_id ?? 'Unavailable'} · status: {block.status ?? 'Unavailable'}</p>
            <p>Cleanup: {block.cleanup_evidence?.state === 'confirmed_reaped' ? 'Confirmed reaped'
              : block.cleanup_evidence?.state === 'confirmed_not_started' ? 'Confirmed not started' : 'Unconfirmed'} · attempt: {block.cleanup_evidence?.attempt ?? 'Unavailable'}</p>
            <p>{recoveryReasons[block.reason_category]} ({block.reason_category})</p>
          </li>)}</ul> : <p>None</p>}
      </section>)}
  </section>
}
function RecoveryReview({ run, store, fetchImpl }) {
  const scope = recoveryScope(), identity = recoveryIdentity(run)
  const [review, setReview] = useState({ scope, identity, pending: false, plan: null, message: '' })
  const request = useRef(null), latest = useRef(null)
  latest.current = { identity, store, fetchImpl }
  useEffect(() => subscribeActiveScope(() => {
    const nextScope = recoveryScope(), token = request.current
    if (token && token.scope !== nextScope) {
      clearTimeout(token.timer)
      token.controller.abort()
      request.current = null
    }
    setReview(previous => previous.scope === nextScope ? previous : { ...previous, scope: nextScope,
      pending: false, plan: null, message: previous.plan || previous.pending ? recoveryChanged : previous.message })
  }), [])
  useEffect(() => {
    setReview(previous => ({ scope, identity, pending: false, plan: null,
      message: previous.plan || previous.pending ? recoveryChanged : previous.message }))
    return () => {
      clearTimeout(request.current?.timer)
      request.current?.controller.abort()
      request.current = null
    }
  }, [scope, identity, store, fetchImpl])
  async function reviewOptions() {
    if (request.current) return
    const controller = new AbortController(), capturedScope = recoveryScope()
    const token = { controller, scope: capturedScope }
    request.current = token
    setReview({ scope: capturedScope, identity, pending: true, plan: null, message: 'Reviewing recovery options…' })
    const current = () => request.current === token && !controller.signal.aborted
      && latest.current.identity === identity && latest.current.store === store && latest.current.fetchImpl === fetchImpl
      && recoveryIdentity(store.getRun(run.id)) === identity
    const finish = patch => {
      if (!current()) return
      const currentScope = recoveryScope()
      setReview({ scope: currentScope, identity, pending: false, plan: null, message: '',
        ...(currentScope === capturedScope ? patch : { message: recoveryChanged }) })
    }
    // fetchWithBudget bounds headers; this lifetime also bounds a stalled body.
    token.timer = setTimeout(() => {
      finish({ message: recoveryUnavailable })
      controller.abort()
      if (request.current === token) request.current = null
    }, 10000)
    const headers = { accept: 'application/json', 'X-Tenant-Id': config.tenant, ...authHeaders() }
    try {
      const response = await fetchWithBudget(fetchImpl, `${config.apiBase}/api/lab/runs/${encodeURIComponent(run.id)}/recovery-plan?include_cleanup=true`, {
        method: 'GET', headers, signal: controller.signal, credentials: 'same-origin', redirect: 'error', cache: 'no-store',
      })
      if (!current()) { void response.body?.cancel?.().catch(() => {}); return }
      if (!response.ok || response.status !== 200 || response.redirected) {
        void response.body?.cancel?.().catch(() => {})
        finish({ message: response.status === 401 ? 'Sign in again to review recovery options.'
          : response.status === 403 ? 'You no longer have access to these recovery options.' : recoveryUnavailable })
        noteUnauthorized(response, 'lab-runs', headers.Authorization)
        return
      }
      const plan = await readRecoveryPlan(response, controller.signal)
      // Body completion is a separate authority boundary from response headers.
      if (!current()) return
      if (capturedScope !== recoveryScope()) { finish({ message: recoveryChanged }); return }
      finish({ plan: verifyRecoveryPlan(plan, run) })
    } catch { finish({ message: recoveryUnavailable }) }
    finally {
      clearTimeout(token.timer)
      controller.abort()
      if (request.current === token) request.current = null
    }
  }
  const visible = review.scope === scope && review.identity === identity
  return <>
    <button type="button" disabled={visible && review.pending} onClick={reviewOptions}>Review recovery options</button>
    {(visible ? review.message : recoveryChanged) && <p role="status">{visible ? review.message : recoveryChanged}</p>}
    {visible && review.plan && <RecoveryPlan plan={review.plan} />}
  </>
}
const corpusEvidence = value => {
  const data = value?.data ?? value
  if (typeof data !== 'string' || data.length > 1114112) return null
  try {
    const evidence = JSON.parse(data)
    return evidence?.policy?.schema === 'leaf.lab-corpus-policy.v1' &&
      evidence?.evaluation?.schema === 'leafhft.block-suite-result.v1' ? evidence : null
  } catch { return null }
}
function EvidenceField({ label, value }) {
  return <div><h5>{label}</h5><pre>{value == null ? 'Unavailable (not evaluated)' : JSON.stringify(value, null, 2)}</pre></div>
}
function ConfirmationGuidance({ confirmation }) {
  if (confirmation?.verdict !== 'insufficient' && confirmation?.verdict !== 'inconclusive') return null

  const count = value => Number.isSafeInteger(value) && value >= 0
  const limits = [
    ['Parent needs', confirmation.parent?.confirm?.n_tickets, confirmation.protocol?.min_confirm_tickets, 'qualifying confirmation ticket'],
    ['Child needs', confirmation.child?.confirm?.n_tickets, confirmation.protocol?.min_confirm_tickets, 'qualifying confirmation ticket'],
    ['Confirmation needs', confirmation.splits?.confirm?.groups, confirmation.protocol?.min_confirm_groups, 'expiry group'],
  ]
  const shortfalls = limits.filter(([, observed, required]) => count(observed) && count(required) && observed < required)
  const interval = confirmation.confirm_comparison?.paired_ci95_c
  const intervalKnown = Array.isArray(interval) && interval.length === 2 &&
    interval.every(Number.isFinite) && interval[0] <= interval[1]

  return <div>
    <h5>What would resolve confirmation</h5>
    {shortfalls.map(([label, observed, required, unit]) => {
      const missing = required - observed
      return <p key={label}>{label} {missing} more {unit}{missing === 1 ? '' : 's'} ({observed} of {required}).</p>
    })}
    {limits.some(([, observed, required]) => !count(observed) || !count(required)) &&
      <p>The saved report is missing confirmation counts or thresholds.</p>}
    {intervalKnown && interval[0] <= 0 && interval[1] >= 0 && <p>The paired 95% interval still crosses or touches zero.</p>}
    {!intervalKnown && <p>The paired 95% interval is unavailable.</p>}
    <p>A directional result requires the minimum sample counts and a paired 95% interval entirely above or below zero.</p>
    <p>Replaying the pinned inputs adds no new confirmation evidence. If additional eligible observations are available, evaluate them under the same saved policy.</p>
  </div>
}
function CorpusEvidence({ evidence }) {
  const report = evidence.evaluation, confirmation = report.confirm_report
  return <section className="lab-corpus-evidence" aria-label="Corpus evaluation evidence">
    <h4>Corpus evaluation</h4>
    <p>Policy digest: {evidence.policy_digest}</p>
    <p>Policy version: {evidence.policy.version}</p>
    <p>Parent suite digest: {evidence.policy.parent_suite_sha256}</p>
    <p>Evaluated suite digest: {evidence.evaluated_suite_sha256}</p>
    <p>Verdict: {report.verdict}</p>
    <p>Confirmation verdict: {confirmation?.verdict ?? 'Unavailable (not evaluated)'}</p>
    {corpusReportDiagnostics(report).map(message => <p key={message} role="status">{message}</p>)}
    <ConfirmationGuidance confirmation={confirmation} />
    <EvidenceField label="Reasons" value={report.reasons} />
    <EvidenceField label="Policy and graph lineage" value={evidence.policy} />
    <EvidenceField label="Input identities" value={{ ...evidence.context, row_count: evidence.row_count,
      rows_sha256: evidence.rows_sha256, result_sha256: evidence.result_sha256,
      run_id: evidence.run_id, job_id: evidence.job_id, worker_id: evidence.worker_id, attempt: evidence.attempt }} />
    <EvidenceField label="Selection evidence" value={{ candidates: report.candidates, chosen: report.chosen }} />
    <EvidenceField label="Confirmation evidence" value={confirmation && {
      parent: confirmation.parent, child: confirmation.child, splits: confirmation.splits, protocol: confirmation.protocol }} />
    <EvidenceField label="Calibration" value={confirmation?.calibration} />
    <EvidenceField label="Uncertainty" value={confirmation?.confirm_comparison} />
    <EvidenceField label="Confirmation reasons" value={confirmation?.reasons} />
    <details><summary>Complete evaluation evidence</summary><EvidenceField label="Evaluation report" value={report} /></details>
  </section>
}
function corpusReports(runs) {
  const reports = new Map()
  for (const run of runs) {
    if (run.id === 'local') continue
    for (const [blockId, block] of Object.entries(run.blocks)) {
      for (const [port, value] of Object.entries(block.value || {})) {
        const evidence = corpusEvidence(value)
        if (!evidence) continue
        const key = JSON.stringify([run.id, run.graph_revision, blockId, port, evidence.result_sha256])
        const corpus = evidence.context?.corpus?.material_id
        const label = [corpus, blockId, port, `revision ${run.graph_revision ?? 'unknown'}`, run.id].filter(Boolean).join(' · ')
        reports.set(key, { evidence, run, label })
      }
    }
  }
  return reports
}
const comparisonValue = value => typeof value === 'string' && value.trim() || Number.isFinite(value)
  ? value : 'Unavailable'
function comparisonFields({ evidence, run }) {
  const report = evidence.evaluation
  const confirmation = report.confirm_report
  const interval = confirmation?.confirm_comparison?.paired_ci95_c
  return [
    run.state, report.verdict, confirmation?.verdict,
    confirmation?.parent?.confirm?.n_tickets, confirmation?.child?.confirm?.n_tickets,
    confirmation?.protocol?.min_confirm_tickets, confirmation?.confirm_comparison?.child_minus_parent_mean_c,
    Array.isArray(interval) && interval.length === 2 && interval.every(Number.isFinite) && interval[0] <= interval[1]
      ? interval.join(' to ') : null,
    Array.isArray(confirmation?.reasons) && confirmation.reasons.every(reason => typeof reason === 'string')
      ? confirmation.reasons.join(' · ') : null,
    Array.isArray(report.reasons) && report.reasons.every(reason => typeof reason === 'string')
      ? report.reasons.join(' · ') : null,
    report.chosen ? JSON.stringify(report.chosen.params ?? report.chosen) : null,
    report.chosen?.child_min_k, evidence.context?.corpus?.sha256,
    evidence.policy.parent_suite_sha256, evidence.evaluated_suite_sha256,
  ].map(comparisonValue)
}
const comparisonRows = [
  'Run state', 'Evaluation verdict', 'Confirmation verdict', 'Parent confirmation tickets',
  'Child confirmation tickets', 'Required confirmation tickets', 'Child minus parent mean (cents)',
  'Paired 95% interval (cents)', 'Confirmation reasons', 'Evaluation reasons', 'Selected parameters',
  'Selected minimum k', 'Corpus digest', 'Parent suite digest', 'Evaluated suite digest',
]
const CorpusComparison = memo(function CorpusComparison({ runs }) {
  const [open, setOpen] = useState(false)
  const [keys, setKeys] = useState(['', '', ''])
  return <details onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>Compare corpus evaluations</summary>
    {open && <CorpusComparisonFields runs={runs} keys={keys} setKeys={setKeys} />}
  </details>
})
function CorpusComparisonFields({ runs, keys, setKeys }) {
  const comparisonId = useId()
  const reports = corpusReports(runs)
  const selected = keys.map(key => reports.get(key)).filter(Boolean)
  const columns = selected.map(comparisonFields)
  const wrap = { overflowWrap: 'anywhere' }
  return <>
    {reports.size < 2 ? <p>Two corpus evaluation reports are required.</p> : keys.map((key, index) =>
      <div key={index}>
        <label htmlFor={`${comparisonId}-${index}`}>Report {index + 1}{index === 2 ? ' (optional)' : ''} </label>
        <select id={`${comparisonId}-${index}`} style={{ maxWidth: '100%' }}
          value={reports.has(key) ? key : ''} onChange={event => {
          const value = event.target.value
          setKeys(previous => previous.map((oldKey, position) => position === index ? value : oldKey))
        }}>
          <option value="">Choose a corpus evaluation</option>
          {[...reports].map(([id, { label }]) =>
            <option key={id} value={id} disabled={id !== key && keys.includes(id)}>{label}</option>)}
        </select>
      </div>)}
    {columns.length >= 2 && <>
      <p>Compare the recorded reasons and inputs. Differences in suites or selected parameters can also affect the result.</p>
      <div role="region" aria-label="Corpus comparison table" tabIndex={0} style={{ overflowX: 'auto' }}>
        <table style={{ minWidth: columns.length === 3 ? '48rem' : '36rem', tableLayout: 'fixed', width: '100%' }}>
          <caption>Corpus evaluation comparison</caption>
          <thead><tr><th scope="col">Evidence</th>{selected.map(({ label }, index) =>
            <th scope="col" key={index} style={wrap}>{label}</th>)}</tr></thead>
          <tbody>{comparisonRows.map((label, row) =>
            <tr key={label}><th scope="row">{label}</th>{columns.map((fields, index) =>
              <td key={index} style={wrap}>{fields[row]}</td>)}</tr>)}</tbody>
        </table>
      </div>
    </>}
  </>
}
const BlockResult = memo(function BlockResult({ store, runId, blockId, now }) {
  const block = useBlockValue(store, runId, blockId)
  if (!block) return null
  return <li><h4>{blockId}</h4>
    {block.error && <p>{summarize(block.error)}</p>}
    {Object.entries(block.value || {}).map(([port, value]) => {
      const asOf = value?.as_of
      const timestamp = typeof asOf === 'number' ? asOf : Date.parse(asOf)
      const age = asOf != null && Number.isFinite(timestamp) ? Math.max(0, now - timestamp) : null
      const evidence = corpusEvidence(value)
      return <div key={port}>{evidence ? <CorpusEvidence evidence={evidence} /> : <p>{port}: {summarize(value)}</p>}
        {value?.error && <p>Output error: {summarize(value.error)}</p>}
        <p>Source age: {age == null ? 'unknown' : `${Math.floor(age / 1000)}s`}</p>
        {(value?.stale || age > 60000) && <span>Stale source</span>}
      </div>
    })}
  </li>
})
const corpusJobStatuses = {
  queued: 'Queued', running: 'Running', failed: 'Failed', completed: 'Completed',
  'not submitted': 'Not submitted', unavailable: 'Unavailable',
}
function validCorpusJobs(data, run) {
  const source = data.source
  return data.corpus === true && source?.run_id === run.id && source.graph_revision === run.graph_revision
    && recoveryHash(source.graph_sha256) && recoveryHash(source.input_digest)
    && (!run.graph_sha256 || run.graph_sha256 === source.graph_sha256)
    && (!run.input_digest || run.input_digest === source.input_digest)
    && Array.isArray(data.blocks) && data.blocks.length > 0 && data.blocks.length <= 200
    && data.blocks.every(block => recoveryText(block?.block_id, 64) && Object.hasOwn(corpusJobStatuses, block.status)
      && (['not submitted', 'unavailable'].includes(block.status)
        ? block.job_id === null && block.attempt === null
        : recoveryText(block.job_id) && Number.isSafeInteger(block.attempt) && block.attempt >= 0)
      && recoveryHash(block.inputs?.corpus_sha256) && recoveryHash(block.inputs?.suite_sha256)
      && recoveryHash(block.inputs?.settings_sha256))
    && new Set(data.blocks.map(block => block.block_id)).size === data.blocks.length
}
function CorpusJobField({ label, value }) {
  return <div><dt>{label}</dt><dd style={{ overflowWrap: 'anywhere' }}><code>{value}</code></dd></div>
}
function CorpusJobs({ run, store, fetchImpl }) {
  const [view, setView] = useState(null)
  const active = useRef(null)
  useEffect(() => {
    const reset = () => {
      clearTimeout(active.current?.timer)
      active.current?.controller.abort()
      active.current = null
      setView(null)
    }
    const unsubscribe = subscribeActiveScope(reset)
    return () => {
      unsubscribe()
      clearTimeout(active.current?.timer)
      active.current?.controller.abort()
      active.current = null
    }
  }, [store, fetchImpl])
  const scope = recoveryScope()
  const currentView = view?.scope === scope && view.store === store && view.fetchImpl === fetchImpl ? view : null
  async function refresh() {
    if (active.current) return
    const controller = new AbortController()
    const unavailable = 'Corpus job status unavailable. Try refreshing.'
    const request = { controller }
    const publish = patch => {
      if (active.current === request && recoveryScope() === scope
        && store.getRun(run.id)?.graph_revision === run.graph_revision) {
        setView({ scope, store, fetchImpl, ...patch })
      }
    }
    active.current = request
    publish({ pending: true, message: 'Reading corpus jobs...' })
    request.timer = setTimeout(() => {
      publish({ message: unavailable })
      active.current = null
      controller.abort()
    }, 10000)
    const data = await readCorpusJobs({ runId: run.id, fetchImpl, signal: controller.signal })
    clearTimeout(request.timer)
    if (controller.signal.aborted) return
    if (data.ok && validCorpusJobs(data, run)) publish({ data })
    else publish({ message: data.ok && data.corpus === false ? 'No corpus jobs for this run.' : unavailable })
    if (active.current === request) active.current = null
  }
  const data = currentView?.data
  return <section aria-label="Corpus jobs">
    <button type="button" onClick={refresh} disabled={currentView?.pending}>
      {data ? 'Refresh corpus jobs' : 'Show corpus jobs'}
    </button>
    {currentView?.message && <p role="status">{currentView.message}</p>}
    {data && <>
      <dl>
        <CorpusJobField label="Graph revision" value={data.source.graph_revision} />
        <CorpusJobField label="Graph SHA-256" value={data.source.graph_sha256} />
        <CorpusJobField label="Run input digest" value={data.source.input_digest} />
      </dl>
      <ul>{data.blocks.map(block => <li key={block.block_id}>
        <strong>{block.block_id}</strong> {corpusJobStatuses[block.status]}
        <dl>
          {block.job_id !== null && <>
            <CorpusJobField label="Job ID" value={block.job_id} />
            <CorpusJobField label="Attempt" value={block.attempt} />
          </>}
          <CorpusJobField label="Corpus SHA-256" value={block.inputs.corpus_sha256} />
          <CorpusJobField label="Suite SHA-256" value={block.inputs.suite_sha256} />
          <CorpusJobField label="Block settings digest" value={block.inputs.settings_sha256} />
        </dl>
      </li>)}</ul>
    </>}
  </section>
}
const RunResult = memo(function RunResult({ store, runId, fetchImpl, now, showCorpusJobs }) {
  const run = useRun(store, runId)
  const [cancelling, setCancelling] = useState(false)
  const [message, setMessage] = useState('')
  if (!run) return null
  async function cancel() {
    if (cancelling) return
    setCancelling(true)
    const result = await cancelRun({ runId, fetchImpl })
    setMessage(result.ok ? 'Cancellation requested' : result.status === 401 ? 'Sign in again' : 'Cancellation unavailable')
    if (!result.ok) setCancelling(false)
  }
  return <article aria-label={`Run ${run.id}`}>
    <h3>{run.id}</h3><p>{run.state} · revision {run.graph_revision ?? 'local'}</p>
    <p>Events: {run.events.length} · Dropped: {run.dropped}</p>
    <p>Stream: {run.transport.status} · lag: {run.transport.lagMs == null ? 'unknown' : `${run.transport.lagMs} ms`}</p>
    {run.transport.status === 'auth_expired' && <p>Sign in again, then resume <button type="button" onClick={() => store.resume(runId)}>Resume</button></p>}
    {run.gaps.map((gap, index) => <p key={index}>{gap.recovered ? 'Recovered from a snapshot after a gap' : 'Gap detected; recovering snapshot'}</p>)}
    {['queued', 'running'].includes(run.state) && <button type="button" disabled={cancelling} onClick={cancel}>Cancel</button>}
    {message && <p role="status">{message}</p>}
    {showCorpusJobs && Number.isInteger(run.graph_revision) && run.state !== 'missing' &&
      <CorpusJobs key={run.graph_revision} run={run} store={store} fetchImpl={fetchImpl} />}
    {recoveryTerminal(run.state) && <RecoveryReview run={run} store={store} fetchImpl={fetchImpl} />}
    <ul>{Object.keys(run.blocks).map(id => <BlockResult key={id} store={store} runId={run.id} blockId={id} now={now} />)}</ul>
  </article>
})
function WorkerStatus({ mock, fetchImpl }) {
  const status = useLabSurfaceStatus(mock)
  const [worker, setWorker] = useState(null)
  useEffect(() => {
    if (mock || status?.worker) return undefined
    const controller = new AbortController()
    // The shared hook retains only rail and posture; read worker details here.
    async function probe() {
      const headers = { accept: 'application/json', 'X-Tenant-Id': config.tenant, ...authHeaders() }
      try {
        const response = await fetchWithBudget(fetchImpl, `${config.apiBase}/api/lab/status`, { headers, signal: controller.signal })
        noteUnauthorized(response, 'lab-runs', headers.Authorization)
        const data = response.ok ? await response.json() : null
        if (!controller.signal.aborted) setWorker(data?.worker || null)
      } catch { if (!controller.signal.aborted) setWorker(null) }
    }
    void probe()
    const timer = setInterval(probe, 15000)
    return () => { controller.abort(); clearInterval(timer) }
  }, [fetchImpl, mock, status])
  const health = status?.worker || worker
  if (health?.state === 'unavailable') return <p>Lab worker status is unavailable right now.</p>
  const offline = health && !health.workers?.some(item => Number.isFinite(item.heartbeat_age_s) && item.heartbeat_age_s <= 60)
  return <>
    {offline && <p>Worker offline</p>}
    {health?.paused && <p>Worker paused</p>}
    {!health && <p>Worker status unavailable</p>}
    <p>Queue age: {health?.queue_age_max_s == null ? 'unknown' : `${health.queue_age_max_s}s`}</p>
  </>
}
export default function RunsView({ hidden = false, store = labValueStore, mock = false, fetchImpl = globalThis.fetch }) {
  const [now, setNow] = useState(Date.now)
  const allRuns = useRuns(store)
  const runs = allRuns.filter(run => run.id !== 'local')
  useEffect(() => {
    if (hidden) return undefined
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [hidden])
  return <div hidden={hidden}>
    <h2>Runs</h2>
    {!hidden && <WorkerStatus mock={mock} fetchImpl={fetchImpl} />}
    {!runs.length && <p>No runs yet. Run a saved revision from the graph.</p>}
    {!!runs.length && <CorpusComparison runs={allRuns} />}
    {runs.map(run => <RunResult key={run.id} store={store} runId={run.id} fetchImpl={fetchImpl} now={now} showCorpusJobs={!hidden && !mock} />)}
  </div>
}
