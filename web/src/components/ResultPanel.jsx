// Renders a Result envelope (CONTRACT §3): result data (counts table or
// key/value), overlay summary, timing + cost receipt. Plus calm live progress
// and a normalized error.
//
// The §10 error is an OBJECT ({error_code, message, retryable}); mock errors
// are plain strings. `errText`/`isRetryable` handle both so the same panel
// renders live and mock without ever passing a raw object to React.

function errText(e) {
  if (!e) return ''
  if (typeof e === 'string') return e
  const code = e.error_code ? `${e.error_code}: ` : ''
  return `${code}${e.message || 'error'}`
}

function isRetryable(e) {
  return !!(e && typeof e === 'object' && e.retryable)
}

// Calm, loader-free progress line: "submitted", then "running · 3.2s", and
// when the backend emits a richer step string, "running · storing version · 3.2s".
function progressText(runStatus, runElapsedMs, runProgress) {
  const status = runStatus || 'running'
  if (status === 'submitted') return 'submitted'
  const secs = runElapsedMs != null ? ` · ${(runElapsedMs / 1000).toFixed(1)}s` : ''
  // Only show the step when it adds information beyond the plain status.
  const step = (runProgress && runProgress !== status && runProgress !== 'running')
    ? ` · ${runProgress}`
    : ''
  return `${status}${step}${secs}`
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
            <td className="k">{k.replace(/_/g, ' ')}</td>
            <td className="v num">{typeof v === 'number' ? v.toLocaleString() : String(v)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function ResultBody({ result }) {
  const data = result?.result
  if (!data) return null
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

function ErrorLine({ err, onRetry, retry, quota }) {
  return (
    <div className={`inline-error ${quota ? 'quota' : ''}`}>
      <span>{errText(err)}</span>
      {retry && onRetry && (
        <button type="button" className="btn ghost retry" onClick={onRetry}>Retry</button>
      )}
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

export default function ResultPanel({ running, runStatus, runProgress, runElapsedMs, error, result, tool, onRetry }) {
  // A quota rejection (broker hard cap, HTTP 402) is an expected budget state,
  // not a failure to alarm about — render it in the amber calm posture (matching
  // QuotaCard), never red FAILED. Only this error_code softens; all others stay red.
  const isQuota = !!(result && !result.ok && result.error && result.error.error_code === 'quota_exceeded')
  // The coarse DAILY run-count limit (HTTP 429) rides in with the same quota
  // error_code but a distinct `quota_kind` — label it honestly ("DAILY LIMIT")
  // rather than "SPEND CAP". Still calm amber.
  const isDailyQuota = !!(result && result.quota_kind === 'daily_runs')
  // An entitlement rejection (HTTP 403 {entitlement_required}) is a plan boundary,
  // also calm amber (not a red failure). Same softening path as quota.
  const isEnt = !!(result && result.entitlement_required)
  const calm = isQuota || isEnt
  return (
    <section className="card result-panel">
      <h3>Result</h3>
      {!running && !result && !error && (
        <p className="panel-sub">Dispatch a prompt or run a tool to see its result and overlay here.</p>
      )}
      {running && (
        <div className="running">
          <span>{progressText(runStatus, runElapsedMs, runProgress)}{tool?.name ? ` · ${tool.name}` : ''}</span>
          <span className="bar" aria-hidden="true"><i /></span>
        </div>
      )}
      {/* transport-level error (network / submit failure) — always retryable */}
      {error && !running && <ErrorLine err={error} onRetry={onRetry} retry />}
      {result && !running && (
        <div className="result-card">
          <div className="result-head">
            <span className={`ok ${result.ok ? 'yes' : (calm ? 'quota' : 'no')}`}>
              {result.ok ? 'OK' : (isDailyQuota ? 'DAILY LIMIT' : (isQuota ? 'SPEND CAP' : (isEnt ? 'PLAN' : 'FAILED')))}
            </span>
            <span className="result-tool">{result.tool} <span className="dim">v{result.version}</span></span>
            {result.degraded_mode && (
              <span className="degraded" title="Ran on the local fallback, not the cloud solver">local fallback</span>
            )}
          </div>

          {result.ok && <ResultBody result={result} />}
          {result.error && <ErrorLine err={result.error} onRetry={onRetry} retry={!isEnt && isRetryable(result.error)} quota={calm} />}

          {result.overlay && (
            <div className="overlay-summary">
              {result.overlay.highlight_handles?.length > 0 && (
                <div className="ov-row">
                  <span className="ov-dot hl" />
                  {result.overlay.highlight_handles.length.toLocaleString()} panels highlighted in the viewer
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
                <div className="ov-row"><span className="ov-dot ov" />{result.overlay.polylines.length} overlay shapes</div>
              )}
            </div>
          )}

          {/* Itemized per-run cost receipt (B3): wall-clock, engine seconds, and
              dollars — each a distinct item. Tied to the header spend chip via the
              tooltip (this run rolls up into "today"). A zero / non-billable run
              reads as a clean "no cloud cost", never "$0.0000" (B1). */}
          <div
            className="receipt"
            title="This run’s cost — it rolls up into today’s spend (see the spend chip in the header)."
          >
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
