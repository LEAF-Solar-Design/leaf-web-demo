// Workspace summary card: the hydration payload for an open project
// (GET /api/projects/{id} -> {project, drawing_versions[], jobs[], built_tools[]}).
// Shows the drawing-version count, the built-tool count, and the project's job
// list with status. Runs made with this project open land here (X-Org-Id +
// X-Project-Id on /api/run), so jobs[] visibly grows after each terminal run.
//
// Calm vocabulary: square mono state tags, no emoji, no spinners. Job status
// vocabulary is the platform's own (queued|running|succeeded|failed|cancelled).

const JOB_STATE = {
  succeeded: { cls: 'done', label: 'succeeded' },
  running: { cls: 'prog', label: 'running' },
  queued: { cls: 'sub', label: 'queued' },
  failed: { cls: 'fail', label: 'failed' },
  cancelled: { cls: 'sub', label: 'cancelled' },
}

function fmtWhen(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
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
          <button className="btn ghost ws-close" onClick={onClose}>Close</button>
        )}
      </div>

      <div className="ws-metrics">
        <div className="ws-metric">
          <span className="ws-n">{versions.length}</span>
          <span className="ws-k">drawing version{versions.length === 1 ? '' : 's'}</span>
        </div>
        <div className="ws-metric">
          <span className="ws-n">{jobs.length}</span>
          <span className="ws-k">job{jobs.length === 1 ? '' : 's'}</span>
        </div>
        <div className="ws-metric">
          <span className="ws-n">{tools.length}</span>
          <span className="ws-k">built tool{tools.length === 1 ? '' : 's'}</span>
        </div>
      </div>

      <div className="ws-jobs">
        <div className="ws-jobs-head">Jobs</div>
        {rows.length === 0 ? (
          <div className="ws-note">No jobs yet. Run a tool with this project open and it appears here.</div>
        ) : (
          <ul className="ws-job-list">
            {rows.map((j) => {
              const st = JOB_STATE[j.status] || { cls: 'sub', label: j.status || 'pending' }
              return (
                <li key={j.job_id} className="ws-job">
                  <span className="ws-job-main">
                    <b>{j.tool_name || j.kind}</b>
                    <span className="ws-job-kind">{j.kind}</span>
                  </span>
                  <span className="ws-job-meta">
                    {typeof j.cost_usd === 'number' && <span className="ws-job-cost">~${j.cost_usd.toFixed(4)}</span>}
                    <span className="ws-job-when">{fmtWhen(j.updated_at || j.created_at)}</span>
                    <span className={`state ${st.cls}`}>{st.label}</span>
                  </span>
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </section>
  )
}
