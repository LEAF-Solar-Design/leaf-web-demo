import { useEffect, useRef, useState } from 'react'
import { buildConductorParams, conductorGaugeOptions, conductorRows, CONDUCTOR_REASONS } from './solarConductorModel.js'

export default function SolarConductorForm({
  row, drawingId, drawingVersion, projectId = null, readIntake, status = null,
  failureCode = null, onSubmit, onClose,
}) {
  const [graph, setGraph] = useState(null)
  const [selected, setSelected] = useState(new Set())
  const [gauge, setGauge] = useState('')
  const [refresh, setRefresh] = useState(0)
  const previousStatus = useRef(status)
  const lock = useRef(false)
  const reader = useRef(readIntake)
  reader.current = readIntake
  const scoped = projectId === null && typeof drawingId === 'string' && drawingId.length > 0
  const finished = status === 'finished'
  const options = conductorGaugeOptions(row)

  useEffect(() => {
    let current = true
    setGraph(null)
    setSelected(new Set())
    setGauge('')
    lock.current = false
    if (!scoped) return undefined
    Promise.resolve().then(() => reader.current(drawingId, drawingVersion))
      .then((value) => {
        if (current) setGraph({ drawingId, drawingVersion, result: conductorRows(value, drawingVersion) })
      })
      .catch(() => {
        if (current) setGraph({ drawingId, drawingVersion, result: { ok: false, reason: 'conductor_graph_unavailable' } })
      })
    return () => { current = false }
  }, [drawingId, drawingVersion, scoped, refresh])

  useEffect(() => {
    if (status === 'finished' && previousStatus.current !== 'finished') setRefresh((value) => value + 1)
    previousStatus.current = status
  }, [status])

  useEffect(() => {
    lock.current = false
  })

  const currentGraph = graph?.drawingId === drawingId && graph?.drawingVersion === drawingVersion ? graph.result : null
  const rows = scoped && currentGraph?.ok && options ? currentGraph.rows : []
  const built = !scoped ? { ok: false, reason: 'conductor_project_scope' }
    : !currentGraph?.ok || !options ? { ok: false, reason: 'conductor_graph_unavailable' }
      : buildConductorParams({ rev: currentGraph.rev, rows, selected, gauge, options })
  const busy = status === 'pending'
  const disabled = !scoped || !currentGraph?.ok || !options || busy

  function submit() {
    if (lock.current || status === 'pending' || !built.ok) return
    lock.current = true
    onSubmit(row, built.params)
  }

  return (
    <div>
      <p>This records conductor choices. It does not check ampacity or voltage drop.</p>
      <table aria-label="String conductors">
        <thead><tr><th scope="col">Select</th><th scope="col">Circuit tag</th><th scope="col">Current conductor</th></tr></thead>
        <tbody>{rows.map((item) => (
          <tr key={item.id}>
            <td><input type="checkbox" aria-label={`Select ${item.tag}`} checked={selected.has(item.id)} disabled={disabled}
              onChange={(event) => {
                const checked = event.target.checked
                setSelected((previous) => {
                  const next = new Set(previous)
                  if (checked) next.add(item.id)
                  else next.delete(item.id)
                  return next
                })
              }} /></td>
            <td>{item.tag}</td><td>{item.gauge.trim() ? item.gauge : 'Not set'}</td>
          </tr>
        ))}</tbody>
      </table>
      <button type="button" disabled={disabled} onClick={() => setSelected(new Set(rows.map((item) => item.id)))}>Select all</button>
      <button type="button" disabled={disabled} onClick={() => setSelected(new Set())}>Clear selection</button>
      <label>Conductor<select value={gauge} disabled={disabled} onChange={(event) => setGauge(event.target.value)}>
        <option value="">Choose a conductor</option>
        {(options || []).map((option) => <option key={option} value={option}>{option}</option>)}
      </select></label>
      {!built.ok && <p role="status">{CONDUCTOR_REASONS[built.reason]}</p>}
      {busy && <p role="status">This step is running. Wait for it to finish.</p>}
      {status === 'failed' && <p role="alert">{failureCode ? `This run failed: ${failureCode}.` : 'This run failed.'}{' '}{selected.size > 0 || gauge ? 'Your inputs are kept.' : 'Choose the strings and the conductor again.'}</p>}
      {finished && <p role="status">Conductor choices applied.</p>}
      <button type="button" disabled={busy || !built.ok} onClick={submit}>Apply to selected strings</button>
      {status === 'failed' && <button type="button" disabled={busy || !built.ok} onClick={submit}>Retry</button>}
      {onClose && <button type="button" onClick={onClose}>Cancel</button>}
    </div>
  )
}
