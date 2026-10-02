// The Ground Physical terrain panel. Built and proven unmounted: a later mount passes the drawing
// scope, a terrain client (solarTerrainClient.js) and a token that changes when the physical head
// changed elsewhere; this file imports no fetch, no api.js and no storage.
//
// It shows the stored terrain's preview state and runs the three preview operations (mesh, slope,
// slope clear) against the head it DISPLAYS: the request names the state artifact of the view on
// screen, captured when the person acts, never a newer head read out of sight. One operation at a
// time, never an automatic second one: a moved head is read once and left for review. Everything
// here is a preview. The kernel's slope status string is never rendered, and no sentence says
// what the server did or did not write after an answer was lost.
//
// A scope is one drawing and project. Another scope, a newer read or an unmount ends what was in
// flight, and a late answer changes nothing: no state, no announcement, no callback, no request.
import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { terrainReason } from './solarTerrainClient.js'
import {
  TERRAIN_LIMIT_FIELDS, TERRAIN_OPERATIONS, buildTerrainOperation, parseTerrainLimitDrafts, terrainSummary,
  validateTerrainOperation, validateTerrainView,
} from './solarTerrainModel.js'

const BUSY_SENTENCE = 'Terrain operations are unavailable while this workspace is busy'
const NO_DRAWING_SENTENCE = 'Open a drawing to view terrain previews'
const LOADING_SENTENCE = 'Loading terrain preview'
const REFRESHING_SENTENCE = 'Refreshing terrain preview'
const NO_CHANGE_SENTENCE = 'No terrain change was created by this preview'
const MAX_DISABLED_REASON_CHARS = 200
const ACTIONS = Object.freeze({
  mesh: Object.freeze({
    label: 'Run mesh preview', pending: 'Running the mesh preview', done: 'The mesh preview was updated',
  }),
  slope: Object.freeze({
    label: 'Run slope preview', pending: 'Running the slope preview', done: 'The slope preview was updated',
  }),
  'slope-clear': Object.freeze({
    label: 'Clear slope preview', pending: 'Clearing the slope preview', done: 'The slope preview was cleared',
  }),
})
// The displayed head is known to be behind: read it once, and run nothing again unasked.
const MOVED_CODES = Object.freeze(['TERRAIN_HEAD_MOVED', 'PHYSICAL_HEAD_CONFLICT'])
// The answer was lost, so the displayed view is no longer known to be the stored one.
const UNCONFIRMED_CODES = Object.freeze([
  'TERRAIN_CLIENT_TIMEOUT', 'TERRAIN_CLIENT_NETWORK', 'TERRAIN_CLIENT_ABORTED', 'TERRAIN_CLIENT_RESPONSE_INVALID',
])
const CODE_PATTERN = /^[A-Z][A-Z0-9_]{0,63}$/
const EMPTY_DRAFTS = Object.freeze(Object.fromEntries(TERRAIN_LIMIT_FIELDS.map((field) => [field.key, ''])))

// The code of a client answer that is a coded refusal; anything else is an unreadable answer.
function refusalCode(result) {
  try {
    if (result !== null && typeof result === 'object' && result.ok === false
      && typeof result.code === 'string' && CODE_PATTERN.test(result.code)) return result.code
  } catch {
    // Falls through to the closed refusal.
  }
  return 'TERRAIN_CLIENT_RESPONSE_INVALID'
}

function succeeded(result) {
  try {
    return result !== null && typeof result === 'object' && result.ok === true
  } catch {
    return false
  }
}

// The mount's reason for `disabled`, at most 200 characters, or the panel's own.
function busyText(disabledReason) {
  if (typeof disabledReason !== 'string' || disabledReason.trim() === '') return BUSY_SENTENCE
  return [...disabledReason.slice(0, 2 * MAX_DISABLED_REASON_CHARS)].slice(0, MAX_DISABLED_REASON_CHARS).join('')
}

export default function SolarTerrainPanel({
  drawingId, projectId = null, client, headSignal, disabled = false, disabledReason, onPhysicalHeadChanged,
}) {
  return (
    <PanelForScope
      key={JSON.stringify([drawingId ?? null, projectId ?? null])}
      drawingId={drawingId}
      projectId={projectId}
      client={client}
      headSignal={headSignal}
      disabled={disabled}
      disabledReason={disabledReason}
      onPhysicalHeadChanged={onPhysicalHeadChanged}
    />
  )
}

function PanelForScope({ drawingId, projectId, client, headSignal, disabled, disabledReason, onPhysicalHeadChanged }) {
  const usable = typeof drawingId === 'string' && drawingId !== '' && client !== null && typeof client === 'object'
    && typeof client.getTerrain === 'function' && typeof client.runTerrainOperation === 'function'
  // The last view read, kept apart from whether the latest read confirmed it.
  const [view, setView] = useState(null)
  const [confirmed, setConfirmed] = useState(false)
  const [reading, setReading] = useState(usable)
  const [pending, setPending] = useState(null)
  const [refusal, setRefusal] = useState(null)
  const [announcement, setAnnouncement] = useState('')
  const [drafts, setDrafts] = useState(EMPTY_DRAFTS)
  const baseId = useId()
  // This instance's scope is live; a late answer for an ended scope is dropped.
  const alive = useRef(false)
  // The newest read wins; an older read's answer is dropped even if it arrives last.
  const readSeq = useRef(0)
  const readController = useRef(null)
  // The operation in flight. Set synchronously, so a second press never sends a second request.
  const lock = useRef(null)
  const refocus = useRef(null)
  const buttons = useRef({})
  const refresh = useRef(null)
  const changed = useRef(onPhysicalHeadChanged)
  changed.current = onPhysicalHeadChanged

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
      readSeq.current += 1
      readController.current?.abort()
      readController.current = null
      const token = lock.current
      lock.current = null
      token?.controller.abort()
    }
  }, [])

  const load = useCallback(() => {
    if (!usable) return
    readSeq.current += 1
    const seq = readSeq.current
    readController.current?.abort()
    const controller = new AbortController()
    readController.current = controller
    setReading(true)
    Promise.resolve()
      .then(() => client.getTerrain({ drawingId, projectId, signal: controller.signal }))
      .then((result) => result, () => null)
      .then((result) => {
        if (!alive.current || seq !== readSeq.current) return
        readController.current = null
        setReading(false)
        const value = succeeded(result) ? validateTerrainView(result.value, { drawingId, projectId }) : null
        if (value !== null) {
          setView(value)
          setConfirmed(true)
          // A refusal of the operation that led to this read stays on screen.
          setRefusal((current) => (current !== null && current.source === 'read' ? null : current))
          return
        }
        const code = refusalCode(result)
        setConfirmed(false)
        setRefusal({ source: 'read', code, text: terrainReason(code) })
      })
  }, [usable, client, drawingId, projectId])

  // The first read, and one more whenever the mount says the physical head changed.
  useEffect(() => {
    load()
  }, [load, headSignal])

  // Focus returns to the action that ran once it can take it, or to Refresh when it cannot.
  useEffect(() => {
    const operation = refocus.current
    if (operation === null || pending !== null || reading) return
    refocus.current = null
    const active = document.activeElement
    const button = buttons.current[operation]
    if (active !== null && active !== document.body && active !== button) return
    if (button && !button.disabled) button.focus()
    else if (refresh.current && !refresh.current.disabled) refresh.current.focus()
  })

  const parsed = parseTerrainLimitDrafts(drafts)
  const busy = busyText(disabledReason)

  // Why an action is off, or null when it would be sent. The same ladder guards the press.
  function offReason(operation) {
    if (!usable) return terrainReason('TERRAIN_CLIENT_REQUEST_INVALID')
    if (pending !== null) return ACTIONS[pending].pending
    if (disabled !== false) return busy
    if (reading) return view === null ? LOADING_SENTENCE : REFRESHING_SENTENCE
    if (!confirmed || view === null) return terrainReason('TERRAIN_EXPECTED_HEAD_INVALID')
    const built = buildTerrainOperation({
      view, operation, limits: operation === 'slope' && parsed.ok ? parsed.limits : undefined,
    })
    if (!built.ok) return terrainReason(built.reason)
    if (operation === 'slope' && !parsed.ok) {
      const field = TERRAIN_LIMIT_FIELDS.find((candidate) => candidate.key === parsed.field)
      return field ? `${field.label}: ${parsed.reason}` : parsed.reason
    }
    return null
  }

  function edit(key, value) {
    setDrafts((current) => ({ ...current, [key]: value }))
  }

  function run(operation) {
    if (lock.current !== null || offReason(operation) !== null) return
    const built = buildTerrainOperation({ view, operation, limits: operation === 'slope' ? parsed.limits : undefined })
    if (!built.ok) return
    const controller = new AbortController()
    const token = { operation, controller }
    lock.current = token
    // The head named is the one on screen now; nothing is read between the press and the request.
    const request = { drawingId, projectId, ...built.request }
    setPending(operation)
    setRefusal(null)
    setAnnouncement(ACTIONS[operation].pending)
    Promise.resolve()
      .then(() => client.runTerrainOperation({ ...request, signal: controller.signal }))
      .then((result) => result, () => null)
      .then((result) => {
        if (!alive.current || lock.current !== token) return
        lock.current = null
        refocus.current = operation
        setPending(null)
        const value = succeeded(result) ? validateTerrainOperation(result.value, request) : null
        if (value !== null) {
          setAnnouncement(value.created ? ACTIONS[operation].done : NO_CHANGE_SENTENCE)
          if (value.created && typeof changed.current === 'function') {
            try {
              changed.current(value)
            } catch {
              // The mount's failure never hides the outcome.
            }
          }
          load()
          return
        }
        const code = succeeded(result) ? 'TERRAIN_CLIENT_RESPONSE_INVALID' : refusalCode(result)
        setAnnouncement('')
        setRefusal({ source: 'operation', code, text: terrainReason(code) })
        if (MOVED_CODES.includes(code)) load()
        else if (UNCONFIRMED_CODES.includes(code)) setConfirmed(false)
      })
  }

  if (!drawingId) {
    return (
      <section aria-label="Terrain preview" data-testid="solar-terrain-panel" data-phase="no-drawing">
        <p>{NO_DRAWING_SENTENCE}</p>
      </section>
    )
  }

  const lines = view === null ? null : terrainSummary(view)
  const reasons = { mesh: offReason('mesh'), slope: offReason('slope'), 'slope-clear': offReason('slope-clear') }
  // Slope carries every reason the other two can have, so its reason is the one shown.
  const reason = reasons.slope
  const phase = pending !== null ? 'pending'
    : reading ? (view === null ? 'loading' : 'refreshing')
      : usable && confirmed ? 'ready' : 'refused'

  return (
    <section aria-label="Terrain preview" data-testid="solar-terrain-panel" data-phase={phase}
      data-head={view !== null && view.stored ? view.head.state.artifact_id : undefined}>
      {lines !== null && (
        <dl aria-label="Stored terrain preview" data-testid="solar-terrain-summary">
          {lines.map((line) => (
            <div key={line.key} data-line={line.key} data-state={line.state}>
              <dt>{line.label}</dt>
              <dd>{line.text}</dd>
            </div>
          ))}
        </dl>
      )}
      <fieldset className="params" disabled={pending !== null} data-testid="solar-terrain-limits">
        <legend>Limits for the next slope preview</legend>
        {TERRAIN_LIMIT_FIELDS.map((field) => {
          const invalid = parsed.ok ? undefined : parsed.invalid[field.key]
          const hintId = `${baseId}-${field.key}-hint`
          const reasonId = `${baseId}-${field.key}-reason`
          return (
            <div key={field.key}>
              <label className="param">
                <span>{field.label}</span>
                <input type="text" inputMode={field.kind === 'columns' ? 'numeric' : 'decimal'}
                  value={drafts[field.key]}
                  aria-invalid={invalid === undefined ? undefined : 'true'}
                  aria-describedby={invalid === undefined ? hintId : `${hintId} ${reasonId}`}
                  onChange={(event) => edit(field.key, event.target.value)} />
              </label>
              <p id={hintId}>{field.hint}</p>
              {invalid !== undefined && <p id={reasonId} data-testid="solar-terrain-draft-reason">{invalid}</p>}
            </div>
          )
        })}
      </fieldset>
      {reason !== null && pending === null && <p role="status" data-testid="solar-terrain-reason">{reason}</p>}
      <div className="solar-terrain-actions">
        {TERRAIN_OPERATIONS.map((operation) => (
          <button key={operation} type="button" data-operation={operation}
            ref={(node) => { buttons.current[operation] = node }}
            disabled={reasons[operation] !== null}
            onClick={() => run(operation)}>
            {ACTIONS[operation].label}
          </button>
        ))}
        <button type="button" ref={refresh} disabled={!usable || reading || pending !== null} onClick={() => load()}>
          Refresh terrain preview
        </button>
      </div>
      {refusal !== null && <p role="alert" data-testid="solar-terrain-refusal">{refusal.text}</p>}
      <p className="solar-terrain-announce" aria-live="polite" aria-atomic="true"
        data-testid="solar-terrain-announce">{announcement}</p>
    </section>
  )
}
