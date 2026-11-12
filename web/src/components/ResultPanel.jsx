import { useEffect, useRef, useState } from 'react'
import { useDrawingObjects } from '../site/DrawingObjectsContext.jsx'
import './resultObjectLinks.css'
import './resultParity.css'
import { humanKey } from '../labels.js'
import { errorActorLabel, errorPresentation } from '../errorPresentation.js'
import { modChord } from '../lib/keys.js'
import { readSingleKeyShortcuts } from '../lib/singleKeyPreference.js'
import ArloProposalReview from './ArloProposalReview.jsx'
import SolarReadResult from '../solar/SolarReadResult.jsx'
import ResultJsonDownload from './ResultJsonDownload.jsx'
import { isSolarProjectReadResult, isSolarReadResult } from '../solar/solarReadResultModel.js'

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

function isNumericCell(value) {
  return typeof value === 'number'
    ? Number.isFinite(value)
    : typeof value === 'string' && /^-?\d[\d,]*(\.\d+)?$/.test(value)
}

function CountsTable({ counts, density }) {
  const rows = Object.entries(counts)
  return (
    <table className="counts" data-density={density}>
      <thead><tr><th scope="col">Layer</th><th scope="col">Count</th></tr></thead>
      <tbody>
        {rows.map(([k, v]) => (
          <tr key={k}><td className={isNumericCell(k) ? 'dp-number' : ''}>{k}</td><td className={isNumericCell(v) ? 'num dp-number' : ''}>{Number(v).toLocaleString()}</td></tr>
        ))}
      </tbody>
    </table>
  )
}

function KeyValue({ data, density }) {
  return (
    <table className="kv" data-density={density}>
      <tbody>
        {Object.entries(data).map(([k, v]) => (
          <tr key={k}>
            <th scope="row" className="k">{humanKey(k)}</th>
            <td className={isNumericCell(v) ? 'v num dp-number' : 'v'}>{typeof v === 'number' ? v.toLocaleString() : String(v)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

// Generic table: result.table = { columns: string[], rows: any[][] }. Used by tools whose
// output is a list of inspection rows, rendered before the scalar key/values.
function GridTable({ table, density }) {
  const cols = Array.isArray(table.columns) ? table.columns : []
  const rows = Array.isArray(table.rows) ? table.rows : []
  return (
    <table className="counts grid" data-density={density}>
      <thead><tr>{cols.map((c, i) => <th key={i} scope="col">{String(c)}</th>)}</tr></thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i}>
            {(Array.isArray(r) ? r : [r]).map((v, j) => (
              <td key={j} className={isNumericCell(v) ? 'num dp-number' : ''}>
                {typeof v === 'number' ? v.toLocaleString() : String(v ?? '')}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function isRowObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

const MAX_ROW_DEPTH = 20
const MAX_ROW_COLUMNS = 50

// Copy only own JSON fields before stringifying, so deep/cyclic values stay text.
function boundedRowValue(value, depth = 0, ancestors = new Set()) {
  if (value === null || typeof value !== 'object') return value
  if (depth >= MAX_ROW_DEPTH) return '[Truncated]'
  if (ancestors.has(value)) return '[Circular]'
  const next = new Set(ancestors).add(value)
  return Array.isArray(value)
    ? value.map((item) => boundedRowValue(item, depth + 1, next))
    : Object.fromEntries(Object.entries(value).map(([key, item]) => [key, boundedRowValue(item, depth + 1, next)]))
}

function rowValueText(value) {
  try {
    return value == null ? '' : typeof value === 'object' ? JSON.stringify(boundedRowValue(value)) : String(value)
  } catch {
    return '[Unreadable value]'
  }
}

// Keep the supplied row/field order, including fields first seen on later pages.
function* rowFields(row, path = []) {
  for (const [key, value] of Object.entries(row)) {
    const fieldPath = [...path, key]
    if (fieldPath.length < MAX_ROW_DEPTH && isRowObject(value) && Object.keys(value).length > 0) {
      yield* rowFields(value, fieldPath)
    } else {
      yield fieldPath
    }
  }
}

function findRowArrays(data, path = [], ancestors = new Set()) {
  if (data === null || typeof data !== 'object' || ancestors.has(data)) return []
  // A row array owns discovery of its visible children, after pagination.
  if (Array.isArray(data) && data.every(isRowObject)) return [{ path, rows: data, ancestors }]
  if (path.length >= MAX_ROW_DEPTH) return [{ path, value: data }]
  const next = new Set(ancestors).add(data)
  return Object.entries(data).flatMap(([key, value]) => {
    // These fields have dedicated renderers; file bytes must not become table cells.
    if (path.length === 0 && ['table', 'counts', 'files', 'warnings'].includes(key)) return []
    const fieldPath = [...path, key]
    return findRowArrays(value, fieldPath, next)
  })
}

function rowFieldValue(row, fieldPath) {
  return fieldPath.reduce((entry, field) =>
    entry !== null && typeof entry === 'object' && Object.hasOwn(entry, field) ? entry[field] : undefined, row)
}

function rowCellText(value) {
  return Array.isArray(value) && value.every(isRowObject)
    ? `${value.length} ${value.length === 1 ? 'row' : 'rows'}` : rowValueText(value)
}

function RowFieldDetails({ row, label }) {
  const [expanded, setExpanded] = useState(false)
  return <>
    <button type="button" className="btn ghost" aria-expanded={expanded}
      aria-label={`${expanded ? 'Hide all fields' : 'All fields'} for ${label}`}
      onClick={() => setExpanded(!expanded)}>{expanded ? 'Hide all fields' : 'All fields'}</button>
    {expanded && <dl aria-label={`All fields for ${label}`}>
      {Array.from(rowFields(row), (fieldPath) => <div key={JSON.stringify(fieldPath)}>
        <dt>{fieldPath.map(humanKey).join(' / ')}</dt>
        <dd>{rowCellText(rowFieldValue(row, fieldPath))}</dd>
      </div>)}
    </dl>}
  </>
}

function RowArrayResults({ entries, density }) {
  return entries.map((entry) => Object.hasOwn(entry, 'rows')
    ? <RowArrayTable key={JSON.stringify(entry.path)} {...entry} density={density} />
    : <p key={JSON.stringify(entry.path)}>{entry.path.map(humanKey).join(' / ')}: {rowValueText(entry.value)}</p>)
}

function RowArrayTable({ path, rows, ancestors, density }) {
  const [page, setPage] = useState(0)
  useEffect(() => { setPage(0) }, [rows])
  const label = path.map(humanKey).join(' / ') || 'result'
  const pageSize = 100
  const currentPage = Math.min(page, Math.max(0, Math.ceil(rows.length / pageSize) - 1))
  const start = currentPage * pageSize
  const visibleRows = rows.slice(start, start + pageSize)
  const childArrays = path.length < MAX_ROW_DEPTH
    ? visibleRows.flatMap((row, index) => findRowArrays(row, [...path, String(start + index)], new Set(ancestors).add(rows)))
    : []
  const columns = new Map()
  const omittedColumns = new Set()
  for (const row of rows) {
    for (const fieldPath of rowFields(row)) {
      const key = JSON.stringify(fieldPath)
      if (columns.has(key)) continue
      if (columns.size < MAX_ROW_COLUMNS) columns.set(key, fieldPath)
      else omittedColumns.add(key)
    }
  }
  if (rows.length === 0) return <p>{label}: No rows.</p>
  return (
    <section aria-label={`${label} rows`}>
      <div className="table-scroll">
        <table className="counts grid" data-density={density}>
          <caption>{label}</caption>
          <thead><tr>{Array.from(columns, ([key, fieldPath]) => (
            <th key={key} scope="col">{fieldPath.map(humanKey).join(' / ')}</th>
          ))}</tr></thead>
          <tbody>
            {visibleRows.map((row, index) => (
              <tr key={start + index}>
                {Array.from(columns, ([key, fieldPath], columnIndex) => {
                  const value = rowFieldValue(row, fieldPath)
                  return <td key={key} className={isNumericCell(value) ? 'num dp-number' : ''}>
                    {rowCellText(value)}
                    {columnIndex === 0 && omittedColumns.size > 0 &&
                      <RowFieldDetails row={row} label={`${label} row ${start + index + 1}`} />}
                  </td>
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {omittedColumns.size > 0 && <p>{omittedColumns.size} more fields available in each row's All fields.</p>}
      {rows.length > pageSize && (
        <nav aria-label={`${label} pages`}>
          <span aria-live="polite">Rows {start + 1} to {Math.min(start + pageSize, rows.length)} of {rows.length}</span>
          <button type="button" className="btn ghost" disabled={currentPage === 0}
            onClick={() => setPage(currentPage - 1)} aria-label={`Previous ${label} page`}>Previous</button>
          <button type="button" className="btn ghost" disabled={start + pageSize >= rows.length}
            onClick={() => setPage(currentPage + 1)} aria-label={`Next ${label} page`}>Next</button>
        </nav>
      )}
      <RowArrayResults entries={childArrays} density={density} />
    </section>
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

function ResultBody({ result, density, solarBinding, authorityKey, projectClient, catalogDigest, projectCurrent, save }) {
  const data = result?.result
  if (!data) return result?.overlay ? null : <p className="result-empty">Completed with no output.</p>
  if (isSolarProjectReadResult(data)) {
    return <SolarReadResult
      data={data}
      solarBinding={solarBinding}
      authorityKey={authorityKey}
      projectClient={projectClient}
      catalogDigest={catalogDigest}
      projectCurrent={projectCurrent}
      save={save}
    />
  }
  if (isSolarReadResult(data)) return <SolarReadResult data={data} />
  if (data.solver === 'arlo-design') return <ArloProposalReview key={`${result.job_context?.job_id || 'local'}:${data.result_sha256}`} envelope={data} context={result.job_context} />
  const rowArrays = findRowArrays(data)
  const rowTables = <>
    <RowArrayResults entries={rowArrays} density={density} />
    {rowArrays.some((entry) => Object.hasOwn(entry, 'rows')) && <ResultJsonDownload result={result} />}
  </>
  const scalars = Object.fromEntries(Object.entries(data).filter(([, value]) => value === null || typeof value !== 'object'))
  if (Object.hasOwn(data, 'table') && data.table && typeof data.table === 'object') {
    return (
      <>
        <div className="table-scroll"><GridTable table={data.table} density={density} /></div>
        {rowTables}
        {Array.isArray(data.warnings) && data.warnings.length > 0 && (
          <ul className="warnings">{data.warnings.map((w, i) => <li key={i}>{String(w)}</li>)}</ul>
        )}
        <FileLinks files={data.files} />
        {Object.keys(scalars).length > 0 && <div className="table-scroll"><KeyValue data={scalars} density={density} /></div>}
      </>
    )
  }
  if (Object.hasOwn(data, 'counts') && data.counts && typeof data.counts === 'object') {
    const summary = Object.fromEntries(Object.entries(scalars).filter(([key]) => key !== 'total'))
    return (
      <>
        <div className="table-scroll"><CountsTable counts={data.counts} density={density} /></div>
        {Object.hasOwn(data, 'total') && <div className="total">Total: <b>{Number(data.total).toLocaleString()}</b></div>}
        {rowTables}
        {Object.keys(summary).length > 0 && <div className="table-scroll"><KeyValue data={summary} density={density} /></div>}
      </>
    )
  }
  return rowArrays.length > 0 || Object.keys(scalars).length > 0
    ? <>
      {rowTables}
      {Object.keys(scalars).length > 0 && <div className="table-scroll"><KeyValue data={scalars} density={density} /></div>}
    </>
    : result.overlay || (Array.isArray(data.files) && data.files.length > 0)
      ? null
      : <p className="result-empty">Completed with no output.</p>
}

// X1 failed-act row: plain sentence naming what failed (code demoted to a
// quiet mono suffix), Retry chip carrying its mnemonic keycap (R — bound at
// the panel level while the row is visible), and an honest fallback note.
// The calm quota/entitlement variants keep the broker's own sentence — a
// budget/plan boundary is not a failure to re-phrase.
function ErrorLine({ err, onRetry, retry, quota, toolName }) {
  const { message, code, nextAction, actor } = errParts(err)
  return (
    <div className={`inline-error ${quota ? 'quota' : ''}`} role="alert">
      <span>
        {quota ? message : `Couldn't run ${toolName || 'the tool'}: ${message}`}
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

export const OBJECT_LINK_REASONS = Object.freeze({
  noOrigin: 'This result does not record which drawing it ran on.',
  differentDrawing: 'This result ran on a different drawing or version than the one open now.',
  loading: "The drawing's object list is still loading.",
  missing: 'This object is not in the drawing that is open now.',
  navigation: 'The drawing viewer is not ready.',
})

function ResultObjectLinks({ result, navigation, announce }) {
  const index = useDrawingObjects()?.index
  const [page, setPage] = useState(0)
  const handles = result.overlay?.highlight_handles || []
  useEffect(() => { setPage(0) }, [result])
  let unavailable = null
  if (!result.origin?.drawingKey) unavailable = OBJECT_LINK_REASONS.noOrigin
  else if (!index?.drawingKey) unavailable = OBJECT_LINK_REASONS.loading
  else if (result.origin.drawingKey !== index.drawingKey) unavailable = OBJECT_LINK_REASONS.differentDrawing
  const choose = (record) => {
    const outcome = navigation?.jump(record.id)
    if (outcome?.ok) announce(`Showing ${record.name}`)
    else announce(outcome?.reason || OBJECT_LINK_REASONS.navigation)
  }
  if (!handles.length) return unavailable === OBJECT_LINK_REASONS.differentDrawing ? <p>{unavailable}</p> : null
  return <div className="result-object-links">
    {unavailable && <p>{unavailable}</p>}
    <details>
      <summary>Show highlighted objects</summary>
      <ul>
        {handles.slice(page * 100, (page + 1) * 100).map((handle, offset) => {
          const record = index?.byHandle.get(String(handle).replace(/^0x/i, '').toUpperCase())
          return <li key={`${page}:${offset}`}>
            {!unavailable && record ? <button type="button" onClick={() => choose(record)}>
              <span>{record.name}</span><small>{record.path}</small>
            </button> : <span>
              <span>Handle {handle}</span>
              {!unavailable && <small>{OBJECT_LINK_REASONS.missing}</small>}
            </span>}
          </li>
        })}
      </ul>
      {page > 0 && <button type="button" onClick={() => setPage((value) => value - 1)}>Previous 100</button>}
      {(page + 1) * 100 < handles.length && <button type="button" onClick={() => setPage((value) => value + 1)}>Show 100 more</button>}
    </details>
  </div>
}

// Note: run progress (runStatus / runProgress / runElapsedMs) moved to the
// SB3 running strip at the bar dock; callers may still pass them — ignored here.
// `notices` is the NR banner slot: ongoing-condition banners dock UNDER the
// header of the pane they affect, so App passes them in and they render
// immediately after the <h3> — never above the pane.
// A tool run carries its tool version as a string or number. A project write carries the new
// canonical drawing version as an object, which must never be rendered as a React child.
export function versionLabel(version) {
  if (version === null || typeof version !== 'object') return `v${version}`
  const id = typeof version.version_id === 'string' && version.version_id ? version.version_id.slice(0, 8) : null
  const seq = Number.isSafeInteger(version.seq) && version.seq > 0 ? version.seq : null
  if (id === null && seq === null) return 'drawing version not reported'
  return `drawing version${seq === null ? '' : ` ${seq}`}${id === null ? '' : ` (${id})`}`
}

export default function ResultPanel({ running, error, result, tool, onRetry, notices, navigation, density,
  solarBinding, authorityKey, projectClient, catalogDigest, projectCurrent, save }) {
  const [announcement, setAnnouncement] = useState('')
  const announcementFrame = useRef(null)
  const tableDensity = density ?? (typeof window !== 'undefined' && window.matchMedia?.('(pointer: coarse)')?.matches
    ? 'comfortable' : 'compact')
  useEffect(() => () => cancelAnimationFrame(announcementFrame.current), [])
  const announce = (message) => {
    cancelAnimationFrame(announcementFrame.current)
    setAnnouncement('')
    announcementFrame.current = requestAnimationFrame(() => setAnnouncement(message))
  }
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
      // S25: the ShortcutSheet switch turns single-key shortcuts off; read at
      // keystroke time so a flip takes effect on the next key.
      if (!readSingleKeyShortcuts()) return
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
      <div className="result-object-announcement" role="status" aria-live="polite" aria-atomic="true">{announcement}</div>
      {notices}
      {!running && !result && !error && (
        <p className="panel-sub">
          Run a tool or type what you want in the bar below, and the result and its
          drawing markup appear here.
          {' '}<span className="key">{modChord('K')}</span>
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
            <span className="result-tool">{result.tool} <span className="dim">{versionLabel(result.version)}</span></span>
            {result.degraded_mode && (
              <span className="degraded">local fallback</span>
            )}
          </div>

          {result.ok && <ResultBody result={result} density={tableDensity} solarBinding={solarBinding} authorityKey={authorityKey}
            projectClient={projectClient} catalogDigest={catalogDigest} projectCurrent={projectCurrent} save={save} />}
          {result.error && <ErrorLine err={result.error} onRetry={onRetry} retry={!isEnt && isRetryable(result.error)} quota={calm} toolName={result.tool} />}
          {!result.ok && !result.error && <p className="result-empty">The run failed without reporting a reason.</p>}
          {!result.overlay && <ResultObjectLinks result={result} navigation={navigation} announce={announce} />}

          {result.overlay && (
            <div className="overlay-summary">
              {result.overlay.highlight_handles?.length > 0 && (
                <div className="ov-row">
                  <span className="ov-dot hl" />
                  {result.overlay.highlight_handles.length.toLocaleString()} panel{result.overlay.highlight_handles.length === 1 ? '' : 's'} highlighted in the viewer
                </div>
              )}
              <ResultObjectLinks result={result} navigation={navigation} announce={announce} />
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
            <span>{result.result?.solver === 'arlo-design' && !Number.isFinite(result.timing_ms) ? 'Timing unavailable' : `${result.timing_ms} ms`}</span>
            {result.cost ? (
              <>
                <span className="dim">·</span>
                <span>Processing time {result.cost.engine_seconds}s</span>
                <span className="dim">·</span>
                {fmtUsd(result.cost.usd_est)
                  ? <b className="usd">{fmtUsd(result.cost.usd_est)}</b>
                  : <span className="dim">no cloud cost</span>}
              </>
            ) : (
              <>
                <span className="dim">·</span>
                <span className="dim">{result.result?.solver === 'arlo-design' ? 'Compute cost unavailable' : 'no cloud cost'}</span>
              </>
            )}
          </div>
        </div>
      )}
    </section>
  )
}
