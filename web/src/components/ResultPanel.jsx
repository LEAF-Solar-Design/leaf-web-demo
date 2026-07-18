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

// Calm, loader-free progress line: "submitted", then "running · 3.2s".
function progressText(runStatus, runElapsedMs) {
  const status = runStatus || 'running'
  if (status === 'submitted') return 'submitted'
  const secs = runElapsedMs != null ? ` · ${(runElapsedMs / 1000).toFixed(1)}s` : ''
  return `${status}${secs}`
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

function ErrorLine({ err, onRetry, retry }) {
  return (
    <div className="inline-error">
      <span>{errText(err)}</span>
      {retry && onRetry && (
        <button type="button" className="btn ghost retry" onClick={onRetry}>Retry</button>
      )}
    </div>
  )
}

export default function ResultPanel({ running, runStatus, runElapsedMs, error, result, tool, onRetry }) {
  return (
    <section className="panel result-panel">
      <h2>Result</h2>
      {!running && !result && !error && (
        <p className="panel-sub">Run a tool to see its result and overlay here.</p>
      )}
      {running && (
        <div className="running">
          <span>{progressText(runStatus, runElapsedMs)}{tool?.name ? ` · ${tool.name}` : ''}</span>
        </div>
      )}
      {/* transport-level error (network / submit failure) — always retryable */}
      {error && !running && <ErrorLine err={error} onRetry={onRetry} retry />}
      {result && !running && (
        <div className="result-card">
          <div className="result-head">
            <span className={`ok ${result.ok ? 'yes' : 'no'}`}>{result.ok ? 'OK' : 'FAILED'}</span>
            <span className="result-tool">{result.tool} <span className="dim">v{result.version}</span></span>
            {result.degraded_mode && (
              <span className="degraded" title="Ran on the local fallback, not the cloud solver">local fallback</span>
            )}
          </div>

          {result.ok && <ResultBody result={result} />}
          {result.error && <ErrorLine err={result.error} onRetry={onRetry} retry={isRetryable(result.error)} />}

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

          <div className="receipt">
            <span>{result.timing_ms} ms</span>
            <span className="dim">·</span>
            {result.cost
              ? <span>{result.cost.engine_seconds}s engine · ~${result.cost.usd_est}</span>
              : <span className="dim">no APS cost (mock)</span>}
          </div>
        </div>
      )}
    </section>
  )
}
