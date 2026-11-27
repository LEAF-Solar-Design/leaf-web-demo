import { useEffect, useState } from 'react'
import { deepRedact } from '../projects/ReceiptPanel.jsx'

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
      const href = URL.createObjectURL(new Blob([lines.join('\r\n') + '\r\n'], { type: 'text/csv;charset=utf-8' }))
      setDownload({ rows, href })
      return () => URL.revokeObjectURL(href)
    } catch {
      setDownload({ rows, error: true })
      return undefined
    }
  }, [rows])

  if (!download || download.rows !== rows) return null
  if (download.error) return <p>{label} CSV download is unavailable.</p>
  const filename = label.replace(/[^\w.-]+/g, '-').replace(/^-+|-+$/g, '') || 'result'
  return <a className="btn ghost" href={download.href} download={filename + '.csv'}>Download {label} CSV</a>
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
      const href = URL.createObjectURL(new Blob([text], { type: 'application/json' }))
      const revoke = URL.revokeObjectURL.bind(URL)
      setDownload({ result, selection, href })
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
  if (selection) return <a className="btn ghost" href={download.href} download="selection.json">Download selection JSON</a>
  return <a className="btn ghost" href={download.href} download="result.json">Download result JSON</a>
}
