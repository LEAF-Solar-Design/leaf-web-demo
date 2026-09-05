import { useEffect, useRef, useState } from 'react'
import './popovers.css'
import useExit from '../useExit.js'

// Standardization slice 8c. Reuses ClaudeAccountPanel.jsx's isolated-field
// discipline verbatim: single-purpose fields (a URL and a label, nothing
// else), no free-text secret paste, fields cleared after submit, a
// reassurance line. It intentionally carries NO masked API-key field: the
// router (server/routers/tenant_mcp.py RegisterRequest) accepts only
// {url, label} today — there is no key-kind wire shape to mount a field
// for, so adding one here would be a client-invented affordance the server
// cannot honor. It also reuses the trigger+popover CHROME classes
// (.claude-acct/.claude-trigger/.claude-pop/.ca-*) byte-for-byte: those
// rules in styles.css/popovers.css are a generic popover primitive, not
// Claude-specific, so a second consumer costs zero new CSS.
//
// State chips read the ROUTER's own state word — registered | connecting |
// connected | error — through the shared `.state` status-word primitive
// (styles.css: .sub hollow-muted, .prog pulsing, .done filled-primary,
// .fail danger). The router's list/health routes never project an error
// reason (tenant_mcp.py `_project()` — id/label/host/state/linked_at only),
// so "error" never claims a cause it was not given; the one place a reason
// IS available is a connect call's own synchronous failure response, shown
// inline and only until the next refresh.
//
// Nothing here ever reads a field beyond {id, label, host, state, linked_at}
// off a server record — never a spread, never a raw dump — so an upstream
// tool name or a credentialed URL riding along in a hostile payload has no
// path to the DOM even if a record carried one.
export default function LinkServiceDrawer({
  mock,
  servers,
  loading,
  busy,
  error,
  open,
  onToggle,
  onRegister,
  onConnect,
  onHealth,
  onUnlink,
}) {
  const [url, setUrl] = useState('')
  const [label, setLabel] = useState('')
  const [confirmUnlink, setConfirmUnlink] = useState(null)
  const [connectErrors, setConnectErrors] = useState({})
  const [healthResults, setHealthResults] = useState({})
  const rootRef = useRef(null)
  const pop = useExit(open)
  const list = Array.isArray(servers) ? servers : []

  useEffect(() => {
    if (!open) return undefined
    const onDoc = (event) => {
      if (rootRef.current && !rootRef.current.contains(event.target)) onToggle(false)
    }
    const onKey = (event) => {
      if (event.key === 'Escape') onToggle(false)
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [open, onToggle])

  useEffect(() => {
    if (!open) setConfirmUnlink(null)
  }, [open])

  if (mock) return null

  const submit = async () => {
    const u = url.trim()
    const l = label.trim()
    if (!u || !l || busy) return
    try {
      await onRegister(u, l)
      setUrl('')
      setLabel('')
    } catch {
      // onRegister's caller (useTenantMcpRegistry) already recorded `error`;
      // fields are deliberately LEFT FILLED on failure so a rejected label
      // or URL is not silently discarded.
    }
  }

  const runConnect = async (id) => {
    setConnectErrors((current) => ({ ...current, [id]: null }))
    try {
      const result = await onConnect(id)
      const startUrl = result?.authorize_url
      if (typeof startUrl === 'string' && startUrl) {
        window.open(startUrl, '_blank', 'noopener,noreferrer')
      }
    } catch (e) {
      const reason = String(e?.body?.error?.message || e?.message || 'connect failed')
      setConnectErrors((current) => ({ ...current, [id]: reason }))
    }
  }

  const runHealth = async (id) => {
    try {
      const result = await onHealth(id)
      setHealthResults((current) => ({ ...current, [id]: result?.state || 'error' }))
    } catch {
      setHealthResults((current) => ({ ...current, [id]: 'error' }))
    }
  }

  const runUnlink = async (id) => {
    await onUnlink(id)
    setConfirmUnlink(null)
    setConnectErrors((current) => { const next = { ...current }; delete next[id]; return next })
    setHealthResults((current) => { const next = { ...current }; delete next[id]; return next })
  }

  const chipClass = (state) => (
    state === 'connected' ? 'done'
      : state === 'connecting' ? 'prog'
        : state === 'error' ? 'fail'
          : 'sub' // 'registered' and any unrecognized word both read as queued, never a guess at a happier state
  )
  const chipWord = (state) => (
    state === 'connected' ? 'Connected'
      : state === 'connecting' ? 'Connecting…'
        : state === 'error' ? 'Error'
          : 'Registered'
  )

  return (
    <span className="claude-acct link-svc" ref={rootRef}>
      <button
        className="claude-trigger"
        onClick={() => onToggle(!open)}
        aria-expanded={open}
        aria-haspopup="dialog"
        title="Link a tenant-owned MCP service"
      >
        <span className="ca-k">Linked services</span>
        <span className={`ca-state ${list.some((s) => s.state === 'connected') ? 'on' : ''}`}>
          {loading ? 'checking' : `${list.length} linked`}
        </span>
      </button>

      {pop.shown && (
        <div className={`claude-pop${pop.exiting ? ' exit' : ''}`} role="dialog" aria-label="Linked services">
          <div className="ca-head">
            <span>Link a service</span>
            <button className="key hot" onClick={() => onToggle(false)} aria-label="Close linked services panel">Esc</button>
          </div>

          <div className="ca-linked">
            {list.length === 0 && !loading && <p className="ca-copy">No services linked yet.</p>}
            <div className="ca-account-list">
              {list.map((server) => {
                const connectErr = connectErrors[server.id]
                const health = healthResults[server.id]
                return (
                  <div className="ca-account" key={server.id}>
                    <div className="ca-account-main">
                      <b>{server.label}</b>
                      <span className={`state ${chipClass(server.state)}`}>{chipWord(server.state)}</span>
                    </div>
                    <div className="ca-account-meta">
                      {server.host && <span>{server.host}</span>}
                      {health && <span>health: {health}</span>}
                    </div>
                    {connectErr && (
                      <div className="field-err" role="alert"><span className="dot red" />{connectErr}</div>
                    )}
                    <div className="ca-account-actions">
                      <button className="chip-act" onClick={() => runConnect(server.id)} disabled={busy}>Connect</button>
                      <button className="chip-neutral" onClick={() => runHealth(server.id)} disabled={busy}>Health</button>
                      {confirmUnlink !== server.id ? (
                        <button className="chip-danger" onClick={() => setConfirmUnlink(server.id)} disabled={busy}>Unlink</button>
                      ) : (
                        <>
                          <button className="chip-danger-confirm" onClick={() => runUnlink(server.id)} disabled={busy}>
                            {busy ? 'Unlinking…' : 'Unlink'}
                          </button>
                          <button className="chip-neutral" onClick={() => setConfirmUnlink(null)} disabled={busy}>Keep</button>
                        </>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          </div>

          <div className="ca-setup">
            <p className="ca-copy">Link an MCP service by its <b>HTTPS</b> address. The connection is authorized server-side; this app never sees a token.</p>
            <label className="ca-field">
              <span className="ca-field-k">Service label</span>
              <input
                type="text"
                value={label}
                onChange={(event) => setLabel(event.target.value)}
                placeholder="Billing tool"
                maxLength={80}
                disabled={busy}
              />
            </label>
            <label className="ca-field">
              <span className="ca-field-k">Service URL</span>
              <input
                type="text"
                value={url}
                onChange={(event) => setUrl(event.target.value)}
                onKeyDown={(event) => { if (event.key === 'Enter') submit() }}
                placeholder="https://mcp.example.com/sse"
                autoComplete="off"
                spellCheck={false}
                disabled={busy}
              />
            </label>
            <button className="btn primary ca-act" onClick={submit} disabled={busy || !url.trim() || !label.trim()}>
              {busy ? 'Linking…' : 'Link service'}
            </button>
            <p className="ca-note">Credentials and upstream tool names stay server-side and never appear in this panel.</p>
          </div>
          {error && <div className="field-err" role="alert"><span className="dot red" />{error}</div>}
        </div>
      )}
    </span>
  )
}
