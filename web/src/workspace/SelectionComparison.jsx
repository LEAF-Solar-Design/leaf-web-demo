import { useId, useRef, useState } from 'react'

const countTool = 'count-modules'
const ruleSchema = 'leaf.count-rule.v1'
const selectionSchema = 'leaf.saved-count-selection.v1'
const isDigest = value => typeof value === 'string' && value.length === 64 && /^[a-f0-9]+$/.test(value)
const isId = value => typeof value === 'string' && Boolean(value.trim())
const isCount = value => Number.isSafeInteger(value) && value >= 0

function savedSelection(report) {
  const { job, id, drawingVersion } = report
  const { project_context: context, rule_reference: rule, result, overlay } = job.result
  if (report.tool !== countTool || job.tool_name !== countTool || job.kind !== 'run' ||
      job.project_id !== context?.project_id || !isId(job.org_id) ||
      job.org_id !== context.organization_id || !isId(context.drawing_id) ||
      result?.selection_required !== false) return null

  return selectionFromSnapshot({
    job_id: id, project_id: job.project_id, organization_id: job.org_id, drawing_id: context.drawing_id,
    drawing_revision: { input_version_id: drawingVersion, intake_sha256: context.intake_sha256 },
    rule, selected_sets: result.selected_sets, highlight_handles: overlay?.highlight_handles, count: result.count,
  })
}

function selectionFromSnapshot(value) {
  if (!value || typeof value !== 'object') return null
  const { job_id, project_id, organization_id, drawing_id, drawing_revision, rule, selected_sets,
    highlight_handles, count } = value
  if (![job_id, project_id, organization_id, drawing_id, drawing_revision?.input_version_id].every(isId) ||
      !isDigest(drawing_revision.intake_sha256) || rule?.schema !== ruleSchema ||
      !isDigest(rule.entry_sha256) || !isDigest(rule.geometry_sha256) || !isCount(count) ||
      !Array.isArray(selected_sets) || !selected_sets.length || !Array.isArray(highlight_handles)) return null

  const sets = new Map()
  for (const row of selected_sets) {
    if (!isId(row?.set_id) || !isCount(row.count) || sets.has(row.set_id)) return null
    sets.set(row.set_id, row.count)
  }
  const handles = new Set(highlight_handles)
  if (handles.size !== highlight_handles.length || ![...handles].every(isId)) return null

  return {
    jobId: job_id,
    scope: { project_id, organization_id, drawing_id },
    drawingRevision: { input_version_id: drawing_revision.input_version_id, intake_sha256: drawing_revision.intake_sha256 },
    rule: { schema: ruleSchema, entry_sha256: rule.entry_sha256, geometry_sha256: rule.geometry_sha256 },
    sets, handles, count,
  }
}

function snapshot(selection) {
  return {
    job_id: selection.jobId,
    ...selection.scope,
    drawing_revision: selection.drawingRevision,
    rule: selection.rule,
    selected_sets: [...selection.sets].map(([set_id, count]) => ({ set_id, count })),
    highlight_handles: [...selection.handles],
    count: selection.count,
  }
}

function selectionChanges(first, second) {
  const changes = []
  for (const id of new Set([...first.sets.keys(), ...second.sets.keys()])) {
    const before = first.sets.get(id) ?? null
    const after = second.sets.get(id) ?? null
    if (before !== after) changes.push({ kind: 'selected_set', id, before, after })
  }
  for (const id of new Set([...first.handles, ...second.handles])) {
    const before = first.handles.has(id)
    const after = second.handles.has(id)
    if (before !== after) changes.push({ kind: 'highlighted_handle', id, before, after })
  }
  if (first.count !== second.count) {
    changes.push({ kind: 'total_count', id: null, before: first.count, after: second.count })
  }
  return changes.map(change => ({ ...change, before_selection: first.jobId, after_selection: second.jobId }))
}

function downloadJson(value, filename) {
  const blob = new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  try {
    link.click()
  } finally {
    setTimeout(() => URL.revokeObjectURL(url), 0)
  }
}

function Reference({ id, selection, label }) {
  return (
    <dl id={id} aria-label={label} tabIndex={-1} style={{ overflowWrap: 'anywhere' }}>
      <dt>{label} drawing revision</dt>
      <dd>{selection.drawingRevision.input_version_id}</dd>
      <dt>Drawing intake SHA-256</dt>
      <dd>{selection.drawingRevision.intake_sha256}</dd>
      <dt>Count rule SHA-256</dt>
      <dd>{selection.rule.entry_sha256}</dd>
      <dt>Geometry dependency SHA-256</dt>
      <dd>{selection.rule.geometry_sha256}</dd>
    </dl>
  )
}

const labels = {
  selected_set: 'Selected set',
  highlighted_handle: 'Highlighted handle',
  total_count: 'Total count',
}

function cellValue(value) {
  if (typeof value === 'number') return value
  return value === true ? 'Present' : 'Absent'
}

export default function SelectionComparison({ first, second }) {
  if (first.tool !== countTool || second.tool !== countTool) return null
  const a = savedSelection(first)
  const b = savedSelection(second)
  if (!a && !b) return <p>Both reports need a saved selection with rule and drawing revision references.</p>
  if (!a) return <p>The first report needs a saved selection with rule and drawing revision references.</p>
  if (!b) return <p>The second report needs a saved selection with rule and drawing revision references.</p>
  return <SelectionChanges a={a} b={b} />
}

function SelectionChanges({ a, b, fromFiles = false }) {
  const referenceId = useId()
  if (a.scope.project_id !== b.scope.project_id || a.scope.organization_id !== b.scope.organization_id ||
      a.scope.drawing_id !== b.scope.drawing_id) {
    return <p>Selection comparison requires two reports for the same drawing.</p>
  }
  if (a.jobId === b.jobId) return <p>Choose exports from two different saved selections.</p>
  const revisionChanged = a.drawingRevision.input_version_id !== b.drawingRevision.input_version_id
  if (!revisionChanged && a.drawingRevision.intake_sha256 !== b.drawingRevision.intake_sha256) {
    return <p>Selection comparison found conflicting intake fingerprints for the same drawing revision.</p>
  }
  const ruleChanged = a.rule.entry_sha256 !== b.rule.entry_sha256 || a.rule.geometry_sha256 !== b.rule.geometry_sha256
  const changes = selectionChanges(a, b)
  const firstRef = `${referenceId}-first`
  const secondRef = `${referenceId}-second`
  const comparison = {
    schema: 'leaf.saved-selection-comparison.v1',
    selections: [snapshot(a), snapshot(b)],
    ...(fromFiles && { source: 'unverified_files' }),
    rule_changed: ruleChanged,
    drawing_revision_changed: revisionChanged,
    changes,
  }
  return (
    <section aria-label={fromFiles ? 'Exported selection comparison' : 'Saved selection comparison'}>
      <p>{ruleChanged ? 'Count rule changed' : 'Same count rule'}</p>
      {fromFiles && <p>{revisionChanged ? 'Drawing revision changed' : 'Same drawing revision'}</p>}
      {changes.length ? (
        <table style={{ overflowWrap: 'anywhere' }}>
          <caption>Saved selection changes</caption>
          <thead>
            <tr><th scope="col">Change</th><th scope="col">First report</th><th scope="col">Second report</th></tr>
          </thead>
          <tbody>
            {changes.map(({ kind, id, before, after }) => (
              <tr key={JSON.stringify([kind, id])}>
                <th scope="row">{labels[kind]}{id && <> <code>{id}</code></>}</th>
                <td>{cellValue(before)} <a href={`#${firstRef}`} aria-label="First rule and drawing revision">Reference</a></td>
                <td>{cellValue(after)} <a href={`#${secondRef}`} aria-label="Second rule and drawing revision">Reference</a></td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : <p>No saved selection changes</p>}
      <Reference id={firstRef} selection={a} label="First report" />
      <Reference id={secondRef} selection={b} label="Second report" />
      <button type="button" onClick={() => downloadJson(comparison, 'selection-comparison.json')}>
        Download selection comparison
      </button>
    </section>
  )
}

function readText(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result)
    reader.onerror = () => reject(reader.error)
    reader.readAsText(file)
  })
}

function SelectionFile({ label, projectId, onSelection }) {
  const request = useRef(0)
  const [feedback, setFeedback] = useState('')
  async function load(file) {
    const current = ++request.current
    onSelection(null)
    setFeedback('')
    if (!file) return
    try {
      const value = JSON.parse(await readText(file))
      if (current !== request.current) return
      const selection = value?.schema === selectionSchema && selectionFromSnapshot(value)
      if (!selection || selection.scope.project_id !== projectId) {
        setFeedback('Choose a Count selection export for this project.')
        return
      }
      onSelection(selection)
    } catch {
      if (current === request.current) setFeedback('This file could not be read as a Count selection export.')
    }
  }
  return (
    <div>
      <label>{label}{' '}
        <input type="file" accept=".json,application/json" onChange={event => load(event.target.files[0])} />
      </label>
      {feedback && <p role="status">{feedback}</p>}
    </div>
  )
}

export function SelectionExports({ reports, projectId }) {
  const [exportId, setExportId] = useState('')
  const [first, setFirst] = useState(null)
  const [second, setSecond] = useState(null)
  const selections = [...reports.values()].filter(report => report.tool === countTool)
    .map(savedSelection).filter(Boolean)
  const selected = selections.find(selection => selection.jobId === exportId)
  return (
    <details>
      <summary>Export or compare Count selections</summary>
      {selections.length > 0 && (
        <p>
          <label>Selection to export{' '}
            <select value={selected?.jobId || ''} onChange={event => setExportId(event.target.value)}>
              <option value="">Choose a saved selection</option>
              {selections.map(({ jobId, drawingRevision }) => (
                <option key={jobId} value={jobId}>{drawingRevision.input_version_id} · {jobId}</option>
              ))}
            </select>
          </label>{' '}
          <button type="button" disabled={!selected}
            onClick={() => downloadJson({ schema: selectionSchema, ...snapshot(selected) }, 'count-selection.json')}>
            Download Count selection
          </button>
        </p>
      )}
      <p>Files are compared locally. Their rule and drawing references come from the files and are unverified.</p>
      <SelectionFile label="First selection file" projectId={projectId} onSelection={setFirst} />
      <SelectionFile label="Second selection file" projectId={projectId} onSelection={setSecond} />
      <div aria-live="polite">{first && second && <SelectionChanges a={first} b={second} fromFiles />}</div>
    </details>
  )
}
