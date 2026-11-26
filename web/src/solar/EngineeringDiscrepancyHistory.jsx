import { useLayoutEffect, useRef, useState } from 'react'
import { createProjectSolarClient, openProject } from '../api.js'
import { createSolarDrawingBinding } from './solarDrawingBinding.js'
import { RECONCILIATION_METRICS, reconciliationRowsFromValues, savedReconciliationValues,
  validReconciliationResponse } from './projectReconciliation.js'

const HISTORY_LIMIT = 12
const HISTORY_TIMEOUT_MS = 30000

function versionHistory(workspace, binding) {
  if (workspace?.project?.org_id !== binding.organization_id ||
      workspace.project.project_id !== binding.project_id || !Array.isArray(workspace.drawing_versions)) {
    throw new Error('Project version history unavailable')
  }
  const versions = workspace.drawing_versions.filter(row => row?.drawing_id === binding.drawing_id)
  const ids = new Set(), sequences = new Set()
  for (const row of versions) {
    if (row.org_id !== binding.organization_id || row.project_id !== binding.project_id ||
        !Number.isSafeInteger(row.seq) || row.seq < 1 || ids.has(row.version_id) || sequences.has(row.seq)) {
      throw new Error('Invalid project version history')
    }
    createSolarDrawingBinding({ ...binding, input_version_id: row.version_id })
    ids.add(row.version_id)
    sequences.add(row.seq)
  }
  const active = versions.find(row => row.version_id === binding.input_version_id)
  if (!active) throw new Error('Selected revision unavailable')
  const eligible = versions.filter(row => row.seq <= active.seq).sort((a, b) => a.seq - b.seq)
  return { versions: eligible.slice(-HISTORY_LIMIT), truncated: eligible.length > HISTORY_LIMIT }
}

async function readRevision(client, binding, authorityKey, signal) {
  client.setScope(binding, authorityKey)
  const context = await client.readContext({ signal })
  if (!validReconciliationResponse(context, binding)) return { reason: 'Saved design unavailable.' }
  const { solar_design_graph: graph, solar_design_graph_sha256: graphSha256 } = context.data.intake
  const report = await client.loadEngineeringTotals({ graphSha256, signal })
  if (!report.ok) return { reason: 'Saved engineering totals unavailable for this design.' }
  if (!report.data.record) return { reason: 'No saved engineering totals for this revision.' }
  return { rows: reconciliationRowsFromValues(savedReconciliationValues(graph), report.data.record.values) }
}

function describeMetric(row, previous) {
  if (!row || row.verdict === 'UNREADABLE') return 'Unavailable'
  if (row.verdict === 'FAIL') {
    const label = previous?.verdict === 'PASS' ? 'Discrepancy appeared' : 'Discrepancy'
    return `${label} (${row.delta})`
  }
  return previous?.verdict === 'FAIL' ? 'Discrepancy resolved' : 'Matches'
}

export default function EngineeringDiscrepancyHistory({ binding, authorityKey, isCurrent }) {
  const pending = useRef(null)
  const [view, setView] = useState(null)
  useLayoutEffect(() => () => pending.current?.abort(), [])

  async function load() {
    if (!isCurrent() || pending.current) return
    const active = new AbortController()
    pending.current = active
    const client = createProjectSolarClient()
    const current = () => !active.signal.aborted && pending.current === active && isCurrent()
    const error = 'Saved discrepancy history could not be read. Try again.'
    const timer = setTimeout(() => {
      if (current()) setView({ error })
      pending.current = null
      active.abort()
    }, HISTORY_TIMEOUT_MS)
    setView({ loading: true })
    try {
      const workspace = await openProject(binding.project_id, binding.organization_id)
      if (!current()) return
      const { versions, truncated } = versionHistory(workspace, binding)
      const revisions = []
      for (const { seq, version_id } of versions) {
        if (!current()) return
        const result = await readRevision(client, { ...binding, input_version_id: version_id }, authorityKey, active.signal)
        revisions.push({ seq, ...result })
      }
      if (current()) setView({ revisions, truncated })
    } catch {
      if (current()) setView({ error })
    } finally {
      clearTimeout(timer)
      client.dispose()
      if (pending.current === active) pending.current = null
    }
  }

  return <section aria-label="Saved discrepancy history">
    <button type="button" disabled={view?.loading} onClick={load}>
      {view?.revisions ? 'Refresh saved discrepancy history' : 'Read saved discrepancy history'}
    </button>
    {view?.loading && <p role="status">Reading saved discrepancy history…</p>}
    {view?.error && <p role="status">{view.error}</p>}
    {view?.revisions && <>
      <p>History uses the current saved evidence for each revision. A discrepancy onset or resolution
        requires readable values on consecutive revisions.</p>
      {view.truncated && <p>Showing the latest {HISTORY_LIMIT} revisions up to the selected revision.</p>}
      <div role="region" aria-label="Discrepancies by revision" tabIndex={0} style={{ overflowX: 'auto' }}>
        <table className="ops-table">
          <caption>Saved engineering discrepancies by drawing revision</caption>
          <thead><tr><th scope="col">Revision</th>{RECONCILIATION_METRICS.map(metric =>
            <th scope="col" key={metric}>{metric}</th>)}</tr></thead>
          <tbody>{view.revisions.map((revision, index) => {
            const prior = view.revisions[index - 1]
            const adjacent = prior?.seq === revision.seq - 1
            return <tr key={revision.seq}>
              <th scope="row">Revision {revision.seq}
                {(prior ? !adjacent : revision.seq > 1 && !view.truncated) &&
                  <small> · Earlier revision evidence missing</small>}
              </th>
              {revision.reason ? <td colSpan={RECONCILIATION_METRICS.length}>{revision.reason}</td> :
                revision.rows.map((row, metricIndex) => <td key={row.metric}>
                  {describeMetric(row, adjacent ? prior.rows?.[metricIndex] : null)}
                </td>)}
            </tr>
          })}</tbody>
        </table>
      </div>
    </>}
  </section>
}
