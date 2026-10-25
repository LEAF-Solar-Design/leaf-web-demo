/**
 * AuditPanel — "audit and artifact links" (Wave 2 Lane E plan). Read-only:
 * GET /api/operator/audit, own-subject rows only (server/routers/
 * operator_sessions.py:read_audit). Each row links its authority_id to the
 * secret-handle metadata surface (never a value) when the audit `extra`
 * payload names one, and to the session it belongs to.
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import * as operatorClient from '../../operatorClient.js'
import { humanizeError } from '../../errorHumanize.js'

export default function AuditPanel({ onSignedOut }) {
  const [rows, setRows] = useState([])
  const [error, setError] = useState(null)
  const auditRequest = useRef(0)

  const load = useCallback(async () => {
    const generation = ++auditRequest.current
    setError(null)
    try {
      const nextRows = await operatorClient.getAudit()
      if (auditRequest.current !== generation) return
      setRows(nextRows)
      setError(null)
    } catch (e) {
      if (operatorClient.isOperatorDenied(e)) onSignedOut?.()
      if (auditRequest.current !== generation) return
      setError(humanizeError(e))
    }
  }, [onSignedOut])

  useEffect(() => { load() }, [load])

  return (
    <section className="operator-panel operator-audit-panel" aria-label="Audit log">
      <h2>Audit</h2>
      <button type="button" className="chip-act" onClick={load}>Refresh</button>
      <table className="operator-audit-table">
        <thead>
          <tr><th>Time</th><th>Action</th><th>Decision</th><th>Reason</th><th>Artifact</th></tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={`${r.authority_id || 'noauth'}-${r.ts}`}>
              <td>{r.ts}</td>
              <td>{r.action}</td>
              <td>{r.decision}</td>
              <td>{r.reason}</td>
              <td>
                {r.session_id ? (
                  <a href={`#operator-session-${encodeURIComponent(r.session_id)}`}>
                    session {r.session_id}
                  </a>
                ) : <span className="dim">—</span>}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr><td colSpan={5} className="dim">No audit rows yet.</td></tr>
          )}
        </tbody>
      </table>
      {error && <p className="operator-panel-error" role="alert">{error}</p>}
    </section>
  )
}
