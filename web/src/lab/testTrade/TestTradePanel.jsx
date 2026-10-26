import { useState } from 'react'
import { Measurement } from '../canvas/LatencyPipe.jsx'
import { nsToMs, readTestTradeEvidence, stepStats } from './evidence.js'
import LatencyWaterfall from './LatencyWaterfall.jsx'

const tabs = ['Latest', 'History', 'Live vs Candidate', 'Budgets']
const money = value => value === undefined ? 'not supplied' : `${value} cents`
const time = value => value === null ? 'Time not supplied' : typeof value === 'number' ? String(value) : value
function Summary({ receipt }) {
  return <div>
    <p>Status {receipt.status} · Mode {receipt.mode ?? 'not supplied'}</p>
    <p>Fill count {receipt.fill?.fill_count ?? 'not supplied'}</p>
    <p>Fill price {money(receipt.fill?.price_cents)} · Fee {money(receipt.fill?.fee_cents)} · Cost {money(receipt.fill?.cost_cents)}</p>
    {receipt.unreadable && <p>Receipt unreadable</p>}
  </div>
}

export default function TestTradePanel({ hidden = false, provider, revision, requestTestTrade }) {
  const [tab, setTab] = useState('Latest')
  const [message, setMessage] = useState('')
  const [pending, setPending] = useState(false)
  // Re-read on each render, including same-provider revision announcements.
  const { receipts, histograms, stale, truncated } = readTestTradeEvidence(provider)
  const latest = receipts[0]
  const unavailable = 'Open the Lab inside the dashboard to place a Test trade'
  async function request() {
    if (pending) return
    setPending(true)
    try {
      const result = await requestTestTrade()
      setMessage(result?.ok ? 'Requested; the dashboard opens its Test trade card' : result?.reason || 'Request unavailable')
    } catch { setMessage('Request unavailable') }
    finally { setPending(false) }
  }
  return <section hidden={hidden} aria-label="Test trade evidence" data-revision={revision}>
    <h2>Test trade evidence</h2>
    {stale && <span data-badge>stale</span>} {truncated && <span data-badge>truncated</span>}
    <button type="button" disabled={!requestTestTrade || pending} onClick={request} title={!requestTestTrade ? unavailable : undefined}>Request Test trade</button>
    {!requestTestTrade && <p>{unavailable}</p>}
    {message && <p role="status">{message}</p>}
    <div role="tablist" aria-label="Test trade evidence tabs">{tabs.map(name => <button type="button" role="tab" key={name} aria-selected={tab === name} onClick={() => setTab(name)}>{name}</button>)}</div>
    <div role="tabpanel" aria-label={tab}>
      {tab === 'Latest' && (latest ? <><Summary receipt={latest} /><LatencyWaterfall receipt={latest} /></> : <p>No Test trades yet</p>)}
      {tab === 'History' && <>
        {!receipts.length && <p>No Test trades yet</p>}
        <ol>{receipts.map((receipt, index) => <li key={`${receipt.receipt_id}-${index}`}>{receipt.status} · {receipt.mode ?? 'not supplied'} · {time(receipt.finished_at ?? receipt.requested_at)}</li>)}</ol>
        {stepStats(receipts).map((stats, index) => <p key={index}>{stats.from ? `${stats.from} → ${stats.to}` : `Segment ${index + 1}`} · {stats.measuredCount} measured samples · {stats.notMeasuredCount} not measured · p50 {stats.p50_ms === null ? 'not measured' : `${stats.p50_ms} ms`} · p99 {stats.p99_ms === null ? 'not measured' : `${stats.p99_ms} ms`}</p>)}
      </>}
      {tab === 'Live vs Candidate' && <>
        <p>Each run was separately approved.</p>
        <div style={{ display: 'flex', gap: 24 }}>{['live', 'candidate'].map(mode => {
          const receipt = receipts.find(item => item.mode === mode)
          const label = mode === 'live' ? 'Live' : 'Candidate'
          return <section key={mode} aria-label={label}><h3>{label}</h3>{receipt ? <><p>Source {receipt.source_sha?.slice(0, 8) ?? 'not supplied'}</p><Summary receipt={receipt} /><LatencyWaterfall receipt={receipt} /></> : <p>No {label} run</p>}</section>
        })}</div>
      </>}
      {tab === 'Budgets' && <>
        {!histograms.length && <p>No budgets measured yet</p>}
        {histograms.map((histogram, index) => {
          const micros = value => value === null ? null : typeof value === 'number' ? value / 1000 : nsToMs(value) * 1000
          const budget = { p50: micros(histogram.budget_ns.p50), p99: micros(histogram.budget_ns.p99) }
          return <section key={index} aria-label={histogram.metric}><h3>{histogram.metric}</h3>
            <Measurement measurement={{ p50_us: micros(histogram.p50_ns), p99_us: micros(histogram.p99_ns), count: histogram.count }} budget={budget} />
            <p>Observed {histogram.observed_count} · Receipt {histogram.receipt_id} · p50 {histogram.over_budget.p50 ? 'over budget' : 'within budget'} · p99 {histogram.over_budget.p99 ? 'over budget' : 'within budget'}</p>
          </section>
        })}
      </>}
    </div>
  </section>
}
