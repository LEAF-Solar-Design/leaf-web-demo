import { useEffect } from 'react'
import { humanKey } from '../labels.js'
import { errorActorLabel, errorPresentation } from '../errorPresentation.js'

// Renders a Result envelope (CONTRACT §3): result data (counts table or
// key/value), overlay summary, timing + cost receipt, and a normalized error
// in the X1 anatomy. Live run progress rides the SB3 running strip above the
// docked command bar (App-level) — the card here is result DATA only.
//
// The §10 error is an OBJECT ({error_code, message, retryable}); mock errors
// are plain strings. `errParts`/`isRetryable` handle both so the same panel
// renders live and mock without ever passing a raw object to React.

// Split an error into a plain sentence + a demoted mono code (X1: the sentence
// names what failed; the raw error_code never leads).
function errParts(e) {
  return errorPresentation(e, 'error')
}

function isRetryable(e) {
  return !!(e && typeof e === 'object' && e.retryable)
}

function CountsTable({ counts }) {
  const rows = Object.entries(counts)
  return (
    <table className="counts">
      <thead><tr><th>Layer</th><th>Count</th></tr></thead>
      <tbody>
        {rows.map(([k, v]) => (
          <tr key={k}><td>{k}</td><td className="num">{Number(v).toLocaleString()}</td></tr>
        ))}
      </tbody>
    </table>
  )
}

function KeyValue({ data }) {
  return (
    <table className="kv">
      <tbody>
        {Object.entries(data).map(([k, v]) => (
          <tr key={k}>
            <td className="k">{humanKey(k)}</td>
            <td className="v num">{typeof v === 'number' ? v.toLocaleString() : String(v)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

// Generic table: result.table = { columns: string[], rows: any[][] }. Used by tools whose
// output is a list of inspection rows, rendered before the scalar key/values.
function GridTable({ table }) {
  const cols = Array.isArray(table.columns) ? table.columns : []
  const rows = Array.isArray(table.rows) ? table.rows : []
  return (
    <table className="counts grid">
      <thead><tr>{cols.map((c, i) => <th key={i}>{String(c)}</th>)}</tr></thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i}>
            {(Array.isArray(r) ? r : [r]).map((v, j) => (
              <td key={j} className={typeof v === 'number' ? 'num' : ''}>
                {typeof v === 'number' ? v.toLocaleString() : String(v ?? '')}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  )
}

// result.files = [{ name, mime, base64 }]: the tool hands back files (CSV, PDF). The bytes
// are already in the envelope, so the link is a blob URL built in the browser; nothing is
// fetched. URLs are revoked when the result changes.
function FileLinks({ files }) {
  const list = (Array.isArray(files) ? files : []).filter((f) => f && typeof f.base64 === 'string' && f.name)
  if (list.length === 0) return null
  return (
    <div className="files">
      {list.map((f) => {
        let href = ''
        try {
          const bin = atob(f.base64)
          const bytes = new Uint8Array(bin.length)
          for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
          href = URL.createObjectURL(new Blob([bytes], { type: f.mime || 'application/octet-stream' }))
        } catch {
          return <span key={f.name} className="dim">{f.name} (unreadable)</span>
        }
        return (
          <a key={f.name} className="btn ghost" href={href} download={String(f.name).replace(/[^\w.-]/g, '_')}>
            Download {f.name}
          </a>
        )
      })}
    </div>
  )
}

function ResultBody({ result }) {
  const data = result?.result
  if (!data) return null
  if (data.table && typeof data.table === 'object') {
    const scalars = {}
    for (const [k, v] of Object.entries(data)) {
      if (v !== null && typeof v === 'object') continue
      scalars[k] = v
    }
    return (
      <>
        <GridTable table={data.table} />
        {Array.isArray(data.warnings) && data.warnings.length > 0 && (
          <ul className="warnings">{data.warnings.map((w, i) => <li key={i}>{String(w)}</li>)}</ul>
        )}
        <FileLinks files={data.files} />
        {Object.keys(scalars).length > 0 && <KeyValue data={scalars} />}
      </>
    )
  }
  if (data.counts && typeof data.counts === 'object') {
    return (
      <>
        <CountsTable counts={data.counts} />
        {'total' in data && <div className="total">Total: <b>{Number(data.total).toLocaleString()}</b></div>}
      </>
    )
  }
  // generic: split scalars from nested
  const scalars = {}
  for (const [k, v] of Object.entries(data)) {
    if (v !== null && typeof v === 'object') continue
    scalars[k] = v
  }
  return <KeyValue data={scalars} />
}

// X1 failed-act row: plain sentence naming what failed (code demoted to a
// quiet mono suffix), Retry chip carrying its mnemonic keycap (R — bound at
// the panel level while the row is visible), and an honest fallback note.
// The calm quota/entitlement variants keep the broker's own sentence — a
// budget/plan boundary is not a failure to re-phrase.
function ErrorLine({ err, onRetry, retry, quota, toolName }) {
  const { message, code, nextAction, actor } = errParts(err)
  return (
    <div className={`inline-error ${quota ? 'quota' : ''}`}>
      <span>
        {quota ? message : `Couldn't run ${toolName || 'the tool'} — ${message}`}
        {!quota && code && <> <code className="dim">{code}</code></>}
      </span>
      {nextAction && <span className="dim">Next: {nextAction}</span>}
      {actor && <span className="key">{errorActorLabel(actor)}</span>}
      {retry && onRetry && (
        <>
          <button type="button" className="btn ghost retry" onClick={onRetry}>Retry</button>
          <span className="key" aria-hidden="true">R</span>
        </>
      )}
      {!quota && <span className="dim">Your drawing is unchanged.</span>}
    </div>
  )
}

// Format a per-run cost in dollars (e.g. "$0.0083"). Small live runs need 4
// decimals to read as non-zero; larger ones stay legible at 2. A ZERO or
// non-finite cost (a mock run, or a live run that did no billable cloud work)
// returns null so the caller shows a clean "no cloud cost" — never "$0.0000"
// (B1). usd_est is a Number or a numeric string in the §3 envelope.
function fmtUsd(v) {
  const n = Number(v)
  if (!Number.isFinite(n) || n <= 0) return null
  return n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`
}

// Note: run progress (runStatus / runProgress / runElapsedMs) moved to the
// SB3 running strip at the bar dock; callers may still pass them — ignored here.
// `notices` is the NR banner slot: ongoing-condition banners dock UNDER the
// header of the pane they affect, so App passes them in and they render
// immediately after the <h3> — never above the pane.
export default function ResultPanel({ running, error, result, tool, onRetry, notices }) {
  // A quota rejection (broker hard cap, HTTP 402) is an expected budget state,
  // not a failure to alarm about — render it in the amber calm posture (matching
  // QuotaCard), never red 'Failed'. Only this error_code softens; all others stay red.
  const isQuota = !!(result && !result.ok && result.error && result.error.error_code === 'quota_exceeded')
  // The coarse DAILY run-count limit (HTTP 429) rides in with the same quota
  // error_code but a distinct `quota_kind` — label it honestly ("Daily limit")
  // rather than "Spend cap". Still calm amber.
  const isDailyQuota = !!(result && result.quota_kind === 'daily_runs')
  // An entitlement rejection (HTTP 403 {entitlement_required}) is a plan boundary,
  // also calm amber (not a red failure). Same softening path as quota.
  const isEnt = !!(result && result.entitlement_required)
  const calm = isQuota || isEnt

  // X1: the Retry chip carries its key — R retries while a retryable error row
  // is visible. Skipped while typing in a field and while a run is in flight;
  // the condition mirrors the `retry` props on the two ErrorLine sites below.
  const canRetryKey = !running && !!onRetry &&
    (!!error || !!(result && result.error && !isEnt && isRetryable(result.error)))
  useEffect(() => {
    if (!canRetryKey) return undefined
    const onKey = (e) => {
      if (e.key !== 'r' && e.key !== 'R') return
      if (e.metaKey || e.ctrlKey || e.altKey) return
      const t = e.target
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
      e.preventDefault()
      onRetry()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [canRetryKey, onRetry])

  return (
    <section className="card result-panel">
      <h3>Result</h3>
      {notices}
      {!running && !result && !error && (
        <p className="panel-sub">
          Run a tool or type what you want in the bar below — the result and its
          drawing markup appear here.
          {' '}<span className="key">⌘K</span>
        </p>
      )}
      {/* Live progress rides the SB3 running strip above the docked bar
          (App-level) — nothing renders here while a run is in flight. */}
      {/* transport-level error (network / submit failure) — always retryable */}
      {error && !running && <ErrorLine err={error} onRetry={onRetry} retry toolName={tool?.name} />}
      {result && !running && (
        <div className="result-card">
          <div className="result-head">
            <span className={`ok ${result.ok ? 'yes' : (calm ? 'quota' : 'no')}`}>
              {result.ok ? 'Passed' : (isDailyQuota ? 'Daily limit' : (isQuota ? 'Spend cap' : (isEnt ? 'Plan' : 'Failed')))}
            </span>
            <span className="result-tool">{result.tool} <span className="dim">v{result.version}</span></span>
            {result.degraded_mode && (
              <span className="degraded">local fallback</span>
            )}
          </div>

          {result.ok && <ResultBody result={result} />}
          {result.error && <ErrorLine err={result.error} onRetry={onRetry} retry={!isEnt && isRetryable(result.error)} quota={calm} toolName={result.tool} />}

          {result.overlay && (
            <div className="overlay-summary">
              {result.overlay.highlight_handles?.length > 0 && (
                <div className="ov-row">
                  <span className="ov-dot hl" />
                  {result.overlay.highlight_handles.length.toLocaleString()} panel{result.overlay.highlight_handles.length === 1 ? '' : 's'} highlighted in the viewer
                </div>
              )}
              {result.overlay.markers?.length > 0 && (
                <div className="ov-row">
                  <span className="ov-dot mk" />
                  {result.overlay.markers.map((m, i) => (
                    <span key={i} className="marker-label">{m.label || `marker ${i + 1}`}</span>
                  ))}
                </div>
              )}
              {result.overlay.polylines?.length > 0 && (
                <div className="ov-row"><span className="ov-dot ov" />{result.overlay.polylines.length} overlay shape{result.overlay.polylines.length === 1 ? '' : 's'}</div>
              )}
            </div>
          )}

          {/* Itemized per-run cost receipt (B3): wall-clock, engine seconds, and
              dollars — each a distinct item. No tooltip (T4: tooltips only on
              unlabeled things — this row is its own label); the roll-up-into-
              today's-spend sentence lives in the run's Details drawer. A zero /
              non-billable run reads as a clean "no cloud cost", never
              "$0.0000" (B1). */}
          <div className="receipt">
            <span>{result.timing_ms} ms</span>
            {result.cost ? (
              <>
                <span className="dim">·</span>
                <span>engine {result.cost.engine_seconds}s</span>
                <span className="dim">·</span>
                {fmtUsd(result.cost.usd_est)
                  ? <b className="usd">{fmtUsd(result.cost.usd_est)}</b>
                  : <span className="dim">no cloud cost</span>}
              </>
            ) : (
              <>
                <span className="dim">·</span>
                <span className="dim">no cloud cost (mock)</span>
              </>
            )}
          </div>
        </div>
      )}
    </section>
  )
}
