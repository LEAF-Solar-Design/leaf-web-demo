// Composer well logic, parsed to DATA — the same split markdown.js/Markdown.jsx
// uses: pure functions live in a .js module so `node --test` can import them
// (a .jsx file cannot be loaded by the node test runner), and the component
// stays responsible only for turning that data into elements.

// Autogrow without measuring the DOM: the well is a fixed 40 px row until the
// text needs a second line, then it grows by line count to a ceiling (past
// that the textarea scrolls, so a pasted wall of text never eats the viewport).
export const LINE_PX = 20
export const MAX_ROWS = 8

export function autoGrowHeight(value, { linePx = LINE_PX, maxRows = MAX_ROWS } = {}) {
  const lines = String(value ?? '').split('\n').length
  if (lines <= 1) return undefined // keep the CSS-owned single-line height
  return `${Math.min(lines, maxRows) * linePx}px`
}

// Prompt history stays in the browser for this page lifetime only. The caller
// supplies the active session id, so a conversation can never recall text from
// another session. Keeping this as plain data makes the keyboard contract easy
// to test without a DOM or React renderer.
const DEFAULT_HISTORY_SESSION = '__default__'

function historySessionKey(sessionId) {
  return sessionId == null ? DEFAULT_HISTORY_SESSION : String(sessionId)
}

export function createPromptHistoryState(sessionId = null) {
  return {
    sessionId,
    histories: {},
    historyIndex: null,
    draft: null,
    value: '',
  }
}

export function promptHistoryFor(state, sessionId = state?.sessionId) {
  return state?.histories?.[historySessionKey(sessionId)] || []
}

export function setPromptHistorySession(state, sessionId) {
  if (state.sessionId === sessionId) return state
  return { ...state, sessionId, historyIndex: null, draft: null }
}

export function setPromptHistoryValue(state, value) {
  return { ...state, value: String(value ?? '') }
}

export function appendPromptHistory(state, prompt, sessionId = state.sessionId) {
  const text = String(prompt ?? '')
  const keyed = setPromptHistorySession(state, sessionId)
  if (!text.trim()) return { ...keyed, historyIndex: null, draft: null }
  const key = historySessionKey(sessionId)
  return {
    ...keyed,
    histories: { ...keyed.histories, [key]: [...promptHistoryFor(keyed, sessionId), text] },
    historyIndex: null,
    draft: null,
  }
}

export function caretOnFirstLine(value, selectionStart = String(value ?? '').length) {
  return !String(value ?? '').slice(0, selectionStart).includes('\n')
}

export function caretOnLastLine(value, selectionStart = String(value ?? '').length) {
  return !String(value ?? '').slice(selectionStart).includes('\n')
}

// `event` deliberately accepts the small shape PromptBox needs instead of a
// DOM event: { key, value, selectionStart, sessionId }. A handled result owns
// the arrow key; an unhandled result must fall through to textarea behaviour.
export function historyKeydown(state, event = {}) {
  const sessionId = event.sessionId ?? state.sessionId
  const keyed = setPromptHistorySession(state, sessionId)
  const value = String(event.value ?? keyed.value ?? '')
  const selectionStart = Number.isFinite(event.selectionStart)
    ? Math.max(0, Math.min(event.selectionStart, value.length))
    : value.length
  const current = { ...keyed, value }
  const history = promptHistoryFor(current, sessionId)
  const result = (next, handled) => ({
    handled,
    value: next.value,
    selectionStart: next.value.length,
    state: next,
  })

  if (event.key === 'ArrowUp') {
    if (!history.length || !caretOnFirstLine(value, selectionStart)) return result(current, false)
    const historyIndex = current.historyIndex == null
      ? history.length - 1
      : Math.max(0, current.historyIndex - 1)
    return result({
      ...current,
      value: history[historyIndex],
      historyIndex,
      draft: current.historyIndex == null ? value : current.draft,
    }, true)
  }

  if (event.key === 'ArrowDown') {
    if (current.historyIndex == null || !caretOnLastLine(value, selectionStart)) return result(current, false)
    if (current.historyIndex >= history.length - 1) {
      return result({ ...current, value: current.draft ?? '', historyIndex: null, draft: null }, true)
    }
    const historyIndex = current.historyIndex + 1
    return result({ ...current, value: history[historyIndex], historyIndex }, true)
  }

  return result(current, false)
}

// The slash menu's entry kinds, in the order the picker groups them. Commands
// act on the session, skills author or run a procedure, tools are the tenant's
// registered capabilities — the same three-way split the terminal client shows.
export const REGISTRY_GROUPS = [
  { kind: 'command', label: 'Commands' },
  { kind: 'skill', label: 'Skills' },
  { kind: 'tool', label: 'Tools' },
  { kind: 'resource', label: 'Resources' },
]

// The registry is the richer source when it knows about a skill. The skills
// endpoint fills gaps while its registry projection catches up, without
// changing the source's existing command and tool order.
export function mergeSkillEntries(registryEntries, skills) {
  const merged = Array.isArray(registryEntries) ? [...registryEntries] : []
  const names = new Set(merged
    .filter((entry) => entry && typeof entry.name === 'string' && entry.name)
    .map((entry) => entry.name))
  for (const skill of skills || []) {
    if (!skill || typeof skill.name !== 'string' || !skill.name || names.has(skill.name)) continue
    names.add(skill.name)
    merged.push({ ...skill, kind: 'skill' })
  }
  return merged
}

// Static client commands and server-backed MCP roots both join the same picker
// source. A root is deliberately not a live MCP resource enumeration: that
// needs a harness proxy endpoint. Its insertion text scopes the follow-up.
export function mergePickerEntries(registryEntries, skills, resources, staticEntries = []) {
  const merged = mergeSkillEntries(registryEntries, skills)
  const names = new Set(merged
    .filter((entry) => entry && typeof entry.name === 'string' && entry.name)
    .map((entry) => `${entry.kind || ''}:${entry.name}`))
  for (const entry of staticEntries) {
    if (!entry?.name || names.has(`${entry.kind || ''}:${entry.name}`)) continue
    names.add(`${entry.kind || ''}:${entry.name}`)
    merged.push(entry)
  }
  for (const server of resources || []) {
    if (!server || typeof server.name !== 'string' || !server.name) continue
    const key = `resource:${server.name}`
    if (names.has(key)) continue
    names.add(key)
    merged.push({
      kind: 'resource',
      name: server.name,
      description: server.host || '',
      insertionText: `@${server.name}:`,
    })
  }
  return merged
}

// The slash picker remains a leading command only. Mentions may start at a
// word boundary, but never inside an address such as a@b. Callers pass IME
// state so candidate confirmation cannot open or steal the picker.
export function pickerTrigger(value, selectionStart = String(value ?? '').length, isComposing = false) {
  if (isComposing) return null
  const text = String(value ?? '')
  const caret = Number.isFinite(selectionStart)
    ? Math.max(0, Math.min(selectionStart, text.length))
    : text.length
  const prefix = text.slice(0, caret)
  if (text.startsWith('/') && !/\s/.test(text.slice(1))) {
    return { kind: 'slash', query: prefix.slice(1), start: 0, end: caret }
  }
  const at = prefix.lastIndexOf('@')
  if (at === -1 || (at > 0 && !/\s/.test(prefix[at - 1]))) return null
  const query = prefix.slice(at + 1)
  if (/\s/.test(query)) return null
  return { kind: 'resource', query, start: at, end: caret }
}

export function replacePickerTrigger(value, trigger, insertion) {
  const text = String(value ?? '')
  if (!trigger || !Number.isInteger(trigger.start) || !Number.isInteger(trigger.end)) return text
  const start = Math.max(0, Math.min(trigger.start, text.length))
  const end = Math.max(start, Math.min(trigger.end, text.length))
  return `${text.slice(0, start)}${String(insertion ?? '')}${text.slice(end)}`
}

// A busy text turn may be parked once. Confirmations and ephemeral credential
// grants cannot be queued by the server, so keep that gate pure and explicit.
export function shouldRetryWithQueue(errorKind, { text, confirm, credential_grant } = {}) {
  return errorKind === 'busy'
    && typeof text === 'string'
    && text.trim().length > 0
    && confirm == null
    && credential_grant == null
}

// THE FIRST subsequent user turn resolves a question — never a later one.
// Scanning every later turn let an unrelated message that happened to say
// "Yes" retro-answer an old card on replay, and the renderer then invented a
// historical selection (review round 2). The rule: the turn the user sends
// right after the question IS the answer moment. If its text matches an
// option (trimmed), that option — and only that option — is selected; if it
// does not match, the user moved on and the card is dismissed-inert. Replay
// is authoritative either way.
export function questionChoiceState(events, questionId) {
  const questionAt = (events || []).findIndex((event) => (
    event?.type === 'question_required' && event.data?.question_id === questionId
  ))
  if (questionAt === -1) return { answered: false, selectedLabel: null, dismissed: false }
  const labels = new Set((events[questionAt].data?.options || [])
    .map((option) => typeof option?.label === 'string' ? option.label.trim() : '')
    .filter(Boolean))
  // A prompt QUEUED before the question was authored before the user ever saw
  // it — its later turn_started (correlated by queued_id) is not a reply and
  // must not resolve the card either way (review round 3).
  const preQuestionQueued = new Set((events || []).slice(0, questionAt)
    .filter((event) => event?.type === 'turn_queued' && event.data?.queued_id)
    .map((event) => event.data.queued_id))
  const firstReply = (events || []).slice(questionAt + 1).find(
    (event) => event?.type === 'turn_started'
      && !(event.data?.queued_id && preQuestionQueued.has(event.data.queued_id)),
  )
  if (!firstReply) return { answered: false, selectedLabel: null, dismissed: false }
  // The first subsequent turn OF ANY KIND resolves. A confirm-only turn (no
  // text) is still the user acting past the question — it DISMISSES; skipping
  // it let a later ordinary message retro-answer (review round 3).
  const replyText = typeof firstReply.data?.text === 'string'
    ? firstReply.data.text.trim() : ''
  if (replyText && labels.has(replyText)) {
    return { answered: true, selectedLabel: replyText, dismissed: false }
  }
  return { answered: false, selectedLabel: null, dismissed: true }
}

// A click has one normal send action. The caller keeps this small state only
// while the post is in flight; durable turn_started data decides the final state.
export function chooseQuestionOption(state = { sendingQuestionIds: [] }, events, questionId, optionLabel) {
  const resolved = questionChoiceState(events, questionId)
  if (resolved.answered || resolved.dismissed
    || state.sendingQuestionIds.includes(questionId)) return { action: 'ignore', state }
  return {
    action: 'send',
    text: optionLabel,
    state: { ...state, sendingQuestionIds: [...state.sendingQuestionIds, questionId] },
  }
}

export function clearSendingQuestion(state = { sendingQuestionIds: [] }, questionId) {
  return {
    ...state,
    sendingQuestionIds: state.sendingQuestionIds.filter((id) => id !== questionId),
  }
}

// A queued prompt is identified by the server's queued_id, never by its text.
// Streams can win the race against the 202 response, so retain recent starts
// until the matching response arrives. This is plain data so ConversePanel only
// has to apply the decision and render it.
export const MAX_SEEN_QUEUED_STARTS = 8

export function createQueuedTurnState() {
  return { queuedTurn: null, seenQueuedStarts: [], seenQueuedDrops: [] }
}

function queuedStartFrom(event = {}) {
  if (event.type !== 'turn_started' || !event.turn_id || !event.data?.queued_id) return null
  return {
    queued_id: event.data.queued_id,
    turn_id: event.turn_id,
    text: String(event.data.text ?? ''),
  }
}

function rememberQueuedStart(seenQueuedStarts, start) {
  if (!start) return seenQueuedStarts
  return [...seenQueuedStarts.filter((seen) => seen.queued_id !== start.queued_id), start]
    .slice(-MAX_SEEN_QUEUED_STARTS)
}

function turnFromQueuedStart(start) {
  return { turnId: start.turn_id, text: start.text }
}

// Apply a stream event to queue state. A started turn with no queued_id is
// unrelated to this queue even when its text happens to be identical.
export function reconcileQueuedTurn(state = createQueuedTurnState(), event = {}) {
  const start = queuedStartFrom(event)
  if (start) {
    const seenQueuedStarts = rememberQueuedStart(state.seenQueuedStarts, start)
    if (state.queuedTurn?.queuedId === start.queued_id) {
      return {
        action: 'promote',
        turn: turnFromQueuedStart(start),
        state: { queuedTurn: null, seenQueuedStarts },
      }
    }
    return { action: 'keep', state: { ...state, seenQueuedStarts } }
  }
  if (event.type === 'turn_queue_dropped' && event.data?.queued_id) {
    // Remember the drop even when no note exists yet: the server can drop
    // SYNCHRONOUSLY (entitlement denied at the enqueue-time kick) so this
    // event can beat the 202 — without the memory, the late response would
    // install a note nothing will ever clear (review round 3).
    const seenQueuedDrops = [...(state.seenQueuedDrops ?? []), event.data.queued_id]
      .slice(-MAX_SEEN_QUEUED_STARTS)
    if (event.data.queued_id === state.queuedTurn?.queuedId) {
      return { action: 'clear', state: { ...state, queuedTurn: null, seenQueuedDrops } }
    }
    return { action: 'keep', state: { ...state, seenQueuedDrops } }
  }
  return { action: 'keep', state }
}

// Apply the queued 202 response. If its turn_started event arrived first, use
// that recorded event to render the bubble immediately instead of flashing a
// stale queue note.
export function acceptQueuedTurn(state = createQueuedTurnState(), queuedTurn) {
  if ((state.seenQueuedDrops ?? []).includes(queuedTurn?.queuedId)) {
    // Dropped before the 202 even landed — never install the note.
    return { action: 'drop', state: { ...state, queuedTurn: null } }
  }
  const started = state.seenQueuedStarts.find(
    (seen) => seen.queued_id === queuedTurn?.queuedId,
  )
  if (started) {
    return {
      action: 'promote',
      turn: turnFromQueuedStart(started),
      state: { ...state, queuedTurn: null },
    }
  }
  return {
    action: 'queue',
    state: { ...state, queuedTurn },
  }
}

// Prefix matches rank ahead of substring matches, then the group order above
// decides ties. Entries with no `kind` (today's tools-only payload, which has
// no registry grouping yet) all share one rank, so a stable sort leaves them
// in server order — i.e. wiring this in changes nothing until the registry
// endpoint starts sending `kind`.
export function rankEntries(entries, query) {
  const q = String(query ?? '').toLowerCase()
  const pre = []
  const sub = []
  for (const e of entries || []) {
    const name = (e.name || '').toLowerCase()
    if (name.startsWith(q)) pre.push(e)
    else if (name.includes(q) || (e.description || '').toLowerCase().includes(q)) sub.push(e)
  }
  const groupRank = (e) => {
    const i = REGISTRY_GROUPS.findIndex((g) => g.kind === e.kind)
    return i === -1 ? REGISTRY_GROUPS.length : i
  }
  const byGroup = (a, b) => groupRank(a) - groupRank(b)
  return [...pre.sort(byGroup), ...sub.sort(byGroup)]
}

// A registry entry is only offered when the client can actually run it.
//
// The server DECLARES what exists (commands, skills, tools); the client knows
// which of those it can dispatch. A command whose `client_action` has no
// handler here would be a dead affordance in a menu the user is trusting, so
// it is filtered out rather than listed and then silently ignored. Tools and
// skills go through the existing dispatch path, so they need no handler to be
// runnable.
//
// Wiring a new command is therefore exactly one change: add its handler.
export function filterRunnable(entries, commandActions = {}) {
  const runnable = []
  for (const entry of entries || []) {
    if (!entry || typeof entry.name !== 'string' || !entry.name) continue
    if (entry.kind === 'command') {
      const action = entry.client_action
      if (!action || typeof commandActions[action] !== 'function') continue
    }
    runnable.push(entry)
  }
  return runnable
}
