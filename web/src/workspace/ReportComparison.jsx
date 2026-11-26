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
          <dl>
            <dt>Drawing version</dt>
            <dd>{first.drawingVersion === second.drawingVersion ? 'Same drawing version' : 'Different drawing versions'}</dd>
            <dt>Tool version</dt>
            <dd>{toolVersionResult(first, second)}</dd>
          </dl>
        )}
      </div>
    </section>
  )
}
