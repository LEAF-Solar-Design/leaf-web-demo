import { useEffect, useState } from 'react'
import { deepRedact } from '../projects/ReceiptPanel.jsx'

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
