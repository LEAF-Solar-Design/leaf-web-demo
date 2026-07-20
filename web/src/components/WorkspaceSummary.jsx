// Workspace summary card: the hydration payload for an open project
// (GET /api/projects/{id} -> {project, drawing_versions[], jobs[], built_tools[]}).
// ONE big number (the job count) with the version/tool counts demoted to a
// muted stats line, then the project's job list as 22g ledger rows (32px
// hairline rows: status dot + event text, mono cost/time columns, TM1 relative
// times with the absolute on hover). Runs made with this project open land
// here (X-Org-Id + X-Project-Id on /api/run), so jobs[] visibly grows after
// each terminal run.
//
// Calm vocabulary: dot-grammar states (no pills), no emoji, no spinners. Job
// status vocabulary is the platform's own (queued|running|succeeded|failed|
// cancelled). Close = the Esc cap (never a ✕ glyph).
import { fmtWhen } from './JobRail.jsx'

const JOB_STATE = {
  succeeded: { dot: 'dot', tint: 'ok', label: 'succeeded' },
  running: { dot: 'dot live pulse', tint: 'ok', label: 'running' },
  queued: { dot: 'dot hollow', tint: 'mut', label: 'queued' },
  failed: { dot: 'dot red', tint: 'err', label: 'failed' },
  cancelled: { dot: 'dot hollow', tint: 'mut', label: 'cancelled' },
}

export default function WorkspaceSummary({ workspace, loading, onClose }) {
  if (!workspace) return null
  const project = workspace.project || {}
  const versions = workspace.drawing_versions || []
  const jobs = workspace.jobs || []
  const tools = workspace.built_tools || []
  const rows = [...jobs].reverse() // newest first (list_jobs returns created ASC)

  return (
    <section className="card workspace-summary">
      <div className="ws-head">
        <div className="ws-title">
          <span className="tag">Workspace</span>
          <b>{project.name || 'project'}</b>
          {loading && <span className="dim"> · refreshing</span>}
        </div>
        {onClose && (
          <button className="key hot" onClick={onClose} aria-label="Close workspace">Esc</button>
        )}
      </div>

      <div className="ws-metrics">
        <div className="ws-metric">
          <span className="ws-n">{jobs.length}</span>
          <span className="ws-k">job{jobs.length === 1 ? '' : 's'}</span>
        </div>
        <div className="ws-stats">
          {versions.length} drawing version{versions.length === 1 ? '' : 's'}
          {' · '}
          {tools.length} built tool{tools.length === 1 ? '' : 's'}
        </div>
      </div>

      <div className="ws-jobs">
        <div className="ws-jobs-head">Jobs</div>
        {rows.length === 0 ? (
          <div className="ws-note">No jobs yet. Run a tool with this project open and it appears here.</div>
        ) : (
          <div className="rail-ledger">
            {rows.map((j) => {
              const st = JOB_STATE[j.status] || { dot: 'dot hollow', tint: 'mut', label: j.status || 'pending' }
              const w = fmtWhen(j.updated_at || j.created_at)
              return (
                <div key={j.job_id} className="rail-row">
                  <span className={st.dot} />
                  <span className="rail-ev">
                    <b className="rail-tool">{j.tool_name || j.kind}</b>
                    <span className={`rail-word ${st.tint}`}>{st.label}</span>
                    {j.tool_name && j.kind && <span className="rail-detail">· {j.kind}</span>}
                  </span>
                  {typeof j.cost_usd === 'number' && <span className="rail-cost">~${j.cost_usd.toFixed(4)}</span>}
                  {w && <span className="rail-when" title={w.abs}>{w.rel}</span>}
                </div>
              )
            })}
          </div>
        )}
      </div>
    </section>
  )
}
