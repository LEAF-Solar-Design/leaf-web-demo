import { useEffect, useRef, useState } from 'react'
import { solarSizingRunSentence } from './solarSizingRunReasons.js'
import { buildSizingParams, MODULE_PARAMETER_KEYS, sizingGraph, sizingTargets, SOLAR_SIZING_REASONS } from './solarSizingModel.js'

export default function SolarSizingForm({
  row, drawingId, drawingVersion, projectId = null, readIntake, status = null,
  failureCode = null, onSubmit, onClose,
}) {
  const [loaded, setLoaded] = useState(null)
  const [mode, setMode] = useState('manual-global')
  const [draft, setDraft] = useState({})
  const [refresh, setRefresh] = useState(0)
  const previousStatus = useRef(status)
  const reader = useRef(readIntake)
  reader.current = readIntake
  const lock = useRef(false)
  const scoped = projectId === null && typeof drawingId === 'string' && drawingId.length > 0

  useEffect(() => {
    let current = true
    setLoaded(null)
    if (!scoped) return undefined
    Promise.resolve().then(() => reader.current(drawingId, drawingVersion))
      .then((view) => {
        if (current) setLoaded({ drawingId, drawingVersion, graph: sizingGraph(view, drawingVersion),
          savedZip: view?.intake?.solar_design_graph?.project?.zip_code })
      })
      .catch(() => { if (current) setLoaded(null) })
    return () => { current = false }
  }, [drawingId, drawingVersion, scoped, refresh])

  useEffect(() => {
    if (status === 'finished' && previousStatus.current !== 'finished') setRefresh((value) => value + 1)
    previousStatus.current = status
  }, [status])

  useEffect(() => { lock.current = false })

  const graph = loaded?.drawingId === drawingId && loaded?.drawingVersion === drawingVersion ? loaded.graph : null
  const built = !scoped ? { ok: false, reason: 'sizing_project_scope' }
    : buildSizingParams({ graph, mode, draft })
  const targets = scoped && graph?.ok ? sizingTargets(graph, mode) : null
  const busy = status === 'pending'
  const invalid = new Set(built.invalid || [])
  const change = (key, value) => setDraft((previous) => ({ ...previous, [key]: value }))
  function input(key, label) {
    return <label key={key}>{label}<input value={draft[key] ?? ''} disabled={busy}
      aria-invalid={invalid.has(key) || undefined} onChange={(event) => change(key, event.target.value)} /></label>
  }
  function boolean(key, label) {
    return <label>{label}<select value={draft[key] === undefined ? '' : String(draft[key])} disabled={busy}
      aria-invalid={invalid.has(key) || undefined}
      onChange={(event) => change(key, event.target.value === '' ? undefined : event.target.value === 'true')}>
      <option value="">Choose</option><option value="true">Yes</option><option value="false">No</option>
    </select></label>
  }
  function submit() {
    if (lock.current || busy || !built.ok) return
    lock.current = true
    onSubmit(row, built.params)
  }

  return <div>
    <label>Scope<select value={mode} disabled={busy} onChange={(event) => setMode(event.target.value)}>
      <option value="manual-global">Saved global length</option>
      <option value="global">Global</option><option value="zones">Zones</option>
    </select></label>
    {scoped && graph?.ok && <p>Saved project ZIP: {loaded.savedZip}</p>}
    {mode === 'manual-global' && <>
      {graph?.ok && <p>Saved global string length: {graph.savedLength}</p>}
      <p>Change the length in Solar settings.</p>
      <p>Manual confirmation does not calculate voltage limits or module power.</p>
    </>}
    {mode !== 'manual-global' && <>
    {targets?.ok && targets.targets.map((id, index) => <div key={id}>
      <p>{id}</p>
      {mode === 'zones' && <p>Module: {graph.zones[index].module_model}; Inverter: {graph.zones[index].inverter_model_a}</p>}
      <p>{Object.hasOwn(graph.power, id) && graph.power[id] != null ? `Confirmed module power: ${graph.power[id]} W` : 'Confirmed module power is unavailable. Re-size strings.'}</p>
    </div>)}
    {mode === 'global' && <>{input('module_name', 'Module')}{input('full_inverter_name', 'Inverter')}</>}
    {boolean('bifacial', 'Bifacial')}
    {input('bifacial_coefficient', 'Bifacial coefficient')}
    <label>Racking<select value={draft.racking_type ?? ''} disabled={busy}
      aria-invalid={invalid.has('racking_type') || undefined} onChange={(event) => change('racking_type', event.target.value)}>
      <option value="">Choose</option><option value="fixed_tilt">Fixed tilt</option><option value="single_axis">Single axis</option>
    </select></label>
    {input('surface_tilt', 'Surface tilt')}{input('surface_azimuth', 'Surface azimuth')}{input('albedo', 'Albedo')}
    {draft.racking_type === 'single_axis' && <>
      {input('axis_tilt', 'Axis tilt')}{input('axis_azimuth', 'Axis azimuth')}
      {input('max_angle', 'Max angle')}{input('gcr', 'Ground coverage ratio')}{boolean('backtrack', 'Backtrack')}
    </>}
    {input('max_voltage', 'Maximum voltage')}{input('thermal_model_type', 'Thermal model')}
    {boolean('open_circuit_rise', 'Open circuit rise')}{input('grant_ref', 'Grant reference')}
    <label><input type="checkbox" checked={draft.use_module_parameters === true} disabled={busy}
      onChange={(event) => change('use_module_parameters', event.target.checked)} />Use module parameters</label>
    {draft.use_module_parameters && MODULE_PARAMETER_KEYS.map((key) => input(key, key))}
    </>}
    {!built.ok && <p role="status">{SOLAR_SIZING_REASONS[built.reason]}</p>}
    {busy && <p role="status">This step is running. Confirm or wait for it to finish.</p>}
    {status === 'failed' && <p role="alert">{solarSizingRunSentence(failureCode)}</p>}
    {status === 'finished' && <p role="status">String sizing applied.</p>}
    <button type="button" disabled={busy || !built.ok} onClick={submit}>Review &amp; run</button>
    {status === 'failed' && <button type="button" disabled={busy || !built.ok} onClick={submit}>Retry</button>}
    {onClose && <button type="button" onClick={onClose}>Cancel</button>}
  </div>
}
