// Slice 10b/10c: the bar's act-scope palette and find-scope result rows, as
// pure DATA (composer.js's split: a .jsx component owns rendering only, this
// module owns the filtering so it is testable with `node --test`/vitest
// without a DOM).
//
// Bounded by construction (build-doctrine: bounds are release blockers, not
// follow-ups). One screenful of rows is the budget; the palette scrolls a
// short list, it does not paginate a long one.
export const MAX_ACTION_ROWS = 24
export const MAX_ARTIFACT_ROWS_PER_KIND = 8

function includesFold(haystack, needle) {
  return String(haystack || '').toLowerCase().includes(needle)
}

function matches(query, ...fields) {
  const q = String(query || '').trim().toLowerCase()
  if (!q) return true
  return fields.some((f) => includesFold(f, q))
}

// Registry action rows (ribbon tools + the shortcut sheet's own row, slice
// 10b), already carrying the LIVE reason from the same builder the ribbon
// itself calls — this module invents no reason of its own. Prefix matches on
// the label rank first, like the "/" picker's own rankEntries.
export function actionPaletteRows(actions, query) {
  const q = String(query || '').trim().toLowerCase()
  const pre = []
  const sub = []
  for (const a of actions || []) {
    if (!a || typeof a.id !== 'string') continue
    const label = String(a.label || '')
    if (!matches(query, label)) continue
    ;(label.toLowerCase().startsWith(q) ? pre : sub).push(a)
  }
  return [...pre, ...sub].slice(0, MAX_ACTION_ROWS).map((a) => ({
    kind: 'action',
    id: a.id,
    label: a.label,
    icon: a.icon || '',
    kbd: a.kbd || null,
    disabled: !!a.disabled,
    reason: a.reason || '',
    onSelect: a.onSelect,
  }))
}

// The version-history artifact index (GET /api/drawings/{id}/versions,
// already fetched by the caller — this is pure filtering, no I/O).
export function versionArtifactRows(versionsPayload, query) {
  const rows = []
  for (const v of versionsPayload?.versions || []) {
    const note = v?.note || ''
    const tool = v?.tool || ''
    const label = `v${v?.v}`
    if (!matches(query, label, note, tool)) continue
    rows.push({
      kind: 'version',
      id: `version:${v?.v}`,
      label,
      description: [tool, note].filter(Boolean).join(' · '),
    })
    if (rows.length >= MAX_ARTIFACT_ROWS_PER_KIND) break
  }
  return rows
}

// The operator-session artifact index (GET /api/operator/sessions). An empty
// payload (a non-operator caller, or the store unavailable) yields zero
// rows honestly — api.js already folds that failure into {sessions: []}.
export function sessionArtifactRows(sessionsPayload, query) {
  const rows = []
  for (const s of sessionsPayload?.sessions || []) {
    const label = s?.session_id || ''
    if (!label) continue
    if (!matches(query, label, s?.profile, s?.environment, s?.status)) continue
    rows.push({
      kind: 'session',
      id: `session:${label}`,
      label,
      description: [s?.profile, s?.environment, s?.status].filter(Boolean).join(' · '),
    })
    if (rows.length >= MAX_ARTIFACT_ROWS_PER_KIND) break
  }
  return rows
}

// The tools/capabilities artifact index — reuses the SAME `tools` list the
// "/" picker already holds (PromptBox's own prop), no second fetch.
export function toolArtifactRows(tools, query) {
  const rows = []
  for (const t of tools || []) {
    const name = t?.name || t?.id || ''
    if (!name) continue
    const desc = t?.description || ''
    if (!matches(query, name, desc)) continue
    rows.push({ kind: 'tool', id: `tool:${name}`, label: name, description: desc })
    if (rows.length >= MAX_ARTIFACT_ROWS_PER_KIND) break
  }
  return rows
}

// The find-scope's rows: the server's own /api/search result list (slice
// 10c), reshaped to the same {kind, id, label, description} the act-scope
// artifact rows use, so ONE row renderer serves both resolvers.
export function findResultRows(searchPayload) {
  const rows = []
  for (const r of searchPayload?.results || []) {
    if (!r || typeof r.id !== 'string' || typeof r.label !== 'string') continue
    rows.push({ kind: r.kind || 'result', id: r.id, label: r.label, description: r.description || '' })
  }
  return rows
}
