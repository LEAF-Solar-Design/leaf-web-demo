import { useEffect, useRef, useState } from 'react'
import './popovers.css'
import useExit from '../useExit.js'

function fmtWhen(iso) {
  if (!iso) return null
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso)
  const mins = Math.round((Date.now() - d.getTime()) / 60000)
  if (mins >= 0 && mins < 60) return `${mins} m`
  if (mins >= 0 && mins < 1440) return `${Math.round(mins / 60)} h`
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

function fmtAbs(iso) {
  if (!iso) return undefined
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso)
  return d.toLocaleString()
}

function detectKind(token) {
  if (/^sk-ant-api/i.test((token || '').trim())) return 'api_key'
  if (/^sk-ant-oat/i.test((token || '').trim())) return 'oauth'
  return null
}

export default function ClaudeAccountPanel({
  mock,
  grant,
  loading,
  busy,
  error,
  open,
  onToggle,
  onLink,
  onUnlink,
}) {
  const [token, setToken] = useState('')
  const [kind, setKind] = useState('oauth')
  const [plan, setPlan] = useState(null)
  const [label, setLabel] = useState('')
  const [confirmRemove, setConfirmRemove] = useState(null)
  const [showAdd, setShowAdd] = useState(false)
  const rootRef = useRef(null)
  const pop = useExit(open)
  const linked = !!grant?.linked
  const accounts = Array.isArray(grant?.accounts) ? grant.accounts : []
  const shownAccounts = accounts.length
    ? accounts
    : linked
      ? [{ id: null, label: grant.kind === 'api_key' ? 'Anthropic API key' : 'Legacy Claude mount', kind: grant.kind, linked_at: grant.linked_at, active: true }]
      : []

  useEffect(() => {
    if (!open) return undefined
    const onDoc = (event) => {
      if (rootRef.current && !rootRef.current.contains(event.target)) onToggle(false)
    }
    const onKey = (event) => {
      if (event.key === 'Escape' && !document.querySelector('.drawer-layer .drawer')) onToggle(false)
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [open, onToggle])

  useEffect(() => {
    if (!open) setConfirmRemove(null)
  }, [open])

  useEffect(() => {
    if (linked) {
      setToken('')
      setLabel('')
      setShowAdd(false)
    } else {
      setShowAdd(true)
      setLabel((current) => current || 'My Claude account')
    }
  }, [linked, accounts.length])

  if (mock) return null

  const changeToken = (value) => {
    setToken(value)
    const detected = detectKind(value)
    if (detected) setKind(detected)
  }

  const submit = async () => {
    const value = token.trim()
    if (!value || busy || (kind === 'oauth' && (!label.trim() || !plan))) return
    await onLink(value, kind, label.trim(), kind === 'oauth' ? plan : undefined)
    setToken('')
  }

  const stateLabel = loading ? 'checking' : linked ? `${shownAccounts.length} mounted` : 'not linked'
  const isApiKey = kind === 'api_key'

  return (
    <span className="claude-acct" ref={rootRef}>
      <button
        className="claude-trigger"
        onClick={() => onToggle(!open)}
        aria-expanded={open}
        aria-haspopup="dialog"
        title="Manage tenant-owned Claude subscription and API-key mounts"
      >
        <span className="ca-k">Claude accounts</span>
        <span className={`ca-state ${linked ? 'on' : ''}`}>{stateLabel}</span>
      </button>

      {pop.shown && (
        <div className={`claude-pop${pop.exiting ? ' exit' : ''}`} role="dialog" aria-label="Claude accounts">
          <div className="ca-head">
            <span>Claude mounts</span>
            <button className="key hot" onClick={() => onToggle(false)} aria-label="Close Claude account panel">Esc</button>
          </div>

          {linked && (
            <div className="ca-linked">
              <div className="ca-kindline">automatic tenant routing</div>
              <p className="ca-copy">Each turn uses the least-used eligible mount in this tenant. Mounts are never pooled across tenants.</p>
              <div className="ca-account-list">
                {shownAccounts.map((account) => {
                  const key = account.id || 'legacy'
                  return (
                    <div className="ca-account" key={key}>
                      <div className="ca-account-main">
                        <b>{account.label}</b>
                        <span>{account.kind === 'api_key' ? 'API key' : `${account.plan ? `${account.plan} ` : ''}subscription`}</span>
                        {account.active && <span className="ca-route">recently routed</span>}
                      </div>
                      <div className="ca-account-meta">
                        {typeof account.usage_tokens === 'number' && <span>{account.usage_tokens.toLocaleString()} routed tokens</span>}
                        {account.cooldown_until && <span>cooldown until {fmtWhen(account.cooldown_until)}</span>}
                        {account.linked_at && <span title={fmtAbs(account.linked_at)}>mounted {fmtWhen(account.linked_at)}</span>}
                      </div>
                      <div className="ca-account-actions">
                        {confirmRemove !== key ? (
                          <button className="chip-danger" onClick={() => setConfirmRemove(key)} disabled={busy}>Unlink</button>
                        ) : (
                          <>
                            <button className="chip-danger-confirm" onClick={() => onUnlink(account.id || undefined)} disabled={busy}>
                              {busy ? 'Unlinking…' : 'Unlink'}
                            </button>
                            <button className="chip-neutral" onClick={() => setConfirmRemove(null)} disabled={busy}>Keep</button>
                          </>
                        )}
                      </div>
                    </div>
                  )
                })}
              </div>
              {!showAdd && <button className="btn ca-act" onClick={() => setShowAdd(true)} disabled={busy}>Add another mount</button>}
            </div>
          )}

          {(!linked || showAdd) && (
            <div className="ca-setup">
              <div className="ca-choice" role="radiogroup" aria-label="Credential type">
                <button type="button" role="radio" aria-checked={!isApiKey} className={`ca-opt ${!isApiKey ? 'on' : ''}`} onClick={() => setKind('oauth')} disabled={busy}>Claude subscription</button>
                <button type="button" role="radio" aria-checked={isApiKey} className={`ca-opt ${isApiKey ? 'on' : ''}`} onClick={() => setKind('api_key')} disabled={busy}>Anthropic API key</button>
              </div>

              {!isApiKey && (
                <>
                  <p className="ca-copy">Mount your authorized <b>Claude Pro, Max, Team, or Enterprise</b> subscription.</p>
                  <ol className="ca-steps">
                    <li>Run <code>claude setup-token</code> in a terminal.</li>
                    <li>Approve the request for your Claude subscription.</li>
                    <li>Paste the one-time token below.</li>
                  </ol>
                  <div className="ca-choice" role="radiogroup" aria-label="Claude workspace plan">
                    {['pro', 'max', 'team', 'enterprise'].map((value) => (
                      <button key={value} type="button" role="radio" aria-checked={plan === value} className={`ca-opt ${plan === value ? 'on' : ''}`} onClick={() => setPlan(value)} disabled={busy}>{value[0].toUpperCase() + value.slice(1)}</button>
                    ))}
                  </div>
                  <label className="ca-field">
                    <span className="ca-field-k">Mount label</span>
                    <input type="text" value={label} onChange={(event) => setLabel(event.target.value)} placeholder="Design team east" maxLength={120} disabled={busy} />
                  </label>
                </>
              )}

              {isApiKey && <p className="ca-copy">Mount an Anthropic organization API key billed at API rates.</p>}
              <label className={`ca-field${error ? ' field-invalid' : ''}`}>
                <span className="ca-field-k">{isApiKey ? 'Anthropic API key' : 'Claude setup token'}</span>
                <input
                  type="password"
                  value={token}
                  onChange={(event) => changeToken(event.target.value)}
                  onKeyDown={(event) => { if (event.key === 'Enter') submit() }}
                  placeholder={isApiKey ? 'sk-ant-api03-…' : 'sk-ant-oat01-…'}
                  autoComplete="off"
                  spellCheck={false}
                  disabled={busy}
                  aria-label={isApiKey ? 'Anthropic API key' : 'Claude token'}
                />
              </label>
              <button className="btn primary ca-act" onClick={submit} disabled={busy || !token.trim() || (!isApiKey && (!label.trim() || !plan))}>
                {busy ? 'Linking…' : isApiKey ? 'Link API key' : 'Link Claude account'}
              </button>
              {linked && <button className="chip-neutral" onClick={() => setShowAdd(false)} disabled={busy}>Cancel</button>}
              <p className="ca-note">Credentials stay server-side, tenant-isolated, and never appear again after mounting.</p>
            </div>
          )}
          {error && <div className="field-err" role="alert"><span className="dot red" />{error}</div>}
        </div>
      )}
    </span>
  )
}
