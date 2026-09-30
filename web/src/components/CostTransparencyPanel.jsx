import { useEffect, useRef, useState } from 'react'
import { getCost } from '../api.js'
import './CostTransparencyPanel.css'

const TITLE = 'What Leaf costs to operate'
const COPY = 'This page shows what Leaf actually costs to run and your share of it. It is not a bill and does not change your plan, quotas, or limits.'
const dimensions = [['development', 'Development'], ['ci', 'CI'], ['fleet', 'Fleet'], ['unattributed', 'Unattributed']]
const payerLabels = { tenant_plan: 'Your Claude plan', tenant_api_key: 'Your own API key', leaf: 'Leaf', mixed: 'Mixed payers', unknown: 'Payer unknown' }

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

export default function CostTransparencyPanel({ mock = false }) {
  const currentMonth = new Date().toISOString().slice(0, 7)
  const [period, setPeriod] = useState(currentMonth)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState(false)
  const [retry, setRetry] = useState(0)
  const trigger = useRef(null)
  const closeButton = useRef(null)

  useEffect(() => {
    let active = true
    if (mock) { setData(null); return undefined }
    setLoading(true)
    setData(null)
    getCost(period).then((result) => { if (active) setData(result) }).catch(() => {
      if (active) setData(null)
    }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [mock, period, retry])

  useEffect(() => {
    if (open) closeButton.current?.focus()
  }, [open])

  const close = () => { setOpen(false); trigger.current?.focus() }
  if (mock) return null
  const resources = data?.resources || []
  const own = data?.own_use
  return (
    <div className="cost-transparency">
      <button type="button" className="chip-neutral" ref={trigger} aria-expanded={open}
        onClick={() => setOpen(!open)}>{TITLE}</button>
      {open && (
        <section className="cost-panel" aria-label={TITLE} onKeyDown={(event) => {
          if (event.key === 'Escape') { event.stopPropagation(); close() }
        }}>
          <button type="button" ref={closeButton} className="chip-neutral cost-close" onClick={close}>Close cost panel</button>
          <h2>{TITLE}</h2>
          <p>{COPY}</p>
          <label>Month <input type="month" value={period} max={currentMonth} onChange={(event) => {
            const value = event.target.value
            if (/^\d{4}-(0[1-9]|1[0-2])$/.test(value) && value <= currentMonth) setPeriod(value)
          }} /></label>
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
              <dt>CAD engine time and runs</dt><dd>{quantity(own?.cad?.engine_seconds)} seconds · {quantity(own?.cad?.runs)} runs</dd>
              <dt>Marathon runs</dt><dd>{quantity(own?.marathon?.runs)} <small>not added to totals</small></dd>
              <dt>Storage</dt><dd>Unavailable. Direct storage use is not published.</dd>
            </dl>
            <h3>Leaf's costs and your share</h3>
            {data.stale && <p role="status">Cost publication is stale. {data.stale_reason || 'Recent use may be missing.'}</p>}
            {!data.publication_id ? <p>{data.degraded_mode ? 'Cost data for this month is unreadable. Publication details are unavailable.' : 'The publication for this month has not been made yet.'}</p> : <>
              <div className="cost-table-scroll" role="region" aria-label="Resource costs" tabIndex={0}>
                <table>
                  <caption>Monthly resource use and costs (USD)</caption>
                  <thead><tr>{['Resource', 'Total used', 'Gross cost', 'Credits', 'Your share', 'Your implied amount', "Leaf’s own share", 'Other customers combined'].map((heading) => <th key={heading} scope="col">{heading}</th>)}</tr></thead>
                  <tbody>{resources.map((row) => <tr key={row.resource_id}>
                    <th scope="row">{row.display_name}<small>{row.status} · {row.coverage}</small>
                      {row.status === 'ESTIMATED' && <small>{row.unit === 'usd-by-environment' ? 'Split by environment tag and activity' : 'Estimated share, not metered'}</small>}
                    </th>
                    <td>{row.physical_usage ? <>
                      <div>{quantity(row.physical_usage.quantity)} {row.physical_usage.unit}</div>
                      <small>Physical use coverage: {row.physical_usage.coverage}</small>
                      <small>Allocation basis: {quantity(row.total_usage)} {row.unit}</small>
                    </> : <>{quantity(row.total_usage)} {row.unit}</>}</td>
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
              <p className="cost-footnote">Missing sources: {data.missing_sources?.length ? data.missing_sources.join(', ') : 'None reported'}. Published: {data.published_at || 'Unavailable'}.</p>
            </>}
          </>}
        </section>
      )}
    </div>
  )
}
