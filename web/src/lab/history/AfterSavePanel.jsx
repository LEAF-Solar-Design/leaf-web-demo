import { registry } from '../canvas/registry.js'
import { formatMoney } from '../widgets/widgetResolve.js'

const money = value => {
  try { return formatMoney(value == null ? null : String(value)) } catch { return 'Not reported' }
}
const decimal = value => value == null || !Number.isFinite(Number(value)) ? 'Not reported' : Number(value).toFixed(2)
const count = value => value == null ? 'Not reported' : String(value)
const fields = [
  ['trades', 'Trades', count],
  ['net_after_fees_cents', 'Net after fees', money],
  ['net_per_dollar_risked', 'Net per dollar risked', decimal],
  ['worst_trade_cents', 'Worst Trade', money],
  ['resized', 'Resized', count], ['skipped', 'Skipped', count], ['new_buys', 'New buys', count],
]

function Side({ value = {}, side, title }) {
  return <article data-side={side} style={side ? { color: `var(--leaf-color-data-series-${side === 'candidate' ? 4 : 1})` } : undefined}>
    <h3>{title}</h3>
    <dl>{fields.map(([key, label, format]) => <div key={key}><dt>{label}</dt><dd>{format(value[key])}</dd></div>)}
      <div><dt>Fees</dt><dd>{money(value.costs?.fees_cents)}</dd></div>
      <div><dt>Slippage</dt><dd>{money(value.costs?.slippage_cents)}</dd></div>
    </dl>
    <p>Without best and worst Trade: {money(value.without_best_and_worst_cents)}</p>
  </article>
}

function Coverage({ data }) {
  const window = data.window || {}
  return <>Window {window.start ?? data.window_start ?? 'Not reported'}..{window.end ?? data.window_end ?? 'Not reported'}, {count(data.coverage?.markets_covered)} Markets, {count(data.coverage?.gaps)} gaps.</>
}

export default function AfterSavePanel({ view, document }) {
  const data = view.match?.data || {}
  const finished = view.status === 'finished'
  const attribution = Array.isArray(data.attribution) ? data.attribution : []
  return <section aria-label="After save">
    <h2>After save{view.saved ? ` · r${view.saved.revision}` : ''}</h2>
    {view.status === 'none' && <p>Save to run a Practice of the last 4 hours.</p>}
    {['pending', 'running'].includes(view.status) && <p role="status">Practice pending · {Math.floor(view.elapsed_ms / 1000)} s</p>}
    {finished && <>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 'var(--leaf-spacing-panel-padding)' }}>
        <Side side="candidate" title="Candidate (simulated)" value={data.candidate} />
        <Side side="live" title="Live" value={data.live} />
      </div>
      <Side title="Deltas" value={data.deltas} />
      <p>p99 decision time {data.latency?.p99_us == null ? 'Not reported' : decimal(Number(data.latency.p99_us) / 1000)} ms</p>
      {!view.changed && <p>Changed blocks unavailable</p>}
      <ul aria-label="Practice blocks">{(data.blocks || []).map(block => {
        const current = document?.blocks?.find(item => item.id === block.block_id)
        const saved = view.savedDocument?.blocks?.find(item => item.id === block.block_id)
        const changed = Boolean(view.changed?.blockIds.includes(block.block_id))
        const title = current?.title || registry[`${saved?.registry?.id || block.registry_id}@${saved?.registry?.version || '1.0.0'}`]?.title || saved?.registry?.id || block.registry_id || block.block_id
        return <li key={block.block_id} data-changed={changed ? 'true' : undefined}
          style={changed ? { outline: '2px solid var(--leaf-color-data-series-3)' } : undefined}>
          {title}: {block.triggered === 0 ? 'triggered 0 times in the window' : `triggered ${count(block.triggered)}`}, reached {count(block.reached)}
        </li>
      })}</ul>
      <h3>Per-Trade attribution</h3>
      <ul aria-label="Per-Trade attribution">{attribution.slice(0, 50).map((trade, index) => <li key={index}>
        {trade.trade_id ?? `Trade ${index + 1}`}: {trade.reason || trade.block_id || 'Not reported'}; Candidate {money(trade.candidate?.net_after_fees_cents ?? trade.candidate_cents)}; Live {money(trade.live?.net_after_fees_cents ?? trade.live_cents)}; Delta {money(trade.delta_cents)}
      </li>)}</ul>
      {(data.truncated || view.match?.truncated || attribution.length > 50) && <p>truncated</p>}
    </>}
    {view.status === 'unavailable' && <><p>{data.reason}</p><p>No statistics: tape unavailable.</p></>}
    {view.status === 'failed' && <p>Practice failed: {data.reason || 'Not reported'}</p>}
    {view.status === 'expired' && <p>Practice request expired.</p>}
    <footer>Simulated, not a verdict.{(finished || view.status === 'unavailable') && <> <Coverage data={data} /></>}</footer>
  </section>
}
