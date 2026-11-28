import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { subscribeBeforeLogout } from '../auth.js'
import { readActiveScope, subscribeActiveScope } from '../auth/activeScope.js'
import { savedReportBinding } from '../lib/savedReport.js'
import { listProjectReferences, readProjectReferenceOriginal } from '../projects/api.js'
import SelectionComparison, { SelectionExports } from './SelectionComparison.jsx'

function ReportPicker({ label, value, exclude = [], reports, onChange }) {
  return (
    <p>
      <label>
        {label}{' '}
        <select value={value || ''} onChange={event => onChange(event.target.value)} style={{ maxWidth: '100%' }}>
          <option value="">Select a saved report</option>
          {[...reports.values()].filter(report => !exclude.includes(report.id)).map(report => (
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

function sameToolVersion(first, second) {
  return Boolean(first.tool && first.toolVersion && first.tool === second.tool &&
    first.toolVersion === second.toolVersion)
}

function savedLayerOptions(job) {
  const result = job.result?.result
  if (!Array.isArray(result?.layers) || !Number.isInteger(result.layer_count)) return null
  const filter = result.layer_filter
  const params = job.params
  const exact = params?.exact_match
  return {
    filter: filter === null || typeof filter === 'string' ? filter : undefined,
    exact: params && typeof params === 'object' && !Array.isArray(params) &&
      (exact === undefined || typeof exact === 'boolean') ? exact ?? false : undefined,
  }
}

function LayerOptionsComparison({ first, second }) {
  const a = savedLayerOptions(first.job)
  const b = savedLayerOptions(second.job)
  if (!a || !b) return null
  if (!sameToolVersion(first, second)) {
    return <p>Comparing saved options requires two reports from the same known tool version.</p>
  }

  const rows = [
    { name: 'Layer filter', left: a.filter, right: b.filter, display: value => value === null ? 'All layers' : value },
    { name: 'Exact match', left: a.exact, right: b.exact, display: value => value ? 'On' : 'Off' },
  ]
  return (
    <table>
      <caption>Saved layer report options</caption>
      <thead>
        <tr><th scope="col">Option</th><th scope="col">First report</th><th scope="col">Second report</th><th scope="col">Comparison</th></tr>
      </thead>
      <tbody>
        {rows.map(({ name, left, right, display }) => (
          <tr key={name}>
            <th scope="row">{name}</th>
            <td>{left === undefined ? 'Unknown' : display(left)}</td>
            <td>{right === undefined ? 'Unknown' : display(right)}</td>
            <td>{left === undefined || right === undefined ? 'Unavailable' : left === right ? 'Same' : 'Changed'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
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
  if (!savedLayerOptions(first.job) && !savedLayerOptions(second.job)) return null
  if (!sameToolVersion(first, second)) {
    return <p>Area ranking requires two reports from the same known tool version.</p>
  }
  const a = layerAreaReport(first.job)
  const b = layerAreaReport(second.job)
  if (!a || !b) return <p>Area ranking requires complete layer area data in both reports.</p>
  if (a.drawing !== b.drawing) return <p>Area ranking requires two reports for the same drawing.</p>
  if (a.filter !== b.filter || a.exactMatch !== b.exactMatch) {
    return <p>Area ranking requires the same layer filter and exact-match setting in both reports.</p>
  }

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
      {matchesChanged && shared.length > 0 && (
        <p>Other matching layers can shift a shared layer’s rank within each report.</p>
      )}
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

const REPORT_LABELS = ['First report', 'Second report', 'Third report']

function rankSeries(reports) {
  const areas = reports.map(report => layerAreaReport(report.job))
  if (!reports.some(report => savedLayerOptions(report.job))) return {}
  if (!reports.slice(1).every(report => sameToolVersion(reports[0], report))) {
    return { reason: 'Area ranking requires three reports from the same known tool version.' }
  }
  if (areas.some(area => !area)) {
    return { reason: 'Area ranking requires complete layer area data in all three reports.' }
  }
  if (!areas.every(area => area.drawing === areas[0].drawing)) {
    return { reason: 'Area ranking requires three reports for the same drawing.' }
  }
  if (!areas.every(area => area.filter === areas[0].filter && area.exactMatch === areas[0].exactMatch)) {
    return { reason: 'Area ranking requires the same layer filter and exact-match setting in all three reports.' }
  }
  const ranks = areas.map(area => rankAreas(area.areas))
  const layers = new Set(ranks.flatMap(rank => [...rank.keys()]))
  return { rows: [...layers].map(layer => ({ layer, ranks: ranks.map(rank => rank.get(layer)) })) }
}

function rankChange(before, after) {
  if (before === after) return before === undefined ? 'Absent' : 'Unchanged'
  if (before === undefined) return 'Added'
  if (after === undefined) return 'Removed'
  return after < before ? `Up ${before - after}` : `Down ${after - before}`
}

function SavedReportSeries({ reports }) {
  const tables = useRef(null)
  const { rows, reason } = rankSeries(reports)

  function download() {
    const html = '<!doctype html><meta charset="utf-8"><title>Saved report ranking changes</title>'
    const blob = new Blob([html, tables.current.innerHTML], { type: 'text/html' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = 'saved-report-ranking-changes.html'
    try {
      link.click()
    } finally {
      setTimeout(() => URL.revokeObjectURL(url), 0)
    }
  }

  return (
    <>
      <div ref={tables}>
        <table>
          <caption>Saved report versions in selected order</caption>
          <thead>
            <tr><th scope="col">Report</th><th scope="col">Saved job</th><th scope="col">Drawing</th>
              <th scope="col">Drawing version</th><th scope="col">Tool</th><th scope="col">Tool version</th></tr>
          </thead>
          <tbody>
            {reports.map(({ id, drawing, drawingVersion, tool, toolVersion }, index) => (
              <tr key={id}>
                <th scope="row">{REPORT_LABELS[index]}</th><td>{id}</td>
                <td>{drawing ?? 'Not recorded'}</td><td>{drawingVersion}</td>
                <td>{tool ?? 'Not recorded'}</td><td>{toolVersion ?? 'Not recorded'}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows && (
          <table>
            <caption>Area rank changes across saved reports</caption>
            <thead>
              <tr><th scope="col">Layer</th><th scope="col">First rank</th><th scope="col">First to second</th>
                <th scope="col">Second rank</th><th scope="col">Second to third</th><th scope="col">Third rank</th></tr>
            </thead>
            <tbody>
              {rows.map(({ layer, ranks: [first, second, third] }) => (
                <tr key={layer}>
                  <th scope="row">{layer}</th><td>{first ?? 'Absent'}</td><td>{rankChange(first, second)}</td>
                  <td>{second ?? 'Absent'}</td><td>{rankChange(second, third)}</td><td>{third ?? 'Absent'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {reason && <p>{reason}</p>}
      {rows && <button type="button" onClick={download}>Download area rank table</button>}
    </>
  )
}

const MAX_REPORT_BYTES = 8 * 1024 * 1024
const RANK_ROWS_PER_PAGE = 50

function rankReport(envelope) {
  if (envelope?.ok !== true || envelope.tool !== 'count-modules' ||
      typeof envelope.version !== 'string' || !envelope.version.trim()) return null
  const { drawing_id: drawing, revisions, layers } = envelope.result?.layer_rank_change ?? {}
  if (typeof drawing !== 'string' || !drawing.trim() || !Array.isArray(revisions) ||
      revisions.length !== 2 || !revisions.every(value => Number.isSafeInteger(value) && value > 0) ||
      !Array.isArray(layers)) return null
  const seen = new Set()
  for (const row of layers) {
    if (typeof row?.layer !== 'string' || !row.layer.trim() || seen.has(row.layer) ||
        ![row.before_rank, row.after_rank].every(rank => rank === null || (Number.isSafeInteger(rank) && rank > 0)) ||
        (row.before_rank === null && row.after_rank === null)) return null
    seen.add(row.layer)
  }
  return { drawing, revisions, layers, tool: envelope.tool, toolVersion: envelope.version }
}

function RankReport({ report, label }) {
  const [page, setPage] = useState(0)
  const lastPage = Math.max(0, Math.ceil(report.layers.length / RANK_ROWS_PER_PAGE) - 1)
  const currentPage = Math.min(page, lastPage)
  const start = currentPage * RANK_ROWS_PER_PAGE
  const end = Math.min(start + RANK_ROWS_PER_PAGE, report.layers.length)
  return <>
    <dl>
      <dt>Drawing in report</dt><dd>{report.drawing}</dd>
      <dt>Revisions in report</dt><dd>{report.revisions.join(' → ')}</dd>
      <dt>Tool version</dt><dd>{report.toolVersion}</dd>
    </dl>
    {report.layers.length ? <table>
      <caption>{label} layer ranks</caption>
      <thead><tr><th scope="col">Layer</th><th scope="col">Before</th><th scope="col">After</th></tr></thead>
      <tbody>{report.layers.slice(start, end).map(row => <tr key={row.layer}>
        <th scope="row">{row.layer}</th><td>{row.before_rank ?? 'Absent'}</td><td>{row.after_rank ?? 'Absent'}</td>
      </tr>)}</tbody>
    </table> : <p>No layer ranks were recorded.</p>}
    {report.layers.length > RANK_ROWS_PER_PAGE && <p>
      Layers {start + 1} to {end} of {report.layers.length}{' '}
      <button type="button" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>Previous {label.toLowerCase()} layers</button>{' '}
      <button type="button" disabled={currentPage === lastPage} onClick={() => setPage(currentPage + 1)}>Next {label.toLowerCase()} layers</button>
    </p>}
  </>
}

function ImportedReportComparison({ projectId, reports, scope }) {
  const [references, setReferences] = useState(null)
  const [cursor, setCursor] = useState(null)
  const [referenceId, setReferenceId] = useState('')
  const [imported, setImported] = useState(null)
  const [localId, setLocalId] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const request = useRef(null)
  const active = useRef(false)

  useEffect(() => {
    active.current = true
    const stop = () => {
      active.current = false
      request.current?.abort()
      request.current = null
    }
    const unsubscribe = subscribeBeforeLogout(() => {
      stop()
      setReferences(null)
      setImported(null)
      setCursor(null)
      setReferenceId('')
      setLocalId('')
      setBusy(false)
      setError('')
    })
    return () => { stop(); unsubscribe() }
  }, [])

  async function load(operation, failure) {
    if (request.current || !active.current || readActiveScope() !== scope) return
    const controller = new AbortController()
    request.current = controller
    const current = () => request.current === controller && readActiveScope() === scope
    setBusy(true)
    setError('')
    try {
      await operation({ signal: controller.signal, expectedScope: scope }, current)
    } catch {
      if (current()) setError(failure)
    } finally {
      if (request.current === controller) {
        if (current()) setBusy(false)
        request.current = null
      }
    }
  }

  function list(after) {
    void load(async (options, current) => {
      const result = await listProjectReferences(projectId, { ...options, cursor: after })
      if (!current()) return
      const rows = result.materials.filter(row => row.media_type === 'application/json')
      setReferences(previous => new Map([...(after ? previous : []), ...rows.map(row => [row.material_id, row])]))
      setCursor(result.next_cursor || null)
    }, 'Could not load the project’s reference files. Try again.')
  }

  function open() {
    const selected = references?.get(referenceId)
    if (!selected) return
    setImported(null)
    void load(async (options, current) => {
      const original = await readProjectReferenceOriginal(projectId, selected.material_id, options)
      if (!current()) return
      const report = original.size <= MAX_REPORT_BYTES ? rankReport(JSON.parse(await original.text())) : null
      if (!report) throw new Error('Unsupported comparison report')
      if (current()) setImported({ ...report, id: selected.material_id, name: selected.name })
    }, 'Could not open this file as a Count Modules layer rank comparison.')
  }

  const local = reports.get(localId)
  const clearLocal = Boolean(localId && !local)
  useEffect(() => {
    if (clearLocal) setLocalId(current => current === localId ? '' : current)
  }, [clearLocal, localId])
  const localRank = local && rankReport(local.job.result)
  return <section className="ground-job-detail" aria-label="Compare imported reports">
    <h3>Compare imported reports</h3>
    {!references && <button type="button" disabled={busy} onClick={() => list(null)}>Browse imported reports</button>}
    {references?.size > 0 && <p>
      <label>Imported JSON file{' '}
        <select style={{ maxWidth: '100%' }} disabled={busy} value={referenceId} onChange={event => setReferenceId(event.target.value)}>
          <option value="">Select a reference file</option>
          {[...references.values()].map(row => <option key={row.material_id} value={row.material_id}>{row.name}</option>)}
        </select>
      </label>{' '}
      <button type="button" disabled={busy || !referenceId} onClick={open}>Open imported report</button>
    </p>}
    {references?.size === 0 && !cursor && <p>No JSON reference files found.</p>}
    {cursor && <button type="button" disabled={busy} onClick={() => list(cursor)}>More reference files</button>}
    {busy && <p role="status">Loading reference file information…</p>}
    {error && <p role="alert">{error}</p>}
    {imported && <>
      <h4>Imported report ({imported.name})</h4>
      <p>Drawing and revisions are reported by the file.</p>
      <RankReport key={imported.id} label="Imported" report={imported} />
      {reports.size > 0 && <ReportPicker label="Local saved report" value={local?.id} reports={reports} onChange={setLocalId} />}
      {local && <div aria-live="polite">
        <h4>Local saved report</h4>
        <dl><dt>Project drawing version</dt><dd>{local.drawingVersion}</dd></dl>
        <p>{toolVersionResult(imported, local)}</p>
        {localRank ? <RankReport key={local.id} label="Local" report={localRank} /> : <p>No layer rank comparison was recorded in the local report.</p>}
      </div>}
    </>}
  </section>
}

export default function ReportComparison({ jobs, projectId }) {
  const scope = useSyncExternalStore(subscribeActiveScope, readActiveScope, () => null)
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
    const { tool, version: toolVersion, project_context: { drawing_id: drawing } } = job.result
    reports.set(id, {
      id,
      job,
      drawing: typeof drawing === 'string' && drawing.trim() ? drawing : null,
      drawingVersion: version,
      tool: typeof tool === 'string' && tool.trim() ? tool : null,
      toolVersion: typeof toolVersion === 'string' && toolVersion.trim() ? toolVersion : null,
    })
  }
  return (
    <>
      <LocalReportComparison key={JSON.stringify([scope, projectId])} reports={reports} />
      {scope && projectId && <ImportedReportComparison key={JSON.stringify([scope, projectId])}
        projectId={projectId} reports={reports} scope={scope} />}
      <SelectionExports reports={reports} projectId={projectId} key={projectId} />
    </>
  )
}

function LocalReportComparison({ reports }) {
  const [ids, setIds] = useState(['', '', ''])
  const selected = ids.map(id => reports.get(id))
  const [first, second, third] = selected
  const missing = ids.map((id, index) => id && !selected[index] ? id : '')
  const [missingFirst, missingSecond, missingThird] = missing
  useEffect(() => {
    if (!missingFirst && !missingSecond && !missingThird) return
    const removed = [missingFirst, missingSecond, missingThird]
    setIds(current => current.map((id, index) => id === removed[index] ? '' : id))
  }, [missingFirst, missingSecond, missingThird])

  if (reports.size < 2) return null
  return (
    <section className="ground-job-detail" aria-label="Compare saved reports">
      <h3>Compare saved reports</h3>
      {REPORT_LABELS.slice(0, reports.size).map((label, index) => (
        <ReportPicker key={label} label={label} value={selected[index]?.id} reports={reports}
          exclude={ids.filter((_, slot) => slot !== index)}
          onChange={value => setIds(current => current.includes(value) && value ? current :
            current.map((id, slot) => slot === index ? value : id))} />
      ))}
      <div aria-live="polite">
        {first && second && (third ? <SavedReportSeries reports={selected} /> : (
          <>
            <dl>
              <dt>Drawing version</dt>
              <dd>{first.drawingVersion === second.drawingVersion ? 'Same drawing version' : 'Different drawing versions'}</dd>
              <dt>Tool version</dt>
              <dd>{toolVersionResult(first, second)}</dd>
            </dl>
            <LayerOptionsComparison first={first} second={second} />
            <LayerAreaComparison first={first} second={second} />
            <SelectionComparison first={first} second={second} />
          </>
        ))}
      </div>
    </section>
  )
}
