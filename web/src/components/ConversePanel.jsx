// The tier-2 conversational surface (agent spine, wire contract §3/§7/§11):
// renders the active drawing's converse session over the durable event stream
// (converse.js openStream — SSE + after_seq replay + transcript poll). The
// panel NEVER executes anything itself: reads run agent-side behind the app
// gate; a write surfaces here as a proposed_run confirm card rendered in the
// SAME visual language as RoutePanel's decision strip, showing the SERVER's
// truth (tool + params + capability from the event, never the model's prose).
// Approve = §7 split turn: record the approval, then post the confirm message
// that starts the resume turn — the deterministic dispatch happens server-side.

import { useEffect, useMemo, useRef, useState } from 'react'
import { openStream, postMessage, approve, classifyAgentError } from '../converse.js'
import Markdown from './Markdown.jsx'

// Calm inline parameter summary — the same rendering RoutePanel gives a
// route's params ("layer roofline · n 4"). Always the SERVER-truth dict.
function paramsSummary(params) {
  const entries = Object.entries(params || {})
  if (entries.length === 0) return null
  return entries.map(([k, v]) => `${k.replace(/_/g, ' ')} ${String(v)}`).join(' · ')
}

const shortId = (s) => String(s || '').slice(0, 8)

// Per-turn spend tick (turn_usage event): cost_tokens is the metered number;
// total_cost_usd is optional and always an estimate (no balance API exists).
function fmtUsage(u) {
  const parts = []
  if (Number.isFinite(Number(u.cost_tokens))) parts.push(`${Number(u.cost_tokens).toLocaleString()} tokens`)
  if (Number.isFinite(Number(u.total_cost_usd))) parts.push(`~$${Number(u.total_cost_usd).toFixed(3)} est`)
  return parts.join(' · ')
}

// Full tool args/result for an expanded chip. Objects pretty-print; strings
// pass through. Bounded because a tool result can be large and the transcript
// must stay scrollable — the drawer, not the chip, is the place for everything.
const DETAIL_MAX_CHARS = 2000
function fmtDetail(value) {
  let text
  if (typeof value === 'string') text = value
  else {
    try {
      text = JSON.stringify(value, null, 2)
    } catch {
      text = String(value)
    }
  }
  if (text.length <= DETAIL_MAX_CHARS) return text
  return `${text.slice(0, DETAIL_MAX_CHARS)}\n… ${text.length - DETAIL_MAX_CHARS} more characters`
}

// The status strip's context reading. The fields are OPTIONAL on turn_usage:
// a backend that has not shipped them yet renders "—" rather than NaN, so the
// strip degrades to what is actually known instead of inventing a number.
function contextPct(usage) {
  const used = Number(usage?.context_used_tokens)
  const window = Number(usage?.context_window_tokens)
  if (!Number.isFinite(used) || !Number.isFinite(window) || window <= 0) return null
  return Math.max(0, Math.min(100, Math.round((used / window) * 100)))
}

// Honest, calm stop-reason notes (turn_complete). end_turn renders nothing;
// llm_quota_exhausted is the banner's job, not a per-turn note.
const STOP_NOTES = {
  awaiting_approval: 'waiting on your decision above',
  cap_hit: 'turn hit its token cap',
  llm_rate_limited: 'rate-limited — try again shortly',
  error: 'the turn ended with an error',
  timeout: 'the turn timed out',
}

// Local send/approve failure -> the same calm degraded copy the App banner uses.
function bannerFor(e) {
  const kind = classifyAgentError(e)
  if (kind === 'quota') return { kind, message: 'AI paused — your built tools keep working.' }
  if (kind === 'rate_limited') return { kind, message: 'AI rate-limited — retry shortly.' }
  if (kind === 'grant') return { kind, message: 'Chat needs a linked Claude account.' }
  if (kind === 'busy') return { kind, message: 'A turn is already in flight — wait for it to finish.' }
  if (kind === 'entitlement') return { kind, message: 'Chat isn’t included in your plan.' }
  if (kind === 'approval_stale') return { kind, message: 'That request was already decided — ask the assistant to propose it again.' }
  if (kind === 'confirmation_expired') return { kind, message: 'That confirmation expired — ask the assistant to propose it again.' }
  return { kind, message: 'Couldn’t reach the assistant — your built tools keep working.' }
}

export default function ConversePanel({
  sessionId,
  userTurns = [],            // [{turnId, text}] — turns App dispatched into this session
  onDismiss,                 // hide the panel (the server-side turn is never cancelled)
  onLinkClaude,              // grant_required CTA -> open the Claude account panel
  onAttachJob,               // (jobId, tool) -> App's existing §7 job attach affordance
  onJobLinked,               // a job_linked event arrived -> refresh the job rail
  writeLocked = false,
}) {
  const [events, setEvents] = useState([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [localTurns, setLocalTurns] = useState([]) // panel-sent follow-ups [{turnId, text}]
  const [sendErr, setSendErr] = useState(null)     // {kind, message} from a failed send/approve
  const [decidedLocal, setDecidedLocal] = useState({}) // confirmation_id -> approved (optimistic; the confirmation_resolved event reconciles)
  const [deciding, setDeciding] = useState(null)   // confirmation_id with an approve/deny in flight
  const [expandedTools, setExpandedTools] = useState({}) // chip key -> expanded (full args/result)
  const logRef = useRef(null)
  const jobSeenRef = useRef(new Set())

  // Stream lifecycle: one open stream per session, replay from seq 0 (the
  // transcript is durable — a remount recovers the whole conversation).
  useEffect(() => {
    if (!sessionId) return undefined
    setEvents([]); setLocalTurns([]); setSendErr(null)
    setDecidedLocal({}); setDeciding(null)
    jobSeenRef.current = new Set()
    const stream = openStream(sessionId, 0, {
      onEvent: (env) => {
        if (env.type === 'job_linked' && env.data?.job_id && !jobSeenRef.current.has(env.data.job_id)) {
          jobSeenRef.current.add(env.data.job_id)
          if (onJobLinked) onJobLinked(env.data.job_id, env.data.tool) // the rail shows the agent-dispatched job
        }
        setEvents((prev) => [...prev, env])
      },
    })
    return () => stream.close()
  }, [sessionId]) // eslint-disable-line react-hooks/exhaustive-deps

  // Keep the log pinned to the newest event while streaming.
  useEffect(() => {
    const el = logRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [events])

  // Fold the raw §3 envelopes into renderable turns. Feed items preserve the
  // interleaving (text · tool chips · job links · confirm cards) exactly as
  // the events arrived; confirmation_resolved reconciles cards cross-turn.
  const model = useMemo(() => {
    const turns = []
    const byId = new Map()
    const decisions = new Map() // confirmation_id -> {approved, by}
    const completed = new Set()
    let quota = false
    let grant = false
    const turnOf = (id) => {
      const key = id || `_${turns.length}`
      let t = byId.get(key)
      if (!t) {
        t = { turnId: key, feed: [], usage: null, stopReason: null, started: false, openCalls: [] }
        byId.set(key, t)
        turns.push(t)
      }
      return t
    }
    for (const env of events) {
      const type = env.type
      const data = env.data || {}
      if (type === 'confirmation_resolved') {
        decisions.set(data.confirmation_id, { approved: !!data.approved, by: data.by || null })
        continue
      }
      if (type === 'session_state') continue
      const t = turnOf(env.turn_id)
      if (type === 'turn_started') {
        t.started = true
        quota = false; grant = false // a fresh turn clears the paused banners
      } else if (type === 'text_delta') {
        const last = t.feed[t.feed.length - 1]
        if (last && last.kind === 'text') last.text += data.text || ''
        else t.feed.push({ kind: 'text', text: data.text || '' })
      } else if (type === 'tool_call') {
        // `args` is OPTIONAL on the wire: when the backend sends it the chip
        // becomes expandable, otherwise the chip is exactly what it is today.
        const chip = { tool: data.tool, summary: data.args_summary || '', ok: null, result: null, args: data.args }
        t.openCalls.push(chip)
        t.feed.push({ kind: 'tool', chip })
      } else if (type === 'tool_result') {
        // Pair with the earliest still-open call for the same tool.
        const open = t.openCalls.find((c) => c.tool === data.tool && c.ok === null)
        if (open) { open.ok = data.ok !== false; open.result = data.summary || ''; open.fullResult = data.result }
        else t.feed.push({ kind: 'tool', chip: { tool: data.tool, summary: '', ok: data.ok !== false, result: data.summary || '', fullResult: data.result } })
      } else if (type === 'job_linked') {
        t.feed.push({ kind: 'job', jobId: data.job_id, tool: data.tool || null })
      } else if (type === 'proposed_run') {
        t.feed.push({
          kind: 'proposal', id: data.confirmation_id, tool: data.tool,
          params: data.params || {}, capability: data.capability || null,
          rationale: data.rationale || '',
        })
      } else if (type === 'confirmation_required') {
        t.feed.push({ kind: 'confirm', id: data.confirmation_id, confirmKind: data.kind || null, payload: data.payload || null })
      } else if (type === 'turn_usage') {
        t.usage = data
      } else if (type === 'turn_complete') {
        t.stopReason = data.stop_reason || 'end_turn'
        completed.add(t.turnId)
        if (t.stopReason === 'llm_quota_exhausted') quota = true
      } else if (type === 'error') {
        const code = String(data.error?.error_code || '').toLowerCase()
        if (code === 'llm_quota_exhausted') quota = true
        else if (code === 'grant_required') grant = true
        else t.feed.push({ kind: 'error', code, message: data.error?.message || 'turn error' })
      }
    }
    const active = turns.some((t) => t.started && !t.stopReason)
    // Latest usage tick wins the status strip: context% is a running session
    // reading, not a per-turn one, so the newest turn_usage is the truth.
    let latestUsage = null
    for (const t of turns) if (t.usage) latestUsage = t.usage
    return { turns, decisions, completed, quota, grant, active, latestUsage }
  }, [events])

  // User bubbles come from the dispatching side (§3 has no user-message event):
  // App-dispatched turns + panel-sent follow-ups, keyed by turn_id.
  const allUserTurns = useMemo(() => [...userTurns, ...localTurns], [userTurns, localTurns])
  const userTextByTurn = useMemo(() => {
    const m = new Map()
    for (const u of allUserTurns) if (u.turnId && u.text) m.set(u.turnId, u.text)
    return m
  }, [allUserTurns])

  // Sent turns whose events haven't streamed in yet — render as pending bubbles.
  const knownTurnIds = useMemo(() => new Set(model.turns.map((t) => t.turnId)), [model])
  const pendingUserTurns = allUserTurns.filter((u) => u.turnId && u.text && !knownTurnIds.has(u.turnId))

  // Input is closed while any turn is in flight (turn_complete re-enables it).
  const lastUser = allUserTurns[allUserTurns.length - 1] || null
  const awaitingTurn = !!(lastUser && lastUser.turnId && !model.completed.has(lastUser.turnId))
  const busy = sending || model.active || awaitingTurn

  const send = async () => {
    const text = input.trim()
    if (!text || busy) return
    setSending(true); setSendErr(null)
    try {
      const res = await postMessage(sessionId, { text })
      setLocalTurns((prev) => [...prev, { turnId: res.turn_id, text }])
      setInput('')
    } catch (e) {
      setSendErr(bannerFor(e))
    } finally {
      setSending(false)
    }
  }

  // §7 split-turn decision: (a) record the approval, (b) post the confirm
  // message that starts the resume turn. Optimistic local mark; the
  // confirmation_resolved event is the authoritative reconciliation.
  // Retry-safe: when (a) succeeded but (b) failed, a retry click re-enters
  // with the decision already recorded — (a)'s already-decided 409 is
  // non-fatal so (b) can still start the resume before the approval TTL.
  // The confirm payload uses the harness §1 camelCase key (confirmationId):
  // the app forwards req.confirm verbatim, and the harness messages route
  // accepts no other spelling.
  const decide = async (confirmationId, ok, blocked = false) => {
    if (deciding || (ok && blocked)) return
    setDeciding(confirmationId); setSendErr(null)
    try {
      try {
        await approve(confirmationId, ok)
      } catch (e) {
        // 409 approval_stale means the decision is ALREADY recorded — this
        // click is a retry after step (b) failed (409 busy from another tab's
        // turn, transient network), and grant_approval refuses re-deciding.
        // The resume turn is still owed, so fall through to the confirm
        // message; the server gate stays authoritative (a mismatched or
        // expired record denies the dispatch, and a truly expired one
        // surfaces on the confirm post as its own 410 banner).
        if (classifyAgentError(e) !== 'approval_stale') throw e
      }
      const res = await postMessage(sessionId, { confirm: { confirmationId, approved: ok } })
      setDecidedLocal((m) => ({ ...m, [confirmationId]: ok }))
      setLocalTurns((prev) => [...prev, { turnId: res.turn_id, text: null }])
    } catch (e) {
      setSendErr(bannerFor(e))
    } finally {
      setDeciding(null)
    }
  }

  const showQuota = model.quota || sendErr?.kind === 'quota'
  const showGrant = model.grant || sendErr?.kind === 'grant'
  const showOtherErr = sendErr && sendErr.kind !== 'quota' && sendErr.kind !== 'grant'

  const renderFeedItem = (item, i) => {
    if (item.kind === 'text') {
      // Assistant prose renders through the element-only markdown path
      // (Markdown.jsx / markdown.js): fenced code, lists and links become
      // real elements, and anything HTML-shaped stays literal text.
      return item.text ? <div key={i} className="converse-msg assistant"><Markdown text={item.text} /></div> : null
    }
    if (item.kind === 'tool') {
      const c = item.chip
      // A chip expands only when the event actually carried the full call —
      // args/result are OPTIONAL on the wire, so a backend that sends only
      // args_summary keeps exactly today's one-line chip (no dead affordance).
      const detail = c.args ?? c.fullResult
      const expandable = detail !== undefined && detail !== null
      const key = `${i}:${c.tool}`
      const open = !!expandedTools[key]
      return (
        <span key={i} className="converse-tool-chip">
          <span className={c.ok === null ? 'dot live pulse' : (c.ok ? 'dot' : 'dot red')} aria-hidden="true" />
          {expandable ? (
            <button
              type="button"
              className="converse-tool-toggle"
              aria-expanded={open}
              onClick={() => setExpandedTools((prev) => ({ ...prev, [key]: !prev[key] }))}
            >
              <span className="route-tool">{c.tool}</span>
              <span className="dim"> {open ? '▾' : '▸'}</span>
            </button>
          ) : (
            <span className="route-tool">{c.tool}</span>
          )}
          {(c.result || c.summary) && <span className="dim"> · {c.result || c.summary}</span>}
          {open && (
            <span className="converse-tool-detail">
              {c.args !== undefined && c.args !== null && (
                <pre className="converse-md-pre"><code>{fmtDetail(c.args)}</code></pre>
              )}
              {c.fullResult !== undefined && c.fullResult !== null && (
                <pre className="converse-md-pre"><code>{fmtDetail(c.fullResult)}</code></pre>
              )}
            </span>
          )}
        </span>
      )
    }
    if (item.kind === 'job') {
      return (
        <div key={i} className="converse-jobrow">
          <span className="dot" aria-hidden="true" />
          <span className="dim">
            job <span className="route-tool">{shortId(item.jobId)}</span>
            {item.tool ? ` · ${item.tool}` : ''}
          </span>
          <button
            type="button"
            className="chip-act"
            onClick={() => onAttachJob && onAttachJob(item.jobId, item.tool)}
          >
            Attach
          </button>
        </div>
      )
    }
    if (item.kind === 'proposal' || item.kind === 'confirm') {
      const resolved = model.decisions.get(item.id)
      const localPick = decidedLocal[item.id]
      const settled = resolved || localPick != null
      const settledOk = resolved ? resolved.approved : localPick
      const isWrite = item.capability === 'drawing.write'
      const summary = item.kind === 'proposal' ? paramsSummary(item.params) : null
      return (
        <div key={i} className="strip-decision converse-confirm">
          <span className="dot square" aria-hidden="true" />
          <span className="strip-sentence">
            {item.kind === 'proposal' ? (
              <>
                Run <span className="route-tool">{item.tool}</span>
                {item.capability && <> <span className={`cap ${isWrite ? 'write' : 'read'}`}>{item.capability}</span></>}
                {summary && <span className="dim"> · {summary}</span>}
                <span className="dim">
                  {' — '}
                  {item.rationale || (isWrite
                    ? 'creates a new version — you confirm before it runs.'
                    : 'you confirm before it runs.')}
                </span>
              </>
            ) : (
              <>
                <span className="route-title">{item.confirmKind || 'Confirmation'}</span>
                {item.payload && <span className="dim"> · {paramsSummary(item.payload) || String(item.payload)}</span>}
                <span className="dim"> — the assistant is asking before it proceeds.</span>
              </>
            )}
          </span>
          {settled ? (
            <span className="dim">{settledOk ? 'Approved' : 'Denied'}{resolved?.by ? ` · ${resolved.by}` : ''}</span>
          ) : (
            <>
              <button
                type="button"
                className="chip-act"
                disabled={deciding === item.id || (isWrite && writeLocked)}
                onClick={() => decide(item.id, true, isWrite && writeLocked)}
              >
                {deciding === item.id ? 'Sending…' : (isWrite && writeLocked ? 'Editing locked' : 'Approve')}
              </button>
              <button
                type="button"
                className="chip-neutral"
                disabled={deciding === item.id}
                onClick={() => decide(item.id, false)}
              >
                Deny
              </button>
            </>
          )}
        </div>
      )
    }
    if (item.kind === 'error') {
      return (
        <div key={i} className="converse-note">
          <span className="dot square" aria-hidden="true" />
          <span className="dim">{item.message}{item.code ? ` · ${item.code}` : ''}</span>
        </div>
      )
    }
    return null
  }

  return (
    <div className="converse-card enter" style={{ '--rank': 3 }}>
      <div className="converse-head">
        <span className={model.active || busy ? 'dot live pulse' : 'dot'} aria-hidden="true" />
        <span className="converse-title">Assistant</span>
        <span className="dim">plans and explains — deterministic tools do the work</span>
        <span className="converse-spacer" />
        {/* Status strip — the terminal client's persistent model/context/cost
            reading, as a component rather than a shell script. Every field is
            optional: an unknown value shows "—", never a fabricated number. */}
        <span className="converse-status" aria-label="session status">
          <span className="dim">model</span>{' '}
          <span className="route-tool">{model.latestUsage?.model || '—'}</span>
          <span className="dim"> · context </span>
          <span className="route-tool">
            {contextPct(model.latestUsage) === null ? '—' : `${contextPct(model.latestUsage)}%`}
          </span>
          <span className="dim"> · </span>
          <span className="route-tool">
            {Number.isFinite(Number(model.latestUsage?.session_cost_usd))
              ? `~$${Number(model.latestUsage.session_cost_usd).toFixed(3)}`
              : '—'}
          </span>
        </span>
        <button type="button" className="chip-neutral" onClick={onDismiss}>Hide</button>
      </div>

      {showQuota && (
        <div className="banner"><span><b>AI paused</b> — your built tools keep working.</span></div>
      )}
      {showGrant && (
        <div className="banner">
          <span>Chat needs a linked Claude account.</span>
          <button type="button" className="chip-act" onClick={onLinkClaude}>Link account</button>
        </div>
      )}
      {showOtherErr && (
        <div className="banner"><span>{sendErr.message}</span></div>
      )}

      <div className="converse-log" ref={logRef}>
        {model.turns.length === 0 && pendingUserTurns.length === 0 && (
          <div className="converse-note">
            <span className="dot hollow" aria-hidden="true" />
            <span className="dim">Starting the conversation…</span>
          </div>
        )}
        {model.turns.map((t) => (
          <div key={t.turnId} className="converse-turn">
            {userTextByTurn.get(t.turnId) && (
              <div className="converse-msg user">{userTextByTurn.get(t.turnId)}</div>
            )}
            {t.feed.map(renderFeedItem)}
            {(t.usage || (t.stopReason && STOP_NOTES[t.stopReason])) && (
              <div className="converse-turnfoot">
                {t.stopReason && STOP_NOTES[t.stopReason] && (
                  <span className="dim">{STOP_NOTES[t.stopReason]}</span>
                )}
                {t.usage && <span className="converse-usage">{fmtUsage(t.usage)}</span>}
              </div>
            )}
          </div>
        ))}
        {pendingUserTurns.map((u) => (
          <div key={u.turnId} className="converse-turn">
            <div className="converse-msg user">{u.text}</div>
            <div className="converse-note">
              <span className="dot live pulse" aria-hidden="true" />
              <span className="dim">thinking…</span>
            </div>
          </div>
        ))}
      </div>

      <div className="converse-input">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send() }
          }}
          placeholder={busy ? 'Assistant is working…' : 'Reply to the assistant…'}
          disabled={busy}
          spellCheck={false}
          aria-label="Reply to the assistant"
        />
        <button type="button" className="chip-act" onClick={send} disabled={busy || !input.trim()}>
          {sending ? 'Sending…' : 'Send'}
        </button>
      </div>
    </div>
  )
}
