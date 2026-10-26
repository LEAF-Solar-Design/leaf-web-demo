import { useEffect, useRef, useState } from 'react'
import { authHeaders, config, getCost } from '../api.js'
import './CostTransparencyPanel.css'
import useEscapeOwner from '../lib/useEscapeOwner.js'
import { isCompositionKey } from '../lib/keyComposition.js'

const TITLE = 'What Leaf costs to operate'
const COPY = 'This page shows what Leaf actually costs to run and your share of it. It is not a bill and does not change your plan, quotas, or limits.'
const dimensions = [['development', 'Development'], ['ci', 'CI'], ['fleet', 'Fleet'], ['unattributed', 'Unattributed']]
const payerLabels = { tenant_plan: 'Your Claude plan', tenant_api_key: 'Your own API key', leaf: 'Leaf', mixed: 'Mixed payers', unknown: 'Payer unknown' }
const statusLabels = { MEASURED: 'Measured', ESTIMATED: 'Estimated' }
const historyStatusLabels = { published: 'Published', unpublished: 'Not yet published', unavailable: 'Unavailable' }
const coverageLabels = { complete: 'Complete coverage', partial: 'Some use may be missing', unknown: 'Coverage unknown' }
const unitLabels = {
  'engine-second': 'seconds of design processing',
  'GB-month': 'gigabyte months',
  'gb-month': 'gigabyte months',
  'build-minutes': 'build minutes',
  'instance-hours': 'server hours',
  'usd-by-environment': 'US dollars split by environment',
  share: 'of the total share',
}
const displayLabel = (labels, value, fallback) => Object.prototype.hasOwnProperty.call(labels, value) ? labels[value] : fallback
const coverageLabel = (value) => displayLabel(coverageLabels, value, 'Coverage unknown')
const unitLabel = (value) => displayLabel(unitLabels, value, 'Units unavailable')

function publicationDate(value) {
  if (!value) return 'Unavailable'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return 'Unavailable'
  return date.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
}

// Decimal strings never pass through binary floating point, including totals.
function decimal(value) {
  if (typeof value !== 'string' || !/^\d+(\.\d+)?$/.test(value)) return null
  const [whole, fraction = ''] = value.split('.')
  return { n: BigInt(whole + fraction), scale: fraction.length }
}
function fixed(value, places, shift = 0) {
  const d = decimal(value)
  if (!d) return 'Unavailable'
  const power = places + shift - d.scale
  const divisor = power < 0 ? 10n ** BigInt(-power) : 1n
  const n = power < 0 ? (d.n + divisor / 2n) / divisor : d.n * 10n ** BigInt(power)
  const digits = n.toString().padStart(places + 1, '0')
  return places ? `${digits.slice(0, -places)}.${digits.slice(-places)}` : digits
}
const money = (value) => decimal(value) ? `$${fixed(value, 6).replace(/(\.\d{2}\d*?)0+$/, '$1')}` : 'Unavailable'
const share = (value) => decimal(value) ? `${fixed(value, 6)} (${fixed(value, 4, 2)}%)` : 'Unavailable'
const quantity = (value) => typeof value === 'number' || decimal(value) ? String(value) : 'Unavailable'

function shiftMonth(period, delta) {
  const [year, month] = period.split('-').map(Number)
  const date = new Date(Date.UTC(year, month - 1 + delta, 1))
  return date.toISOString().slice(0, 7)
}

async function getCostHistory(from, to) {
  try {
    const response = await fetch(`${config.apiBase}/api/cost/history?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`, {
      headers: { 'X-Tenant-Id': config.tenant, ...authHeaders() },
    })
    if (!response.ok) return null
    return await response.json()
  } catch {
    return null
  }
}

function MonthInput({ value, min, max, onCommit, onCancel }) {
  const [draft, setDraft] = useState(value)
  const inputRef = useRef(null)
  const cancelled = useRef(false)
  useEffect(() => { setDraft(value) }, [value])
  // Handle Escape before the panel returns focus and blur can commit the draft.
  useEscapeOwner('cost-month', true, () => {
    cancelled.current = true
    setDraft(value)
    onCancel()
  }, { layer: 'menu', scope: inputRef, scoped: true })
  const commit = () => {
    if (cancelled.current) return
    if (/^\d{4}-(0[1-9]|1[0-2])$/.test(draft) && (!min || draft >= min) && (!max || draft <= max)) {
      onCommit(draft)
    } else {
      setDraft(value)
    }
  }
  return <input ref={inputRef} type="month" value={draft} min={min} max={max}
    onChange={(event) => { cancelled.current = false; setDraft(event.target.value) }} onBlur={commit}
    onKeyDown={(event) => {
      if (event.key === 'Enter' && !isCompositionKey(event)) { event.preventDefault(); commit() }
    }} />
}

function HistoryView({ data, loading, from, to, currentMonth, onFrom, onTo, onRetry, onCancel }) {
  if (loading) return <p role="status">Loading cost history...</p>
  if (!data) return <>
    <p role="status">Cost history is unavailable.</p>
    <button type="button" className="chip-neutral" onClick={onRetry}>Retry history</button>
  </>
  const earliestFrom = shiftMonth(to, -23)
  const latestAllowedTo = shiftMonth(from, 23)
  const latestTo = latestAllowedTo < currentMonth ? latestAllowedTo : currentMonth
  return <>
    <div>
      <label>From month <MonthInput value={from} min={earliestFrom} max={to} onCommit={onFrom} onCancel={onCancel} /></label>
      <label>To month <MonthInput value={to} min={from} max={latestTo} onCommit={onTo} onCancel={onCancel} /></label>
    </div>
    <h3>Months</h3>
    <ul aria-label="Cost history months">{(data.months || []).map((month) => <li key={month.period}>
      {month.period}: {displayLabel(historyStatusLabels, month.status, 'Status unavailable')}. Your implied amount: {money(month.your_implied_cost_usd)}
      {month.provisional && ' (provisional)'}
    </li>)}</ul>
    <h3>Cumulative resource costs</h3>
    <div className="cost-table-scroll" role="region" aria-label="Cumulative resource costs" tabIndex={0}>
      <table>
        <caption>Cumulative resource costs and your implied amounts (USD)</caption>
        <thead><tr><th scope="col">Resource</th><th scope="col">Gross cost</th><th scope="col">Credits</th><th scope="col">Your implied amount</th><th scope="col">Monthly shares</th></tr></thead>
        <tbody>{(data.resources || []).map((row) => <tr key={row.resource_id}>
          <th scope="row">{row.display_name}</th>
          <td>{money(row.gross_cost_usd)}</td>
          <td>{money(row.credits_usd)}</td>
          <td>{money(row.your_implied_cost_usd)}</td>
          <td>{(row.monthly_shares || []).map((entry) => <div key={entry.period}>{entry.period}: {share(entry.your_share)}</div>)}</td>
        </tr>)}</tbody>
        <tfoot><tr><th scope="row">Cumulative totals</th><td>{money(data.totals?.gross_cost_usd)}</td><td>{money(data.totals?.credits_usd)}</td><td>{money(data.totals?.your_implied_cost_usd)}</td><td>Not applicable</td></tr></tfoot>
      </table>
    </div>
  </>
}

export default function CostTransparencyPanel({ mock = false, readsPaused = false }) {
  const currentMonth = new Date().toISOString().slice(0, 7)
  const [period, setPeriod] = useState(currentMonth)
  const [view, setView] = useState('monthly')
  const [historyFrom, setHistoryFrom] = useState(shiftMonth(currentMonth, -2))
  const [historyTo, setHistoryTo] = useState(currentMonth)
  const [history, setHistory] = useState(null)
  const [historyLoading, setHistoryLoading] = useState(false)
  const [historyRetry, setHistoryRetry] = useState(0)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState(false)
  const [retry, setRetry] = useState(0)
  const trigger = useRef(null)
  const closeButton = useRef(null)
  const panelRef = useRef(null)

  // A known guest keeps the panel and its button but makes no cost reads.
  useEffect(() => {
    let active = true
    if (mock || readsPaused) { setData(null); setLoading(false); return undefined }
    setLoading(true)
    setData(null)
    getCost(period).then((result) => { if (active) setData(result) }).catch(() => {
      if (active) setData(null)
    }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [mock, readsPaused, period, retry])

  useEffect(() => {
    let active = true
    if (mock || readsPaused) { setHistory(null); setHistoryLoading(false); return undefined }
    if (view !== 'history') return undefined
    setHistoryLoading(true)
    setHistory(null)
    getCostHistory(historyFrom, historyTo).then((result) => { if (active) setHistory(result) }).finally(() => {
      if (active) setHistoryLoading(false)
    })
    return () => { active = false }
  }, [mock, readsPaused, view, historyFrom, historyTo, historyRetry])

  useEffect(() => {
    if (open) closeButton.current?.focus()
  }, [open])

  // KEYS-b: the owner below is not focus-scoped, so Escape can arrive while focus sits in a control outside
  // the panel. Closing returns focus to the trigger only from inside the panel or from nowhere: taking it
  // from a control that is mid-edit would blur that control and commit what the key should let it discard.
  const close = () => {
    setOpen(false)
    const active = document.activeElement
    if (!active || active === document.body || panelRef.current?.contains(active)) trigger.current?.focus()
  }
  // S27: Escape while the panel is open is the owner stack's (menu layer).
  // KEYS-b: the owner is NOT focus-scoped. An open panel is a dismissible
  // overlay, so it owns Escape wherever focus sits, its own trigger
  // included; scoped, a key from the trigger fell to whatever was open
  // below it. The scope still orders it against a nested owner.
  useEscapeOwner('cost-panel', open && !mock, close, { layer: 'menu', scope: panelRef })
  if (mock) return null
  const resources = data?.resources || []
  const own = data?.own_use
  return (
    <div className="cost-transparency">
      <button type="button" className="chip-neutral" ref={trigger} aria-expanded={open}
        onClick={() => setOpen(!open)}>{TITLE}</button>
      {open && (
        <section ref={panelRef} className="cost-panel" aria-label={TITLE}>
          <button type="button" ref={closeButton} className="chip-neutral cost-close" onClick={close}>Close cost panel</button>
          <h2>{TITLE}</h2>
          <p>{COPY}</p>
          {readsPaused ? <p role="status">Sign in to view your costs and cost history.</p> : <>
          <div role="group" aria-label="Cost view">
            <button type="button" className="chip-neutral" aria-pressed={view === 'monthly'} onClick={() => setView('monthly')}>Monthly</button>
            <button type="button" className="chip-neutral" aria-pressed={view === 'history'} onClick={() => setView('history')}>History</button>
          </div>
          {view === 'history' ? <HistoryView data={history} loading={historyLoading}
            from={historyFrom} to={historyTo} currentMonth={currentMonth}
            onFrom={(value) => { if (/^\d{4}-(0[1-9]|1[0-2])$/.test(value) && value >= shiftMonth(historyTo, -23) && value <= historyTo) setHistoryFrom(value) }}
            onTo={(value) => { if (/^\d{4}-(0[1-9]|1[0-2])$/.test(value) && value >= historyFrom && value <= shiftMonth(historyFrom, 23) && value <= currentMonth) setHistoryTo(value) }}
            onRetry={() => setHistoryRetry((value) => value + 1)} onCancel={close} /> : <>
          <label>Month <MonthInput value={period} max={currentMonth} onCommit={setPeriod} onCancel={close} /></label>
          {loading ? <p role="status">Loading costs…</p> : !data ? <>
            <p role="status">Costs are unavailable for this month.</p>
            <button type="button" className="chip-neutral" onClick={() => setRetry((value) => value + 1)}>Retry costs</button>
          </> : <>
            <h3>Your use</h3>
            {[['llm', 'LLM'], ['cad', 'CAD'], ['marathon', 'Marathon']].map(([key, label]) => own?.[key]?.coverage !== 'complete' && (
              <p key={key}>{label} use coverage: {own?.[key]?.coverage === 'partial' ? 'partial. Some use may be missing.' : 'unknown. Complete use could not be determined.'}</p>
            ))}
            <dl className="cost-use">
              <dt>LLM turns</dt><dd>{quantity(own?.llm?.turns)}</dd>
              <dt>Tokens</dt><dd>Input: {quantity(own?.llm?.tokens?.input)} · Output: {quantity(own?.llm?.tokens?.output)} · Cache read: {quantity(own?.llm?.tokens?.cache_read)} · Cache write: {quantity(own?.llm?.tokens?.cache_write)}</dd>
              <dt>LLM API-equivalent value</dt><dd>{money(own?.llm?.usd_est)} <span>{own?.llm?.payer === 'tenant_plan' ? 'Covered by your Claude plan' : own?.llm?.payer === 'tenant_api_key' ? 'Paid through your own API key' : 'Payer may vary or is unknown'}</span>
                {Object.entries(own?.llm?.by_payer || {}).map(([payer, bucket]) => <div key={payer}>
                  {payerLabels[payer] || payerLabels.unknown}: {quantity(bucket?.turns)} {bucket?.turns === 1 ? 'turn' : 'turns'} · {money(bucket?.usd_est)} API-equivalent value
                </div>)}
              </dd>
              <dt>CAD processing time and runs</dt><dd>{quantity(own?.cad?.engine_seconds)} seconds · {quantity(own?.cad?.runs)} runs</dd>
              <dt>Marathon runs</dt><dd>{quantity(own?.marathon?.runs)} <small>not added to totals</small></dd>
              <dt>Storage</dt><dd>Unavailable. Direct storage use is not published.</dd>
            </dl>
            <h3>Leaf's costs and your share</h3>
            {data.stale && <p role="status">Cost publication is stale. Recent use may be missing.</p>}
            {!data.publication_id ? <p>{data.degraded_mode ? 'Cost data for this month is unreadable. Publication details are unavailable.' : 'The publication for this month has not been made yet.'}</p> : <>
              <div className="cost-table-scroll" role="region" aria-label="Resource costs" tabIndex={0}>
                <table>
                  <caption>Monthly resource use and costs (USD)</caption>
                  <thead><tr>{['Resource', 'Total used', 'Gross cost', 'Credits', 'Your share', 'Your implied amount', "Leaf’s own share", 'Other customers combined'].map((heading) => <th key={heading} scope="col">{heading}</th>)}</tr></thead>
                  <tbody>{resources.map((row) => <tr key={row.resource_id}>
                    <th scope="row">{row.display_name}<small>{displayLabel(statusLabels, row.status, 'Status unknown')} · {coverageLabel(row.coverage)}</small>
                      {row.status === 'ESTIMATED' && <small>{row.unit === 'usd-by-environment' ? 'Split by environment tag and activity' : 'Estimated share, not metered'}</small>}
                    </th>
                    <td>{row.physical_usage ? <>
                      <div>{quantity(row.physical_usage.quantity)} {unitLabel(row.physical_usage.unit)}</div>
                      <small>Physical use coverage: {coverageLabel(row.physical_usage.coverage)}</small>
                      <small>Allocation basis: {quantity(row.total_usage)} {unitLabel(row.unit)}</small>
                    </> : <>{quantity(row.total_usage)} {unitLabel(row.unit)}</>}</td>
                    <td>{money(row.gross_cost_usd)}</td>
                    <td>{money(row.credits_usd)}<small>covered by credits</small></td>
                    <td>{share(row.your_share)}</td>
                    <td>{money(row.your_implied_cost_usd)}</td>
                    <td>{dimensions.map(([key, label]) => <div key={key}>{label}: {share(row.leaf_share?.[key])}</div>)}</td>
                    <td>{share(row.other_customers_share)}</td>
                  </tr>)}</tbody>
                  <tfoot><tr><th scope="row">Totals</th><td>Not applicable</td><td>{money(data.totals?.gross_cost_usd)}</td><td>{money(data.totals?.credits_usd)}</td><td>Not applicable</td><td>{money(data.your_total_implied_cost_usd)}</td><td>Not applicable</td><td>Not applicable</td></tr></tfoot>
                </table>
              </div>
              <p className="cost-footnote">Missing sources: {data.missing_sources?.length ? `${data.missing_sources.length} unavailable` : 'None reported'}. Published: {publicationDate(data.published_at)}.</p>
            </>}
          </>}
          </>}
          </>}
        </section>
      )}
    </div>
  )
}
