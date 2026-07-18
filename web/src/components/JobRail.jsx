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

function stateTag(job) {
  if (job.status === 'running') return { cls: 'prog', label: 'running' }
  if (job.status === 'submitted') return { cls: 'sub', label: 'submitted' }
  if (job.status === 'failed') return isQuota(job) ? { cls: 'quota', label: 'spend cap' } : { cls: 'fail', label: 'failed' }
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
  const quota = isQuota(job)
  const cls = [
    'job',
    job.degraded_mode ? 'deg' : '',
    (job.status === 'failed' && !quota) ? 'fail' : '',
    quota ? 'quota' : '',
    current ? 'current' : '',
  ].filter(Boolean).join(' ')

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
          : <span>{elapsed || ''}</span>}
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
