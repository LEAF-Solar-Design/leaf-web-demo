// Right-rail live events ledger (22g). Renders every async state
// (submitted / running / complete / degraded / failed) as 32px hairline ledger
// rows: leading status dot (grammar: solid green = done, pulse = running,
// hollow = queued, amber square = advisory/degraded, red = failed), 12.5px
// event text, mono 11px clock column (day-grouped, clock-only inside groups —
// TM1; hover reveals the absolute time). LIVE: the recent GET /api/jobs list
// for the demo tenant + a re-attach chip for the durable in-flight job pointer
// (localStorage). Clicking a terminal row asks the parent to re-render its
// stored envelope; the row showing in the center carries the SL1 current mark.
// MOCK: no /api calls — shows the current in-session run only.
import { useEffect, useState } from 'react'
import './rails.css'

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
// returns null so the row shows just the clock, never an awkward "$0.0000" (B1).
function costUsd(job) {
  const c = job.cost || (job.result && job.result.cost)
  const v = c && c.usd_est
  const n = Number(v)
  if (!Number.isFinite(n) || n <= 0) return null
  return n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`
}

// Status word (sentence case, matching the app's calm vocabulary) + which
// tint voice it speaks: ok (green), warn (amber), err (red), mut (neutral —
// hollow states keep neutral words).
function stateTag(job) {
  if (job.status === 'running') return { tint: 'ok', label: 'running' }
  if (job.status === 'submitted') return { tint: 'mut', label: 'submitted' }
  if (job.status === 'failed') {
    if (isQuota(job)) return { tint: 'warn', label: 'spend cap' }
    if (isEnt(job)) return { tint: 'warn', label: 'plan' }
    return { tint: 'err', label: 'failed' }
  }
  if (job.status === 'complete') {
    return job.degraded_mode ? { tint: 'warn', label: 'degraded' } : { tint: 'ok', label: 'complete' }
  }
  return { tint: 'mut', label: job.status || 'pending' }
}

// Dot grammar (contract .dot classes): pulse only on live, hollow = not in
// play, square = advisory/degraded, red = failed. Calm plan/quota rejections
// are advisories, not failures.
function dotFor(job) {
  if (job.status === 'running') return 'dot live pulse'
  if (job.status === 'submitted' || job.status === 'queued') return 'dot hollow'
  if (job.status === 'failed') return isCalm(job) ? 'dot square' : 'dot red'
  if (job.status === 'complete') return job.degraded_mode ? 'dot square' : 'dot'
  return 'dot hollow'
}

// --- TM1 time. Accepts epoch seconds (the server's REAL columns), epoch ms,
// or ISO strings. Shared with WorkspaceSummary (imported from here — the
// rails-lane anchor module).
function toDate(ts) {
  if (ts == null) return null
  if (typeof ts === 'number') return new Date(ts < 1e12 ? ts * 1000 : ts)
  const d = new Date(ts)
  return Number.isNaN(d.getTime()) ? null : d
}
export function fmtWhen(ts) {
  const d = toDate(ts)
  if (!d) return null
  const abs = d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
  const age = Date.now() - d.getTime()
  let rel
  if (age < 60_000) rel = 'now'
  else if (age < 3_600_000) rel = `${Math.floor(age / 60_000)} m`
  else if (age < 86_400_000) rel = `${Math.floor(age / 3_600_000)} h`
  else rel = d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  const clock = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
  return { rel, abs, clock, day: d.toDateString(), date: d }
}

// 26px day-boundary label ("Today · Jul 16") for the ledger. Exported so
// other panels rendering the same TM1 day-grouped ledger convention (e.g.
// operator/SessionPanel's transcript) don't reimplement the "Today"/
// "Yesterday" boundary text.
export function dayLabel(d) {
  const md = d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  if (d.toDateString() === new Date().toDateString()) return `Today · ${md}`
  if (d.toDateString() === new Date(Date.now() - 86_400_000).toDateString()) return `Yesterday · ${md}`
  return md
}

// The empty-state action chip focuses the composer directly (the docked bar's
// input, falling back to the legacy prompt textarea) — no parent wiring needed.
function focusComposer() {
  const el = document.querySelector('.bar textarea') || document.querySelector('.bar input')
    || document.querySelector('.prompt textarea')
  if (el) el.focus()
}

function JobRow({ job, current, onSelect }) {
  const st = stateTag(job)
  const terminal = job.status === 'complete' || job.status === 'failed'
  const elapsed = job.elapsed_ms != null ? `${(job.elapsed_ms / 1000).toFixed(1)}s` : null
  const usd = job.status === 'complete' ? costUsd(job) : null
  const when = fmtWhen(job.created_at)

  // Quiet muted detail after the tinted status word (one line, ellipsized).
  let detail = null
  if (job.status === 'failed') {
    const e = job.error
    detail = (e && (e.message || e.error_code)) || null
  } else if (job.status === 'running' && job.progress && job.progress !== 'running') {
    detail = job.progress
  } else if (job.status === 'complete' && job.degraded_mode) {
    detail = 'local fallback'
  } else if (usd) {
    detail = usd
  }
  if (current) detail = detail ? `this session · ${detail}` : 'this session'

  // Right column: running = elapsed (indeterminate work is pulse dot + verb +
  // elapsed — never a fake progress bar); terminal = clock-only inside the day
  // group, absolute revealed on hover (TM1).
  const tail = job.status === 'running' ? elapsed : ((when && when.clock) || elapsed)

  const cls = ['rail-row', current ? 'current' : ''].filter(Boolean).join(' ')
  const inner = (
    <>
      <span className={dotFor(job)} />
      <span className="rail-ev">
        <b className="rail-tool">{job.tool}</b>
        <span className={`rail-word ${st.tint}`}>{st.label}</span>
        {detail && <span className="rail-detail">· {detail}</span>}
      </span>
      {tail && <span className="rail-when" title={when ? when.abs : undefined}>{tail}</span>}
    </>
  )

  if (terminal) {
    return (
      <button className={cls} onClick={() => onSelect && onSelect(job)}>
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
  // SL1: the row whose envelope the center pane is showing carries `current`.
  const selectedId = currentJob && currentJob.job_id

  // First-fetch grace: in live mode an empty list means "still loading" until
  // ~two poll ticks have passed — skeletons until then, so the empty-state
  // sentence is only ever asserted once it is true.
  const [settled, setSettled] = useState(false)
  useEffect(() => {
    if (mock) return undefined
    const t = setTimeout(() => setSettled(true), 4000)
    return () => clearTimeout(t)
  }, [mock])
  const loaded = mock || settled || list.length > 0

  // Day-grouped ledger rows (list arrives newest-first from the server).
  const rows = []
  let lastDay = null
  for (const job of list) {
    const w = fmtWhen(job.created_at)
    if (w && w.day !== lastDay) {
      rows.push(<div key={`day-${w.day}`} className="rail-day">{dayLabel(w.date)}</div>)
      lastDay = w.day
    }
    rows.push(
      <JobRow
        key={job.job_id}
        job={job}
        current={selectedId != null && job.job_id === selectedId}
        onSelect={onSelectJob}
      />,
    )
  }

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

      <div className="rail-ledger">
        {showCurrent && <JobRow job={currentJob} current onSelect={onSelectJob} />}
        {rows}
      </div>

      {!loaded && !showCurrent && list.length === 0 && (
        <div className="rail-ske">
          <div className="skeleton-row" />
          <div className="skeleton-row" />
        </div>
      )}

      {loaded && !showCurrent && list.length === 0 && (
        <div className="rail-empty">
          <div className="rail-note">Jobs you dispatch will appear here.</div>
          <button className="chip-act" onClick={focusComposer}>Dispatch a prompt</button>
        </div>
      )}
    </aside>
  )
}
