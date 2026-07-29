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
