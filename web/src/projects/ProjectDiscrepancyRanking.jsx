import { useLayoutEffect, useRef, useState } from 'react'
import { createProjectSolarClient, openProject } from '../api.js'
import { createSolarDrawingBinding } from '../solar/solarDrawingBinding.js'
import {
  parseReconciliationNumber, RECONCILIATION_METRICS, reconciliationRowsFromValues,
  savedReconciliationValues, validReconciliationResponse,
} from '../solar/projectReconciliation.js'

const PAGE_SIZE = 8
const READ_CONCURRENCY = 3
const RANK_SIZE = 3

function currentRevisions(workspace, orgId, projectId) {
  const scoped = row => row?.org_id === orgId && row.project_id === projectId
  if (!scoped(workspace?.project) || !Array.isArray(workspace.drawing_versions)) {
    throw new Error('Project unavailable')
  }
  const heads = new Map(), revisions = new Set(), sequences = new Set()
  for (const row of workspace.drawing_versions) {
    const sequence = JSON.stringify([row?.drawing_id, row?.seq])
    if (!scoped(row) || !Number.isSafeInteger(row.seq) || row.seq < 1 ||
      revisions.has(row.version_id) || sequences.has(sequence)) throw new Error('Invalid saved revisions')
    const binding = createSolarDrawingBinding({
      kind: 'project', organization_id: orgId, project_id: projectId,
      drawing_id: row.drawing_id, input_version_id: row.version_id,
    })
    revisions.add(row.version_id)
    sequences.add(sequence)
    if (!heads.has(row.drawing_id) || heads.get(row.drawing_id).seq < row.seq) {
      heads.set(row.drawing_id, { binding, seq: row.seq })
    }
  }
  return heads.values()
}

async function readProjectRanking(project, orgId, principalId, signal) {
  const projectId = project.project_id || project.id
  const result = { projectId, name: project.name || projectId, metrics: {}, unreadable: 0, unavailable: false }
  const client = createProjectSolarClient()
  try {
    const workspace = await openProject(projectId, orgId)
    const revisions = currentRevisions(workspace, orgId, projectId)
    for (const { binding, seq } of revisions) {
      if (signal.aborted) break
      client.setScope(binding, principalId)
      const context = await client.readContext({ signal })
      if (!validReconciliationResponse(context, binding) || !context.data.context.is_head ||
        context.data.context.head_version_id !== binding.input_version_id) {
        result.unreadable += 1
        continue
      }
      const { solar_design_graph: graph, solar_design_graph_sha256: graphSha256 } = context.data.intake
      const report = await client.loadEngineeringTotals({ graphSha256, signal })
      const rows = reconciliationRowsFromValues(savedReconciliationValues(graph), report.data?.record?.values)
      if (rows.some(row => row.verdict === 'UNREADABLE')) result.unreadable += 1
      for (const { metric, delta, verdict } of rows) {
        if (verdict !== 'FAIL') continue
        const magnitude = parseReconciliationNumber(delta.replace(/^-/, ''))
        const previous = result.metrics[metric]
        if (!previous || magnitude > previous.magnitude || (magnitude === previous.magnitude &&
          binding.input_version_id < previous.binding.input_version_id)) {
          result.metrics[metric] = { binding, delta, magnitude, seq }
        }
      }
    }
    return result
  } catch {
    return { ...result, unavailable: true, metrics: {} }
  } finally {
    client.dispose()
  }
}

export default function ProjectDiscrepancyRanking({ orgId, principalId, projects, onOpenRevision }) {
  const pending = useRef(null)
  const navigation = useRef(false)
  const [rows, setRows] = useState([])
  const [loading, setLoading] = useState(false)
  const [started, setStarted] = useState(false)
  const [opening, setOpening] = useState(false)
  const [error, setError] = useState(null)
  useLayoutEffect(() => () => pending.current?.abort(), [])

  async function load(start) {
    if (pending.current || navigation.current) return
    const request = new AbortController()
    pending.current = request
    const page = projects.slice(start, start + PAGE_SIZE)
    const results = []
    setStarted(true)
    setLoading(true)
    setError(null)
    const timer = setTimeout(() => {
      request.abort()
      pending.current = null
      setError('The project scan timed out. Retry this page.')
      setLoading(false)
    }, 30000)
    try {
      for (let i = 0; i < page.length && !request.signal.aborted; i += READ_CONCURRENCY) {
        results.push(...await Promise.all(page.slice(i, i + READ_CONCURRENCY)
          .map(project => readProjectRanking(project, orgId, principalId, request.signal))))
      }
      if (!request.signal.aborted) setRows(previous => [...previous.slice(0, start), ...results])
    } finally {
      clearTimeout(timer)
      if (pending.current === request) {
        pending.current = null
        setLoading(false)
      }
    }
  }

  async function open(binding) {
    if (navigation.current) return
    navigation.current = true
    setOpening(true)
    setError(null)
    try {
      if (!await onOpenRevision(binding)) setError('This saved revision could not be opened.')
    } catch {
      setError('This saved revision could not be opened.')
    } finally {
      navigation.current = false
      setOpening(false)
    }
  }

  const unavailable = rows.filter(project => project.unavailable).length
  const unreadable = rows.reduce((total, project) => total + project.unreadable, 0)
  const rankings = RECONCILIATION_METRICS.map(metric => ({
    metric,
    winners: rows.filter(project => project.metrics[metric])
      .sort((a, b) => {
        const left = a.metrics[metric].magnitude, right = b.metrics[metric].magnitude
        return left === right ? a.projectId.localeCompare(b.projectId) : left > right ? -1 : 1
      }).slice(0, RANK_SIZE),
  }))

  return <section aria-label="Engineering discrepancies across projects">
    <div className="resolver-header">Engineering discrepancies</div>
    <button type="button" className="chip-act" role="menuitem" disabled={loading || opening}
      onClick={() => { if (rows.length === projects.length) setRows([]); void load(rows.length === projects.length ? 0 : rows.length) }}>
      {loading ? 'Checking saved revisions…' : rows.length === projects.length ? 'Refresh discrepancies' :
        rows.length ? 'Check more projects' : 'Rank engineering discrepancies'}
    </button>
    {started && <p role="status">{rows.length} of {projects.length} projects checked.
      {unavailable > 0 && ` ${unavailable} unavailable.`}
      {unreadable > 0 && ` ${unreadable} revisions lack complete comparison evidence.`}
    </p>}
    {error && <p role="alert">{error}</p>}
    {rows.length > 0 && <dl>
      {rankings.map(({ metric, winners }) => <div key={metric}>
        <dt>{metric}</dt>
        <dd>{winners.length ? <ol>{winners.map(project => {
          const { binding, delta, seq } = project.metrics[metric]
          return <li key={project.projectId}>{delta} · <button className="chip-act" type="button"
            role="menuitem" disabled={opening || loading} onClick={() => void open(binding)}>
            {project.name}, revision {seq}
          </button></li>
        })}</ol> : 'No verified discrepancy found'}</dd>
      </div>)}
    </dl>}
  </section>
}
