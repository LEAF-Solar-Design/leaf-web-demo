import { useCallback, useEffect, useRef, useState } from 'react'
import { getAccountControls, getOpsTenants, setTenantDisabled, updateAccountControls } from '../api.js'
import './popovers.css'

// Internal ops drawer — visible only with ?ops=1 in the URL. A DT2 right drawer
// (title + Esc cap header, endpoint provenance in the foot) listing tenants from
// GET /api/ops/tenants (X-Internal-Role: qa): tenant / runs / spend / state.
// Disable is destructive, so it follows D1: quiet danger chip -> the row's
// actions swap inline to "Disable <tenant>? [filled danger chip] [Keep]" — red
// only on the destroying act. A kill-switched tenant is a deliberate hold, not
// a failure: amber square-dot "Disabled", never red. A 403 (role not granted)
// renders a calm "ops role required" note; a load failure gets the centered
// X3 takeover with a Retry chip. Clearly labelled Internal, distinct from the
// tenant-facing app. Esc (key or cap) hides the drawer.
// `exiting`: App holds the mount through the 180 ms M1 exit fade (useExit).
export default function OpsDrawer({ onDismiss, exiting }) {
  const drawerRef = useRef(null)
  const closeRef = useRef(null)
  const restoreRef = useRef(null)
  const [tenants, setTenants] = useState(null)
  const [err, setErr] = useState(null)
  const [forbidden, setForbidden] = useState(false)
  const [loading, setLoading] = useState(false)
  const [confirming, setConfirming] = useState(null) // tenant_id awaiting confirm
  const [acting, setActing] = useState(null)         // tenant_id with an action in flight
  const [accountControls, setAccountControls] = useState(null)
  const [accountLoading, setAccountLoading] = useState(false)
  const [accountForbidden, setAccountForbidden] = useState(false)
  const [accountErr, setAccountErr] = useState(null)
  const [accountConfirming, setAccountConfirming] = useState(false)
  const [accountActing, setAccountActing] = useState(false)

  const load = useCallback(async () => {
    setLoading(true); setErr(null); setForbidden(false)
    try {
      setTenants(await getOpsTenants())
    } catch (e) {
      if (e && e.status === 403) { setForbidden(true); setTenants(null) }
      else { setErr(String(e.message || e)); setTenants(null) }
    } finally {
      setLoading(false)
    }
  }, [])

  const loadAccountControls = useCallback(async () => {
    setAccountLoading(true); setAccountErr(null); setAccountForbidden(false); setAccountConfirming(false)
    try {
      setAccountControls(await getAccountControls())
    } catch (e) {
      if (e && e.status === 403) { setAccountForbidden(true); setAccountControls(null) }
      else { setAccountErr(String(e.message || e)); setAccountControls(null) }
    } finally {
      setAccountLoading(false)
    }
  }, [])

  useEffect(() => { load(); loadAccountControls() }, [load, loadAccountControls])

  useEffect(() => {
    restoreRef.current = document.activeElement
    closeRef.current?.focus()
    return () => {
      const target = restoreRef.current
      if (target?.isConnected && typeof target.focus === 'function') target.focus()
    }
  }, [])

  // Own Escape before the scene-level ladder can navigate away from /try.
  useEffect(() => {
    if (!onDismiss) return
    const onKey = (event) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      event.stopImmediatePropagation()
      onDismiss()
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [onDismiss])

  const ownKeyboard = (event) => {
    if (event.key === 'Escape') {
      event.preventDefault()
      event.stopPropagation()
      onDismiss?.()
      return
    }
    if (event.key !== 'Tab') return
    const focusable = [...(drawerRef.current?.querySelectorAll('button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])') || [])]
      .filter((element) => element.getClientRects().length > 0)
    if (!focusable.length) return
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    if (focusable.length === 1 || (!event.shiftKey && document.activeElement === last)) {
      event.preventDefault()
      first.focus()
    } else if (event.shiftKey && document.activeElement === first) {
      event.preventDefault()
      last.focus()
    }
  }

  const act = useCallback(async (tid, disabled) => {
    setActing(tid); setConfirming(null); setErr(null)
    try {
      await setTenantDisabled(tid, disabled)
      // The disable/enable ack is authoritative for THIS tenant's kill-switch
      // state, so reflect it directly. We deliberately do NOT re-fetch the list
      // here: the list reads kill-switch state from the broker's /health, which
      // lags the proxied write by a beat, so an immediate reload would briefly
      // clobber the just-applied truth with a stale value. The Refresh chip
      // (and the next open) reconcile the full list.
      setTenants((prev) => (prev || []).map((t) => (t.tenant_id === tid ? { ...t, disabled } : t)))
    } catch (e) {
      if (e && e.status === 403) setForbidden(true)
      else setErr(String(e.message || e))
    } finally {
      setActing(null)
    }
  }, [load])

  const actOnAccountControl = useCallback(async () => {
    if (!accountControls) return
    const nextRequired = !Boolean(accountControls.tool_publication_approval_required)
    setAccountActing(true); setAccountErr(null)
    try {
      const updated = await updateAccountControls(nextRequired, accountControls.revision)
      setAccountControls(updated)
      setAccountConfirming(false)
    } catch (e) {
      if (e && e.status === 403) setAccountForbidden(true)
      else if (e && e.status === 409) {
        setAccountConfirming(false)
        setAccountErr('Account controls changed elsewhere. Refresh before trying again.')
      }
      else setAccountErr(String(e.message || e))
    } finally {
      setAccountActing(false)
    }
  }, [accountControls])

  return (
    <aside ref={drawerRef} className={`drawer${exiting ? ' exit' : ''}`} role="dialog" aria-modal="true" aria-label="Internal ops" onKeyDown={ownKeyboard}>
      <div className="drawer-head">
        <span className="ops-badge">Internal</span>
        <span className="drawer-title">Ops · tenants</span>
        {onDismiss && (
          <button ref={closeRef} className="key hot" onClick={onDismiss} aria-label="Hide drawer">Esc</button>
        )}
      </div>

      <div className="drawer-body">
        <section className="ops-account-controls" aria-labelledby="ops-account-controls-title">
          <div className="ops-section-head">
            <div>
              <div id="ops-account-controls-title" className="ops-section-title">Current account</div>
              <div className="ops-section-copy">Tool authoring publication policy</div>
            </div>
          </div>

          {accountLoading && !accountControls && (
            <div className="skeleton-row" aria-label="Loading account controls" />
          )}
          {accountForbidden && (
            <div className="ops-note">Account admin required to manage tool publication approval.</div>
          )}
          {accountErr && !accountForbidden && (
            <div className="field-err" role="alert"><span className="dot red" />{accountErr}</div>
          )}
          {accountControls && !accountForbidden && (
            <div className="ops-control-row">
              <div className="ops-control-main">
                <span className="ops-control-label">Require independent approval before publishing authored tools</span>
                <span className="ops-control-help">
                  {accountControls.tool_publication_approval_required
                    ? 'On. An independent trusted approver must approve each publication.'
                    : 'Off. Authored tools can publish without an independent approval.'}
                </span>
              </div>
              {accountConfirming ? (
                <div className="ops-control-confirm">
                  <span className="confirm-q">
                    {accountControls.tool_publication_approval_required
                      ? 'Turn off independent publication approval?'
                      : 'Turn on strict independent publication approval?'}
                  </span>
                  <button className="chip-act" onClick={actOnAccountControl} disabled={accountActing}>
                    {accountActing ? 'Saving…' : 'Confirm'}
                  </button>
                  <button className="chip-neutral" onClick={() => setAccountConfirming(false)} disabled={accountActing}>
                    Keep current
                  </button>
                </div>
              ) : (
                <button
                  className={accountControls.tool_publication_approval_required ? 'chip-act' : 'chip-neutral'}
                  onClick={() => setAccountConfirming(true)}
                  disabled={accountActing}
                  aria-label={`${accountControls.tool_publication_approval_required ? 'On' : 'Off'}: require independent approval before publishing authored tools`}
                >
                  {accountControls.tool_publication_approval_required ? 'On' : 'Off'}
                </button>
              )}
            </div>
          )}
          {(accountErr || accountForbidden) && (
            <button className="chip-act" onClick={loadAccountControls} disabled={accountLoading}>
              {accountLoading ? 'Refreshing…' : 'Refresh account controls'}
            </button>
          )}
        </section>

        {forbidden && (
          <div className="ops-note">ops role required — this surface needs the QA/internal role.</div>
        )}
        {err && !forbidden && (
          <div className="pane-fail" role="alert">
            <span className="pane-fail-title"><span className="dot red" />Couldn’t load tenants</span>
            <span className="pane-fail-reason">{err}</span>
            <button className="chip-act" onClick={load} disabled={loading}>Retry</button>
          </div>
        )}

        {!forbidden && !err && !tenants && loading && (
          <div className="skeleton-stack" aria-label="Loading tenants">
            <div className="skeleton-row" />
            <div className="skeleton-row" />
            <div className="skeleton-row" />
          </div>
        )}

        {!forbidden && !err && tenants && tenants.length === 0 && (
          <div className="ops-note">No tenants recorded yet.</div>
        )}

        {!forbidden && !err && tenants && tenants.length > 0 && (
          <table className="ops-table">
            <thead>
              <tr><th>Tenant</th><th className="num">Runs</th><th className="num">Spend</th><th>State</th><th /></tr>
            </thead>
            <tbody>
              {tenants.map((t) => {
                const disabled = !!t.disabled
                const isConfirming = confirming === t.tenant_id
                const isActing = acting === t.tenant_id
                return (
                  <tr key={t.tenant_id} className={disabled ? 'ops-row-off' : ''}>
                    <td className="ops-tid">{t.tenant_id}</td>
                    <td className="num">{Number(t.runs || 0).toLocaleString()}</td>
                    <td className="num">${Number(t.usd_est || 0).toFixed(3)}</td>
                    <td>
                      {/* Kill-switch = deliberate hold: amber square dot, not red/fail. */}
                      <span className={`state ${disabled ? 'quota' : 'done'}`}>
                        {disabled ? 'Disabled' : 'Active'}
                      </span>
                    </td>
                    <td className="ops-act-cell">
                      {isConfirming ? (
                        <span className="ops-confirm">
                          <span className="confirm-q">
                            {disabled ? `Enable ${t.tenant_id}?` : `Disable ${t.tenant_id}?`}
                          </span>
                          <button
                            className={disabled ? 'chip-act' : 'chip-danger-confirm'}
                            onClick={() => act(t.tenant_id, !disabled)}
                            disabled={isActing}
                          >
                            {isActing ? 'Working…' : (disabled ? 'Enable' : 'Disable')}
                          </button>
                          <button className="chip-neutral" onClick={() => setConfirming(null)} disabled={isActing}>
                            Keep
                          </button>
                        </span>
                      ) : (
                        <button
                          className={disabled ? 'chip-act' : 'chip-danger'}
                          onClick={() => setConfirming(t.tenant_id)}
                          disabled={isActing}
                        >
                          {disabled ? 'Enable' : 'Disable'}
                        </button>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}

        <button className="chip-act drawer-act" onClick={() => { load(); loadAccountControls() }} disabled={loading || accountLoading}>
          {loading || accountLoading ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>

      <div className="drawer-foot">Account controls use signed-in admin access · tenant operations use server-authorized internal access</div>
    </aside>
  )
}
