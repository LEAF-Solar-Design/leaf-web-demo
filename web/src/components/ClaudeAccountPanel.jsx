import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import EscCap from './EscCap.jsx'
import './popovers.css'
import useExit from '../useExit.js'
import useEscapeOwner from '../lib/useEscapeOwner.js'
import { absoluteWithZone, relativeTime } from '../lib/railTime.js'
import useRelativeNow from '../lib/useRelativeNow.js'
import { isCompositionKey } from '../lib/keyComposition.js'
import { humanizeError } from '../errorHumanize.js'

const ANTHROPIC_CONSOLE_KEYS_URL = 'https://platform.claude.com/settings/keys'

export default function ClaudeAccountPanel({
  mock,
  signedOut = false,
  grant,
  loading,
  busy,
  error,
  open,
  onToggle,
  onLink,
  onUnlink,
  onRefresh,
}) {
  const now = useRelativeNow()
  const [token, setToken] = useState('')
  const [label, setLabel] = useState('')
  const [confirmRemove, setConfirmRemove] = useState(null)
  const [showAdd, setShowAdd] = useState(false)
  const pending = useRef(false)
  const panelLifetime = useRef(0)
  const refreshOpen = useRef(open)
  const replacement = useRef(null)
  const successRef = useRef(null)
  const formErrorRef = useRef(null)
  const errorRef = useRef(null)
  const [success, setSuccess] = useState(null)
  const [formError, setFormError] = useState(null)
  useEffect(() => {
    if (open && !refreshOpen.current && !mock && !signedOut) onRefresh?.()
    refreshOpen.current = open
  }, [open, mock, signedOut, onRefresh])
  useEffect(() => {
    if (!open || signedOut || mock) {
      panelLifetime.current += 1
      setToken('')
      setFormError(null)
      setSuccess(null)
      replacement.current = null
    }
  }, [open, signedOut, mock])
  useEffect(() => () => { panelLifetime.current += 1 }, [])
  const rootRef = useRef(null)
  const triggerRef = useRef(null)
  const dialogRef = useRef(null)
  const closeRef = useRef(null)
  const wasOpen = useRef(false)
  const pop = useExit(open)
  useLayoutEffect(() => {
    if (mock) return
    if (open) closeRef.current?.focus()
    else if (wasOpen.current && (document.activeElement === document.body || dialogRef.current?.contains(document.activeElement))) triggerRef.current?.focus()
    wasOpen.current = open
  }, [open, mock])
  // The card is right-anchored to its trigger. When the trigger sits near the left edge (the
  // cockpit header), a right-anchored card runs off screen, so anchor it to the left instead.
  const [alignStart, setAlignStart] = useState(false)
  useLayoutEffect(() => {
    if (mock || !open) return
    const trigger = triggerRef.current?.getBoundingClientRect()
    const width = dialogRef.current?.offsetWidth || 320
    if (trigger) setAlignStart(trigger.right - width < 8)
  }, [open, mock])
  const known = typeof grant?.linked === 'boolean'
  const restricted = grant?.read_status === 'restricted'
  const canManage = !loading && known
  const linked = grant?.linked === true
  const accounts = Array.isArray(grant?.accounts) ? grant.accounts : []
  const shownAccounts = accounts.length
    ? accounts
    : linked
      ? [{ id: null, label: grant.kind === 'api_key' ? 'Anthropic API key' : 'Claude account', kind: grant.kind, linked_at: grant.linked_at, active: true }]
      : []

  // S27: Escape is the owner stack's (menu layer: an anchored popover); the
  // stack's order replaces the old "an open drawer owns Esc" selector check.
  useEscapeOwner('claude-account', !!open && !mock, () => onToggle(false), { layer: 'menu', scope: rootRef })
  useEffect(() => {
    if (!open) return undefined
    const onDoc = (event) => {
      if (rootRef.current && !rootRef.current.contains(event.target)) onToggle(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open, onToggle])

  useEffect(() => {
    if (!open) setConfirmRemove(null)
  }, [open])

  useEffect(() => {
    if (!canManage) {
      setShowAdd(false)
      setConfirmRemove(null)
    } else if (linked) {
      setToken('')
      setLabel('')
      setShowAdd(false)
    } else {
      setShowAdd(true)
      setLabel((current) => current || 'My API key')
    }
  }, [canManage, linked, accounts.length])

  useEffect(() => { if (success && canManage && linked) successRef.current?.focus() }, [success, canManage, linked])
  useEffect(() => {
    if (!open || signedOut) return
    if (formError) formErrorRef.current?.scrollIntoView?.({ block: 'nearest' })
    if (error) errorRef.current?.scrollIntoView?.({ block: 'nearest' })
  }, [error, formError, open, signedOut, canManage, linked, showAdd])

  if (mock) return null

  const submit = async () => {
    let value = token.trim()
    if (value.startsWith('sk-ant-')) value = value.replace(/\s/g, '')
    if (!canManage || signedOut || !value || busy || pending.current) return
    setSuccess(null)
    if (/sk-ant-/i.test(label)) { setLabel(''); setFormError('The key goes in the key field, not the label.'); return }
    if (/^sk-ant-oat/i.test(value)) {
      setFormError('That is not an API key. Create an API key in Claude Console and paste that instead.'); return
    }
    if (value.includes('#') || /^https?:\/\//i.test(value) || /^claude /i.test(value)) {
      setFormError('That is not an API key. Copy the key from Claude Console (it starts with sk-ant-api).'); return
    }
    setFormError(null)
    pending.current = true
    setToken('')
    const oldId = replacement.current
    const lifetime = panelLifetime.current
    try {
      const result = await onLink(value, 'api_key', label.trim(), undefined)
      if (result?.linked === true && lifetime === panelLifetime.current) {
        const added = (result.active_account_id && result.accounts?.find((account) => account.id === result.active_account_id))
          || result.accounts?.find((account) => account.active)
        const planName = added?.kind === 'api_key' ? 'API key'
          : ['pro', 'max', 'team', 'enterprise'].includes(added?.plan) ? added.plan[0].toUpperCase() + added.plan.slice(1) : null
        let finalResult = result
        let oldListed = false
        if (oldId && result.accounts?.some((account) => account.id === oldId && account.auth_refused_code)
            && result.accounts.some((account) => account.id !== oldId && !account.auth_refused_code)) {
          try {
            const removed = await onUnlink(oldId)
            oldListed = !removed || !Array.isArray(removed.accounts) || removed.accounts.some((account) => account.id === oldId)
            if (!oldListed) finalResult = removed
          } catch { oldListed = true }
        }
        const count = finalResult.accounts?.length || 1
        const text = count === 1 ? added?.kind === 'api_key' ? '1 API key linked.' : `1 account linked${planName ? `, ${planName}` : ''}.`
          : `${count} accounts linked.${planName ? ` Added: ${planName}.` : ''}`
        if (lifetime === panelLifetime.current) setSuccess(`${text} Claude checks it on your first request.${oldListed ? ' The old entry is still listed. You can unlink it.' : ''}`)
        replacement.current = null
      }
    } catch {
      if (lifetime === panelLifetime.current) setFormError('The link could not be finished. Reload and check your Claude accounts before you retry.')
    } finally { pending.current = false }
  }

  const refusedCount = shownAccounts.filter((account) => account.auth_refused_code).length
  const stateLabel = signedOut ? 'sign in' : loading ? 'checking' : restricted ? 'access restricted'
    : !known ? 'unavailable' : linked ? refusedCount === shownAccounts.length ? 'link again needed'
      : refusedCount ? `${shownAccounts.length - refusedCount} of ${shownAccounts.length} linked` : `${shownAccounts.length} linked` : 'not linked'
  const addFormShown = !signedOut && canManage && (!linked || showAdd)
  const serverError = !signedOut && error && <div ref={errorRef} className="field-err" role="alert"><span className="dot red" />{humanizeError(error)}</div>

  return (
    <span className="claude-acct" ref={rootRef}>
      <button
        className="claude-trigger"
        ref={triggerRef}
        onClick={() => onToggle(!open)}
        aria-expanded={open}
        aria-haspopup="dialog"
        title="Manage linked Claude accounts and API keys"
      >
        <span className="ca-k">Claude accounts</span>
        <span className={`ca-state ${linked && refusedCount < shownAccounts.length ? 'on' : ''}`}>{stateLabel}</span>
      </button>

      {pop.shown && (
        <div ref={dialogRef} inert={!open ? '' : undefined} aria-hidden={!open || undefined} title={busy ? 'Please wait for the account update.' : undefined} className={`claude-pop${pop.exiting ? ' exit' : ''}`} data-align={alignStart ? 'start' : undefined} role="dialog" aria-label="Claude accounts">
          <div className="ca-head">
            <span>Claude accounts</span>
            <EscCap ref={closeRef} onClick={() => onToggle(false)} label="Close Claude account panel" />
          </div>

          {signedOut && <p className="ca-copy" role="status">Sign in to view or link Claude accounts.</p>}
          {!signedOut && loading && <p className="ca-copy" role="status">Checking your Claude accounts...</p>}
          {!signedOut && !loading && !known && (
            <p className="ca-copy" role="status">
              {restricted
                ? 'Only the workspace owner can view or manage Claude accounts. This does not indicate whether authoring is available.'
                : 'Claude account status is unavailable. This does not indicate whether an account is linked.'}
            </p>
          )}
          {!signedOut && canManage && linked && (
            <div className="ca-linked">
              {success && <p className="ca-success" role="status" tabIndex={-1} ref={successRef}>{success}</p>}
              <div className="ca-kindline">Automatic account selection</div>
              <p className="ca-copy">Each request uses an available account in your workspace. Accounts are never shared with other workspaces.</p>
              <div className="ca-account-list">
                {shownAccounts.map((account) => {
                  const key = account.id || 'legacy'
                  return (
                    <div className="ca-account" key={key}>
                      <div className="ca-account-main">
                        <b>{account.label}</b>
                        <span>{account.kind === 'api_key' ? 'API key' : `${account.plan ? `${account.plan} ` : ''}subscription`}</span>
                        {account.auth_refused_code ? <span className="ca-refused">Needs a new link</span>
                          : account.active && <span className="ca-route">Recently used</span>}
                      </div>
                      {account.auth_refused_code && <p className="ca-refused-why">{account.auth_refused_code === 'authentication_failed'
                        ? 'Claude no longer accepts this account. It expired or was revoked.'
                        : account.auth_refused_code === 'oauth_org_not_allowed'
                          ? "This account's organization does not allow it here." : 'Claude refused this account.'}</p>}
                      <div className="ca-account-meta">
                        {typeof account.usage_tokens === 'number' && <span>{account.usage_tokens.toLocaleString()} tokens used</span>}
                        {account.cooldown_until && <span title={absoluteWithZone(Date.parse(account.cooldown_until))}>cooldown until {relativeTime(Date.parse(account.cooldown_until), now)}</span>}
                        {account.linked_at && <span title={absoluteWithZone(Date.parse(account.linked_at))}>Linked {relativeTime(Date.parse(account.linked_at), now)}</span>}
                      </div>
                      <div className="ca-account-actions">
                        {account.auth_refused_code && <button className="chip-neutral" aria-label={account.kind === 'api_key' ? `Link again: ${account.label}` : `Add an API key instead of ${account.label}`} disabled={busy} onClick={() => {
                          replacement.current = account.kind === 'api_key' ? account.id : null
                          setLabel(account.kind === 'api_key' ? account.label : 'My API key')
                          setToken('')
                          setFormError(null)
                          setShowAdd(true)
                        }}>{account.kind === 'api_key' ? 'Link again' : 'Add an API key'}</button>}
                        {confirmRemove !== key ? (
                          <button className="chip-danger" onClick={() => { setSuccess(null); setConfirmRemove(key) }} disabled={busy}>Unlink</button>
                        ) : (
                          <>
                            <button className="chip-danger-confirm" onClick={() => { setSuccess(null); onUnlink(account.id || undefined) }} disabled={busy}>
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
              {!showAdd && <button className="btn ca-act" onClick={() => { replacement.current = null; setToken(''); setFormError(null); setLabel('My API key'); setShowAdd(true) }} disabled={busy} title={busy ? 'Please wait for the account update.' : undefined}>Add an API key</button>}
            </div>
          )}

          {addFormShown && (
            <div className="ca-setup">
              {!linked && (
                <p className="ca-copy ca-empty">
                  No Anthropic API key is linked yet.
                </p>
              )}
              <ol className="ca-steps" aria-label="How to get an Anthropic API key">
                <li>Sign in to <a href={ANTHROPIC_CONSOLE_KEYS_URL} target="_blank" rel="noreferrer">Claude Console</a> and open API keys.</li>
                <li>Create a key and copy it. Console shows it only once.</li>
                <li>Paste it below.</li>
              </ol>
              <p className="ca-copy">Usage bills to your Anthropic account at API rates. Add credit in Claude Console if your account has none.</p>
              <label className="ca-field">
                <span className="ca-field-k">Key label</span>
                <input type="text" value={label} onChange={(event) => setLabel(event.target.value)} maxLength={120} disabled={busy} />
              </label>
              <label className={`ca-field${error ? ' field-invalid' : ''}`}>
                <span className="ca-field-k">Anthropic API key</span>
                <input
                  // A masked text field, not type=password: Chrome offers to save a password
                  // field's value, and this token must never land in a password manager (issue 25).
                  type="text"
                  className="ca-secret"
                  name="leaf-claude-credential"
                  autoCapitalize="off"
                  autoCorrect="off"
                  data-1p-ignore="true"
                  data-lpignore="true"
                  data-bwignore="true"
                  writingsuggestions="false"
                  data-form-type="other"
                  value={token}
                  onChange={(event) => setToken(event.target.value)}
                  onKeyDown={(event) => {
                    if (isCompositionKey(event)) return
                    if (event.key === 'Enter') submit()
                  }}
                  placeholder="sk-ant-api03-…"
                  autoComplete="off"
                  spellCheck={false}
                  disabled={busy}
                  aria-label="Anthropic API key"
                />
              </label>
              <button className="btn primary ca-act" onClick={submit} disabled={busy || !token.trim()} title={busy ? 'Please wait for the account update.' : !token.trim() ? 'Enter your credential to link an account.' : undefined}>
                {busy ? 'Linking…' : 'Link API key'}
              </button>
              {formError && <p ref={formErrorRef} className="field-err ca-form-err" role="alert">{formError}</p>}
              {serverError}
              {linked && <button className="chip-neutral" onClick={() => { replacement.current = null; setToken(''); setFormError(null); setShowAdd(false) }} disabled={busy}>Cancel</button>}
              <p className="ca-note">Credentials are stored securely for your workspace and are hidden after linking.</p>
            </div>
          )}
          {!addFormShown && serverError}
        </div>
      )}
    </span>
  )
}
