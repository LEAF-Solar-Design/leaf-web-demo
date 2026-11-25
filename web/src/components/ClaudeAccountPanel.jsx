import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import EscCap from './EscCap.jsx'
import './popovers.css'
import useExit from '../useExit.js'
import useEscapeOwner from '../lib/useEscapeOwner.js'
import { absoluteWithZone, relativeTime } from '../lib/railTime.js'
import useRelativeNow from '../lib/useRelativeNow.js'
import { isCompositionKey } from '../lib/keyComposition.js'
import { humanizeError } from '../errorHumanize.js'

export const SETUP_TOKEN_COMMAND = 'claude setup-token'
const CLAUDE_CODE_INSTALL_URL = 'https://code.claude.com/docs/en/setup'
const INSTALL_COMMAND = 'irm https://claude.ai/install.ps1 | iex'
const ANTHROPIC_CONSOLE_KEYS_URL = 'https://console.anthropic.com/settings/keys'

function detectKind(token) {
  if (/^sk-ant-api/i.test((token || '').trim())) return 'api_key'
  if (/^sk-ant-oat/i.test((token || '').trim())) return 'oauth'
  return null
}

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
  const [kind, setKind] = useState('oauth')
  const [plan, setPlan] = useState(null)
  const [label, setLabel] = useState('')
  const [confirmRemove, setConfirmRemove] = useState(null)
  const [showAdd, setShowAdd] = useState(false)
  const [copied, setCopied] = useState({})
  const [copyError, setCopyError] = useState(false)
  const copyTimers = useRef({})
  const copyMounted = useRef(true)
  const pending = useRef(false)
  const panelLifetime = useRef(0)
  const refreshOpen = useRef(open)
  const replacement = useRef(null)
  const successRef = useRef(null)
  const formErrorRef = useRef(null)
  const errorRef = useRef(null)
  const [success, setSuccess] = useState(null)
  const [formError, setFormError] = useState(null)
  const copyCommand = async (name, command) => {
    try {
      await navigator.clipboard.writeText(command)
      if (!copyMounted.current) return
      setCopyError(false)
      setCopied((current) => ({ ...current, [name]: true }))
      clearTimeout(copyTimers.current[name])
      copyTimers.current[name] = setTimeout(() => setCopied((current) => ({ ...current, [name]: false })), 2000)
    } catch { if (copyMounted.current) setCopyError(true) }
  }
  useEffect(() => {
    copyMounted.current = true
    return () => {
      copyMounted.current = false
      Object.values(copyTimers.current).forEach(clearTimeout)
    }
  }, [])
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
      setLabel((current) => current || 'My Claude account')
    }
  }, [canManage, linked, accounts.length])

  useEffect(() => { if (success && canManage && linked) successRef.current?.focus() }, [success, canManage, linked])
  useEffect(() => {
    if (!open || signedOut) return
    if (formError) formErrorRef.current?.scrollIntoView?.({ block: 'nearest' })
    if (error) errorRef.current?.scrollIntoView?.({ block: 'nearest' })
  }, [error, formError, open, signedOut, canManage, linked, showAdd])

  if (mock) return null

  const changeToken = (value) => {
    setToken(value)
    const detected = detectKind(value)
    if (detected) setKind(detected)
  }

  const submit = async () => {
    let value = token.trim()
    if (value.startsWith('sk-ant-')) value = value.replace(/\s/g, '')
    if (!canManage || signedOut || !value || busy || pending.current) return
    setSuccess(null)
    // A click with a missing choice explains itself instead of doing nothing (rehearsal issue 24).
    if (/sk-ant-/i.test(label)) { setLabel(''); setFormError('The token goes in the token field, not the account label.'); return }
    if (kind === 'oauth' && !plan) { setFormError('Choose your Claude plan above (Pro, Max, Team or Enterprise).'); return }
    if (kind === 'oauth' && !label.trim()) { setFormError('Enter an account label.'); return }
    if (kind === 'oauth' && (value.includes('#') || /^https?:\/\//.test(value))) {
      setFormError('That is the sign-in code or link, not the token. Paste the code into the terminal, then copy the token it prints (it starts with sk-ant-oat01-).'); return
    }
    if (kind === 'oauth' && value.startsWith('claude ')) {
      setFormError('That is the command. Run it in a terminal, then paste the token it prints.'); return
    }
    setFormError(null)
    pending.current = true
    setToken('')
    const oldId = replacement.current
    const lifetime = panelLifetime.current
    try {
      const result = await onLink(value, kind, label.trim(), kind === 'oauth' ? plan : undefined)
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
        const text = count === 1 ? `1 account linked${planName ? `, ${planName}` : ''}.`
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
  const isApiKey = kind === 'api_key'
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
                        ? 'Claude no longer accepts this token. It expired or was revoked.'
                        : account.auth_refused_code === 'oauth_org_not_allowed'
                          ? "This account's organization does not allow it here." : 'Claude refused this account.'}</p>}
                      <div className="ca-account-meta">
                        {typeof account.usage_tokens === 'number' && <span>{account.usage_tokens.toLocaleString()} tokens used</span>}
                        {account.cooldown_until && <span title={absoluteWithZone(Date.parse(account.cooldown_until))}>cooldown until {relativeTime(Date.parse(account.cooldown_until), now)}</span>}
                        {account.linked_at && <span title={absoluteWithZone(Date.parse(account.linked_at))}>Linked {relativeTime(Date.parse(account.linked_at), now)}</span>}
                      </div>
                      <div className="ca-account-actions">
                        {account.auth_refused_code && <button className="chip-neutral" aria-label={`Link again: ${account.label}`} disabled={busy} onClick={() => {
                          replacement.current = account.id
                          setKind(account.kind)
                          setPlan(account.plan || null)
                          setLabel(account.label)
                          setToken('')
                          setFormError(null)
                          setShowAdd(true)
                        }}>Link again</button>}
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
              {!showAdd && <button className="btn ca-act" onClick={() => { replacement.current = null; setToken(''); setFormError(null); setShowAdd(true) }} disabled={busy} title={busy ? 'Please wait for the account update.' : undefined}>Link another account</button>}
            </div>
          )}

          {addFormShown && (
            <div className="ca-setup">
              {!linked && (
                <p className="ca-copy ca-empty">
                  No Claude account is linked yet. Link your Claude subscription with a token from <code>claude setup-token</code>. You do not need an API key.
                </p>
              )}
              <div className="ca-choice" role="radiogroup" aria-label="Credential type">
                <button type="button" role="radio" aria-checked={!isApiKey} className={`ca-opt ${!isApiKey ? 'on' : ''}`} onClick={() => { setKind('oauth'); setFormError(null) }} disabled={busy}>Claude subscription</button>
                <button type="button" role="radio" aria-checked={isApiKey} className={`ca-opt ${isApiKey ? 'on' : ''}`} onClick={() => { setKind('api_key'); setFormError(null) }} disabled={busy}>Anthropic API key</button>
              </div>

              {!isApiKey && (
                <>
                  <p className="ca-copy">Link your authorized <b>Claude Pro, Max, Team, or Enterprise</b> subscription.</p>
                  <ol className="ca-steps" aria-label="How to get a Claude setup token">
                    <li>Install <a href={CLAUDE_CODE_INSTALL_URL} target="_blank" rel="noreferrer">Claude Code</a> (free, one time, no Node.js needed).{' '}
                      Windows PowerShell:{' '}
                      <span className="ca-cmd"><code>{INSTALL_COMMAND}</code>
                        <button type="button" className="chip-neutral ca-copy-cmd" aria-label={copied.install ? 'Install command copied' : 'Copy install command'} onClick={() => copyCommand('install', INSTALL_COMMAND)}>{copied.install ? 'Copied' : 'Copy'}</button>
                      </span>
                    </li>
                    <li>
                      Open a new terminal and run{' '}
                      <span className="ca-cmd"><code>{SETUP_TOKEN_COMMAND}</code>
                        <button type="button" className="chip-neutral ca-copy-cmd" aria-label={copied.setup ? 'Setup command copied' : 'Copy setup command'} onClick={() => copyCommand('setup', SETUP_TOKEN_COMMAND)}>{copied.setup ? 'Copied' : 'Copy'}</button>
                      </span>
                    </li>
                    <li>Approve the request in the browser window that opens. If no browser opens, press c in the terminal to copy the link and open it yourself. If the page shows a code, paste it into the terminal. Codes expire quickly: if one is refused, run the command again.</li>
                    <li>Copy the token it prints (it starts with <code>sk-ant-oat01-</code>) and paste it below.</li>
                  </ol>
                  {copyError && <p className="ca-copy" role="status">Copy was blocked. Select the command and copy it by hand.</p>}
                  <div className="ca-choice" role="radiogroup" aria-label="Claude workspace plan">
                    {['pro', 'max', 'team', 'enterprise'].map((value) => (
                      <button key={value} type="button" role="radio" aria-checked={plan === value} className={`ca-opt ${plan === value ? 'on' : ''}`} onClick={() => { setPlan(value); setFormError(null) }} disabled={busy}>{value[0].toUpperCase() + value.slice(1)}</button>
                    ))}
                  </div>
                  <label className="ca-field">
                    <span className="ca-field-k">Account label</span>
                    <input type="text" value={label} onChange={(event) => setLabel(event.target.value)} placeholder="Design team east" maxLength={120} disabled={busy} />
                  </label>
                </>
              )}

              {isApiKey && (
                <p className="ca-copy">
                  Create a key at <a href={ANTHROPIC_CONSOLE_KEYS_URL} target="_blank" rel="noreferrer">console.anthropic.com</a> under
                  {' '}API keys, then paste it below. Usage is billed at API rates.
                </p>
              )}
              <label className={`ca-field${error ? ' field-invalid' : ''}`}>
                <span className="ca-field-k">{isApiKey ? 'Anthropic API key' : 'Claude setup token'}</span>
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
                  onChange={(event) => changeToken(event.target.value)}
                  onKeyDown={(event) => {
                    if (isCompositionKey(event)) return
                    if (event.key === 'Enter') submit()
                  }}
                  placeholder={isApiKey ? 'sk-ant-api03-…' : 'sk-ant-oat01-…'}
                  autoComplete="off"
                  spellCheck={false}
                  disabled={busy}
                  aria-label={isApiKey ? 'Anthropic API key' : 'Claude token'}
                />
              </label>
              <button className="btn primary ca-act" onClick={submit} disabled={busy || !token.trim()} title={busy ? 'Please wait for the account update.' : !token.trim() ? 'Enter your credential to link an account.' : undefined}>
                {busy ? 'Linking…' : isApiKey ? 'Link API key' : 'Link Claude account'}
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
