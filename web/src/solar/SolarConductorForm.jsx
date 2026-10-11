import { useEffect, useId, useRef, useState } from 'react'
import { buildConductorParams, conductorGaugeOptions, conductorRows, CONDUCTOR_REASONS,
  deriveProjectConductorState, buildProjectConductorRequest } from './solarConductorModel.js'
import { projectSizingConductorReason } from './solarSizingModel.js'
import { useProjectSizingConductor } from './SolarSizingForm.jsx'
import { clickIntent, isToggleKey, LONG_PRESS_MS, LONG_PRESS_SLOP_PX, rangeIds } from '../cadedit/selection.js'

const toggled = (set, id) => {
  const next = new Set(set)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  return next
}

function ProjectSolarConductorForm(props) {
  const [selection, setSelection] = useState(null)
  const [choice, setChoice] = useState(null)
  const selectionRef = useRef(null)
  const choiceRef = useRef(null)
  const anchor = useRef(null)
  const press = useRef(null)
  const lastPointer = useRef(null)
  const swallowClick = useRef(false)
  const touchSelecting = useRef(false)
  const blocked = useRef(false)
  const hintId = useId()
  const options = conductorGaugeOptions(props.row)
  const declarationValid = Boolean(options && options.every((value) => value.trim().length > 0)
    && props.row?.name === 'solar-string-conductors'
    && props.row?.catalog_digest === props.catalogDigest)
  const declarationIdentity = JSON.stringify([props.row?.name, props.row?.catalog_digest, props.row?.params, options])
  const form = useProjectSizingConductor(props, { toolName: 'solar-string-conductors',
    deriveState: deriveProjectConductorState,
    buildRequest: (args) => buildProjectConductorRequest({ ...args,
      selected: selection?.scope === args.scope ? selection.selected : new Set(),
      gauge: choice?.scope === args.scope ? choice.gauge : '', options }),
    declarationIdentity, declarationCurrent: () => declarationIdentity === JSON.stringify([
      props.row?.name, props.row?.catalog_digest, props.row?.params, conductorGaugeOptions(props.row)]),
    declarationValid })
  // Selection belongs to the local scope object, including client-instance and authority identity.
  const rows = form.editable && declarationValid ? form.state.rows : []
  const currentSelected = selection?.scope === form.scope ? selection.selected : new Set()
  const currentGauge = choice?.scope === form.scope ? choice.gauge : ''
  selectionRef.current = { scope: form.scope, selected: currentSelected }
  choiceRef.current = { scope: form.scope, gauge: currentGauge }
  const disabled = !form.editable || !declarationValid || form.submitting || !form.isScopeCurrent()
  blocked.current = disabled
  const sentence = projectSizingConductorReason(form.reason)
    || (form.reason === 'conductor_selection_required' ? CONDUCTOR_REASONS.conductor_selection_required
      : form.reason === 'conductor_gauge_required' ? CONDUCTOR_REASONS.conductor_gauge_required
        : form.reason === 'conductor_selection_too_large' ? CONDUCTOR_REASONS.conductor_selection_too_large
          : form.reason === 'conductor_graph_unavailable' ? CONDUCTOR_REASONS.conductor_graph_unavailable : null)
  function setSelected(update) {
    if (blocked.current || form.scope.lock || !form.isScopeCurrent()) return
    const previous = selectionRef.current?.scope === form.scope ? selectionRef.current.selected : new Set()
    const next = typeof update === 'function' ? update(previous) : update
    if (next.size === previous.size && [...next].every((id) => previous.has(id))) return
    form.invalidateCandidate()
    selectionRef.current = { scope: form.scope, selected: next }
    setSelection(selectionRef.current)
  }
  function setGauge(value) {
    if (blocked.current || form.scope.lock || !form.isScopeCurrent()) return
    if (choiceRef.current?.scope === form.scope && choiceRef.current.gauge === value) return
    form.invalidateCandidate()
    choiceRef.current = { scope: form.scope, gauge: value }
    setChoice(choiceRef.current)
  }
  useEffect(() => {
    anchor.current = null
    touchSelecting.current = false
    swallowClick.current = false
    return cancelPress
  }, [form.scope])
  form.scope.cancelTouch = cancelPress
  useEffect(() => cancelPress, [form.scope, form.scope.generation])
  function cancelPress() {
    if (press.current) clearTimeout(press.current.timer)
    press.current = null
  }

  // The anchor is the last row chosen alone or toggled; failing that, the
  // newest selected row still shown; failing that, the row itself.
  function anchorFor(id) {
    if (anchor.current !== null && rows.some((item) => item.id === anchor.current)) return anchor.current
    const shown = new Set(rows.map((item) => item.id))
    const newest = [...currentSelected].reverse().find((candidate) => shown.has(candidate))
    return newest ?? id
  }

  // Selection has no checkbox column. A plain click or Enter selects only the
  // row, Shift selects a range from the anchor in graph order (Ctrl or Cmd
  // with it adds the range), Ctrl or Cmd alone toggles one row, and after a
  // touch long press, taps toggle until the selection is empty again.
  function choose(event, item, pointer) {
    if (blocked.current || !form.isScopeCurrent()) return
    if (currentSelected.size === 0) touchSelecting.current = false
    const intent = clickIntent(event)
    if (intent === 'range' || intent === 'rangeAdd') {
      const from = anchorFor(item.id)
      anchor.current = from
      const range = rangeIds(rows, from, item.id)
      setSelected((previous) => new Set(intent === 'rangeAdd' ? [...previous, ...range] : range))
      return
    }
    anchor.current = item.id
    if (intent === 'toggle' || (pointer === 'touch' && touchSelecting.current)) {
      setSelected((previous) => toggled(previous, item.id))
    } else setSelected(new Set([item.id]))
  }

  return (
    <div data-testid="project-solar-conductor-form" data-mode={form.state?.mode ?? 'loading'}>
      <p>This records conductor choices. It does not check ampacity or voltage drop.</p>
      <p id={hintId}>Click a string to select it. Shift-click selects a range, Ctrl-click or Cmd-click adds or removes one, and X adds or removes the focused string. On touch, press and hold a string to start selecting.</p>
      <table aria-label="String conductors" aria-describedby={hintId}>
        <thead><tr><th scope="col">Circuit tag</th><th scope="col">Current conductor</th></tr></thead>
        <tbody>{rows.map((item) => {
          const isSelected = currentSelected.has(item.id)
          return (
            <tr key={item.id} data-string-id={item.id} aria-label={`Select ${item.tag}`} aria-selected={isSelected}
              aria-disabled={disabled || undefined} tabIndex={disabled ? -1 : 0}
              onClick={(event) => {
                const pointer = lastPointer.current
                lastPointer.current = null
                if (disabled) return
                if (swallowClick.current) {
                  // The release that ends a long press is not a second tap.
                  swallowClick.current = false
                  return
                }
                choose(event, item, pointer)
              }}
              onKeyDown={(event) => {
                swallowClick.current = false
                if (disabled) return
                if (isToggleKey(event.nativeEvent)) {
                  event.preventDefault()
                  anchor.current = item.id
                  setSelected((previous) => toggled(previous, item.id))
                } else if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault()
                  choose(event, item, null)
                } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                  const sibling = event.key === 'ArrowDown' ? event.currentTarget.nextElementSibling : event.currentTarget.previousElementSibling
                  if (sibling) {
                    event.preventDefault()
                    sibling.focus()
                  }
                }
              }}
              onPointerDown={(event) => {
                lastPointer.current = event.pointerType || null
                swallowClick.current = false
                cancelPress()
                if (disabled || event.pointerType !== 'touch') return
                const generation = form.scope.generation
                const timer = setTimeout(() => {
                  press.current = null
                  if (blocked.current || generation !== form.scope.generation || !form.isScopeCurrent()) return
                  swallowClick.current = true
                  touchSelecting.current = true
                  anchor.current = item.id
                  setSelected((previous) => (previous.has(item.id) ? previous : new Set([...previous, item.id])))
                }, LONG_PRESS_MS)
                press.current = { timer, x: event.clientX, y: event.clientY }
              }}
              onPointerMove={(event) => {
                const start = press.current
                if (start && Math.hypot(event.clientX - start.x, event.clientY - start.y) > LONG_PRESS_SLOP_PX) cancelPress()
              }}
              onPointerUp={cancelPress}
              onPointerCancel={cancelPress}
              onContextMenu={(event) => { if (swallowClick.current) event.preventDefault() }}
            >
              <td>{item.tag}{isSelected && <span aria-hidden="true"> (selected)</span>}</td>
              <td>{item.gauge.trim() ? item.gauge : 'Not set'}</td>
            </tr>
          )
        })}</tbody>
      </table>
      <button type="button" disabled={disabled} onClick={() => setSelected(new Set(rows.map((item) => item.id)))}>Select all</button>
      <button type="button" disabled={disabled} onClick={() => setSelected(new Set())}>Clear selection</button>
      <label>Conductor<select value={currentGauge} disabled={disabled} onChange={(event) => setGauge(event.target.value)}>
        <option value="">Choose a conductor</option>
        {(options || []).map((option) => <option key={option} value={option}>{option}</option>)}
      </select></label>
      {form.state === null && <p role="status">Reading the drawing.</p>}
      {sentence && <p role="status">{sentence}</p>}
      {form.runMessage && <p role="status" data-testid="project-solar-conductor-feedback">
        {form.runMessage.text}{form.runMessage.code ? ` (${form.runMessage.code})` : ''}
      </p>}
      <button type="button" disabled={Boolean(form.reason) || form.submitting || !form.editable}
        onClick={form.submit}>Apply to selected strings</button>
      {form.retryAvailable && <button type="button"
        disabled={Boolean(form.reason) || form.submitting || !form.editable} onClick={form.submit}>Retry</button>}
      {form.editable && <button type="button" disabled={form.submitting} onClick={form.refreshPreview}>Refresh preview</button>}
      {props.onClose && <button type="button" onClick={props.onClose}>Cancel</button>}
    </div>
  )
}

export default function SolarConductorForm(props) {
  if (Object.prototype.hasOwnProperty.call(props, 'solarBinding') || props.projectId != null) {
    return <ProjectSolarConductorForm {...props} />
  }
  return <StandaloneSolarConductorForm {...props} />
}

function StandaloneSolarConductorForm({
  row, drawingId, drawingVersion, projectId = null, readIntake, status = null,
  failureCode = null, onSubmit, onClose,
}) {
  const [graph, setGraph] = useState(null)
  const [selected, setSelected] = useState(new Set())
  const [gauge, setGauge] = useState('')
  const [refresh, setRefresh] = useState(0)
  const previousStatus = useRef(status)
  const lock = useRef(false)
  const anchor = useRef(null)
  const press = useRef(null)
  const lastPointer = useRef(null)
  const swallowClick = useRef(false)
  const touchSelecting = useRef(false)
  const blocked = useRef(false)
  const hintId = useId()
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
    anchor.current = null
    touchSelecting.current = false
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

  useEffect(() => () => { if (press.current) clearTimeout(press.current.timer) }, [])

  const currentGraph = graph?.drawingId === drawingId && graph?.drawingVersion === drawingVersion ? graph.result : null
  const rows = scoped && currentGraph?.ok && options ? currentGraph.rows : []
  const built = !scoped ? { ok: false, reason: 'conductor_project_scope' }
    : !currentGraph?.ok || !options ? { ok: false, reason: 'conductor_graph_unavailable' }
      : buildConductorParams({ rev: currentGraph.rev, rows, selected, gauge, options })
  const busy = status === 'pending'
  const disabled = !scoped || !currentGraph?.ok || !options || busy
  blocked.current = disabled

  function submit() {
    if (lock.current || status === 'pending' || !built.ok) return
    lock.current = true
    onSubmit(row, built.params)
  }

  function cancelPress() {
    if (press.current) clearTimeout(press.current.timer)
    press.current = null
  }

  // The anchor is the last row chosen alone or toggled; failing that, the
  // newest selected row still shown; failing that, the row itself.
  function anchorFor(id) {
    if (anchor.current !== null && rows.some((item) => item.id === anchor.current)) return anchor.current
    const shown = new Set(rows.map((item) => item.id))
    const newest = [...selected].reverse().find((candidate) => shown.has(candidate))
    return newest ?? id
  }

  // Selection has no checkbox column. A plain click or Enter selects only the
  // row, Shift selects a range from the anchor in graph order (Ctrl or Cmd
  // with it adds the range), Ctrl or Cmd alone toggles one row, and after a
  // touch long press, taps toggle until the selection is empty again.
  function choose(event, item, pointer) {
    if (selected.size === 0) touchSelecting.current = false
    const intent = clickIntent(event)
    if (intent === 'range' || intent === 'rangeAdd') {
      const from = anchorFor(item.id)
      anchor.current = from
      const range = rangeIds(rows, from, item.id)
      setSelected((previous) => new Set(intent === 'rangeAdd' ? [...previous, ...range] : range))
      return
    }
    anchor.current = item.id
    if (intent === 'toggle' || (pointer === 'touch' && touchSelecting.current)) {
      setSelected((previous) => toggled(previous, item.id))
    } else setSelected(new Set([item.id]))
  }

  return (
    <div>
      <p>This records conductor choices. It does not check ampacity or voltage drop.</p>
      <p id={hintId}>Click a string to select it. Shift-click selects a range, Ctrl-click or Cmd-click adds or removes one, and X adds or removes the focused string. On touch, press and hold a string to start selecting.</p>
      <table aria-label="String conductors" aria-describedby={hintId}>
        <thead><tr><th scope="col">Circuit tag</th><th scope="col">Current conductor</th></tr></thead>
        <tbody>{rows.map((item) => {
          const isSelected = selected.has(item.id)
          return (
            <tr key={item.id} data-string-id={item.id} aria-label={`Select ${item.tag}`} aria-selected={isSelected}
              aria-disabled={disabled || undefined} tabIndex={disabled ? -1 : 0}
              onClick={(event) => {
                const pointer = lastPointer.current
                lastPointer.current = null
                if (disabled) return
                if (swallowClick.current) {
                  // The release that ends a long press is not a second tap.
                  swallowClick.current = false
                  return
                }
                choose(event, item, pointer)
              }}
              onKeyDown={(event) => {
                swallowClick.current = false
                if (disabled) return
                if (isToggleKey(event.nativeEvent)) {
                  event.preventDefault()
                  anchor.current = item.id
                  setSelected((previous) => toggled(previous, item.id))
                } else if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault()
                  choose(event, item, null)
                } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                  const sibling = event.key === 'ArrowDown' ? event.currentTarget.nextElementSibling : event.currentTarget.previousElementSibling
                  if (sibling) {
                    event.preventDefault()
                    sibling.focus()
                  }
                }
              }}
              onPointerDown={(event) => {
                lastPointer.current = event.pointerType || null
                swallowClick.current = false
                cancelPress()
                if (disabled || event.pointerType !== 'touch') return
                const timer = setTimeout(() => {
                  press.current = null
                  if (blocked.current) return
                  swallowClick.current = true
                  touchSelecting.current = true
                  anchor.current = item.id
                  setSelected((previous) => (previous.has(item.id) ? previous : new Set([...previous, item.id])))
                }, LONG_PRESS_MS)
                press.current = { timer, x: event.clientX, y: event.clientY }
              }}
              onPointerMove={(event) => {
                const start = press.current
                if (start && Math.hypot(event.clientX - start.x, event.clientY - start.y) > LONG_PRESS_SLOP_PX) cancelPress()
              }}
              onPointerUp={cancelPress}
              onPointerCancel={cancelPress}
              onContextMenu={(event) => { if (swallowClick.current) event.preventDefault() }}
            >
              <td>{item.tag}{isSelected && <span aria-hidden="true"> (selected)</span>}</td>
              <td>{item.gauge.trim() ? item.gauge : 'Not set'}</td>
            </tr>
          )
        })}</tbody>
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
