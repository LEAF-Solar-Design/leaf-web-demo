import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import EscCap from './EscCap.jsx'
import './popovers.css'
import useExit from '../useExit.js'
import useEscapeOwner from '../lib/useEscapeOwner.js'
import { isCompositionKey } from '../lib/keyComposition.js'
import { humanizeError } from '../errorHumanize.js'

// Standardization slice 8c. Reuses ClaudeAccountPanel.jsx's isolated-field
// discipline verbatim: single-purpose fields (a URL and a label, nothing
// else), no free-text secret paste, fields cleared after submit, a
// reassurance line. It intentionally carries NO masked API-key field: the
// router (server/routers/tenant_mcp.py RegisterRequest) accepts only
// {url, label} today — there is no key-kind wire shape to mount a field
// for, so adding one here would be a client-invented affordance the server
// cannot honor. It also reuses the trigger+popover CHROME classes
// (.claude-acct/.claude-pop/.ca-*) byte-for-byte, and the trigger's look by selector (.link-svc-trigger beside .claude-trigger): those
// rules in styles.css/popovers.css are a generic popover primitive, not
// Claude-specific, so a second consumer costs zero new CSS.
//
// State chips use the registry's authorization state as their source of truth.
// Health checks report reachability separately and can downgrade a row to
// unreachable, but can never upgrade a row to connected. The router's list/health routes never project an error
// reason (tenant_mcp.py `_project()` — id/label/host/state/linked_at only),
// so "error" never claims a cause it was not given; the one place a reason
// IS available is a connect call's own synchronous failure response, shown
// inline and only until the next refresh.
//
// Nothing here ever reads a field beyond {id, label, host, state, linked_at}
// off a server record — never a spread, never a raw dump — so an upstream
// tool name or a credentialed URL riding along in a hostile payload has no
// path to the DOM even if a record carried one.
const authorizationState = (server) => (server.state === 'connected' ? 'connected' : 'sign-in-needed')
const reachabilityState = (result) => (result?.state === 'error' ? 'unreachable' : 'reachable')

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
  const [registerError, setRegisterError] = useState(null)
  const rootRef = useRef(null)
  const triggerRef = useRef(null)
  const dialogRef = useRef(null)
  const closeRef = useRef(null)
  const wasOpen = useRef(false)
  const healthGeneration = useRef({})
  const automaticallyCheckedIds = useRef(new Set())
  const drawerOpenRef = useRef(open)
  const pop = useExit(open)
  const list = Array.isArray(servers) ? servers : []
  const serverIdsKey = JSON.stringify([...new Set(list.map((server) => server.id))].sort())
  const listFailed = error?.kind === 'list'
  const mutationError = listFailed ? error.mutationError : error
  const shownState = (server) => {
    const result = healthResults[server.id]
    if (result?.sourceState === server.state && result.reachability === 'unreachable') return 'unreachable'
    return authorizationState(server)
  }
  const healthContext = useRef(null)
  healthContext.current = { list, onHealth }
  drawerOpenRef.current = open

  useLayoutEffect(() => {
    if (mock) return
    if (open) closeRef.current?.focus()
    else if (wasOpen.current && (document.activeElement === document.body || dialogRef.current?.contains(document.activeElement))) triggerRef.current?.focus()
    wasOpen.current = open
  }, [open, mock])

  useEffect(() => {
    if (!open || mock) {
      automaticallyCheckedIds.current.clear()
      return undefined
    }
    const currentIds = new Set(healthContext.current.list.map((server) => server.id))
    automaticallyCheckedIds.current.forEach((id) => {
      if (!currentIds.has(id)) automaticallyCheckedIds.current.delete(id)
    })
    const check = async (server) => {
      const generation = (healthGeneration.current[server.id] || 0) + 1
      healthGeneration.current[server.id] = generation
      try {
        const result = await healthContext.current.onHealth?.(server.id)
        if (!drawerOpenRef.current || generation !== healthGeneration.current[server.id]) return
        if (!result?.state) return
        setHealthResults((current) => ({
          ...current,
          [server.id]: { reachability: reachabilityState(result), sourceState: server.state },
        }))
      } catch (e) {
        if (drawerOpenRef.current && generation === healthGeneration.current[server.id]) {
          setHealthResults((current) => ({
            ...current,
            [server.id]: { reachability: 'unreachable', sourceState: server.state },
          }))
          setConnectErrors((current) => ({ ...current, [server.id]: humanizeError(e) }))
        }
      }
    }
    const refresh = () => {
      healthContext.current.list.forEach((server) => { void check(server) })
    }
    // Check each row once when it first appears, including rows loaded after opening.
    healthContext.current.list.forEach((server) => {
      if (automaticallyCheckedIds.current.has(server.id)) return
      automaticallyCheckedIds.current.add(server.id)
      void check(server)
    })
    // Recheck reachability when returning from service sign-in.
    const onVisible = () => { if (document.visibilityState === 'visible') refresh() }
    window.addEventListener('focus', refresh)
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      window.removeEventListener('focus', refresh)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [open, mock, serverIdsKey])

  // S27: Escape is the owner stack's (menu layer: this is an anchored popover).
  useEscapeOwner('link-service', !!open && !mock, () => onToggle(false), { layer: 'menu', scope: rootRef })
  useEffect(() => {
    if (!open) return undefined
    const onDoc = (event) => {
      if (rootRef.current && !rootRef.current.contains(event.target)) onToggle(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open, onToggle])

  useEffect(() => {
    if (!open) setConfirmUnlink(null)
  }, [open])

  if (mock) return null

  const submit = async () => {
    const u = url.trim()
    const l = label.trim()
    if (!u || !l || busy) return
    setRegisterError(null)
    try {
      const result = await onRegister(u, l)
      if (!result) {
        setRegisterError('The service was not linked. Sign in to your workspace and try again.')
        return
      }
      setUrl('')
      setLabel('')
    } catch (e) {
      setRegisterError(humanizeError(e))
      // onRegister's caller (useTenantMcpRegistry) already recorded `error`;
      // fields are deliberately LEFT FILLED on failure so a rejected label
      // or URL is not silently discarded.
    }
  }

  const runConnect = async (id) => {
    setConnectErrors((current) => ({ ...current, [id]: null }))
    // Reserve the tab during the click, before the asynchronous authorization request.
    let popup
    let result
    try {
      popup = window.open('about:blank', '_blank')
      if (popup) popup.opener = null
      result = await onConnect(id)
    } catch (e) {
      popup?.close()
      const reason = e?.body?.error?.message || humanizeError(e)
      setConnectErrors((current) => ({ ...current, [id]: reason }))
      return
    }
    const startUrl = result?.authorize_url
    try {
      if (typeof startUrl === 'string' && startUrl.trim()) {
        if (!popup || popup.closed) {
          setConnectErrors((current) => ({ ...current, [id]: 'The sign-in tab was blocked or closed. Allow pop-ups and try Connect again.' }))
        } else {
          popup.location.replace(new URL(startUrl, window.location.href).toString())
        }
      } else {
        popup?.close()
        setConnectErrors((current) => ({ ...current, [id]: 'The service did not provide a sign-in link. Try Connect again.' }))
      }
    } catch (e) {
      const reason = e?.body?.error?.message || humanizeError(e)
      setConnectErrors((current) => ({ ...current, [id]: reason }))
    }
  }

  const runHealth = async (id) => {
    const generation = (healthGeneration.current[id] || 0) + 1
    healthGeneration.current[id] = generation
    try {
      const result = await onHealth(id)
      if (generation !== healthGeneration.current[id]) return
      if (!result?.state) throw new Error('Connection status is unavailable. Try Health again.')
      setHealthResults((current) => ({
        ...current,
        [id]: {
          reachability: reachabilityState(result),
          sourceState: list.find((server) => server.id === id)?.state,
        },
      }))
      setConnectErrors((current) => ({ ...current, [id]: null }))
    } catch (e) {
      if (generation === healthGeneration.current[id]) {
        setHealthResults((current) => ({
          ...current,
          [id]: { reachability: 'unreachable', sourceState: list.find((server) => server.id === id)?.state },
        }))
        setConnectErrors((current) => ({ ...current, [id]: humanizeError(e) }))
      }
    }
  }

  const runUnlink = async (id) => {
    try {
      await onUnlink(id)
      healthGeneration.current[id] = (healthGeneration.current[id] || 0) + 1
      automaticallyCheckedIds.current.delete(id)
      setConnectErrors((current) => { const next = { ...current }; delete next[id]; return next })
      setHealthResults((current) => { const next = { ...current }; delete next[id]; return next })
    } catch (e) {
      const reason = e?.body?.error?.message || humanizeError(e)
      setConnectErrors((current) => ({ ...current, [id]: reason }))
    } finally {
      setConfirmUnlink(null)
    }
  }

  const chipClass = (state) => (
    state === 'connected' ? 'done'
      : state === 'unreachable' ? 'fail'
        : 'sub'
  )
  const chipWord = (state) => (
    state === 'connected' ? 'Connected'
      : state === 'unreachable' ? 'Unreachable'
        : 'Sign in needed'
  )

  return (
    <span className="claude-acct link-svc" ref={rootRef}>
      <button
        // Its OWN class, not `.claude-trigger`: the trust-state e2e row locates
        // the account panel's trigger by that class (panel.locator('.claude-trigger'),
        // web/e2e/local/trust-state.spec.mjs:98) and strict mode needs it alone.
        // The look is shared by selector (`.claude-trigger, .link-svc-trigger` in
        // styles.css and landing.css), never by wearing the account's class.
        className="link-svc-trigger"
        ref={triggerRef}
        onClick={() => onToggle(!open)}
        aria-expanded={open}
        aria-haspopup="dialog"
        title="Manage linked services for your workspace"
      >
        <span className="ca-k">Linked services</span>
        <span className={`ca-state ${list.some((s) => shownState(s) === 'connected') ? 'on' : ''}`}>
          {loading ? 'checking' : listFailed ? 'Unavailable' : `${list.length} linked`}
        </span>
      </button>

      {pop.shown && (
        <div ref={dialogRef} inert={!open ? '' : undefined} aria-hidden={!open || undefined} title={busy ? 'Please wait for the service update.' : undefined} className={`claude-pop${pop.exiting ? ' exit' : ''}`} role="dialog" aria-label="Linked services">
          <div className="ca-head">
            <span>Link a service</span>
            <EscCap ref={closeRef} onClick={() => onToggle(false)} label="Close linked services panel" />
          </div>

          <div className="ca-linked">
            {listFailed && (
              <div className="field-err" role="alert">
                <p>Linked services could not be loaded.</p>
                <p>{humanizeError(error)}</p>
                <button className="chip-act" onClick={error.retry} disabled={loading} title={loading ? 'Checking linked services.' : undefined}>Retry</button>
              </div>
            )}
            {list.length === 0 && !loading && !listFailed && <p className="ca-copy">No services linked yet.</p>}
            <div className="ca-account-list">
              {list.map((server) => {
                const connectErr = connectErrors[server.id]
                const result = healthResults[server.id]
                const health = result && result.sourceState === server.state ? result.reachability : null
                const state = shownState(server)
                return (
                  <div className="ca-account" key={server.id}>
                    <div className="ca-account-main">
                      <b>{server.label}</b>
                      <span className={`state ${chipClass(state)}`}>{chipWord(state)}</span>
                    </div>
                    <div className="ca-account-meta">
                      {server.host && <span>{server.host}</span>}
                      {health && <span>Connection checked: {health === 'unreachable' ? 'Unreachable' : 'Reachable'}</span>}
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
            <p className="ca-copy">Link a service by its secure <b>HTTPS</b> address. You may be asked to sign in to the service.</p>
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
                onKeyDown={(event) => {
                  if (isCompositionKey(event)) return
                  if (event.key === 'Enter') submit()
                }}
                placeholder="https://mcp.example.com/sse"
                autoComplete="off"
                spellCheck={false}
                disabled={busy}
              />
            </label>
            <button className="btn primary ca-act" onClick={submit} disabled={busy || !url.trim() || !label.trim()} title={busy ? 'Please wait for the service update.' : !url.trim() || !label.trim() ? 'Enter a service label and address.' : undefined}>
              {busy ? 'Linking…' : 'Link service'}
            </button>
            <p className="ca-note">Your sign-in details are stored securely and are hidden in this panel.</p>
          </div>
          {registerError && <div className="field-err" role="alert">{registerError}</div>}
          {mutationError && <div className="field-err" role="alert"><span className="dot red" />{humanizeError(mutationError)}</div>}
        </div>
      )}
    </span>
  )
}
