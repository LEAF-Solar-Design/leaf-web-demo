import { useEffect, useRef, useState } from 'react'
import { downloadStagedSource, stagedSourceIdentity } from '../api'
import { readActiveScope, subscribeActiveScope } from '../auth/activeScope.js'

export default function StagedSourceDownload({ staged, disabled = false }) {
  const identity = stagedSourceIdentity(staged)
  return identity ? <DownloadAction key={JSON.stringify(identity)} staged={staged} disabled={disabled} /> : null
}

function DownloadAction({ staged, disabled }) {
  const [scope] = useState(readActiveScope)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [stale, setStale] = useState(false)
  const active = useRef(null)
  const current = useRef({ staged, disabled })
  current.current = { staged, disabled }
  useEffect(() => {
    const invalidate = () => {
      if (readActiveScope() !== scope) {
        active.current?.abort()
        setStale(true)
        setBusy(false)
        setError('Session changed. Refresh the saved request before downloading.')
      }
    }
    const unsubscribe = subscribeActiveScope(invalidate)
    return () => { unsubscribe(); active.current?.abort() }
  }, [scope])

  async function download() {
    if (active.current || disabled || stale) return
    const abort = new AbortController()
    active.current = abort
    setBusy(true)
    setError(null)
    let href
    let anchor
    try {
      const result = await downloadStagedSource(staged, { expectedScope: scope, signal: abort.signal })
      if (abort.signal.aborted || current.current.disabled || current.current.staged !== staged
        || readActiveScope() !== scope) return
      href = URL.createObjectURL(new Blob([result.bytes], { type: 'application/octet-stream' }))
      anchor = document.createElement('a')
      anchor.href = href
      anchor.download = result.filename
      document.body.appendChild(anchor)
      anchor.click()
    } catch {
      if (!abort.signal.aborted) setError('Source download unavailable. Refresh the saved request and try again.')
    } finally {
      anchor?.remove()
      if (href) URL.revokeObjectURL(href)
      if (!abort.signal.aborted) setBusy(false)
      active.current = null
    }
  }

  return <>
    <button type="button" className="chip-act" disabled={disabled || busy || stale || !scope}
      onClick={download}>{busy ? 'Verifying source…' : 'Download staged source'}</button>
    {error && <p role="status">{error}</p>}
  </>
}
