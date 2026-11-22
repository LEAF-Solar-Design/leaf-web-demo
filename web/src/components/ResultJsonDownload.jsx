import { useEffect, useState } from 'react'
import { deepRedact } from '../projects/ReceiptPanel.jsx'

// Export the returned envelope, not the paged or formatted table in the DOM.
export default function ResultJsonDownload({ result }) {
  const [download, setDownload] = useState(null)
  useEffect(() => {
    setDownload(null)
    if (!result?.ok || result.result == null) return undefined
    try {
      const text = JSON.stringify(deepRedact(result), null, 2)
      const href = URL.createObjectURL(new Blob([text], { type: 'application/json' }))
      const revoke = URL.revokeObjectURL.bind(URL)
      setDownload({ result, href })
      return () => revoke(href)
    } catch {
      setDownload({ result, error: true })
      return undefined
    }
  }, [result])

  // A new result must never briefly offer the previous report's download.
  if (!download || download.result !== result) return null
  if (download.error) return <p role="status">Result download is unavailable.</p>
  return <a className="btn ghost" href={download.href} download="result.json">Download result JSON</a>
}
