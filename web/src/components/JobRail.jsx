// Right-rail job monitor (design 20ab544c9f6b). Renders every async state
// (submitted / running / complete / degraded / failed) side by side. LIVE: the
// recent GET /api/jobs list for the demo tenant + a re-attach chip for the
// durable in-flight job pointer (localStorage). Clicking a terminal job asks the
// parent to re-render its stored envelope. MOCK: no /api calls — shows the
// current in-session run plus a note that live lists recent cloud jobs.

function shortId(id) {
  if (!id) return ''
  const s = String(id)
  return `job·${s.slice(0, 4)}-${s.slice(4, 8)}`
}

// A quota rejection (broker hard cap) rides in as a failed job whose error_code
// is 'quota_exceeded'. It is an expected budget state, not an alarm — render it
// amber (calm), never red. Only this code softens; all other failures stay red.
function isQuota(job) {
  const c = (job.error && job.error.error_code) || job.error_code
  return job.status === 'failed' && c === 'quota_exceeded'
}

// An entitlement rejection (a write tool the plan doesn't include) also rides in
// as a failed job — carried as `entitlement_required` on the current-session card.
// Same calm amber treatment as quota (a plan boundary, not a failure to alarm).
function isEnt(job) {
  return job.status === 'failed' && !!job.entitlement_required
}
function isCalm(job) { return isQuota(job) || isEnt(job) }

// Per-run cost (live APS runs). Read from the job's own cost or its stored §3
// envelope; null for mock runs (no APS cost) -> nothing shown. A ZERO cost also
// returns null so the compact card shows just elapsed, never an awkward
// "$0.0000" (B1).
function costUsd(job) {
  const c = job.cost || (job.result && job.result.cost)
  const v = c && c.usd_est
  const n = Number(v)
  if (!Number.isFinite(n) || n <= 0) return null
  return n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`
}

function stateTag(job) {
  if (job.status === 'running') return { cls: 'prog', label: 'running' }
  if (job.status === 'submitted') return { cls: 'sub', label: 'submitted' }
  if (job.status === 'failed') {
    if (isQuota(job)) return { cls: 'quota', label: 'spend cap' }
    if (isEnt(job)) return { cls: 'quota', label: 'plan' }
    return { cls: 'fail', label: 'failed' }
  }
  if (job.status === 'complete') {
    return job.degraded_mode ? { cls: 'deg', label: 'degraded' } : { cls: 'done', label: 'complete' }
  }
  return { cls: 'sub', label: job.status || 'pending' }
}

function footText(job) {
  if (job.status === 'failed') {
    const e = job.error
    return (e && (e.message || e.error_code)) || 'failed'
  }
  if (job.status === 'complete') return job.degraded_mode ? 'complete · local fallback' : 'complete'
  if (job.status === 'running') return job.progress || 'running'
  return 'queued'
}

function JobCard({ job, current, onSelect }) {
  const st = stateTag(job)
  const terminal = job.status === 'complete' || job.status === 'failed'
  const elapsed = job.elapsed_ms != null ? `${(job.elapsed_ms / 1000).toFixed(1)}s` : null
  const calm = isCalm(job)
  const usd = job.status === 'complete' ? costUsd(job) : null
  const cls = [
    'job',
    job.degraded_mode ? 'deg' : '',
    (job.status === 'failed' && !calm) ? 'fail' : '',
    calm ? 'quota' : '',
    current ? 'current' : '',
  ].filter(Boolean).join(' ')

  // Bottom-right meta: cost (live APS complete) preferred over bare elapsed.
  const meta = usd ? `${usd}${elapsed ? ` · ${elapsed}` : ''}` : (elapsed || '')

  const inner = (
    <>
      <div className="h">
        <b>{job.tool}</b>
        <span className={`state ${st.cls}`}>{st.label}</span>
      </div>
      <div className="jid">{shortId(job.job_id)}{current ? ' · this session' : ''}</div>
      {job.status === 'running' && <div className="bar"><i style={{ width: '34%' }} /></div>}
      <div className="foot">
        <span>{footText(job)}</span>
        {job.status === 'running'
          ? <span className="safe">safe to leave</span>
          : <span className="job-meta">{meta}</span>}
      </div>
    </>
  )

  if (terminal) {
    return (
      <button className={cls} onClick={() => onSelect && onSelect(job)} title="Show this result">
        {inner}
      </button>
    )
  }
  return <div className={cls}>{inner}</div>
}

export default function JobRail({ mock, jobs, currentJob, inflight, reattaching, onSelectJob }) {
  const list = jobs || []
  const knownIds = new Set(list.map((j) => j.job_id))
  const showCurrent = currentJob && (!currentJob.job_id || !knownIds.has(currentJob.job_id))
  const liveCount = list.filter((j) => j.status === 'running' || j.status === 'submitted').length

  return (
    <aside className="rail">
      <h2>
        Job monitor
        <span className="n">{mock ? 'session' : `${liveCount} live`}</span>
      </h2>

      {mock && (
        <div className="rail-note">
          Live mode lists your recent cloud jobs here (GET /api/jobs). In mock mode this shows the
          run in this session.
        </div>
      )}

      {reattaching && inflight && (
        <div className="reattach">
          <span>Re-attaching to in-flight job — {inflight.tool}</span>
        </div>
      )}

      {showCurrent && <JobCard job={currentJob} current onSelect={onSelectJob} />}

      {list.map((job) => (
        <JobCard key={job.job_id} job={job} onSelect={onSelectJob} />
      ))}

      {!showCurrent && list.length === 0 && (
        <div className="rail-note">No jobs yet. Dispatch a prompt to see it appear here.</div>
      )}
    </aside>
  )
}
