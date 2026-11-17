import { useLayoutEffect, useMemo, useRef, useState } from 'react'
import { sameSolarDrawingBinding, solarDrawingBindingKey } from './solarDrawingBinding.js'
import { parseReconciliationNumber, reconciliationRows, reconciliationCsv, validReconciliationResponse } from './projectReconciliation.js'

export default function ProjectReconciliationPanel({ solarBinding, authorityKey, projectClient, isCurrent, busy = false }) {
  let bindingKey = null
  try { bindingKey = solarDrawingBindingKey(solarBinding) } catch { /* unavailable */ }
  const scope = useMemo(() => ({ binding: solarBinding, authorityKey, projectClient, isCurrent, alive: true }),
    [bindingKey, authorityKey, projectClient, isCurrent])
  const [view, setView] = useState(null)
  const [draft, setDraft] = useState(null)
  const request = useRef(null)
  const current = () => {
    try {
      const snapshot = projectClient.getSnapshot()
      return scope.alive && !!bindingKey && typeof authorityKey === 'string' && !!authorityKey.trim() &&
        typeof projectClient.readContext === 'function' && typeof projectClient.subscribe === 'function' &&
        isCurrent() === true && sameSolarDrawingBinding(snapshot?.binding, scope.binding) && snapshot.authorityKey === authorityKey
    } catch { return false }
  }
  useLayoutEffect(() => {
    scope.alive = true
    const invalidate = () => {
      if (current()) return
      scope.alive = false
      request.current?.abort()
      setView({ scope, graph: null })
      setDraft(null)
    }
    let unsubscribe
    try { unsubscribe = projectClient?.subscribe(invalidate) } catch { scope.alive = false }
    invalidate()
    return () => {
      scope.alive = false
      request.current?.abort()
      try { unsubscribe?.() } catch { /* observer isolation */ }
    }
  }, [scope])
  const live = current()
  const saved = live && view?.scope === scope ? view : null
  const drafts = live && draft?.scope === scope ? draft.values : ['', '', '', '', '']
  const rows = reconciliationRows(saved?.graph, drafts)
  const pending = saved?.pending === true
  const reportValid = drafts.every((value, index) => parseReconciliationNumber(value, index !== 4) !== null)
  const canExport = live && !busy && !pending && !!saved?.graph && reportValid
  async function read() {
    if (!current() || busy || pending) return
    request.current?.abort()
    const abort = new AbortController()
    request.current = abort
    setView({ scope, pending: true, graph: null, status: 'Reading saved design.' })
    try {
      const response = await projectClient.readContext({ signal: abort.signal })
      if (abort.signal.aborted || request.current !== abort || !current()) return
      if (!validReconciliationResponse(response, scope.binding)) {
        setView({ scope, status: 'Saved design unavailable: the response, drawing identity or graph is invalid.' })
        return
      }
      const graph = response.data.intake.solar_design_graph
      setView({ scope, graph, status: `Saved design revision ${graph.rev}.` })
    } catch {
      if (!abort.signal.aborted && request.current === abort && current()) {
        setView({ scope, status: 'Saved design could not be read. Try reading it again.' })
      }
    }
  }
  function download() {
    if (!canExport || !current()) return
    const url = URL.createObjectURL(new Blob([reconciliationCsv(rows)], { type: 'text/csv;charset=utf-8' }))
    try {
      if (!current()) return
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = 'engineering-reconciliation.csv'
      anchor.click()
    } finally { URL.revokeObjectURL(url) }
  }
  return <section aria-label="Engineering reconciliation">
    <h3>Engineering reconciliation</h3>
    <p>Enter user-supplied engineering report/BOM totals. These values are not independently verified.</p>
    <button type="button" disabled={!live || busy || pending} onClick={read}>Read saved design</button>
    <p aria-live="polite">{!live ? 'Reconciliation unavailable: the selected drawing scope is stale or unavailable.'
      : saved?.status || 'Read the saved design to compare totals.'}</p>
    <table className="ops-table"><caption>Saved Studio vs engineering report/BOM totals</caption>
      <thead><tr><th scope="col">Metric</th><th scope="col">Studio</th><th scope="col">Engineering report/BOM</th>
        <th scope="col">Delta</th><th scope="col">Verdict</th></tr></thead>
      <tbody>{rows.map((row, index) => <tr key={row.metric}>
        <th scope="row">{row.metric}</th><td>{row.studio || 'Unavailable'}</td>
        <td><input aria-label={`${row.metric} engineering report/BOM`} inputMode={index === 4 ? 'decimal' : 'numeric'}
          disabled={!live || busy || pending} value={drafts[index]} maxLength={37}
          aria-invalid={drafts[index] !== '' && parseReconciliationNumber(drafts[index], index !== 4) === null}
          onChange={(event) => {
            if (!current() || busy || pending) return
            const values = [...drafts]
            values[index] = event.target.value
            setDraft({ scope, values })
          }} /></td>
        <td>{row.delta || 'Unavailable'}</td><td>{row.verdict === 'UNREADABLE' ? 'Unavailable' : row.verdict}
          {row.reason && <small>: {row.reason}</small>}</td>
      </tr>)}</tbody>
    </table>
    <p>Delta is Studio minus the supplied report/BOM value.</p>
    <button type="button" disabled={!canExport} onClick={download}>Download CSV</button>
  </section>
}
