import { useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { sameSolarDrawingBinding, solarDrawingBindingKey } from './solarDrawingBinding.js'
import { parseReconciliationNumber, savedReconciliationValues, reconciliationRowsFromValues, reconciliationCsv, validReconciliationResponse } from './projectReconciliation.js'

export const RECONCILIATION_READ_TIMEOUT_MS = 20000
const unavailableValues = savedReconciliationValues(null)

export default function ProjectReconciliationPanel({ solarBinding, authorityKey, projectClient, isCurrent, busy = false }) {
  let bindingKey = null
  try { bindingKey = solarDrawingBindingKey(solarBinding) } catch { /* unavailable */ }
  const scope = useMemo(() => ({ binding: solarBinding, authorityKey, projectClient, isCurrent, alive: false }),
    [bindingKey, authorityKey, projectClient, isCurrent])
  const [view, setView] = useState(null)
  const [draft, setDraft] = useState(null)
  const [, refreshObserver] = useState(0)
  const inputId = useId()
  const request = useRef(null)
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
  useLayoutEffect(() => {
    if (!pending && restoreReadFocus.current) {
      if (live && document.activeElement === document.body) readButton.current?.focus()
      restoreReadFocus.current = false
    }
  }, [pending, live])
  const reportValid = drafts.every((value, index) => (index === 4 && value === '') || parseReconciliationNumber(value, index !== 4) !== null)
  const canExport = live && !busy && !pending && !!saved?.graph && reportValid
  async function read() {
    if (!current() || busy || request.current) return
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
      setView({ scope, graph, values, status: `Saved design revision ${graph.rev}.` })
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
    let url
    try {
      url = URL.createObjectURL(new Blob([reconciliationCsv(rows)], { type: 'text/csv;charset=utf-8' }))
      if (!current()) return
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = 'engineering-reconciliation.csv'
      anchor.click()
      if (current()) setView({ ...saved, error: null })
    } catch {
      if (current()) setView({ ...saved, error: 'CSV could not be downloaded. Try Download CSV again.' })
    } finally {
      if (url !== undefined) {
        try { URL.revokeObjectURL(url) } catch { /* cleanup must not erase accepted evidence */ }
      }
    }
  }
  return <section aria-label="Engineering reconciliation">
    <h3>Engineering reconciliation</h3>
    <p>Enter user-supplied engineering report/BOM totals. These values are not independently verified.</p>
    <p id={`${inputId}-dc-optional`}>kW DC is optional when unknown. Leave it blank to export an unreadable comparison, with no report value or delta; it cannot pass.</p>
    <button ref={readButton} type="button" disabled={!live || busy || pending} onClick={read}>Read saved design</button>
    <p aria-live="polite">{!live ? scope.observationError || 'Reconciliation unavailable: the selected drawing scope is stale or unavailable.'
      : saved?.status || 'Read the saved design to compare totals.'}</p>
    {saved?.error && <p role="alert">{saved.error}</p>}
    <table className="ops-table"><caption>Saved Studio vs engineering report/BOM totals</caption>
      <thead><tr><th scope="col">Metric</th><th scope="col">Studio</th><th scope="col">Engineering report/BOM</th>
        <th scope="col">Delta</th><th scope="col">Verdict</th></tr></thead>
      <tbody>{rows.map((row, index) => <tr key={row.metric}>
        <th scope="row">{row.metric}</th><td>{row.studio || 'Unavailable'}</td>
        <td><input id={`${inputId}-${index}`} aria-label={`${row.metric} engineering report/BOM`} inputMode={index === 4 ? 'decimal' : 'numeric'}
          aria-describedby={`${inputId}-${index}-help${index === 4 ? ` ${inputId}-dc-optional` : ''}`}
          disabled={!live || busy || pending} value={drafts[index]} maxLength={37}
          aria-invalid={drafts[index] !== '' && parseReconciliationNumber(drafts[index], index !== 4) === null}
          onChange={(event) => {
            if (!current() || busy || pending) return
            const values = [...drafts]
            values[index] = event.target.value
            setDraft({ scope, values })
          }} /><small id={`${inputId}-${index}-help`}>{index === 4
            ? 'Use a nonnegative decimal with up to 18 integer and 18 fractional digits, or leave blank when unknown.'
            : 'Enter a whole number from 0 to 9007199254740991. Counts are required; do not use signs, decimals or exponents.'}</small></td>
        <td>{row.delta || 'Unavailable'}</td><td>{row.verdict === 'UNREADABLE' ? 'Unavailable' : row.verdict}
          {row.reason && <small>: {row.reason}</small>}</td>
      </tr>)}</tbody>
    </table>
    <p>Delta is Studio minus the supplied report/BOM value.</p>
    <button type="button" disabled={!canExport} onClick={download}>Download CSV</button>
  </section>
}
