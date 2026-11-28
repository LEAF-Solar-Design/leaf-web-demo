import { useLayoutEffect, useRef, useState } from 'react'
import { createProjectSolarClient, openProject } from '../api.js'
import { savedOptimizerMeasurement, validReconciliationResponse } from '../solar/projectReconciliation.js'
import { createSolarDrawingBinding } from '../solar/solarDrawingBinding.js'

const DRAWING_LIMIT = 20
const READ_CONCURRENCY = 4

function currentDrawings(workspace, orgId, projectId) {
  const scoped = row => row?.org_id === orgId && row.project_id === projectId
  if (!scoped(workspace?.project) || !Array.isArray(workspace.drawing_versions) ||
      !Array.isArray(workspace.drawing_artifacts)) throw new Error('Invalid workspace')

  const names = new Map(workspace.drawing_artifacts.filter(scoped).map(row => [row.drawing_id, row.name]))
  const heads = new Map()
  const versionIds = new Set()
  const sequences = new Set()
  for (const row of workspace.drawing_versions) {
    const seqKey = `${row?.drawing_id}:${row?.seq}`
    if (!scoped(row) || !Number.isSafeInteger(row.seq) || row.seq < 1 ||
        versionIds.has(row.version_id) || sequences.has(seqKey)) throw new Error('Invalid drawing versions')
    const binding = createSolarDrawingBinding({ kind: 'project', organization_id: orgId,
      project_id: projectId, drawing_id: row.drawing_id, input_version_id: row.version_id })
    versionIds.add(row.version_id)
    sequences.add(seqKey)
    if (!heads.has(row.drawing_id) || heads.get(row.drawing_id).seq < row.seq) {
      heads.set(row.drawing_id, { binding, seq: row.seq, name: names.get(row.drawing_id) || row.drawing_id })
    }
  }
  return [...heads.values()].sort((a, b) => a.name.localeCompare(b.name) ||
    a.binding.drawing_id.localeCompare(b.binding.drawing_id))
}

async function readCoverage(drawing, viewerId, signal) {
  const { binding } = drawing
  const client = createProjectSolarClient()
  try {
    client.setScope(binding, viewerId)
    const response = await client.readContext({ signal })
    if (!validReconciliationResponse(response, binding)) return { ...drawing, state: 'unavailable' }
    const { context, intake } = response.data
    if (!context.is_head || context.head_version_id !== binding.input_version_id) {
      return { ...drawing, state: 'changed' }
    }
    const graph = intake.solar_design_graph
    const evidence = graph.extra?.solaredge_import?.optimizer_measurement
    if (evidence == null) return { ...drawing, state: 'missing' }
    if (Number.isSafeInteger(evidence.graph_rev) && evidence.graph_rev !== graph.rev) {
      return { ...drawing, state: 'stale' }
    }
    const measurement = savedOptimizerMeasurement(graph)
    return measurement ? { ...drawing, state: 'measured', count: measurement.total_count }
      : { ...drawing, state: 'invalid' }
  } finally {
    client.dispose()
  }
}

const statuses = {
  missing: 'Missing measured optimizer count',
  stale: 'Saved optimizer measurement is for an earlier or different graph revision',
  invalid: 'Missing verified optimizer count (saved measurement is invalid)',
  unavailable: 'Saved design unavailable',
  changed: 'Saved revision changed. Refresh to check the current revision.',
}
const order = ['missing', 'stale', 'invalid', 'changed', 'unavailable', 'measured']

export default function ProjectOptimizerCoverage({ orgId, projectId, viewerId }) {
  const pending = useRef(null)
  const [view, setView] = useState(null)
  useLayoutEffect(() => () => pending.current?.abort(), [])

  async function load(previous) {
    if (pending.current) return
    const request = new AbortController()
    pending.current = request
    const current = () => pending.current === request && !request.signal.aborted
    const error = 'Optimizer measurement coverage could not be read. Try again.'
    const failed = { ...previous, error }
    const timer = setTimeout(() => {
      if (current()) setView(failed)
      pending.current = null
      request.abort()
    }, 30000)
    setView({ ...previous, error: null, loading: true })
    try {
      const drawings = currentDrawings(await openProject(projectId, orgId), orgId, projectId)
      if (!current()) return
      const prior = new Map((previous?.rows ?? []).map(row => [row.binding.input_version_id, row]))
      const rows = [], unchecked = []
      for (const drawing of drawings) {
        const row = prior.get(drawing.binding.input_version_id)
        if (row && row.binding.drawing_id === drawing.binding.drawing_id &&
          row.seq === drawing.seq && !['changed', 'unavailable'].includes(row.state)) {
          rows.push({ ...row, name: drawing.name })
        } else unchecked.push(drawing)
      }
      const selected = unchecked.slice(0, DRAWING_LIMIT)
      for (let offset = 0; offset < selected.length && current(); offset += READ_CONCURRENCY) {
        const batch = selected.slice(offset, offset + READ_CONCURRENCY)
        rows.push(...await Promise.all(batch.map(drawing => readCoverage(drawing, viewerId, request.signal))))
      }
      rows.sort((a, b) => order.indexOf(a.state) - order.indexOf(b.state))
      if (current()) setView({ rows, drawings })
    } catch {
      if (current()) setView(failed)
    } finally {
      clearTimeout(timer)
      if (pending.current === request) pending.current = null
    }
  }

  return <section aria-label="Optimizer measurement coverage">
    <h3>Optimizer measurement coverage</h3>
    <button type="button" className="chip-act" disabled={view?.loading} onClick={() => load()}>
      {view?.rows ? 'Refresh optimizer coverage' : 'Check optimizer coverage'}
    </button>
    {view?.loading && <p role="status">Checking saved drawing revisions…</p>}
    {view?.error && <p role="status">{view.error}</p>}
    {view?.rows?.length === 0 && <p>No saved drawing revisions.</p>}
    {view?.rows?.length > 0 && <>
      <p>Checked {view.rows.length} of {view.drawings.length} saved drawings.</p>
      <table className="ops-table">
        <caption>Optimizer evidence on current saved revisions</caption>
        <thead><tr><th scope="col">Drawing</th><th scope="col">Revision</th><th scope="col">Optimizer count</th></tr></thead>
        <tbody>{view.rows.map(row => <tr key={row.binding.drawing_id}>
          <th scope="row">{row.name}</th><td>{row.seq}</td>
          <td>{row.state === 'measured' ? `${row.count} measured` : statuses[row.state]}</td>
        </tr>)}</tbody>
      </table>
      {view.drawings.length > view.rows.length && <button type="button" className="chip-act"
        disabled={view.loading} onClick={() => load(view)}>Check more drawings</button>}
    </>}
  </section>
}
