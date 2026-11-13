import { memo, useEffect, useState } from 'react'
import { authHeaders, config, noteUnauthorized } from '../../api.js'
import { fetchWithBudget } from '../../fetchBudget.js'
import { useLabSurfaceStatus } from '../useLabSurfaceStatus.js'
import { labValueStore, useBlockValue, useRun, useRuns } from './valueStore.js'
import { cancelRun } from './runStream.js'

const summarize = value => {
  try { return (typeof value === 'string' ? value : JSON.stringify(value) ?? 'null').slice(0, 200) }
  catch { return 'Value unavailable' }
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
function CorpusEvidence({ evidence }) {
  const report = evidence.evaluation, confirmation = report.confirm_report
  return <section aria-label="Corpus evaluation evidence">
    <h4>Corpus evaluation</h4>
    <p>Policy digest: {evidence.policy_digest}</p>
    <p>Policy version: {evidence.policy.version}</p>
    <p>Parent suite digest: {evidence.policy.parent_suite_sha256}</p>
    <p>Evaluated suite digest: {evidence.evaluated_suite_sha256}</p>
    <p>Verdict: {report.verdict}</p>
    <p>Confirmation verdict: {confirmation?.verdict ?? 'Unavailable (not evaluated)'}</p>
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
const RunResult = memo(function RunResult({ store, runId, fetchImpl, now }) {
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
  const runs = useRuns(store).filter(run => run.id !== 'local')
  useEffect(() => {
    if (hidden) return undefined
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [hidden])
  return <div hidden={hidden}>
    <h2>Runs</h2>
    {!hidden && <WorkerStatus mock={mock} fetchImpl={fetchImpl} />}
    {!runs.length && <p>No runs yet. Run a saved revision from the graph.</p>}
    {runs.map(run => <RunResult key={run.id} store={store} runId={run.id} fetchImpl={fetchImpl} now={now} />)}
  </div>
}
