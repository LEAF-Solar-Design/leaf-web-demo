import { useEffect, useRef, useState } from 'react'
import './popovers.css'

// Claude account (CONCERN 2 — the user's Claude login). This is the SIBLING of
// the platform identity (the Auth0 JWT / tenant chip), and NEVER the same thing
// (contract/AUTH.md §0): the platform JWT says WHICH workspace a request belongs
// to; this grant says WHOSE Anthropic credit runs the authoring agent. The two
// are kept strictly apart — this panel touches only the Claude OAuth grant.
//
// The user links one of two credential KINDS, one masked field either way:
//   • "oauth"   — a Claude SUBSCRIPTION token from `claude setup-token` (runs
//                 authoring on their Claude Pro/Max/Team seat).
//   • "api_key" — an Anthropic org API KEY (enterprise; usage billed to their
//                 Anthropic account at API rates).
// The choice is sent EXPLICITLY as `kind`; the linked state names the kind
// plainly ("linked · subscription" / "linked · API key"). A recognizable token
// prefix (sk-ant-api… / sk-ant-oat…) auto-selects the matching kind as a
// convenience — the user can still override.
//
// The token/key is WRITE-ONLY here: it is typed into a masked field, POSTed once,
// and the field is cleared immediately. We never read it back (GET returns only
// {linked, linked_at, kind}), never echo it, never log it.
//
// Calm vocabulary: a square mono trigger in the header .who cluster, a plain
// bordered popover, quiet chips, the Esc cap to close — no emoji, no rounded
// status pills, no spinner. Unlink is destructive, so it follows D1: quiet
// danger chip -> inline swap to "[Unlink = filled danger chip] [Keep]".
//
// TM1 time: relative under a day ("2 m", "2 h"), "Jul 12" after; the absolute
// clock rides the hover title.
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
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' })
}

// Plain-word label for a credential kind. Unknown/absent -> null (show "linked").
function kindLabel(kind) {
  if (kind === 'api_key') return 'API key'
  if (kind === 'oauth') return 'subscription'
  return null
}

// Recognize a definitive token prefix so we can auto-select the matching kind.
// sk-ant-api… = an Anthropic API key; sk-ant-oat… = a setup-token (subscription).
// Anything ambiguous returns null (leave the manual choice untouched).
function detectKind(tok) {
  const s = (tok || '').trim()
  if (/^sk-ant-api/i.test(s)) return 'api_key'
  if (/^sk-ant-oat/i.test(s)) return 'oauth'
  return null
}

export default function ClaudeAccountPanel({ mock, grant, loading, busy, error, open, onToggle, onLink, onUnlink }) {
  const [token, setToken] = useState('')
  const [kind, setKind] = useState('oauth') // the chosen credential kind (setup-token by default)
  const [confirmUnlink, setConfirmUnlink] = useState(false)
  const rootRef = useRef(null)

  // Close on outside-click / Escape (matches the ProjectSwitcher popover).
  useEffect(() => {
    if (!open) return
    const onDoc = (e) => { if (rootRef.current && !rootRef.current.contains(e.target)) onToggle(false) }
    const onKey = (e) => { if (e.key === 'Escape') onToggle(false) }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('mousedown', onDoc); document.removeEventListener('keydown', onKey) }
  }, [open, onToggle])

  // Reset the inline unlink confirm + the chooser whenever the popover closes.
  useEffect(() => { if (!open) { setConfirmUnlink(false); setKind('oauth') } }, [open])
  const linked = !!(grant && grant.linked)
  useEffect(() => { if (linked) setToken('') }, [linked])

  // Mock: Concern 2 is a live-mode surface only — render nothing.
  if (mock) return null

  const changeToken = (v) => {
    setToken(v)
    // Auto-select the kind when the pasted value has a definitive prefix.
    const d = detectKind(v)
    if (d) setKind(d)
  }

  const submit = async () => {
    const t = token.trim()
    if (!t || busy) return
    // Hand the token off ONCE with the explicit kind; clear our field immediately
    // regardless of outcome so it never lingers in component state. Never logged.
    await onLink(t, kind)
    setToken('')
  }

  const kl = kindLabel(grant && grant.kind)
  const stateLabel = loading ? 'checking' : (linked ? (kl ? `linked · ${kl}` : 'linked') : 'not linked')
  const isApiKey = kind === 'api_key'

  return (
    <span className="claude-acct" ref={rootRef}>
      <button
        className="claude-trigger"
        onClick={() => onToggle(!open)}
        aria-expanded={open}
        aria-haspopup="dialog"
        title="Link the Claude account that funds agent authoring (Concern 2 — separate from platform sign-in)"
      >
        <span className="ca-k">Claude account</span>
        <span className={`ca-state ${linked ? 'on' : ''}`}>{stateLabel}</span>
      </button>

      {open && (
        <div className="claude-pop" role="dialog" aria-label="Claude account">
          <div className="ca-head">
            <span>Claude account</span>
            <button className="key hot" onClick={() => onToggle(false)} aria-label="Close Claude account panel">Esc</button>
          </div>

          {linked ? (
            <div className="ca-linked">
              <div className="ca-kindline">linked · {kl || 'account'}</div>
              <p className="ca-copy">
                Agent authoring is running on{' '}
                {grant.kind === 'api_key'
                  ? <b>your Anthropic API key</b>
                  : <b>your Claude subscription</b>}.
                {grant.linked_at ? <> Linked <b title={fmtAbs(grant.linked_at)}>{fmtWhen(grant.linked_at)}</b>.</> : null}
              </p>
              {error && (
                <div className="field-err" role="alert"><span className="dot red" />{error}</div>
              )}
              {!confirmUnlink ? (
                <button className="chip-danger ca-act" onClick={() => setConfirmUnlink(true)} disabled={busy}>
                  Unlink
                </button>
              ) : (
                <div className="ca-confirm">
                  <span className="ca-confirm-q">Unlink this Claude account?</span>
                  <button className="chip-danger-confirm" onClick={onUnlink} disabled={busy}>
                    {busy ? 'Unlinking…' : 'Unlink'}
                  </button>
                  <button className="chip-neutral" onClick={() => setConfirmUnlink(false)} disabled={busy}>Keep</button>
                </div>
              )}
              <p className="ca-note">
                Unlinking stops agent authoring for this workspace. Templated authoring keeps working.
              </p>
            </div>
          ) : (
            <div className="ca-setup">
              <div className="ca-choice" role="radiogroup" aria-label="Credential type">
                <button
                  type="button"
                  role="radio"
                  aria-checked={!isApiKey}
                  className={`ca-opt ${!isApiKey ? 'on' : ''}`}
                  onClick={() => setKind('oauth')}
                  disabled={busy}
                >
                  Claude subscription token
                </button>
                <button
                  type="button"
                  role="radio"
                  aria-checked={isApiKey}
                  className={`ca-opt ${isApiKey ? 'on' : ''}`}
                  onClick={() => setKind('api_key')}
                  disabled={busy}
                >
                  Anthropic API key
                </button>
              </div>

              {isApiKey ? (
                <>
                  <p className="ca-copy">
                    Paste an <b>Anthropic API key</b> — for org API keys — usage billed to your Anthropic
                    account at API rates.
                  </p>
                  <label className={`ca-field${error ? ' field-invalid' : ''}`}>
                    <span className="ca-field-k">Anthropic API key</span>
                    <input
                      type="password"
                      value={token}
                      onChange={(e) => changeToken(e.target.value)}
                      onKeyDown={(e) => { if (e.key === 'Enter') submit() }}
                      placeholder="sk-ant-api03-…"
                      autoComplete="off"
                      spellCheck={false}
                      disabled={busy}
                    />
                  </label>
                </>
              ) : (
                <>
                  <p className="ca-copy">
                    Build authoring uses <b>your Claude subscription</b>. To link it:
                  </p>
                  <ol className="ca-steps">
                    <li>Run <code>claude setup-token</code> in a terminal.</li>
                    <li>Approve the request in the browser it opens.</li>
                    <li>Paste the token it prints below.</li>
                  </ol>
                  <label className={`ca-field${error ? ' field-invalid' : ''}`}>
                    <span className="ca-field-k">Claude token</span>
                    <input
                      type="password"
                      value={token}
                      onChange={(e) => changeToken(e.target.value)}
                      onKeyDown={(e) => { if (e.key === 'Enter') submit() }}
                      placeholder="sk-ant-oat01-…"
                      autoComplete="off"
                      spellCheck={false}
                      disabled={busy}
                    />
                  </label>
                </>
              )}

              {error && (
                <div className="field-err" role="alert"><span className="dot red" />{error}</div>
              )}
              <button className="btn primary ca-act" onClick={submit} disabled={busy || !token.trim()}>
                {busy ? 'Linking…' : (isApiKey ? 'Link API key' : 'Link Claude account')}
              </button>
              <p className="ca-note">
                {isApiKey
                  ? 'The key is stored securely for this workspace and never shown again. It is not your Leaf sign-in — it only funds the authoring agent.'
                  : 'The token is stored securely for this workspace and never shown again. It is not your Leaf sign-in — it only funds the authoring agent.'}
              </p>
            </div>
          )}
        </div>
      )}
    </span>
  )
}
