import { useEffect, useState } from 'react'
import { useSubmission, Feedback } from './ProjectSolveReview.jsx'
import { deriveProjectSolarGraphFormState } from './solarProjectGraphFormModel.js'

const current = (guard) => { try { return guard?.() === true } catch { return false } }
const code = (value) => typeof value === 'string' && /^[A-Za-z][A-Za-z0-9_]{0,127}$/.test(value) ? value : null

function hasSavedCoverage(frame) {
  const { committed_panel_count: committed, remaining_panel_count: remaining } = frame
  return Number.isSafeInteger(committed) && committed >= 0 &&
    Number.isSafeInteger(remaining) && remaining >= 0 &&
    committed <= frame.panel_count && remaining === frame.panel_count - committed
}

const solveHistoryLabels = new Map([
  ['queued', 'Queued'],
  ['running', 'Running'],
  ['failed', 'Failed'],
  ['cancelled', 'Cancelled'],
  ['succeeded', 'Proposal finished'],
])

function latestSolveLabel(history, available) {
  if (!available) return 'Unavailable'
  const label = solveHistoryLabels.get(history?.status)
  if (!label) return 'No matching solve'
  const reason = history.status === 'failed' && code(history.reason_code)
  return reason ? `${label} (${reason})` : label
}

export function ProjectSolarPreparedSolveForm(props) {
  const { projectClient, solarBinding, authorityKey, isCurrent, onClose, draft, onDraftChange } = props
  const [connection, setConnection] = useState(draft?.params?.grant_ref ?? '')
  const [frame, setFrame] = useState(draft?.context?.frame_ref ?? '')
  const [phase, setPhase] = useState(draft?.context?.phase ?? 'initial')
  const [loaded, setLoaded] = useState(null)
  const run = useSubmission(props, 'solar-solve-proposal')
  useEffect(() => {
    const controller = new AbortController()
    let alive = true
    setLoaded(null)
    const captured = projectClient.captureCatalogScope()
    const update = (value) => {
      if (!alive || !current(isCurrent) || !current(captured)) return
      setLoaded({ projectClient, solarBinding, authorityKey, isCurrent, value })
    }
    projectClient.readContext({ prepareSolve: true, signal: controller.signal }).then((response) => {
      const state = deriveProjectSolarGraphFormState({ binding: solarBinding, contextView: response.ok ? response.data : null })
      return state.mode === 'edit' ? response.data.solve_preparation : { available: false }
    }).catch(() => ({ available: false })).then(update)
    return () => { alive = false; controller.abort() }
  }, [projectClient, solarBinding, authorityKey, isCurrent])
  const preparation = loaded?.projectClient === projectClient && loaded.solarBinding === solarBinding &&
    loaded.authorityKey === authorityKey && loaded.isCurrent === isCurrent && current(isCurrent) ? loaded.value : null
  const frames = preparation?.available ? preparation.frames : []
  const showHistory = typeof preparation?.solve_history_available === 'boolean'
  const save = (nextConnection, nextFrame, nextPhase) => onDraftChange?.({
    params: { grant_ref: nextConnection }, context: { frame_ref: nextFrame, phase: nextPhase } })
  const ready = /^[a-zA-Z0-9_-]{1,64}$/.test(connection) && frames.some((item) => item.id === frame)
  return <form aria-label="Solve" onSubmit={(event) => {
    event.preventDefault()
    if (ready) void run.submit({ grant_ref: connection }, {
      prepareFromGraph: true, solveContext: { frame_ref: frame, phase } })
  }}>
    <h3>Solve</h3>
    <p>Use the saved panel groups and confirmed string length. Review the proposal before saving strings.</p>
    {!preparation && <p role="status">Reading saved panel groups.</p>}
    {preparation?.available === false && <p role="status">This saved drawing cannot be prepared for Solve.{code(preparation.reason) && <> ({code(preparation.reason)})</>}</p>}
    {frames.some(hasSavedCoverage) && <div className="table-scroll"><table>
      <caption>Saved panel group progress</caption>
      <thead><tr><th scope="col">Panel group</th><th scope="col">In saved strings</th><th scope="col">Remaining</th>
        {showHistory && <th scope="col">Latest solve</th>}</tr></thead>
      <tbody>{frames.map((item) => {
        const known = hasSavedCoverage(item)
        return <tr key={item.id}><th scope="row">{item.name}</th>
          <td>{known ? item.committed_panel_count : 'Unavailable'}</td>
          <td>{known ? item.remaining_panel_count : 'Unavailable'}</td>
          {showHistory && <td>{latestSolveLabel(item.latest_solve, preparation.solve_history_available)}</td>}</tr>
      })}</tbody>
    </table></div>}
    <fieldset disabled={run.locked}>
      <label>Solver connection<input value={connection} autoComplete="off" onChange={(event) => {
        setConnection(event.target.value); save(event.target.value, frame, phase)
      }} /></label>
      <p>Use an existing connection verified for this project. This form does not create a connection.</p>
      <label>Panel group<select aria-label="Panel group" value={frame} onChange={(event) => {
        setFrame(event.target.value); save(connection, event.target.value, phase)
      }}><option value="">Choose a panel group</option>{frames.map((item) => <option key={item.id} value={item.id}>
        {item.name} ({item.panel_count} panels)
      </option>)}</select></label>
      <label>Solve stage<select aria-label="Solve stage" value={phase} onChange={(event) => {
        setPhase(event.target.value); save(connection, frame, event.target.value)
      }}><option value="initial">Initial search</option><option value="background">Background search</option></select></label>
      <button type="submit" disabled={!ready}>Run solve</button>
    </fieldset>
    <Feedback value={run.feedback} />
    <button type="button" onClick={onClose}>Close</button>
  </form>
}
