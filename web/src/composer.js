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
]

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
