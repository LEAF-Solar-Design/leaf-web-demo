import React, { useEffect, useState } from 'react'

// LCERR: the browser's own words for "nothing answered on this port" (Chromium, Firefox, Safari). Unreachable already
// says AutoCAD was not found, so these add nothing; any other error (an HTTP status, a bad reply, a timeout) means
// something answered or hung, and stays visible.
const UNREACHED = /^(?:Failed to fetch|NetworkError when attempting to fetch resource\.?|Load failed)$/

export default function LocalCadConnect({ status = 'stopped', drawing, summary, truncated, error, textPending, onConnect, onDisconnect }) {
  const [denied, setDenied] = useState(false)
  useEffect(() => {
    let live = true
    let permission
    const update = () => { if (live) setDenied(permission?.state === 'denied') }
    ;(async () => {
      try {
        permission = await navigator.permissions.query({ name: 'loopback-network' })
        update()
        if (live) permission.addEventListener?.('change', update)
      } catch { /* Unsupported permission names are normal in other browsers. */ }
    })()
    return () => { live = false; permission?.removeEventListener?.('change', update) }
  }, [status])
  const connect = status === 'stopped'
  const label = status === 'connected' ? `Connected to AutoCAD · ${drawing ?? ''}`
    : status === 'connecting' ? 'AutoCAD: connecting'
      : status === 'waiting' ? 'AutoCAD: waiting for a drawing'
        : status === 'stopped' ? 'AutoCAD: not connected'
          : denied ? "Allow this site to reach apps on this computer in your browser's site settings"
            : 'AutoCAD: not found on this computer'
  return <div className="local-cad-connect" style={{ display: 'inline-flex', order: 3, alignItems: 'center', gap: 6, minWidth: 0, maxWidth: 400, flexShrink: 1, whiteSpace: 'nowrap' }}>
    <button type="button" className="chip-act" data-testid="local-cad-connect"
      style={{ flexShrink: 0 }}
      onClick={connect ? onConnect : onDisconnect}>{connect ? 'Connect AutoCAD' : 'Disconnect'}</button>
    <span aria-live="polite" style={{ fontSize: 11, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }}>
      <span data-testid="local-cad-status">{label}</span>
      {error && !(status === 'unreachable' && UNREACHED.test(error)) && <span>{`. The last update failed: ${error}`}</span>}
      {(textPending || (error && /text|font|glyph|label/i.test(error))) && <span> · labels unavailable</span>}
      <span data-testid="local-cad-summary" style={{ marginLeft: summary || truncated ? 6 : 0 }}>{summary ?? ''}{truncated ? ' (showing the first 200,000 points)' : ''}</span>
    </span>
  </div>
}
