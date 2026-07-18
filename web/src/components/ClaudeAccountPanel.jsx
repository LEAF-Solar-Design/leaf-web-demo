import { useEffect, useRef, useState } from 'react'

// Claude account (CONCERN 2 — the user's Claude login). This is the SIBLING of
// the platform identity (the Auth0 JWT / tenant chip), and NEVER the same thing
// (contract/AUTH.md §0): the platform JWT says WHICH workspace a request belongs
// to; this grant says WHOSE Anthropic credit runs the authoring agent. The two
// are kept strictly apart — this panel touches only the Claude OAuth grant.
//
// The token is WRITE-ONLY here: it is typed into a masked field, POSTed once,
// and the field is cleared immediately. We never read it back (GET returns only
// {linked, linked_at}), never echo it, never log it.
//
// States (LIVE only — mock renders nothing):
//   not linked — plain copy + a masked paste field + Link.
//   linked     — the linked date + Unlink (inline confirm).
//
// Calm vocabulary: a square mono trigger in the header .who cluster, a plain
// bordered popover, word actions — no emoji, no rounded status pills, no spinner.
function fmtWhen(iso) {
  if (!iso) return null
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso)
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' })
}

export default function ClaudeAccountPanel({ mock, grant, loading, busy, error, open, onToggle, onLink, onUnlink }) {
  const [token, setToken] = useState('')
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

  // Reset the inline unlink confirm whenever the popover closes or linkage flips.
  useEffect(() => { if (!open) setConfirmUnlink(false) }, [open])
  const linked = !!(grant && grant.linked)
  useEffect(() => { if (linked) setToken('') }, [linked])

  // Mock: Concern 2 is a live-mode surface only — render nothing.
  if (mock) return null

  const submit = async () => {
    const t = token.trim()
    if (!t || busy) return
    // Hand the token off ONCE; clear our field immediately regardless of outcome
    // so it never lingers in component state. Never logged/echoed.
    await onLink(t)
    setToken('')
  }

  const state = linked ? 'linked' : 'not linked'

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
        <span className={`ca-state ${linked ? 'on' : ''}`}>{loading ? 'checking' : state}</span>
      </button>

      {open && (
        <div className="claude-pop" role="dialog" aria-label="Claude account">
          <div className="ca-head">
            <span>Claude account</span>
            <button className="ca-x" onClick={() => onToggle(false)}>close</button>
          </div>

          {linked ? (
            <div className="ca-linked">
              <p className="ca-copy">
                Agent authoring is running on <b>your Claude subscription</b>.
                {grant.linked_at ? <> Linked <b>{fmtWhen(grant.linked_at)}</b>.</> : null}
              </p>
              {error && <div className="ca-err">{error}</div>}
              {!confirmUnlink ? (
                <button className="btn ghost ca-act" onClick={() => setConfirmUnlink(true)} disabled={busy}>
                  Unlink
                </button>
              ) : (
                <div className="ca-confirm">
                  <span className="ca-confirm-q">Unlink this Claude account?</span>
                  <button className="btn ghost ca-yes" onClick={onUnlink} disabled={busy}>
                    {busy ? 'unlinking…' : 'yes, unlink'}
                  </button>
                  <button className="btn ghost" onClick={() => setConfirmUnlink(false)} disabled={busy}>cancel</button>
                </div>
              )}
              <p className="ca-note">
                Unlinking stops agent authoring for this workspace. Templated authoring keeps working.
              </p>
            </div>
          ) : (
            <div className="ca-setup">
              <p className="ca-copy">
                Build authoring uses <b>your Claude subscription</b>. To link it:
              </p>
              <ol className="ca-steps">
                <li>Run <code>claude setup-token</code> in a terminal.</li>
                <li>Approve the request in the browser it opens.</li>
                <li>Paste the token it prints below.</li>
              </ol>
              <label className="ca-field">
                <span className="ca-field-k">Claude token</span>
                <input
                  type="password"
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') submit() }}
                  placeholder="sk-ant-oat01-…"
                  autoComplete="off"
                  spellCheck={false}
                  disabled={busy}
                />
              </label>
              {error && <div className="ca-err">{error}</div>}
              <button className="btn primary ca-act" onClick={submit} disabled={busy || !token.trim()}>
                {busy ? 'linking…' : 'Link Claude account'}
              </button>
              <p className="ca-note">
                The token is stored securely for this workspace and never shown again. It is not your
                Leaf sign-in — it only funds the authoring agent.
              </p>
            </div>
          )}
        </div>
      )}
    </span>
  )
}
