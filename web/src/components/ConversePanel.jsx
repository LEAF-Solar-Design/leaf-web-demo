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
import {
  openStream,
  postMessage,
  resolveApproval,
  listPendingApprovals,
  cancelTurn,
  classifyAgentError,
} from '../converse.js'
import { track } from '../telemetry.js'
import {
  acceptQueuedTurn,
  clearSendingQuestion,
  chooseQuestionOption,
  createQueuedTurnState,
  questionChoiceState,
  reconcileQueuedTurn,
  shouldRetryWithQueue,
  clipboardImagesToAttachments,
  thumbnailImages,
} from '../composer.js'
import { formatElementId } from '../lib/elementIdentity.js'
import { isSecretRefused } from '../lib/secretGuardTransport.js'
import Markdown from './Markdown.jsx'
import LiveRegion from './LiveRegion.jsx'
import { contextPct, fmtDetail, orDash, usageCost, usageModel } from '../usage.js'
import { errorActorLabel, errorPresentation } from '../errorPresentation.js'

// Calm inline parameter summary — the same rendering RoutePanel gives a
// route's params ("layer roofline · n 4"). Always the SERVER-truth dict.
function paramsSummary(params) {
  const entries = Object.entries(params || {})
  if (entries.length === 0) return null
  // Objects must never reach String(): it renders the useless "[object
  // Object]", which on a platform self-edit chip hid the very paths the
  // approver needed to see (sol-critic PR #417 round 1). Everything else keeps
  // String()'s EXACT previous rendering — an array still lists its elements
  // and null still reads "null". Summarizing them (a count, a blank) would
  // conceal the parameters of every existing proposal chip, which is the same
  // blind-approval defect wearing different clothes (round 3).
  const val = (v) => {
    // Array#join renders null and undefined as EMPTY, and this must keep
    // rendering every existing chip byte-identically: only a genuine object,
    // which used to read "[object Object]", may change.
    if (Array.isArray(v)) return v.map((e) => (e == null ? '' : val(e))).join(',')
    if (v !== null && typeof v === 'object') return JSON.stringify(v)
    return String(v)
  }
  return entries.map(([k, v]) => `${k.replace(/_/g, ' ')} ${val(v)}`).join(' · ')
}

// A path is only a control if it renders as EXACTLY what the server holds.
// React escapes HTML, but it happily prints a newline (which collapses a row)
// or a bidi override (which reorders what the eye reads), so one path can be
// made to look like another. The lane rejects both at propose time; this is
// the second barrier, for a record written before that check existed
// (sol-critic PR #417 round 3).
function displayPath(raw) {
  let out = ''
  for (const ch of String(raw ?? '')) {
    // Allowlist, not denylist: the invisible and confusable characters that
    // make one path read as another span half the Unicode table, so each
    // denied class leaves the next. Every tracked path in this repo uses only
    // these characters, so nothing legitimate is ever escaped.
    out += /[A-Za-z0-9._/+@-]/.test(ch)
      ? ch
      : '\\u' + ch.codePointAt(0).toString(16).padStart(4, '0').toUpperCase()
  }
  return out
}

// A platform self-edit chip states, in one calm line, exactly what would be
// written: the op, the title, and EVERY path with its action and size. The
// paths are the control — an edit to a file the user never mentioned is the
// signal that catches an injected proposal.
function customizeChipLines(payload) {
  const p = payload || {}
  if (p.op === 'land') {
    return {
      head: 'Land platform change',
      detail: `${shortId(p.change_id)} · commit ${shortId(p.commit_sha)}`,
      edits: [],
      undisplayed: 0,
    }
  }
  const edits = Array.isArray(p.edits) ? p.edits : []
  const count = Number.isInteger(p.edit_count) ? p.edit_count : edits.length
  return {
    head: 'Edit the platform itself',
    detail: `${p.title || 'untitled'} · ${count} file${count === 1 ? '' : 's'}`,
    edits,
    // Every path the server would accept is displayed, so this is 0 in
    // practice. Non-zero means the chip is INCOMPLETE: say do-not-approve
    // rather than showing a count that reads like a harmless overflow.
    undisplayed: Number(p.edits_undisplayed) || 0,
  }
}

function CustomizeChipBody({ payload }) {
  const { head, detail, edits, undisplayed } = customizeChipLines(payload)
  return (
    <>
      <span className="route-title">{head}</span>
      <span className="dim"> · {detail}</span>
      {edits.length > 0 && (
        <span className="customize-edit-list">
          {edits.map((e, i) => (
            <span key={`${e.path}-${i}`} className="customize-edit">
              <code dir="ltr">{displayPath(e.path)}</code>
              <span className="dim">
                {' '}{e.action === 'delete' ? 'delete' : `write${
                  typeof e.bytes === 'number' ? ` ${e.bytes} bytes` : ''}`}
              </span>
            </span>
          ))}
        </span>
      )}
      {undisplayed > 0 && (
        <span className="customize-incomplete">
          {undisplayed} more file{undisplayed === 1 ? '' : 's'} would change and are NOT
          listed here. Do not approve this — deny it and ask for a smaller change.
        </span>
      )}
      <span className="dim">
        {' — '}this changes the code of the product itself. Landing pushes a review
        branch; nothing goes live until it is reviewed, merged and deployed.
      </span>
    </>
  )
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

// Status-strip readings and expanded-chip formatting live in usage.js so they
// are unit-testable and so the EXACT wire-contract field names (models[],
// total_cost_usd) live in one place. Absent values render "—", never a
// fabricated zero: Number(null) is 0, which would otherwise report a confident
// 0% context and ~$0.000 cost for a turn that simply never sent them.

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
  const fallbacks = {
    quota: 'AI paused — your built tools keep working.',
    rate_limited: 'AI rate-limited — retry shortly.',
    grant: 'Chat needs a linked Claude account.',
    busy: 'A turn is already in flight — wait for it to finish.',
    entitlement: 'Chat isn’t included in your plan.',
    approval_stale: 'That request was already decided — ask the assistant to propose it again.',
    confirmation_expired: 'That confirmation expired — ask the assistant to propose it again.',
    too_large: 'That message is too large — try fewer or smaller images.',
  }
  const fallback = fallbacks[kind] || 'Couldn’t reach the assistant — your built tools keep working.'
  return { kind, ...errorPresentation(e, fallback), message: fallback }
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
  const [queuedTurn, setQueuedTurn] = useState(null) // {queuedId, text}, parked behind the active turn
  const [sendErr, setSendErr] = useState(null)     // {kind, message} from a failed send/approve
  const [decidedLocal, setDecidedLocal] = useState({}) // confirmation_id -> approved (optimistic; the confirmation_resolved event reconciles)
  const [deciding, setDeciding] = useState(null)   // confirmation_id with an approve/deny in flight
  const [pendingApprovals, setPendingApprovals] = useState([])
  const [pendingApprovalsError, setPendingApprovalsError] = useState(false)
  const [questionChoices, setQuestionChoices] = useState({ sendingQuestionIds: [] })
  const [stopping, setStopping] = useState(false)  // an interrupt is in flight
  const [expandedTools, setExpandedTools] = useState({}) // chip key -> expanded (full args/result)
  const [attachments, setAttachments] = useState([])
  const [attachmentError, setAttachmentError] = useState(null)
  // The credential refusal the TRANSPORT raised, held only to render it:
  // {id, reason, masked, overridable} or null. This composer evaluates nothing
  // itself (round 3) — converse.postMessage refuses and throws, send() catches.
  const [secretNotice, setSecretNotice] = useState(null)
  const attachmentUrlsRef = useRef(new Set())
  const logRef = useRef(null)
  const jobSeenRef = useRef(new Set())
  const queueStateRef = useRef(createQueuedTurnState())
  const setQueueState = (next) => {
    queueStateRef.current = next
    setQueuedTurn(next.queuedTurn)
  }
  const releaseAttachment = (image) => {
    if (image?.thumbnailUrl) {
      URL.revokeObjectURL(image.thumbnailUrl)
      attachmentUrlsRef.current.delete(image.thumbnailUrl)
    }
  }
  const clearAttachments = () => setAttachments((current) => {
    for (const image of current) releaseAttachment(image)
    return []
  })
  useEffect(() => () => {
    for (const url of attachmentUrlsRef.current) URL.revokeObjectURL(url)
    attachmentUrlsRef.current.clear()
  }, [])

  // Stream lifecycle: one open stream per session, replay from seq 0 (the
  // transcript is durable — a remount recovers the whole conversation).
  useEffect(() => {
    if (!sessionId) return undefined
    setEvents([]); setLocalTurns([]); setSendErr(null); clearAttachments()
    setDecidedLocal({}); setDeciding(null)
    setQuestionChoices({ sendingQuestionIds: [] })
    jobSeenRef.current = new Set()
    setQueueState(createQueuedTurnState())
    track('conversation.opened') // P2: panel opened/reopened a session — no session id in labels (identity is server-stamped)
    const stream = openStream(sessionId, 0, {
      onEvent: (env) => {
        const queued = reconcileQueuedTurn(queueStateRef.current, env)
        setQueueState(queued.state)
        if (queued.action === 'promote') {
          setLocalTurns((prev) => [...prev, queued.turn])
        }
        if (env.type === 'job_linked' && env.data?.job_id && !jobSeenRef.current.has(env.data.job_id)) {
          jobSeenRef.current.add(env.data.job_id)
          if (onJobLinked) onJobLinked(env.data.job_id, env.data.tool) // the rail shows the agent-dispatched job
        }
        setEvents((prev) => [...prev, env])
      },
    })
    return () => stream.close()
  }, [sessionId]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    let closed = false
    const refresh = async () => {
      try {
        const approvals = await listPendingApprovals(sessionId)
        if (!closed) {
          setPendingApprovals(approvals)
          setPendingApprovalsError(false)
        }
      } catch {
        if (!closed) setPendingApprovalsError(true)
      }
    }
    void refresh()
    const timer = window.setInterval(refresh, 5000)
    return () => { closed = true; window.clearInterval(timer) }
  }, [sessionId])

  // Keep the log pinned to the newest event while streaming - but ONLY when
  // the reader is already at (or near) the bottom. Yanking a user who
  // scrolled up to re-read mid-stream is the classic streaming-UI defect
  // (W0 craft #21); the ref tracks proximity from their own scrolls.
  const nearBottomRef = useRef(true)
  const onLogScroll = () => {
    const el = logRef.current
    if (el) nearBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48
  }
  useEffect(() => {
    const el = logRef.current
    if (el && nearBottomRef.current) el.scrollTop = el.scrollHeight
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
      // Queue bookkeeping events carry NO turn_id — letting them fall through
      // to turnOf() would mint an empty synthetic turn per event (an `_N` key)
      // and render a blank bubble. They are transcript-only records.
      if (type === 'turn_queued' || type === 'turn_queue_dropped') continue
      // T1 overlay lifecycle events likewise carry NO turn_id and are not
      // chat content — useOverlay owns them. Falling through would mint the
      // same blank synthetic bubble the queue events used to.
      if (type === 'overlay_proposed' || type === 'overlay_decided'
          || type === 'overlay_revoked') continue
      const t = turnOf(env.turn_id)
        if (type === 'turn_started') {
          t.started = true
          t.images = thumbnailImages(data.images)
          t.imageDescriptors = (data.images || []).filter((image) => !image?.data)
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
      } else if (type === 'question_required') {
        t.feed.push({ kind: 'question', id: data.question_id, question: data.question || '', options: data.options || [] })
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
    // The turn an interrupt would end: started, not yet terminal. Held as the
    // turn ITSELF (not just a boolean) so Stop/Esc can name the exact turn_id
    // — the server refuses a stale one rather than ending its replacement.
    const activeTurn = turns.find((t) => t.started && !t.stopReason) || null
    const active = !!activeTurn
    // Latest usage tick wins the status strip: context% is a running session
    // reading, not a per-turn one, so the newest turn_usage is the truth.
    let latestUsage = null
    for (const t of turns) if (t.usage) latestUsage = t.usage
    return { turns, decisions, completed, quota, grant, active, latestUsage,
             activeTurnId: activeTurn ? activeTurn.turnId : null }
  }, [events])

  // User bubbles come from the dispatching side (§3 has no user-message event):
  // App-dispatched turns + panel-sent follow-ups, keyed by turn_id.
  const allUserTurns = useMemo(() => [...userTurns, ...localTurns], [userTurns, localTurns])
  const userTextByTurn = useMemo(() => {
    const m = new Map()
    for (const u of allUserTurns) if (u.turnId && u.text) m.set(u.turnId, u.text)
    return m
  }, [allUserTurns])

  const localImagesByTurn = useMemo(() => {
    const m = new Map()
    for (const u of allUserTurns) if (u.turnId && u.images) m.set(u.turnId, thumbnailImages(u.images))
    return m
  }, [allUserTurns])

  // Sent turns whose events haven't streamed in yet — render as pending bubbles.
  const knownTurnIds = useMemo(() => new Set(model.turns.map((t) => t.turnId)), [model])
  const pendingUserTurns = allUserTurns.filter((u) => u.turnId && u.text && !knownTurnIds.has(u.turnId))

  // Input is closed while any turn is in flight (turn_complete re-enables it).
  const lastUser = allUserTurns[allUserTurns.length - 1] || null
  const awaitingTurn = !!(lastUser && lastUser.turnId && !model.completed.has(lastUser.turnId))
  const busy = sending || model.active || awaitingTurn

  // The turn Stop/Esc would end. `model.activeTurnId` is the streaming turn;
  // before its first event lands there is still a dispatched turn to
  // interrupt, which is what the awaiting fallback covers.
  const stoppableTurnId = model.activeTurnId
    || (awaitingTurn && lastUser ? lastUser.turnId : null)

  const stop = async () => {
    if (!stoppableTurnId || stopping) return
    setStopping(true); setSendErr(null)
    try {
      await cancelTurn(sessionId, stoppableTurnId)
      track('conversation.truncated', { reason: 'user_stop' }) // P2: interrupt ends the turn's output early
      // No optimistic local state: the server appends
      // turn_complete{stop_reason:'interrupted'} and the stream reconciles,
      // so the panel shows the turn ending for the SAME reason a spontaneous
      // completion would — one code path, not two.
    } catch (e) {
      setSendErr(bannerFor(e))
    } finally {
      setStopping(false)
    }
  }

  // Esc interrupts, matching the terminal client. Document-level because the
  // reply input is disabled while a turn runs (a disabled control receives no
  // keys).
  //
  // It sits BETWEEN the existing Esc consumers, which is why both calls below
  // are load-bearing:
  //   * `defaultPrevented` DEFERS to anything nearer the target that already
  //     handled the key — PromptBox's slash menu closes itself with
  //     preventDefault (its scope resolver never reaches us at all, stopping
  //     the key in the capture phase) — so that ladder is unchanged.
  //   * `stopPropagation` keeps the key from ALSO reaching App's window-level
  //     ladder (App.jsx: `window.addEventListener('keydown', …)`), which
  //     dismisses the drawer/route and does NOT check defaultPrevented.
  //     Without it, one Esc would interrupt the turn AND dismiss whatever was
  //     behind it. Document listeners fire before window ones in the bubble
  //     phase, so stopping here is what makes "interrupt the running turn"
  //     the single effect of that keypress.
  // Both only happen when there is actually a turn to stop; otherwise the key
  // is left entirely alone.
  useEffect(() => {
    if (!busy || !stoppableTurnId) return undefined
    const onEsc = (e) => {
      if (e.key !== 'Escape' || e.defaultPrevented) return
      e.preventDefault()
      e.stopPropagation()
      stop()
    }
    document.addEventListener('keydown', onEsc)
    return () => document.removeEventListener('keydown', onEsc)
  }, [busy, stoppableTurnId, stopping]) // eslint-disable-line react-hooks/exhaustive-deps

  const attachmentPayloads = async () => Promise.all(attachments.map((image) => new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(new Error('Could not read the image attachment.'))
    reader.onload = () => {
      const data = String(reader.result || '').split(',', 2)[1] || ''
      resolve({ media_type: image.media_type, data })
    }
    reader.readAsDataURL(image.file)
  })))

  // allowSecretOnce is the "Send anyway" click's authorisation, carried as a
  // PARAMETER into the one postMessage it authorises. Nothing here remembers
  // it: a click that lands while the box is busy returns early below and
  // authorises exactly nothing, which is the fail-closed direction and the
  // round-3 fix for the latch two earlier rounds shipped.
  const send = async (nextText = input, { allowSecretOnce = false } = {}) => {
    const text = String(nextText).trim()
    if ((!text && !attachments.length) || busy) return false
    let delivered = false
    setSending(true); setSendErr(null)
    const accept = (res, images) => {
      if (res.status === 'queued') {
        const queued = acceptQueuedTurn(queueStateRef.current, {
          queuedId: res.queued_id || null,
          text,
        })
        setQueueState(queued.state)
        if (queued.action === 'promote') {
          setLocalTurns((prev) => [...prev, queued.turn])
        }
      } else {
        setLocalTurns((prev) => [...prev, { turnId: res.turn_id, text, images }])
      }
      setInput('')
      clearAttachments()
      setAttachmentError(null)
      // The send landed, so whatever refusal was on screen is spent.
      setSecretNotice(null)
    }
    try {
      const images = await attachmentPayloads()
      try {
        accept(await postMessage(sessionId, {
          ...(text ? { text } : {}),
          ...(images.length ? { images } : {}),
          allowSecretOnce,
        }), images)
        track('conversation.message_sent', { input_kind: text ? 'typed' : 'image_only', text_len: text.length })
      } catch (e) {
        if (!shouldRetryWithQueue(classifyAgentError(e), { text, images })) throw e
        accept(await postMessage(sessionId, { text, queue: true, allowSecretOnce }), images)
        // P2: the direct post failed (turn busy) and the queued retry landed —
        // the panel recovered the send rather than surfacing an error.
        track('conversation.recovered', { reason: 'busy_retry_queued' })
      }
      delivered = true
    } catch (e) {
      // A credential refusal never left the browser, so it is NOT a send
      // failure banner ("the assistant is unavailable" would be a lie about a
      // client-side decision). It renders as this composer's own notice.
      if (isSecretRefused(e)) setSecretNotice(e.refusal)
      else setSendErr(bannerFor(e))
    } finally {
      setSending(false)
    }
    return delivered
  }

  const answerQuestion = async (questionId, optionLabel) => {
    const choice = chooseQuestionOption(questionChoices, events, questionId, optionLabel)
    if (choice.action !== 'send') return
    // A refusal raised by an app-supplied choice string has no text of its own
    // for the user to edit, so the reply box's onChange can never retire it.
    // The next choice does instead: picking again is the natural next act on
    // this surface, and it is what that notice was about.
    setSecretNotice(null)
    setQuestionChoices(choice.state)
    if (!await send(choice.text)) {
      setQuestionChoices((state) => clearSendingQuestion(state, questionId))
    }
  }

  const onPaste = (e) => {
    const result = clipboardImagesToAttachments(e.clipboardData?.items, attachments)
    if (result.error) { e.preventDefault(); setAttachmentError(result.error); return }
    if (!result.attachments.length) return
    e.preventDefault()
    setAttachmentError(null)
    setAttachments((current) => [...current, ...result.attachments.map((image) => {
      const thumbnailUrl = URL.createObjectURL(image.file)
      attachmentUrlsRef.current.add(thumbnailUrl)
      return { ...image, id: `${Date.now()}-${Math.random()}`, thumbnailUrl }
    })])
  }
  const removeAttachment = (id) => setAttachments((current) => {
    const found = current.find((image) => image.id === id)
    releaseAttachment(found)
    return current.filter((image) => image.id !== id)
  })

  // §7 split-turn decision: (a) record the approval, (b) post the confirm
  // message that starts the resume turn. Optimistic local mark; the
  // confirmation_resolved event is the authoritative reconciliation.
  // Retry-safe: when (a) succeeded but (b) failed, a retry click re-enters
  // with the decision already recorded — (a)'s already-decided 409 is
  // non-fatal so (b) can still start the resume before the approval TTL.
  // The confirm payload uses the harness §1 camelCase key (confirmationId):
  // the app forwards req.confirm verbatim, and the harness messages route
  // accepts no other spelling.
  const decide = async (
    confirmationId, ok, blocked = false, owningSessionId = sessionId,
    decisionRecorded = false,
  ) => {
    if (deciding || (ok && blocked)) return
    setDeciding(confirmationId); setSendErr(null)
    try {
      const res = await resolveApproval(
        confirmationId, owningSessionId, ok, decisionRecorded)
      setDecidedLocal((m) => ({ ...m, [confirmationId]: ok }))
      setPendingApprovals((current) => current.filter(
        (approval) => approval.confirmation_id !== confirmationId))
      if (owningSessionId === sessionId) {
        setLocalTurns((prev) => [...prev, { turnId: res.turn_id, text: null }])
      }
    } catch (e) {
      setSendErr(bannerFor(e))
      const kind = classifyAgentError(e)
      if (kind === 'approval_stale' || kind === 'confirmation_expired' || kind === 'not_found') {
        setPendingApprovals((current) => current.filter(
          (approval) => approval.confirmation_id !== confirmationId))
      }
    } finally {
      setDeciding(null)
    }
  }

  // Removes the panel from view (the server-side conversation is untouched —
  // "Hide" is the closest panel-level analog to the server's conversation.deleted).
  const dismiss = () => {
    track('conversation.deleted', { reason: 'user_hide' })
    if (onDismiss) onDismiss()
  }

  const showQuota = model.quota || sendErr?.kind === 'quota'
  const showGrant = model.grant || sendErr?.kind === 'grant'
  const showOtherErr = sendErr && sendErr.kind !== 'quota' && sendErr.kind !== 'grant'

  const renderPendingApproval = (approval) => {
    const isWrite = approval.capability === 'drawing.write'
    const summary = paramsSummary(approval.params)
    const resumeRequired = approval.resume_required === true
    return (
      <div key={approval.confirmation_id} className="strip-decision converse-confirm" data-element-id={formatElementId('approval', approval.confirmation_id) || undefined}>
        <span className="dot square" aria-hidden="true" />
        <span className="strip-sentence">
          {approval.kind === 'customize_platform' ? (
            // A reloaded self-edit chip must carry the SAME facts the live one
            // did; the generic "Run requested tool" line judged nothing.
            <CustomizeChipBody payload={approval.payload} />
          ) : (
          <>
          Run <span className="route-tool">{approval.tool || 'requested tool'}</span>
          {approval.capability && <>{' '}<span className={`cap ${isWrite ? 'write' : 'read'}`}>{approval.capability}</span></>}
          {summary && <span className="dim"> · {summary}</span>}
          <span className="dim"> · {approval.rationale || 'waiting for your decision'}</span>
          </>
          )}
        </span>
        {resumeRequired ? (
          <button
            type="button"
            className="chip-act"
            disabled={deciding === approval.confirmation_id || (approval.approved && isWrite && writeLocked)}
            onClick={() => decide(
              approval.confirmation_id,
              !!approval.approved,
              !!approval.approved && isWrite && writeLocked,
              approval.session_id,
              true,
            )}
          >
            {deciding === approval.confirmation_id
              ? 'Resuming…'
              : (approval.approved ? 'Resume approved request' : 'Complete denial')}
          </button>
        ) : (
          <>
            <button
              type="button"
              className="chip-act"
              disabled={deciding === approval.confirmation_id || (isWrite && writeLocked)}
              onClick={() => decide(
                approval.confirmation_id, true, isWrite && writeLocked, approval.session_id)}
            >
              {deciding === approval.confirmation_id ? 'Sending…' : (isWrite && writeLocked ? 'Editing locked' : 'Approve')}
            </button>
            <button
              type="button"
              className="chip-neutral"
              disabled={deciding === approval.confirmation_id}
              onClick={() => decide(approval.confirmation_id, false, false, approval.session_id)}
            >
              Deny
            </button>
          </>
        )}
      </div>
    )
  }

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
              {/* the disclosure names what it opens (W0 craft #22): an
                  unlabeled glyph is an affordance nobody finds */}
              <span className="dim">
                {' '}{open ? 'hide ▾'
                  : c.args != null && c.fullResult != null ? 'args · result ▸'
                  : c.args != null ? 'args ▸' : 'result ▸'}
              </span>
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
      if (!settled && pendingApprovals.some(
        (approval) => approval.confirmation_id === item.id)) return null
      return (
        <div key={i} className="strip-decision converse-confirm" data-element-id={formatElementId('item', item.id) || undefined}>
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
              item.confirmKind === 'customize_platform' ? (
                <CustomizeChipBody payload={item.payload} />
              ) : (
              <>
                <span className="route-title">{item.confirmKind || 'Confirmation'}</span>
                {item.payload && <span className="dim"> · {paramsSummary(item.payload) || ''}</span>}
                <span className="dim"> — the assistant is asking before it proceeds.</span>
              </>
              )
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
    if (item.kind === 'question') {
      return (
        <div key={i} className="strip-decision converse-question">
          <span className="dot square" aria-hidden="true" />
          <span className="strip-sentence">{item.question}</span>
          <span className="converse-question-options">
            {item.options.map((option, optionIndex) => {
              const label = typeof option?.label === 'string' ? option.label : ''
              // Resolution is FIRST-subsequent-turn (composer.js): only the
              // option the user actually sent reads "selected"; a card the
              // user moved past (dismissed) simply inerts — the renderer must
              // never invent a historical selection (review round 2).
              const resolved = questionChoiceState(events, item.id)
              const inert = resolved.answered || resolved.dismissed
              const isSelected = resolved.selectedLabel === label.trim()
              const sendingChoice = questionChoices.sendingQuestionIds.includes(item.id)
              return (
                <button
                  key={`${item.id}-${optionIndex}`}
                  type="button"
                  className="chip-act"
                  disabled={!label || inert || sendingChoice || busy}
                  onClick={() => answerQuestion(item.id, label)}
                  title={option?.description || undefined}
                >
                  {isSelected ? `${label} selected` : label}
                </button>
              )
            })}
          </span>
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
          <span className="route-tool">{orDash(usageModel(model.latestUsage))}</span>
          <span className="dim"> · context </span>
          <span className="route-tool">{orDash(contextPct(model.latestUsage), (p) => `${p}%`)}</span>
          <span className="dim"> · </span>
          <span className="route-tool">{orDash(usageCost(model.latestUsage), (c) => `~$${c.toFixed(3)}`)}</span>
        </span>
        <button type="button" className="chip-neutral" onClick={dismiss}>Hide</button>
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
        <div className="banner">
          <span>{sendErr.message}</span>
          {sendErr.code && <code className="dim">{sendErr.code}</code>}
          {sendErr.nextAction && <span className="dim">Next: {sendErr.nextAction}</span>}
          {sendErr.actor && <span className="key">{errorActorLabel(sendErr.actor)}</span>}
        </div>
      )}

      {(pendingApprovals.length > 0 || pendingApprovalsError) && (
        <section className="converse-pending" aria-label="Pending approvals">
          <div className="converse-pending-head">
            <span className="converse-title">Pending approvals</span>
            {pendingApprovals.length > 0 && <span className="chip-neutral">{pendingApprovals.length}</span>}
            {pendingApprovalsError && <span className="dim">Could not refresh</span>}
          </div>
          {pendingApprovals.map(renderPendingApproval)}
        </section>
      )}

      <LiveRegion
        className="converse-log"
        ref={logRef}
        onScroll={onLogScroll}
        role="log"
        atomic={false}
        label="Assistant conversation"
      >
        {model.turns.length === 0 && pendingUserTurns.length === 0 && (
          <div className="converse-note">
            <span className="dot hollow" aria-hidden="true" />
            <span className="dim">Starting the conversation…</span>
          </div>
        )}
        {model.turns.map((t) => (
          <div key={t.turnId} className="converse-turn" data-element-id={formatElementId('turn', t.turnId) || undefined}>
            {userTextByTurn.get(t.turnId) && (
              <div className="converse-msg user">{userTextByTurn.get(t.turnId)}</div>
            )}
            {(t.images?.length ? t.images : localImagesByTurn.get(t.turnId))?.map((src, index) => (
              <img key={`${t.turnId}-image-${index}`} src={src} alt="User attached image" width="96" height="72" style={{ objectFit: 'cover', margin: '4px 4px 4px 0' }} />
            ))}
            {t.imageDescriptors?.length > 0 && (
              <div className="converse-note"><span className="dim">{t.imageDescriptors.length} image attachment{t.imageDescriptors.length === 1 ? '' : 's'} sent. Preview is unavailable after reload.</span></div>
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
        {queuedTurn && (
          <div className="converse-note">
            <span className="dot square" aria-hidden="true" />
            <span className="dim">Queued — will run when the current turn finishes</span>
          </div>
        )}
        {pendingUserTurns.map((u) => (
          <div key={u.turnId} className="converse-turn" data-element-id={formatElementId('turn', u.turnId) || undefined}>
            {(u.text || u.images?.length > 0) && <div className="converse-msg user">{u.text}</div>}
            {thumbnailImages(u.images).map((src, index) => (
              <img key={`${u.turnId}-pending-image-${index}`} src={src} alt="Pending image attachment" width="96" height="72" style={{ objectFit: 'cover', margin: '4px 4px 4px 0' }} />
            ))}
            <div className="converse-note">
              <span className="dot live pulse" aria-hidden="true" />
              <span className="dim">thinking…</span>
            </div>
          </div>
        ))}
      </LiveRegion>

      {secretNotice && (
        <div className="converse-secret-notice" role="alert" data-testid="converse-secret-notice">
          <span className="dot red" aria-hidden="true" />
          <span className="strip-sentence" data-testid="converse-secret-notice-reason">{secretNotice.reason}</span>
          {/* At most a four-character shape prefix behind a fixed bullet run
              (maskForNotice). This is the ONLY place any character of the
              pasted credential is rendered, and it is never the entropy. */}
          <span className="dim" data-testid="converse-secret-notice-mask">{secretNotice.masked}</span>
          {/* Rendered only when re-sending would actually send something: a
              refusal raised by an app-supplied choice string has no text in the
              box, and a button whose click does nothing is worse than no
              button. Disabled while a turn is in flight for the same reason. */}
          {secretNotice.overridable && !!input.trim() && (
            <button
              type="button"
              className="chip-neutral"
              data-testid="converse-secret-send-anyway"
              disabled={busy}
              onClick={() => send(undefined, { allowSecretOnce: true })}
            >
              Send anyway
            </button>
          )}
        </div>
      )}

      <div className="converse-input">
        <input
          value={input}
          // Any edit retires the refusal, which was about the text that WAS
          // there; a notice outliving its text reads as a stuck error.
          onChange={(e) => { setInput(e.target.value); setSecretNotice(null) }}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send() }
          }}
          onPaste={onPaste}
          placeholder={busy ? 'Assistant is working…' : 'Reply to the assistant…'}
          disabled={busy}
          spellCheck={false}
          aria-label="Reply to the assistant"
        />
        {(attachmentError || attachments.length > 0) && (
          <span className="dim" role={attachmentError ? 'alert' : undefined}>
            {attachmentError || attachments.map((image) => (
              <span key={image.id} style={{ display: 'inline-flex', alignItems: 'center', gap: 3, marginLeft: 4 }}>
                <img src={image.thumbnailUrl} alt="Pending image attachment" width="24" height="24" style={{ objectFit: 'cover' }} />
                <button type="button" className="chip-neutral" onClick={() => removeAttachment(image.id)} aria-label="Remove image attachment">Remove</button>
              </span>
            ))}
          </span>
        )}
        {busy && stoppableTurnId ? (
          // While a turn runs, Send has nothing to do (the input is disabled),
          // so the primary control becomes Stop — the terminal client's Esc,
          // given a visible affordance because a browser has no status line to
          // advertise the shortcut.
          <button
            type="button"
            className="chip-neutral"
            onClick={stop}
            disabled={stopping}
            title="Interrupt this turn (Esc)"
          >
            {stopping ? 'Stopping…' : 'Stop'}
          </button>
        ) : (
          // onClick={send} handed the CLICK EVENT to send()'s text parameter,
          // so the button posted the literal string "[object Object]" instead
          // of the typed reply (probed on this branch before the fix:
          // postMessage received {text: "[object Object]"}). Wrapping it is
          // what makes the button send the input at all, and therefore what
          // puts it behind the credential guard.
          <button type="button" className="chip-act" onClick={() => send()} disabled={busy || (!input.trim() && !attachments.length)}>
            {sending ? 'Sending…' : 'Send'}
          </button>
        )}
      </div>
    </div>
  )
}
