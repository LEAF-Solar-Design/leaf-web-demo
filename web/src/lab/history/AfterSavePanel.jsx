import { registry } from '../canvas/registry.js'
import { sentence } from '../honesty.js'
import { blockLine, count, decisionTime, ledgerLines, practiceMoney as money, registrationLines, scheduleLine, sideFigures, withheld, WINDOW_LINE } from './afterSaveHonesty.js'
import LivePredictionGap from '../changes/LivePredictionGap.jsx'

const fields = [
  ['trades', 'Trades'], ['net', 'Net after fees'],
  ['netPerDollar', 'Net per dollar risked'], ['worst', 'Worst Trade'],
  ['fees', 'Fees'], ['slippage', 'Slippage'],
]

function Side({ value = {}, side, title }) {
  const figures = sideFigures(value)
  return <article data-side={side} style={side ? { color: `var(--leaf-color-data-series-${side === 'candidate' ? 4 : 1})` } : undefined}>
    <h3>{title}</h3>
    <dl>{fields.map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{figures[key]}</dd></div>)}
      {['resized', 'skipped', 'new_buys'].map((key, index) => <div key={key}><dt>{['Resized', 'Skipped', 'New buys'][index]}</dt><dd>{count(value[key])}</dd></div>)}
    </dl>
    <p>Without best and worst: {figures.withoutBestWorst}</p>
  </article>
}

function Coverage({ data }) {
  const window = data.window || {}
  return <>Window {window.start ?? data.window_start ?? 'Not reported'}..{window.end ?? data.window_end ?? 'Not reported'}, {count(Array.isArray(data.coverage?.markets_covered) ? data.coverage.markets_covered.length : data.coverage?.markets_covered)} Markets, {count(data.coverage?.gaps)} gaps.</>
}

export default function AfterSavePanel({ view, document, ledger, registration, sourcesState }) {
  const data = view.match?.data || {}
  const finished = view.status === 'finished'
  const attribution = Array.isArray(data.attribution) ? data.attribution : []
  const hold = withheld(data)
  const loading = sourcesState === 'loading', error = sourcesState === 'error'
  const exchange = loading || error ? null : ledgerLines(ledger)
  return <section aria-label="After save">
    <h2>After save{view.saved ? ` · r${view.saved.revision}` : ''}</h2>
    <p>{WINDOW_LINE}</p>
    <p>{scheduleLine(data.fee_schedule_id)}</p>
    {view.status === 'dashboard_only' && <p>Practice runs from the ops dashboard (Practice this draft).</p>}
    {view.status === 'none' && <p>Save to run a Practice of the last 4 hours.</p>}
    {['pending', 'running'].includes(view.status) && <p role="status">Practice pending · {Math.floor(view.elapsed_ms / 1000)} s</p>}
    {finished && <>
      {hold ? <p role="alert">{hold.sentence}</p> : <>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 'var(--leaf-spacing-panel-padding)' }}>
        <Side side="candidate" title="Candidate (Simulated)" value={data.candidate} />
        <Side side="live" title="Live (Simulated)" value={data.live} />
      </div>
      <article><h3>Deltas (Simulated)</h3><p>Net after fees: {money(data.deltas?.net_after_fees_cents)}</p></article>
      </>}
      <p>{decisionTime(data.latency)}</p>
      {!view.changed && <p>Changed blocks unavailable</p>}
      <ul aria-label="Practice blocks">{(data.blocks || []).map(block => {
        const current = document?.blocks?.find(item => item.id === block.block_id)
        const saved = view.savedDocument?.blocks?.find(item => item.id === block.block_id)
        const changed = Boolean(view.changed?.blockIds.includes(block.block_id))
        const title = current?.title || registry[`${saved?.registry?.id || block.registry_id}@${saved?.registry?.version || '1.0.0'}`]?.title || saved?.registry?.id || block.registry_id || block.block_id
        return <li key={block.block_id} data-changed={changed ? 'true' : undefined}
          style={changed ? { outline: '2px solid var(--leaf-color-data-series-3)' } : undefined}>
          {title}: {blockLine(block)}
        </li>
      })}</ul>
      {!hold && <><h3>Per-Trade attribution</h3>
      <ul aria-label="Per-Trade attribution">{attribution.slice(0, 50).map((trade, index) => <li key={index}>
        {trade.trade_id ?? `Trade ${index + 1}`}: {trade.reason || trade.block_id || 'Not reported'}; Simulated Candidate {money(trade.candidate?.net_after_fees_cents ?? trade.candidate_cents)}; Simulated Live {money(trade.live?.net_after_fees_cents ?? trade.live_cents)}; Simulated Delta {money(trade.delta_cents)}
      </li>)}</ul>
      {(data.truncated || view.match?.truncated || attribution.length > 50) && <p>truncated</p>}
      </>}
    </>}
    <LivePredictionGap comparison={finished ? data : null} />
    {view.status === 'unavailable' && (data.reason?.startsWith('unsupported_block:')
      ? <p>No Practice replay exists for this block yet.</p>
      : <><p>{data.reason ?? 'Not reported'}</p><p>No statistics: Practice unavailable.</p></>)}
    {view.status === 'failed' && <p>Practice failed: {data.reason || 'Not reported'}</p>}
    {view.status === 'expired' && <p>Practice request expired.</p>}
    <section aria-label="Pre-registration" data-state={loading ? 'loading' : error ? 'error' : registration?.length ? 'value' : 'empty'}>
      <h3>Pre-registration</h3>
      {loading ? <p role="status">Loading Exchange ledger</p> : error ? <p role="alert">The Exchange ledger could not be loaded.</p> : registrationLines(registration).map((line, index) => <p key={index}>{line}</p>)}
    </section>
    <section aria-label="Exchange ledger" data-state={loading ? 'loading' : error ? 'error' : ledger?.length ? 'value' : 'empty'}>
      <h3>Exchange ledger</h3>
      {loading ? <p role="status">Loading Exchange ledger</p> : error ? <p role="alert">The Exchange ledger could not be loaded.</p> : <>
        {exchange.lines.map((line, index) => <p key={index}>{line}</p>)}
        {exchange.withheldDays.map((day, index) => <p key={index} data-day-utc={day.day_utc}>{day.sentence}</p>)}
      </>}
    </section>
    <footer>{sentence('replay_is_direction')} Simulated.{(finished || view.status === 'unavailable') && <> <Coverage data={data} /></>}</footer>
  </section>
}
