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

import { fmtWhen } from '../JobRail.jsx'
import * as operatorClient from '../../operatorClient.js'
import { deepRedact, isRedactedField, renderFieldValue } from '../../projects/ReceiptPanel.jsx'

function isScalar(v) {
  return v == null || typeof v !== 'object'
}

// An event's own timestamp field, whichever name the server used for it —
// the wire contract is not fixed here, so check the common spellings
// rather than assume one. Shared TM1 formatting (fmtWhen) with the job
// rail's ledger so clock columns read the same everywhere in the app.
function eventWhen(ev) {
  const ts = ev?.timestamp ?? ev?.ts ?? ev?.time ?? ev?.created_at ?? null
  return fmtWhen(ts)
}

// First 2-3 scalar fields of an event's data, redacted the same way a
// receipt field is (reusing ReceiptPanel's exact denylist) — a compact
// one-line summary, never the full object.
function eventSummary(data) {
  if (data == null) return null
  // a scalar payload is still a VALUE and must clear the denylist - the
  // server writes dicts today, but a relaxed contract must not leak (panel
  // round 2)
  if (isScalar(data)) return String(deepRedact(data, 'data'))
  const scalarEntries = Object.entries(data).filter(([, v]) => isScalar(v)).slice(0, 3)
  if (scalarEntries.length === 0) return null
  return scalarEntries
    .map(([k, v]) => `${k}: ${isRedactedField(k, v) ? '[redacted]' : renderFieldValue(v)}`)
    .join(' · ')
}

// True when there is more in `data` than the compact summary above shows —
// gates the disclosure toggle so a fully-summarized scalar payload doesn't
// grow a dead "expand" affordance with nothing new behind it.
function hasMoreThanSummary(data) {
  if (data == null || isScalar(data)) return false
  return Object.keys(data).length > 3 || Object.values(data).some((v) => !isScalar(v))
}

export default function SessionPanel({ onSignedOut }) {
  const [sessions, setSessions] = useState([])
  const [activeId, setActiveId] = useState(null)
  const [events, setEvents] = useState([])
  const [expandedEvents, setExpandedEvents] = useState({}) // ev.seq -> full data shown
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
            {events.map((ev) => {
              const when = eventWhen(ev)
              const summary = eventSummary(ev.data)
              const expandable = hasMoreThanSummary(ev.data)
              const open = !!expandedEvents[ev.seq]
              return (
                <li key={ev.seq}>
                  <div className="ledger-row">
                    <span className="ledger-time">{when ? when.clock : '—'}</span>
                    <span className="ledger-event">
                      <code>{ev.type}</code>
                      {summary && <span className="dim"> · {summary}</span>}
                      {expandable && (
                        <button
                          type="button"
                          className="chip-act"
                          aria-expanded={open}
                          aria-label={open ? `Hide full event data for ${ev.type}` : `Show full event data for ${ev.type}`}
                          onClick={() => setExpandedEvents((prev) => ({ ...prev, [ev.seq]: !prev[ev.seq] }))}
                        >
                          {open ? '▾' : '▸'}
                        </button>
                      )}
                    </span>
                  </div>
                  {expandable && open && (
                    <pre style={{ fontFamily: 'var(--font-mono)', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
                      {JSON.stringify(deepRedact(ev.data), null, 2)}
                    </pre>
                  )}
                </li>
              )
            })}
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
