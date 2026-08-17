/**
 * OperatorConsole — composes the Wave 2 Lane E deliverables into one drawer:
 * clear current profile/environment, session list + transcript, fleet/tenant
 * inspection, worker job status, approval cards, and audit + artifact links.
 *
 * Acceptance #4 (stale/revoked session -> safe signed-out state): this is
 * the ONE place that subscribes to operatorClient's signed-out channel,
 * which fires on 401/403/404 from ANY operator call any panel makes. On
 * that signal every child unmounts (dropping its local state — no cached
 * transcript survives) and this renders a calm "signed out" message with no
 * retry loop; the operator must close and reopen (a fresh probe) to try again.
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import * as operatorClient from '../../operatorClient.js'
import AuditPanel from './AuditPanel.jsx'
import RunbooksPanel from './RunbooksPanel.jsx'
import SessionPanel from './SessionPanel.jsx'
import WorkerJobsPanel from './WorkerJobsPanel.jsx'

export default function OperatorConsole({ onClose }) {
  const [context, setContext] = useState(null) // {profile, environment} from the default session
  const [signedOut, setSignedOut] = useState(false)
  const closeButtonRef = useRef(null)
  // Once signed out, NOTHING repopulates identity: a createSession promise
  // that resolves after the reset must not restore the profile badge
  // (acceptance #4 — no cached identity in the signed-out state).
  const signedOutRef = useRef(false)

  const reset = useCallback(() => {
    signedOutRef.current = true
    setSignedOut(true)
    setContext(null)
  }, [])

  useEffect(() => operatorClient.subscribeOperatorSignedOut(reset), [reset])

  useEffect(() => {
    let cancelled = false
    operatorClient.createSession()
      .then((session) => { if (!cancelled && !signedOutRef.current) setContext(session) })
      .catch((e) => { if (!cancelled && operatorClient.isOperatorDenied(e)) reset() })
    return () => { cancelled = true }
  }, [reset])

  useEffect(() => { closeButtonRef.current?.focus() }, [])

  const onKeyDown = (e) => {
    if (e.key === 'Escape') onClose?.()
  }

  return (
    <div
      className="operator-console"
      role="dialog"
      aria-modal="true"
      aria-label="Operator console"
      onKeyDown={onKeyDown}
    >
      <header className="operator-console-head">
        <h1>Operator console</h1>
        {context && !signedOut && (
          <p className="operator-profile-badge" role="status">
            Profile <strong>{context.profile}</strong> · Environment <strong>{context.environment}</strong>
          </p>
        )}
        <button type="button" ref={closeButtonRef} className="chip-act" onClick={onClose}>
          Close
        </button>
      </header>

      {signedOut ? (
        <p className="operator-signed-out" role="alert">
          Your operator session is no longer valid. Close this console and reopen it to try again.
        </p>
      ) : (
        <div className="operator-console-body">
          <SessionPanel onSignedOut={reset} />
          <RunbooksPanel sessionEnvironment={context?.environment} onSignedOut={reset} />
          <WorkerJobsPanel onSignedOut={reset} />
          <AuditPanel onSignedOut={reset} />
        </div>
      )}
    </div>
  )
}
