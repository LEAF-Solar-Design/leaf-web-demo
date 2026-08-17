/**
 * SessionPanel — "operator entry point" + "clear current profile and
 * environment" + "session list and transcript" (Wave 2 Lane E plan).
 *
 * Sessions are personal: UNIQUE(subject, profile, environment) server-side
 * (contract/OPERATOR.md section 2). This panel lists the operator's own
 * sessions, lets them open one (or start one), shows its event transcript,
 * and sends a message. No text field here is named/labeled secret, token,
 * password, or key (acceptance #1) — the only free-text input is the
 * operator's own chat message.
 */
import { useCallback, useEffect, useState } from 'react'

import * as operatorClient from '../../operatorClient.js'

export default function SessionPanel({ onSignedOut }) {
  const [sessions, setSessions] = useState([])
  const [activeId, setActiveId] = useState(null)
  const [events, setEvents] = useState([])
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState(null)

  const active = sessions.find((s) => s.session_id === activeId) || null

  const load = useCallback(async () => {
    try {
      setSessions(await operatorClient.listSessions())
    } catch (e) {
      setError(e?.body?.detail || e?.message || 'Could not load sessions.')
      if (operatorClient.isOperatorDenied(e)) onSignedOut?.()
    }
  }, [onSignedOut])

  useEffect(() => { load() }, [load])

  const openSession = async (sessionId) => {
    setActiveId(sessionId)
    setError(null)
    try {
      setEvents(await operatorClient.getEvents(sessionId))
    } catch (e) {
      setError(e?.body?.detail || e?.message || 'Could not load the transcript.')
      if (operatorClient.isOperatorDenied(e)) onSignedOut?.()
    }
  }

  const startSession = async () => {
    setError(null)
    try {
      const created = await operatorClient.createSession()
      await load()
      await openSession(created.session_id)
    } catch (e) {
      setError(e?.body?.detail || e?.message || 'Could not start a session.')
      if (operatorClient.isOperatorDenied(e)) onSignedOut?.()
    }
  }

  const send = async () => {
    const text = draft.trim()
    if (!text || !activeId || sending) return
    setSending(true)
    setError(null)
    try {
      await operatorClient.postMessage(activeId, text)
      setDraft('')
      setEvents(await operatorClient.getEvents(activeId))
    } catch (e) {
      setError(e?.body?.detail || e?.message || 'The message did not go through — nothing changed.')
      if (operatorClient.isOperatorDenied(e)) onSignedOut?.()
    } finally {
      setSending(false)
    }
  }

  return (
    <section className="operator-panel operator-session-panel" aria-label="Operator sessions">
      <h2>Sessions</h2>

      {active && (
        <p className="operator-profile-badge" role="status">
          Profile <strong>{active.profile}</strong> · Environment <strong>{active.environment}</strong>
        </p>
      )}

      <button type="button" className="chip-act" onClick={startSession}>Start session</button>

      <ul className="operator-session-list">
        {sessions.map((s) => (
          <li key={s.session_id}>
            <button
              type="button"
              className="operator-session-item"
              aria-pressed={s.session_id === activeId}
              onClick={() => openSession(s.session_id)}
            >
              {s.profile} · {s.environment} · {s.status}
            </button>
          </li>
        ))}
        {sessions.length === 0 && <li className="dim">No sessions yet.</li>}
      </ul>

      {activeId && (
        <div className="operator-transcript" aria-label="Session transcript">
          <ul>
            {events.map((ev) => (
              <li key={ev.seq}>
                <code>{ev.type}</code>{' '}
                <span>{typeof ev.data === 'object' ? JSON.stringify(ev.data) : String(ev.data)}</span>
              </li>
            ))}
            {events.length === 0 && <li className="dim">No events yet.</li>}
          </ul>

          <label htmlFor="operator-message-draft">Message</label>
          <textarea
            id="operator-message-draft"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            disabled={sending}
          />
          <button type="button" className="chip-act" disabled={sending || !draft.trim()} onClick={send}>
            {sending ? 'Sending…' : 'Send'}
          </button>
        </div>
      )}

      {error && <p className="operator-panel-error" role="alert">{error}</p>}
    </section>
  )
}
