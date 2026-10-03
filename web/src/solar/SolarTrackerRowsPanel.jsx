import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { buildTrackerRowsRequest, validateTrackerRowsResult } from './solarTrackerRowsModel.js'
import { TRACKER_ROWS_REASONS } from './solarTrackerRowsReasons.js'
import { TRACKER_ROWS_CLIENT_REASONS } from './solarTrackerRowsClient.js'
import { validateTerrainView } from './solarTerrainModel.js'

// R3's project-id rule (solarTrackerRowsClient.js isProjectId): 1 to 100 code points, at most 200 UTF-16 units,
// URI-encodable (a lone surrogate is not). A graph whose project id fails it cannot read back a publication.
function readableProject(value) {
  if (typeof value !== 'string' || value.length === 0 || value.length > 200 || [...value].length > 100) return false
  try { encodeURIComponent(value) } catch { return false }
  return true
}

export const TRACKER_ROWS_PANEL_REASONS = Object.freeze({
  loading: 'Reading the current physical state.',
  refreshing: 'Refreshing the current physical state.',
  units_loading: 'Reading the drawing units.',
  units_unreadable: 'The drawing units could not be read, so publishing stays off.',
  no_head: 'No physical state has been published for this drawing.',
  ready: 'Review the row coordinates, slot counts and module power before publishing.',
  pending: 'Publishing the manual tracker rows.',
  published: 'Manual tracker rows were published for this drawing.',
  retry: 'These manual tracker rows were already published.',
  unknown: 'The publication outcome is unknown, so refresh before retrying the original request.',
  retry_ready: 'The physical state was refreshed, so you may retry the original request.',
  read_failed: 'The current physical state could not be read, so publishing stays off.',
  run_pending: 'A run is in progress, so wait for it to finish.',
  landxml_pending: 'The LandXML file is being imported, so wait for it to finish.',
  terrain_pending: 'A terrain operation is in progress, so wait for it to finish.',
  combiner_pending: 'The combiner intake is being imported, so wait for it to finish.',
  drawing_refresh: 'The drawing must finish refreshing before tracker rows can be published.',
  tracker_pending: 'Tracker rows are being published, so wait for the request to finish.',
  unrecognized: 'The tracker row request stopped.',
  manual_scope: 'Publishing manual rows does not convert them into panel groups.',
})

const S = TRACKER_ROWS_PANEL_REASONS
const UNCERTAIN = new Set(['TRACKER_ROWS_CLIENT_TIMEOUT', 'TRACKER_ROWS_CLIENT_NETWORK',
  'TRACKER_ROWS_CLIENT_ABORTED', 'TRACKER_ROWS_CLIENT_RESPONSE_INVALID'])
const TERMINAL = new Set(['TRACKER_ROWS_ALREADY_EXISTS', 'TRACKER_ROWS_GRAPH_CONVERTED'])
const FIELDS = [
  ['Axis start X', 'axis_start.0'], ['Axis start Y', 'axis_start.1'],
  ['Axis end X', 'axis_end.0'], ['Axis end Y', 'axis_end.1'],
  ['Cross axis width', 'cross_axis_width_du'], ['Slots', 'slots'],
]
const blankRow = () => ({ axis_start: ['', ''], axis_end: ['', ''], cross_axis_width_du: '', slots: '' })
function sentence(code) {
  return Object.hasOwn(TRACKER_ROWS_REASONS, code) ? TRACKER_ROWS_REASONS[code]
    : Object.hasOwn(TRACKER_ROWS_CLIENT_REASONS, code) ? TRACKER_ROWS_CLIENT_REASONS[code] : S.unrecognized
}
function failureCode(result) {
  try {
    if (result?.ok === false && typeof result.code === 'string') return result.code
  } catch { /* An unreadable injected answer is uncertain. */ }
  return 'TRACKER_ROWS_CLIENT_RESPONSE_INVALID'
}

export default function SolarTrackerRowsPanel(props) {
  return <PanelForScope key={JSON.stringify([props.drawingId ?? null, props.projectId ?? null])} {...props} />
}

function PanelForScope({ drawingId, projectId = null, drawingVersion = null, client, terrainClient,
  readIntake, headSignal, active = true, checkoutHeld, busy = false, blockedReason = null,
  onPhysicalHeadChanged, onClose }) {
  const usable = !!drawingId && typeof client?.createTrackerRows === 'function'
    && typeof terrainClient?.getTerrain === 'function' && typeof readIntake === 'function'
  const [rows, setRows] = useState(() => [blankRow()])
  const [power, setPower] = useState('')
  const [context, setContext] = useState(null)
  const [confirmed, setConfirmed] = useState(false)
  const [reading, setReading] = useState(null)
  const [readError, setReadError] = useState(null)
  const [refusal, setRefusal] = useState(null)
  const [pending, setPending] = useState(false)
  const [receipt, setReceipt] = useState(null)
  const [uncertain, setUncertain] = useState(null)
  const [recovery, setRecovery] = useState(null)
  const alive = useRef(false)
  const visible = useRef(active)
  visible.current = active
  const read = useRef(null)
  const sequence = useRef(0)
  const lock = useRef(null)
  const retained = useRef(null)
  const heading = useRef(null)
  const resultHeading = useRef(null)
  const action = useRef(null)
  const changed = useRef(onPhysicalHeadChanged)
  changed.current = onPhysicalHeadChanged
  const baseId = useId()

  const stopRead = useCallback(() => {
    sequence.current += 1
    read.current?.controller.abort()
    clearTimeout(read.current?.timer)
    read.current = null
  }, [])

  useLayoutEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
      stopRead()
      lock.current?.controller.abort()
      lock.current = null
    }
  }, [stopRead])

  const load = useCallback(async (refreshing = false) => {
    if (!usable || !visible.current) return
    stopRead()
    const seq = sequence.current
    const controller = new AbortController()
    const token = { controller, timer: null }
    read.current = token
    const current = () => alive.current && seq === sequence.current && !controller.signal.aborted
    setConfirmed(false)
    setReading(refreshing ? 'refreshing' : 'loading')
    setReadError(null)
    let next = null
    let error = 'read_failed'
    try {
      const result = await terrainClient.getTerrain({ drawingId, projectId, signal: controller.signal })
      if (!current()) return
      const value = result?.ok === true ? validateTerrainView(result.value, { drawingId, projectId }) : null
      if (value) {
        if (value.stored) {
          next = { head: value.head, units: value.terrain.frame.drawing_units }
        } else {
          setReading('units-loading')
          error = 'units_unreadable'
          // Intake has no cancellation contract. The deadline settles this generation only.
          const intake = await Promise.race([
            Promise.resolve().then(() => readIntake(drawingId, 'head')),
            new Promise((_, reject) => { token.timer = setTimeout(() => reject(new Error()), 120000) }),
          ])
          clearTimeout(token.timer)
          if (!current()) return
          const graph = intake?.intake?.solar_design_graph
          if (!intake?.intake || typeof intake.intake !== 'object') error = 'units_unreadable'
          else if (!graph) error = 'TRACKER_ROWS_GRAPH_REQUIRED'
          else if (typeof graph.project?.id !== 'string' || !graph.project.id) error = 'units_unreadable'
          else if (!readableProject(graph.project.id)) error = 'TRACKER_ROWS_PROJECT_ID_INVALID'
          else if (projectId !== null && graph.project.id !== projectId) error = 'TRACKER_ROWS_PROJECT_MISMATCH'
          else if (!Number.isInteger(intake.version) || intake.version < 1 || intake.version !== intake.head) error = 'units_unreadable'
          else if (drawingVersion !== null && intake.version !== drawingVersion) error = 'drawing_refresh'
          else {
            const units = graph.project.units
            if (!['m', 'ft'].includes(units?.drawing_units)) error = 'TRACKER_ROWS_UNITS_UNSUPPORTED'
            else if (units.meters_per_unit !== (units.drawing_units === 'm' ? 1 : 0.3048)) error = 'units_unreadable'
            else next = { head: null, units: units.drawing_units }
          }
        }
      }
    } catch { /* Closed read refusal; never render transport prose. */ }
    if (!current()) return
    clearTimeout(token.timer)
    read.current = null
    setReading(null)
    if (!next) {
      setReadError(error)
      return
    }
    setContext((previous) => previous?.head?.index > next.head?.index ? previous : next)
    setConfirmed(true)
    const original = retained.current
    if (original) {
      const base = original.request.expected_head
      if (next.units !== original.drawingUnits) {
        setRecovery('superseded')
        setRefusal('TRACKER_ROWS_UNITS_MISMATCH')
      } else if ((next.head?.state.artifact_id ?? null) === base || (next.head !== null && next.head.parent === base)) {
        setRecovery('retry-ready')
      } else {
        setRecovery('superseded')
        setRefusal('TRACKER_ROWS_STALE_HEAD')
      }
    }
  }, [usable, stopRead, terrainClient, drawingId, projectId, drawingVersion, readIntake])

  useEffect(() => {
    setConfirmed(false)
    if (active) {
      load()
    } else {
      stopRead()
      setReading(null)
    }
    return stopRead
  }, [active, load, headSignal, stopRead])
  useLayoutEffect(() => { if (active) heading.current?.focus() }, [active])

  const built = buildTrackerRowsRequest({ rows, modulePowerWatts: power,
    expectedHead: context?.head?.state.artifact_id ?? null, drawingUnits: context?.units })
  const gate = busy ? S.run_pending : blockedReason && Object.hasOwn(S, blockedReason) ? S[blockedReason]
    : checkoutHeld !== true ? sentence('TRACKER_ROWS_CHECKOUT_REQUIRED') : null
  // Shared by the controls and the handler; the ref closes same-batch submissions.
  function eligible(retry = false) {
    return usable && !lock.current && !pending && active && !gate && confirmed && !reading
      && !receipt && !TERMINAL.has(refusal) && (retry ? uncertain && recovery === 'retry-ready' : !uncertain && built.ok)
  }

  async function publish(retry = false, source) {
    if (!eligible(retry)) return
    const captured = retry ? retained.current : {
      drawingId, projectId, drawingUnits: context.units,
      request: JSON.parse(JSON.stringify(built.body)),
    }
    if (!captured) return
    const controller = new AbortController()
    const token = { controller }
    lock.current = token
    action.current = source
    setPending(true)
    setRefusal(null)
    let answer
    let value = null
    try {
      // The client gets its own copy: a client that mutates its argument cannot rewrite the retained request.
      answer = await client.createTrackerRows({ ...captured, request: JSON.parse(JSON.stringify(captured.request)),
        signal: controller.signal })
      if (answer?.ok === true) value = validateTrackerRowsResult(
        { ...answer.value, error: null, degraded_mode: false }, { ...captured, status: answer.status })
    } catch { answer = null }
    if (!alive.current || lock.current !== token) return
    lock.current = null
    setPending(false)
    if (value) {
      retained.current = null
      setUncertain(null)
      setRecovery(null)
      setReceipt(value)
      setContext((previous) => previous?.head?.index > value.head.index ? previous : { head: value.head, units: value.frame.drawing_units })
      try {
        const notified = changed.current?.(value)
        if (notified?.then) notified.then(undefined, () => {})
      } catch { /* A receipt survives observer failure. */ }
      load(true)
    } else {
      let code
      try { code = answer?.ok === true ? 'TRACKER_ROWS_CLIENT_RESPONSE_INVALID' : failureCode(answer) }
      catch { code = 'TRACKER_ROWS_CLIENT_RESPONSE_INVALID' }
      setRefusal(code)
      if (UNCERTAIN.has(code)) {
        retained.current = captured
        setUncertain(code)
        setRecovery(null)
        setConfirmed(false)
      } else if (code === 'TRACKER_ROWS_STALE_HEAD') load(true)
    }
    if (visible.current && (document.activeElement === source || document.activeElement === document.body)) {
      // Focus after React has committed the result heading.
      action.current = source ?? document.body
    } else action.current = null
  }

  useLayoutEffect(() => {
    if (!pending && action.current && active) {
      const target = document.activeElement
      if (target === action.current || target === document.body) resultHeading.current?.focus()
      action.current = null
    }
  }, [pending, active, receipt, refusal])

  if (!drawingId) return <section data-testid="solar-tracker-rows-panel" data-phase="no-drawing">
    <p>{sentence('TRACKER_ROWS_DRAWING_NOT_FOUND')}</p>
  </section>
  const phase = !usable ? 'unavailable' : pending ? 'pending'
    : receipt ? receipt.created ? 'published' : 'already-published'
      : reading ?? (uncertain ? recovery ?? 'unknown' : readError ? 'read-refused'
        : refusal ? 'refused' : gate ? 'disabled' : !confirmed ? 'loading' : !built.ok ? 'invalid' : 'ready')
  const readText = readError ? Object.hasOwn(S, readError) ? S[readError] : sentence(readError) : null
  const status = !usable ? sentence('TRACKER_ROWS_CLIENT_REQUEST_INVALID') : pending ? S.pending
    : receipt ? receipt.created ? S.published : S.retry
      : reading ? reading === 'units-loading' ? S.units_loading : S[reading]
        : uncertain ? recovery === 'retry-ready' ? S.retry_ready : recovery === 'superseded' ? '' : S.unknown
          : readText ?? gate ?? (!built.ok && confirmed ? sentence(built.code) : S.ready)
  const frozen = pending || !!uncertain
  function editRow(index, path, value) {
    if (frozen) return
    const [key, coordinate] = path.split('.')
    setRows((current) => current.map((row, i) => i !== index ? row : {
      ...row, [key]: coordinate === undefined ? value : row[key].map((number, j) => j === Number(coordinate) ? value : number),
    }))
  }
  function invalid(path) {
    return confirmed && !built.ok && (built.field === path || path.startsWith(`${built.field}.`))
  }
  return <section aria-label="Tracker layout" hidden={!active} data-testid="solar-tracker-rows-panel"
    data-phase={phase} data-head={context?.head?.state.artifact_id}>
    <h3 tabIndex={-1} ref={heading}>Tracker layout</h3>
    {context && <dl aria-label="Current physical state">
      <div><dt>Drawing units</dt><dd>{context.units === 'm' ? 'Metres' : 'Feet'}</dd></div>
      {context.head && <div><dt>Current head index</dt><dd>{context.head.index}</dd></div>}
    </dl>}
    {confirmed && context?.head === null && <p>{S.no_head}</p>}
    <fieldset className="params" disabled={frozen}>
      <label className="param"><span>Module power</span>
        <input type="text" inputMode="decimal" value={power} aria-invalid={invalid('module_power_watts') || undefined}
          aria-describedby={invalid('module_power_watts') ? `${baseId}-validation` : undefined}
          onChange={(event) => { if (!frozen) setPower(event.target.value) }} />
      </label>
      {rows.map((row, index) => <fieldset className="params" key={index}>
        <legend>Row {index + 1}</legend>
        {FIELDS.map(([label, path]) => {
          const [key, coordinate] = path.split('.')
          const field = `rows.${index}.${path}`
          return <label className="param" key={path}><span>{label}</span>
            <input type="text" inputMode={key === 'slots' ? 'numeric' : 'decimal'}
              value={coordinate === undefined ? row[key] : row[key][Number(coordinate)]}
              aria-invalid={invalid(field) || undefined} aria-describedby={invalid(field) ? `${baseId}-validation` : undefined}
              onChange={(event) => editRow(index, path, event.target.value)} />
          </label>
        })}
        <button type="button" disabled={frozen} onClick={() => { if (!frozen) setRows((current) => current.filter((_, i) => i !== index)) }}>Remove row</button>
      </fieldset>)}
      <button type="button" disabled={frozen || rows.length >= 256}
        onClick={() => { if (!frozen) setRows((current) => current.length < 256 ? [...current, blankRow()] : current) }}>Add row</button>
    </fieldset>
    {rows.length >= 256 && <p>{sentence('TRACKER_ROWS_LIMIT_EXCEEDED')}</p>}
    {confirmed && !built.ok && <p id={`${baseId}-validation`}>{sentence(built.code)}</p>}
    <button type="button" disabled={!eligible()} onClick={(event) => publish(false, event.currentTarget)}>Publish tracker rows</button>
    <button type="button" disabled={!usable || pending || !!reading || !active} onClick={() => load(true)}>Refresh physical state</button>
    {uncertain && <button type="button" disabled={!eligible(true)} onClick={(event) => publish(true, event.currentTarget)}>Retry original request</button>}
    <button type="button" onClick={onClose}>Close</button>
    {(receipt || refusal) && <h4 tabIndex={-1} ref={resultHeading}>Tracker rows result</h4>}
    {refusal && <p role="alert">{sentence(refusal)}</p>}
    {uncertain && refusal !== uncertain && <p role="alert">{sentence(uncertain)}</p>}
    {receipt && <>
      <dl aria-label="Published tracker rows">
        <div><dt>Rows</dt><dd>{receipt.summary.rows}</dd></div>
        <div><dt>Slots</dt><dd>{receipt.summary.slots}</dd></div>
        <div><dt>Module power</dt><dd>{receipt.summary.module_power_watts} W</dd></div>
        <div><dt>Published head index</dt><dd>{receipt.head.index}</dd></div>
        <div><dt>Drawing units</dt><dd>{receipt.frame.drawing_units === 'm' ? 'Metres' : 'Feet'}</dd></div>
      </dl>
      <p>{S.manual_scope}</p>
      {readText && <p role="alert">{readText}</p>}
    </>}
    <p aria-live="polite" aria-atomic="true" data-testid="solar-tracker-rows-status">{status}</p>
  </section>
}
