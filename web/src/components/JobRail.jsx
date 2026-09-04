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
//
// Slice 11a: the rail is the HOST of the BuildQueueCard. Every row is one
// build record (lib/buildQueue.js): a job row maps through fromBrokerJob,
// which carries this file's former stateTag / costUsd / dot vocabulary
// verbatim, so a broker row renders exactly as before. `builds` (optional,
// GET /api/builds records already parsed) adds the other lanes below the
// jobs; its broker-lane records are skipped here because `jobs` is the live
// source for that lane and already carries the current-session semantics.
import { useEffect, useState } from 'react'
import './rails.css'

import BuildQueueCard from './BuildQueueCard.jsx'
import { fromBrokerJob, runningBuildCount } from '../lib/buildQueue.js'
import { dayLabel, fmtWhen } from '../lib/railTime.js'

// TM1 time helpers moved to lib/railTime.js (slice 11a); re-exported so
// WorkspaceSummary and operator/SessionPanel keep importing from here.
export { dayLabel, fmtWhen }

// The empty-state action chip focuses the composer directly (the docked bar's
// input, falling back to the legacy prompt textarea) — no parent wiring needed.
function focusComposer() {
  const el = document.querySelector('.bar textarea') || document.querySelector('.bar input')
    || document.querySelector('.prompt textarea')
  if (el) el.focus()
}

function JobRow({ job, current, onSelect }) {
  const record = fromBrokerJob(job)
  // A job whose status this rail has no word for renders nothing rather than
  // a guessed row (fromBrokerJob fails closed on an unknown status).
  if (!record) return null
  return (
    <BuildQueueCard
      record={record}
      current={current}
      onSelect={onSelect ? () => onSelect(job) : undefined}
    />
  )
}

// The records `builds` adds to the rail: every lane but broker (see the
// header), newest first by `started`.
function extraBuilds(builds) {
  if (!Array.isArray(builds) || builds.length === 0) return []
  return builds
    .filter((r) => r && r.lane !== 'broker')
    .sort((a, b) => (b.started || 0) - (a.started || 0))
}

// W4d Slice D seating: `spine` renders the rail as a 44px strip (the live
// count and one expand button); `onCollapse` adds the collapse control to the
// expanded rail's header. Both undefined = the rail exactly as before (rail
// OFF is byte-identical by construction).
export default function JobRail({ mock, jobs, currentJob, inflight, reattaching, onSelectJob, builds, spine = false, onExpand, onCollapse }) {
  const list = jobs || []
  const knownIds = new Set(list.map((j) => j.job_id))
  const showCurrent = currentJob && (!currentJob.job_id || !knownIds.has(currentJob.job_id))
  const extra = extraBuilds(builds)
  const liveCount = list.filter((j) => j.status === 'running' || j.status === 'submitted').length
    + runningBuildCount(extra)
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
  if (extra.length > 0) {
    rows.push(<div key="builds-group" className="rail-day rail-builds-group">Autonomous runs and fleet tasks</div>)
    for (const record of extra) {
      rows.push(<BuildQueueCard key={`${record.lane}:${record.id}`} record={record} />)
    }
  }

  if (spine) {
    return (
      <aside className="rail" data-spine="true">
        <div className="rail-spine" role="toolbar" aria-label="Job monitor" aria-orientation="vertical">
          <button
            type="button"
            className="spine-btn spine-expand"
            aria-label={`Expand the job monitor (${liveCount} live)`}
            title="Expand the job monitor"
            onClick={() => onExpand?.()}
          >
            «
          </button>
          <span className="rail-spine-count" title={`${liveCount} live jobs`} aria-hidden="true">{liveCount}</span>
        </div>
      </aside>
    )
  }
  return (
    <aside className="rail">
      <h2>
        Job monitor
        <span className="n">{mock ? 'session' : `${liveCount} live`}</span>
        {onCollapse && (
          <button
            type="button"
            className="spine-btn spine-collapse"
            aria-label="Collapse the job monitor to a spine"
            title="Collapse to spine"
            onClick={onCollapse}
          >
            »
          </button>
        )}
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

      {!loaded && !showCurrent && list.length === 0 && extra.length === 0 && (
        <div className="rail-ske">
          <div className="skeleton-row" />
          <div className="skeleton-row" />
        </div>
      )}

      {loaded && !showCurrent && list.length === 0 && extra.length === 0 && (
        <div className="rail-empty">
          <div className="rail-note">Jobs you dispatch will appear here.</div>
          <button className="chip-act" onClick={focusComposer}>Dispatch a prompt</button>
        </div>
      )}
    </aside>
  )
}
