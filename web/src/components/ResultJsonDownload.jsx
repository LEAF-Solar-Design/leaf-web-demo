import { useEffect, useId, useMemo, useState } from 'react'
import { deepRedact } from '../projects/ReceiptPanel.jsx'

function SizedDownloadLink({ download, filename, label }) {
  const sizeId = useId()
  const size = `${download.bytes.toLocaleString()} ${download.bytes === 1 ? 'byte' : 'bytes'}`

  return (
    <a className="btn ghost" href={download.href} download={filename} aria-label={label} aria-describedby={sizeId}>
      {label}{' '}<span id={sizeId}>({size})</span>
    </a>
  )
}

function csvCell(value) {
  if (value == null) return ''
  let text = typeof value === 'object' ? JSON.stringify(value) : String(value)
  // Preserve numeric negatives, while keeping user-entered text inert in spreadsheets.
  if (typeof value === 'string' && /^[\s\uFEFF]*[=+\-@]|^[\t\r\n]/u.test(text)) text = "'" + text
  return '"' + text.replaceAll('"', '""') + '"'
}

export function ResultTableCsvDownload({ rows, label }) {
  const [download, setDownload] = useState(null)
  useEffect(() => {
    setDownload(null)
    try {
      const safeRows = deepRedact(rows)
      const columns = [...new Set(safeRows.flatMap(Object.keys))]
      if (!columns.length) return undefined
      const lines = [
        columns.map(csvCell).join(','),
        ...safeRows.map(row => columns.map(key => csvCell(Object.hasOwn(row, key) ? row[key] : undefined)).join(',')),
      ]
      const blob = new Blob([lines.join('\r\n') + '\r\n'], { type: 'text/csv;charset=utf-8' })
      const href = URL.createObjectURL(blob)
      setDownload({ rows, href, bytes: blob.size })
      return () => URL.revokeObjectURL(href)
    } catch {
      setDownload({ rows, error: true })
      return undefined
    }
  }, [rows])

  if (!download || download.rows !== rows) return null
  if (download.error) return <p role="status">{label} CSV download is unavailable.</p>
  const filename = label.replace(/[^\w.-]+/g, '-').replace(/^-+|-+$/g, '') || 'result'
  return <SizedDownloadLink download={download} filename={filename + '.csv'} label={`Download ${label} CSV`} />
}

const POOLED_SHARE_FIELDS = ['increment_numerator', 'numerator', 'denominator', 'percentage']
const REDACTED_SHARES_LABEL = 'Pooled shares with layer names redacted'

export function RedactedPooledSharesDownload({ report }) {
  const layers = report?.layers
  const rows = useMemo(() => {
    if (!Array.isArray(layers) || !layers.length) return null
    if (!layers.every(row => row && POOLED_SHARE_FIELDS.every(field => Number.isFinite(row[field])))) return null
    // Only rank and measured values enter the CSV, even if the report carries other metadata.
    const ranked = layers.map((row, index) => ({
      rank: index + 1,
      ...Object.fromEntries(POOLED_SHARE_FIELDS.map(field => [field, row[field]])),
    }))
    const last = ranked.at(-1)
    return [...ranked, {
      rank: 'Total',
      increment_numerator: last.numerator,
      numerator: last.numerator,
      denominator: last.denominator,
      percentage: last.percentage,
    }]
  }, [layers])
  if (report == null || (report.defined === false && Array.isArray(layers) && !layers.length)) return null
  if (!rows) return <p role="status">{REDACTED_SHARES_LABEL} CSV download is unavailable.</p>
  return <ResultTableCsvDownload rows={rows} label={REDACTED_SHARES_LABEL} />
}

// Use returned data so paging, formatting, and viewer state cannot change the export.
export default function ResultJsonDownload({ result, selection = false }) {
  const [download, setDownload] = useState(null)
  useEffect(() => {
    setDownload(null)
    if (!result?.ok) return undefined
    let payload = result
    if (selection) {
      const handles = result.overlay?.highlight_handles
      if (!Array.isArray(handles) || !handles.every((handle) => typeof handle === 'string' && handle.trim())) {
        return undefined
      }
      payload = { tool: result.tool, version: result.version, handles }
    } else if (result.result == null) {
      return undefined
    }
    try {
      const text = JSON.stringify(deepRedact(payload), null, 2)
      const blob = new Blob([text], { type: 'application/json' })
      const href = URL.createObjectURL(blob)
      const revoke = URL.revokeObjectURL.bind(URL)
      setDownload({ result, selection, href, bytes: blob.size })
      return () => revoke(href)
    } catch {
      setDownload({ result, selection, error: true })
      return undefined
    }
  }, [result, selection])

  // A new result must never briefly offer the previous report's download.
  if (!download || download.result !== result || download.selection !== selection) return null
  if (download.error) {
    return selection
      ? <p>Selection download is unavailable.</p>
      : <p role="status">Result download is unavailable.</p>
  }
  if (selection) return <SizedDownloadLink download={download} filename="selection.json" label="Download selection JSON" />
  return <SizedDownloadLink download={download} filename="result.json" label="Download result JSON" />
}
