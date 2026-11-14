import { useEffect, useState } from 'react'
import { useSubmission, Feedback } from './ProjectSolveReview.jsx'
import { deriveProjectSolarGraphFormState } from './solarProjectGraphFormModel.js'

const current = (guard) => { try { return guard?.() === true } catch { return false } }
const code = (value) => typeof value === 'string' && /^[A-Za-z][A-Za-z0-9_]{0,127}$/.test(value) ? value : null

export function ProjectSolarPreparedSolveForm(props) {
  const { projectClient, solarBinding, authorityKey, isCurrent, onClose, draft, onDraftChange } = props
  const [connection, setConnection] = useState(draft?.params?.grant_ref ?? '')
  const [frame, setFrame] = useState(draft?.context?.frame_ref ?? '')
  const [phase, setPhase] = useState(draft?.context?.phase ?? 'initial')
  const [preparation, setPreparation] = useState(null)
  const run = useSubmission(props, 'solar-solve-proposal')
  useEffect(() => {
    const controller = new AbortController()
    let alive = true
    setPreparation(null)
    const captured = projectClient.captureCatalogScope()
    projectClient.readContext({ prepareSolve: true, signal: controller.signal }).then((response) => {
      if (!alive || !current(isCurrent) || !current(captured)) return
      const state = deriveProjectSolarGraphFormState({ binding: solarBinding, contextView: response.ok ? response.data : null })
      setPreparation(state.mode === 'edit' ? response.data.solve_preparation : { available: false })
    }).catch(() => { if (alive) setPreparation({ available: false }) })
    return () => { alive = false; controller.abort() }
  }, [projectClient, solarBinding, authorityKey, isCurrent])
  const frames = preparation?.available ? preparation.frames : []
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
    <fieldset disabled={run.locked}>
      <label>Solver connection<input value={connection} autoComplete="off" onChange={(event) => {
        setConnection(event.target.value); save(event.target.value, frame, phase)
      }} /></label>
      <p>Use an existing connection verified for this project. This form does not create a connection.</p>
      <label>Panel group<select value={frame} onChange={(event) => {
        setFrame(event.target.value); save(connection, event.target.value, phase)
      }}><option value="">Choose a panel group</option>{frames.map((item) => <option key={item.id} value={item.id}>
        {item.name} ({item.panel_count} panels)
      </option>)}</select></label>
      <label>Solve stage<select value={phase} onChange={(event) => {
        setPhase(event.target.value); save(connection, frame, event.target.value)
      }}><option value="initial">Initial search</option><option value="background">Background search</option></select></label>
      <button type="submit" disabled={!ready}>Run solve</button>
    </fieldset>
    <Feedback value={run.feedback} />
    <button type="button" onClick={onClose}>Close</button>
  </form>
}
