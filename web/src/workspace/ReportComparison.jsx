import { useState } from 'react'
import { savedReportBinding } from '../lib/savedReport.js'

function ReportPicker({ label, value, exclude, reports, onChange }) {
  return (
    <p>
      <label>
        {label}{' '}
        <select value={value || ''} onChange={event => onChange(event.target.value)} style={{ maxWidth: '100%' }}>
          <option value="">Select a saved report</option>
          {[...reports.values()].filter(report => report.id !== exclude).map(report => (
            <option key={report.id} value={report.id}>
              {report.tool || 'Report'} · {report.toolVersion || 'Version not recorded'} · {report.id}
            </option>
          ))}
        </select>
      </label>
    </p>
  )
}

function toolVersionResult(first, second) {
  if (!first.tool || !second.tool) return 'Tool identity could not be verified'
  if (first.tool !== second.tool) return 'Different tools'
  if (!first.toolVersion || !second.toolVersion) return 'Tool version could not be verified'
  return first.toolVersion === second.toolVersion ? 'Same tool version' : 'Different tool versions'
}

function layerAreaReport(job) {
  const { layers, layer_count: count, layer_filter: filter } = job.result?.result ?? {}
  const drawing = job.result?.project_context?.drawing_id
  const exactMatch = job.params?.exact_match
  if (typeof drawing !== 'string' || !drawing.trim() || !Array.isArray(layers) || layers.length !== count ||
      (filter !== null && typeof filter !== 'string') ||
      (exactMatch !== undefined && typeof exactMatch !== 'boolean')) return null

  const areas = new Map()
  for (const row of layers) {
    if (typeof row?.layer !== 'string' || !row.layer.trim() || areas.has(row.layer) ||
        !Number.isFinite(row.area) || row.area < 0) return null
    areas.set(row.layer, row.area)
  }

  return { drawing, filter, exactMatch: exactMatch ?? false, areas }
}

function rankAreas(areas, layers = areas.keys()) {
  const sorted = [...layers].sort((left, right) => areas.get(right) - areas.get(left))
  const ranks = new Map()
  let previousArea
  let rank
  sorted.forEach((layer, index) => {
    const area = areas.get(layer)
    if (area !== previousArea) rank = index + 1
    ranks.set(layer, rank)
    previousArea = area
  })
  return ranks
}

function LayerAreaComparison({ first, second }) {
  if (!first.tool || !first.toolVersion || first.tool !== second.tool ||
      first.toolVersion !== second.toolVersion) return null
  const a = layerAreaReport(first.job)
  const b = layerAreaReport(second.job)
  if (!a || !b || a.drawing !== b.drawing || a.filter !== b.filter || a.exactMatch !== b.exactMatch) return null

  const aRanks = rankAreas(a.areas)
  const bRanks = rankAreas(b.areas)
  const layers = new Set([...aRanks.keys(), ...bRanks.keys()])
  const shared = [...aRanks.keys()].filter(layer => bRanks.has(layer))
  const sharedARanks = rankAreas(a.areas, shared)
  const sharedBRanks = rankAreas(b.areas, shared)
  const rankChanged = shared.some(layer => sharedARanks.get(layer) !== sharedBRanks.get(layer))
  const matchesChanged = shared.length !== layers.size
  const summary = shared.length === 0 ? 'No shared layers' :
    rankChanged ? 'Layer area ranking changed' : 'No rank changes among shared layers'
  return (
    <>
      <p>{summary}</p>
      {matchesChanged && <p>Matching layers changed</p>}
      {layers.size > 0 && (
        <table>
          <caption>Area rank within each report</caption>
          <thead>
            <tr><th scope="col">Layer</th><th scope="col">First report</th><th scope="col">Second report</th></tr>
          </thead>
          <tbody>
            {[...layers].map(layer => (
              <tr key={layer}>
                <th scope="row">{layer}</th>
                <td>{aRanks.get(layer) ?? 'Absent'}</td>
                <td>{bRanks.get(layer) ?? 'Absent'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  )
}

export default function ReportComparison({ jobs, projectId }) {
  const [firstId, setFirstId] = useState('')
  const [secondId, setSecondId] = useState('')
  const reports = new Map()
  const seenJobIds = new Set()
  for (const job of jobs) {
    const id = job.job_id
    if (typeof id !== 'string' || !id) continue
    if (seenJobIds.has(id)) {
      reports.delete(id)
      continue
    }
    seenJobIds.add(id)
    const { version } = savedReportBinding(job, projectId)
    if (!version) continue
    const { tool, version: toolVersion } = job.result
    reports.set(id, {
      id,
      job,
      drawingVersion: version,
      tool: typeof tool === 'string' && tool.trim() ? tool : null,
      toolVersion: typeof toolVersion === 'string' && toolVersion.trim() ? toolVersion : null,
    })
  }
  const first = reports.get(firstId)
  const second = reports.get(secondId)
  if (firstId && !first) setFirstId('')
  if (secondId && !second) setSecondId('')
  if (reports.size < 2) return null
  return (
    <section className="ground-job-detail" aria-label="Compare saved reports">
      <h3>Compare saved reports</h3>
      <ReportPicker label="First report" value={first?.id} exclude={second?.id} reports={reports} onChange={setFirstId} />
      <ReportPicker label="Second report" value={second?.id} exclude={first?.id} reports={reports} onChange={setSecondId} />
      <div aria-live="polite">
        {first && second && (
          <>
            <dl>
              <dt>Drawing version</dt>
              <dd>{first.drawingVersion === second.drawingVersion ? 'Same drawing version' : 'Different drawing versions'}</dd>
              <dt>Tool version</dt>
              <dd>{toolVersionResult(first, second)}</dd>
            </dl>
            <LayerAreaComparison first={first} second={second} />
          </>
        )}
      </div>
    </section>
  )
}
