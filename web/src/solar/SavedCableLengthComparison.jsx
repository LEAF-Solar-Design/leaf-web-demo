import { useLayoutEffect, useRef, useState } from 'react'
import { openProject } from '../api.js'
import { createSolarDrawingBinding } from './solarDrawingBinding.js'

function savedRevisions(workspace, binding) {
  if (workspace?.project?.org_id !== binding.organization_id ||
      workspace.project.project_id !== binding.project_id || !Array.isArray(workspace.drawing_versions)) {
    throw new Error('Project revisions unavailable')
  }
  const revisions = []
  const ids = new Set()
  const numbers = new Set()
  for (const row of workspace.drawing_versions) {
    if (row?.drawing_id !== binding.drawing_id) continue
    const revision = createSolarDrawingBinding({ ...binding, input_version_id: row.version_id })
    if (row.org_id !== binding.organization_id || row.project_id !== binding.project_id ||
        !Number.isSafeInteger(row.seq) || row.seq < 1 || ids.has(revision.input_version_id) || numbers.has(row.seq)) {
      throw new Error('Invalid drawing revision')
    }
    ids.add(revision.input_version_id)
    numbers.add(row.seq)
    revisions.push({ id: revision.input_version_id, seq: row.seq })
  }
  if (!ids.has(binding.input_version_id)) throw new Error('Active drawing revision unavailable')
  return revisions.sort((a, b) => a.seq - b.seq)
}

function CableChanges({ page, compare, loading }) {
  if (page.total_changes === 0) return <p role="status">No cable lengths changed.</p>
  return <>
    <p role="status">{page.offset + 1}–{page.offset + page.changes.length} of {page.total_changes} cable length changes</p>
    <div role="region" aria-label="Cable length changes" tabIndex={0} style={{ overflowX: 'auto' }}>
      <table aria-label="Saved cable length comparison">
        <thead><tr>{['Cable', 'Type', 'Before (ft)', 'After (ft)', 'Change'].map(label =>
          <th key={label} scope="col">{label}</th>)}</tr></thead>
        <tbody>{page.changes.map(({ route_id: id, before, after, change }) => {
          const cable = after || before
          const oldLabel = before && after && before.schedule_label !== after.schedule_label ? before.schedule_label : null
          return <tr key={id}>
            <th scope="row">{oldLabel ? `${oldLabel} → ${cable.schedule_label}` : cable.schedule_label}</th>
            <td>{cable.route_kind}</td><td>{before?.length_ft ?? '—'}</td><td>{after?.length_ft ?? '—'}</td><td>{change}</td>
          </tr>
        })}</tbody>
      </table>
    </div>
    {page.offset > 0 && <button type="button" disabled={loading}
      onClick={() => compare(page.offset - 250)}>Previous page</button>}
    {page.next_offset !== null && <button type="button" disabled={loading}
      onClick={() => compare(page.next_offset)}>Next page</button>}
  </>
}

export default function SavedCableLengthComparison({ binding, projectClient, captureScope, busy }) {
  const pending = useRef(null)
  const [state, setState] = useState(null)
  useLayoutEffect(() => () => pending.current?.abort(), [])
  const view = state?.current() ? state : null

  async function run(current, loadingView, action, error) {
    if (!current()) return
    pending.current?.abort()
    const controller = new AbortController()
    pending.current = controller
    setState({ ...loadingView, current, loading: true, error: null })
    const active = () => !controller.signal.aborted && current()
    try {
      const result = await action(controller.signal)
      if (active()) setState({ ...loadingView, ...result, current, loading: false, error: null })
    } catch {
      if (active()) setState({ ...loadingView, current, loading: false, error })
    } finally {
      if (pending.current === controller) pending.current = null
    }
  }

  function load() {
    run(captureScope(), {}, async () => {
      const workspace = await openProject(binding.project_id, binding.organization_id)
      const revisions = savedRevisions(workspace, binding)
      const index = revisions.findIndex(row => row.id === binding.input_version_id)
      return { revisions, before: revisions[Math.max(0, index - 1)].id, after: binding.input_version_id }
    }, 'Saved revisions could not be loaded. Try again.')
  }

  function compare(offset) {
    if (view?.loading || busy || !view?.revisions) return
    run(view.current, view, async (signal) => {
      const outcome = await projectClient.compareSavedCableLengths({
        beforeVersionId: view.before, afterVersionId: view.after, offset, signal,
      })
      if (!outcome.ok) throw new Error('Cable length comparison refused')
      return { page: outcome.data.result }
    }, 'Cable lengths could not be compared. Select revisions with valid saved cable layouts and try again.')
  }

  function select(event, side) {
    const id = event.target.value
    if (!view.current() || !view.revisions.some(row => row.id === id)) return
    setState({ ...view, [side]: id, page: null, error: null })
  }

  const disabled = busy || !!view?.loading
  return <section aria-label="Compare saved cable lengths">
    {!view?.revisions && <button type="button" disabled={disabled} onClick={load}>Compare saved cable lengths</button>}
    {view?.revisions && <>
      <label>Before revision <select disabled={disabled} value={view.before} onChange={event => select(event, 'before')}>
        {view.revisions.map(row => <option value={row.id} key={row.id}>Revision {row.seq}</option>)}
      </select></label>
      <label>After revision <select disabled={disabled} value={view.after} onChange={event => select(event, 'after')}>
        {view.revisions.map(row => <option value={row.id} key={row.id}>Revision {row.seq}</option>)}
      </select></label>
      <button type="button" disabled={disabled} onClick={() => compare(0)}>Compare lengths</button>
      {view.page && <CableChanges page={view.page} compare={compare} loading={disabled} />}
    </>}
    {view?.loading && <p role="status">{view.revisions ? 'Comparing cable lengths…' : 'Loading saved revisions…'}</p>}
    {view?.error && <p role="status">{view.error}</p>}
  </section>
}
