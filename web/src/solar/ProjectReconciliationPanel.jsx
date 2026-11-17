import { useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { sameSolarDrawingBinding, solarDrawingBindingKey } from './solarDrawingBinding.js'
import { mintCorrelationId } from '../runIntent.js'
import { parseReconciliationNumber, parseEngineeringTotalsRecord, savedReconciliationValues, reconciliationRowsFromValues, reconciliationCsv, validReconciliationResponse } from './projectReconciliation.js'

export const RECONCILIATION_READ_TIMEOUT_MS = 20000
const unavailableValues = savedReconciliationValues(null)
const validDrafts = values => Array.isArray(values) && values.length === 5 && Array.from(values).every((value, index) =>
  typeof value === 'string' && ((index === 4 && value === '') || parseReconciliationNumber(value, index !== 4) !== null))
function validTotals(response, binding, hash) {
  const data = response?.data, record = data?.record
  return response?.ok === true && Number.isSafeInteger(data?.revision) && data.revision >= 0 && data.revision <= 2147483647 &&
    typeof data.canWrite === 'boolean' && (record === null ? data.revision === 0 :
      !!record && data.revision > 0 && parseEngineeringTotalsRecord(JSON.stringify(record), binding, hash) !== null)
}

export default function ProjectReconciliationPanel({ solarBinding, authorityKey, projectClient, isCurrent, busy = false }) {
  let bindingKey = null
  try { bindingKey = solarDrawingBindingKey(solarBinding) } catch { /* unavailable */ }
  const scope = useMemo(() => ({ binding: solarBinding, authorityKey, projectClient, isCurrent, alive: false }),
    [bindingKey, authorityKey, projectClient, isCurrent])
  const [view, setView] = useState(null)
  const [draft, setDraft] = useState(null)
  const [report, setReport] = useState(null)
  const [, refreshObserver] = useState(0)
  const inputId = useId()
  const request = useRef(null)
  const storageRequest = useRef(null)
  const generation = useRef(0)
  const readButton = useRef(null)
  const restoreReadFocus = useRef(false)
  const current = () => {
    try {
      const snapshot = projectClient.getSnapshot()
      return scope.alive && !!bindingKey && typeof authorityKey === 'string' && !!authorityKey.trim() &&
        typeof projectClient.readContext === 'function' && typeof projectClient.subscribe === 'function' &&
        isCurrent() === true && sameSolarDrawingBinding(snapshot?.binding, scope.binding) && snapshot.authorityKey === authorityKey
    } catch { return false }
  }
  useLayoutEffect(() => {
    const observer = {}
    scope.observer = observer
    scope.alive = true
    scope.observationError = null
    const cancelRead = () => {
      if (storageRequest.current?.observer === observer) {
        const active = storageRequest.current
        storageRequest.current = null
        active.abort.abort()
      }
      if (request.current?.observer !== observer) return
      const active = request.current
      request.current = null
      active.abort.abort()
    }
    const invalidate = () => {
      if (scope.observer !== observer) return
      if (current()) return
      scope.alive = false
      cancelRead()
      setView({ scope, graph: null })
      setDraft(null)
      setReport(null)
    }
    let unsubscribe
    try {
      unsubscribe = projectClient?.subscribe(invalidate)
      if (typeof unsubscribe !== 'function') throw new Error('Invalid observer')
    } catch {
      scope.alive = false
      scope.observationError = 'Reconciliation unavailable: saved design changes could not be observed. Reconnect the project client to try again.'
    }
    invalidate()
    refreshObserver(value => value + 1)
    return () => {
      if (scope.observer === observer) {
        scope.alive = false
        scope.observer = null
      }
      cancelRead()
      try { if (typeof unsubscribe === 'function') unsubscribe() } catch { /* observer isolation */ }
    }
  }, [scope])
  const live = current()
  const saved = live && view?.scope === scope ? view : null
  const drafts = live && draft?.scope === scope ? draft.values : ['', '', '', '', '']
  const rows = reconciliationRowsFromValues(saved?.values || unavailableValues, drafts)
  const pending = saved?.pending === true
  const totals = live && report?.scope === scope ? report : null
  const storagePending = !!totals?.pending
  function updateReport(value) {
    // Keep the CAS baseline synchronous across read completion and its separate report load.
    scope.report = value
    setReport({ scope, ...value })
  }
  useLayoutEffect(() => {
    if (!pending && restoreReadFocus.current) {
      if (live && document.activeElement === document.body) readButton.current?.focus()
      restoreReadFocus.current = false
    }
  }, [pending, live])
  const reportValid = validDrafts(drafts)
  const canExport = live && !busy && !pending && !!saved?.graph && reportValid
  const canSave = canExport && !storagePending && totals?.canWrite === true &&
    totals?.hash === saved.hash && Number.isSafeInteger(totals?.revision) && !totals?.conflict && typeof projectClient.saveEngineeringTotals === 'function'
  async function store(operation, hash, readGeneration, reload = false) {
    if (!current() || storageRequest.current || generation.current !== readGeneration) return
    const abort = new AbortController()
    const active = { abort, observer: scope.observer, operation }
    storageRequest.current = active
    const before = scope.report || {}, edits = scope.edits || 0
    const accepted = () => !abort.signal.aborted && storageRequest.current === active &&
      scope.observer === active.observer && generation.current === readGeneration && current()
    const failed = () => updateReport({ ...before, pending: null,
      status: operation === 'save' ? 'Engineering totals could not be saved. Retry Save engineering totals.'
        : 'Engineering totals could not be loaded. Reload saved engineering totals to try again.' })
    updateReport({ ...before, pending: operation, status: operation === 'save' ? 'Saving engineering totals.' : 'Loading engineering totals.' })
    const timer = setTimeout(() => {
      if (storageRequest.current !== active) return
      if (accepted()) failed()
      storageRequest.current = null
      abort.abort()
    }, RECONCILIATION_READ_TIMEOUT_MS)
    abort.signal.addEventListener('abort', () => clearTimeout(timer), { once: true })
    try {
      let response, submitted
      if (operation === 'save') {
        const signature = JSON.stringify([hash, before.revision, drafts])
        if (scope.retry?.signature !== signature) scope.retry = { signature, request: Object.freeze({
          graphSha256: hash, drafts: Object.freeze([...drafts]), expectedRevision: before.revision, idempotencyKey: mintCorrelationId(),
        }) }
        if (!accepted()) return
        submitted = scope.retry.request
        response = await projectClient.saveEngineeringTotals({ ...submitted, signal: abort.signal })
      } else response = await projectClient.loadEngineeringTotals({ graphSha256: hash, signal: abort.signal })
      if (!accepted()) return
      if (response?.status === 409) {
        scope.retry = null
        updateReport({ ...before, pending: null, conflict: true,
          status: 'Engineering totals changed. Reload saved engineering totals before saving.' })
        return
      }
      if (!validTotals(response, scope.binding, hash) || (operation === 'save' &&
          (!response.data.record || response.data.record.values.some((value, index) => value !== submitted.drafts[index]) ||
            response.data.revision !== before.revision + 1))) { failed(); return }
      const { record, revision, canWrite } = response.data
      const restoredValues = record ? [...record.values] : null
      if (!accepted()) return
      const unchanged = (scope.edits || 0) === edits
      const conflict = operation === 'load' && scope.dirty &&
        ((!reload || !unchanged) && Number.isSafeInteger(before.revision) && before.revision !== revision ||
          before.conflict && (!reload || !unchanged))
      if (operation === 'save') {
        scope.dirty = !unchanged
        scope.retry = null
      } else if (record && unchanged && !conflict && (reload || !scope.dirty)) {
        setDraft({ scope, values: restoredValues })
        scope.dirty = false
      }
      updateReport({ hash, revision: conflict ? before.revision : revision, canWrite, conflict, pending: null,
        status: operation === 'save' ? unchanged ? 'Engineering totals saved.'
          : 'Submitted engineering totals saved. Newer edits remain unsaved.' : conflict
          ? 'Engineering totals changed. Reload saved engineering totals before saving.'
          : record && scope.dirty ? 'Saved engineering totals exist. Your entered drafts were retained.'
            : record ? 'Saved engineering totals loaded.' : 'No saved engineering totals. Entered drafts were retained.' })
    } catch { if (accepted()) failed() }
    finally {
      clearTimeout(timer)
      if (storageRequest.current === active) storageRequest.current = null
    }
  }
  async function read() {
    if (!current() || busy || request.current) return
    if (storageRequest.current) {
      if (storageRequest.current.operation !== 'save') return
      const old = storageRequest.current
      storageRequest.current = null
      old.abort.abort()
      updateReport({ ...scope.report, pending: null,
        status: 'Save response cancelled. Reloading the saved design.' })
      if (!current()) return
    }
    const readGeneration = ++generation.current
    restoreReadFocus.current = document.activeElement === readButton.current
    const abort = new AbortController()
    const active = { abort, observer: scope.observer }
    request.current = active
    setView({ scope, pending: true, graph: null, status: 'Reading saved design.' })
    const timer = setTimeout(() => {
      if (request.current !== active) return
      request.current = null
      if (current()) {
        setView({ scope, error: 'Saved design read timed out. Try reading it again.' })
      }
      abort.abort()
    }, RECONCILIATION_READ_TIMEOUT_MS)
    abort.signal.addEventListener('abort', () => clearTimeout(timer), { once: true })
    try {
      const response = await projectClient.readContext({ signal: abort.signal })
      if (abort.signal.aborted || request.current !== active || !current()) return
      if (!validReconciliationResponse(response, scope.binding)) {
        setView({ scope, error: 'Saved design unavailable: the response, drawing identity or graph is invalid. Try reading it again.' })
        return
      }
      const graph = structuredClone(response.data.intake.solar_design_graph)
      const values = savedReconciliationValues(graph)
      if (abort.signal.aborted || request.current !== active || !current()) return
      const hash = response.data.intake.solar_design_graph_sha256
      if (scope.hash !== hash) {
        scope.hash = hash
        scope.retry = null
      }
      setView({ scope, graph, values, hash, generation: readGeneration, status: `Saved design revision ${graph.rev}.` })
      if (typeof projectClient.loadEngineeringTotals === 'function') void store('load', hash, readGeneration)
    } catch {
      if (!abort.signal.aborted && request.current === active && current()) {
        setView({ scope, error: 'Saved design could not be read. Try reading it again.' })
      }
    } finally {
      clearTimeout(timer)
      if (request.current === active) request.current = null
    }
  }
  function download() {
    if (!canExport || !current()) return
    let url, anchor
    try {
      url = URL.createObjectURL(new Blob([reconciliationCsv(rows)], { type: 'text/csv;charset=utf-8' }))
      if (!current()) return
      anchor = document.createElement('a')
      anchor.href = url
      anchor.download = 'engineering-reconciliation.csv'
      anchor.click()
      if (current()) setView({ ...saved, error: null })
    } catch {
      if (current()) setView({ ...saved, error: 'CSV could not be downloaded. Try Download CSV again.' })
    } finally {
      try { anchor?.remove() } catch { /* cleanup failures are independent */ }
      if (url !== undefined) {
        try { URL.revokeObjectURL(url) } catch { /* cleanup must not erase accepted evidence */ }
      }
    }
  }
  return <section aria-label="Engineering reconciliation" style={{ minWidth: 0, maxWidth: '100%', overflowWrap: 'anywhere' }}>
    <h3>Engineering reconciliation</h3>
    <p>Enter user-supplied engineering report/BOM totals. These values are not independently verified.</p>
    <p id={`${inputId}-dc-optional`}>kW DC is optional when unknown. Leave it blank to export an unreadable comparison, with no report value or delta; it cannot pass.</p>
    <button ref={readButton} type="button" disabled={!live || busy || pending || (storagePending && totals.pending !== 'save')} onClick={read}>Read saved design</button>
    <p aria-live="polite">{!live ? scope.observationError || 'Reconciliation unavailable: the selected drawing scope is stale or unavailable.'
      : saved?.status || 'Read the saved design to compare totals.'}</p>
    {saved?.error && <p role="alert">{saved.error}</p>}
    <button type="button" disabled={!canSave} onClick={() => { if (canSave) void store('save', saved.hash, saved.generation) }}>Save engineering totals</button>
    <button type="button" disabled={!live || busy || pending || storagePending || !saved?.hash || typeof projectClient.loadEngineeringTotals !== 'function'}
      onClick={() => { if (current() && !busy && saved?.hash) void store('load', saved.hash, saved.generation, true) }}>Reload saved engineering totals</button>
    <p aria-live="polite">{totals?.status}{totals?.canWrite === false ? ' Engineering totals are read-only.' : ''}</p>
    <div role="region" aria-label="Reconciliation comparison table" tabIndex={0} style={{ maxWidth: '100%', overflowX: 'auto' }}>
    <table className="ops-table" style={{ display: 'table', width: '44rem', minWidth: '44rem', tableLayout: 'fixed', overflowWrap: 'normal' }}><caption>Saved Studio vs engineering report/BOM totals</caption>
      <thead><tr><th scope="col">Metric</th><th scope="col">Studio</th><th scope="col">Engineering report/BOM</th>
        <th scope="col">Delta</th><th scope="col">Verdict</th></tr></thead>
      <tbody>{rows.map((row, index) => <tr key={row.metric}>
        <th scope="row">{row.metric}</th><td style={{ overflowWrap: 'anywhere' }}>{row.studio || 'Unavailable'}</td>
        <td style={{ verticalAlign: 'top' }}><input id={`${inputId}-${index}`} aria-label={`${row.metric} engineering report/BOM`} inputMode={index === 4 ? 'decimal' : 'numeric'}
          aria-describedby={`${inputId}-${index}-help${index === 4 ? ` ${inputId}-dc-optional` : ''}`}
          disabled={!live || busy || pending} value={drafts[index]} maxLength={37} style={{ width: '12ch' }}
          aria-invalid={drafts[index] !== '' && parseReconciliationNumber(drafts[index], index !== 4) === null}
          onChange={(event) => {
            if (!current() || busy || pending) return
            const values = [...drafts]
            values[index] = event.target.value
            scope.edits = (scope.edits || 0) + 1
            scope.dirty = true
            scope.retry = null
            if (scope.report && !scope.report.pending && !scope.report.conflict) {
              updateReport({ ...scope.report, status: 'Engineering totals have unsaved changes.' })
            }
            setDraft({ scope, values })
          }} /><small id={`${inputId}-${index}-help`}>{index === 4
            ? 'Use a nonnegative decimal with up to 18 integer and 18 fractional digits, or leave blank when unknown.'
            : 'Enter a whole number from 0 to 9007199254740991. Counts are required; do not use signs, decimals or exponents.'}</small></td>
        <td style={{ overflowWrap: 'anywhere' }}>{row.delta || 'Unavailable'}</td><td>{row.verdict === 'UNREADABLE' ? 'Unavailable' : row.verdict}
          {row.reason && <small>: {row.reason}</small>}</td>
      </tr>)}</tbody>
    </table>
    </div>
    <p>Delta is Studio minus the supplied report/BOM value.</p>
    <button type="button" disabled={!canExport} onClick={download}>Download CSV</button>
  </section>
}
