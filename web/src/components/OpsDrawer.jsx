import { useCallback, useEffect, useState } from 'react'
import { getOpsTenants, setTenantDisabled } from '../api.js'

// INTERNAL ops drawer — visible only with ?ops=1 in the URL. Lists tenants from
// GET /api/ops/tenants (X-Internal-Role: qa): tenant / runs / spend / state,
// with calm disable/enable actions (inline confirm-before-act, plain words).
// A 403 (role not granted) renders a calm "ops role required" note, not a red
// failure. This is an internal surface — clearly labelled INTERNAL, distinct
// from the tenant-facing app.
export default function OpsDrawer({ onDismiss }) {
  const [tenants, setTenants] = useState(null)
  const [err, setErr] = useState(null)
  const [forbidden, setForbidden] = useState(false)
  const [loading, setLoading] = useState(false)
  const [confirming, setConfirming] = useState(null) // tenant_id awaiting confirm
  const [acting, setActing] = useState(null)         // tenant_id with an action in flight

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

  useEffect(() => { load() }, [load])

  const act = useCallback(async (tid, disabled) => {
    setActing(tid); setConfirming(null); setErr(null)
    try {
      await setTenantDisabled(tid, disabled)
      // The disable/enable ack is authoritative for THIS tenant's kill-switch
      // state, so reflect it directly. We deliberately do NOT re-fetch the list
      // here: the list reads kill-switch state from the broker's /health, which
      // lags the proxied write by a beat, so an immediate reload would briefly
      // clobber the just-applied truth with a stale value. The Refresh button
      // (and the next open) reconcile the full list.
      setTenants((prev) => (prev || []).map((t) => (t.tenant_id === tid ? { ...t, disabled } : t)))
    } catch (e) {
      if (e && e.status === 403) setForbidden(true)
      else setErr(String(e.message || e))
    } finally {
      setActing(null)
    }
  }, [load])

  return (
    <aside className="ops-drawer" role="region" aria-label="Internal ops">
      <div className="ops-head">
        <span className="ops-badge">INTERNAL</span>
        <span className="ops-title">Ops · tenants</span>
        <button className="ops-x" onClick={load} disabled={loading} title="Refresh">
          {loading ? 'loading' : 'refresh'}
        </button>
        {onDismiss && <button className="ops-x" onClick={onDismiss} title="Hide drawer">close</button>}
      </div>

      {forbidden && (
        <div className="ops-note">ops role required — this surface needs the QA/internal role.</div>
      )}
      {err && !forbidden && <div className="ops-note ops-err">Couldn’t load tenants: {err}</div>}

      {!forbidden && !err && tenants && tenants.length === 0 && (
        <div className="ops-note">No tenants recorded yet.</div>
      )}

      {!forbidden && !err && tenants && tenants.length > 0 && (
        <table className="ops-table">
          <thead>
            <tr><th>tenant</th><th className="num">runs</th><th className="num">spend</th><th>state</th><th /></tr>
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
                    <span className={`state ${disabled ? 'fail' : 'done'}`}>
                      {disabled ? 'disabled' : 'active'}
                    </span>
                  </td>
                  <td className="ops-act-cell">
                    {isConfirming ? (
                      <span className="ops-confirm">
                        <span className="ops-confirm-q">
                          {disabled ? 'Enable this tenant?' : 'Disable this tenant?'}
                        </span>
                        <button className="btn ghost ops-yes" onClick={() => act(t.tenant_id, !disabled)} disabled={isActing}>
                          {isActing ? 'working…' : 'yes'}
                        </button>
                        <button className="btn ghost" onClick={() => setConfirming(null)} disabled={isActing}>cancel</button>
                      </span>
                    ) : (
                      <button
                        className="btn ghost ops-toggle"
                        onClick={() => setConfirming(t.tenant_id)}
                        disabled={isActing}
                      >
                        {disabled ? 'enable' : 'disable'}
                      </button>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}

      <div className="ops-foot">GET /api/ops/tenants · X-Internal-Role: qa</div>
    </aside>
  )
}
