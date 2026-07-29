// turn_usage readings for the status strip, parsed to DATA (same split as
// markdown.js / composer.js so `node --test` can import them).
//
// Field names are the EXACT wire contract (harness/src/ports/index.ts,
// ConverseTurnUsage): `models?: string[]` and `total_cost_usd?: number`.
// `context_used_tokens` / `context_window_tokens` are NOT on the contract yet
// — the strip therefore reads them defensively and shows "unknown" until the
// backend ships them, which is the honest rendering, not a placeholder zero.

/**
 * Absent-or-number. The whole point of this helper: `Number(null)`,
 * `Number('')` and `Number([])` are all 0, so a naive `Number.isFinite(Number(x))`
 * turns a MISSING value into a confident `0%` / `~$0.000`. Only real, finite
 * numerics (or numeric strings) survive; everything else is absent.
 */
export function finiteOrNull(value) {
  if (value === null || value === undefined) return null
  if (typeof value === 'boolean') return null
  if (typeof value === 'string' && value.trim() === '') return null
  if (Array.isArray(value)) return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

/** Percent of the context window used, or null when either side is unknown. */
export function contextPct(usage) {
  const used = finiteOrNull(usage?.context_used_tokens)
  const window = finiteOrNull(usage?.context_window_tokens)
  if (used === null || window === null || window <= 0) return null
  return Math.max(0, Math.min(100, Math.round((used / window) * 100)))
}

/**
 * The model label. The contract carries `models` (an ARRAY — a turn can touch
 * more than one), so join them; a bare `model` string is tolerated for
 * forward/backward compatibility but never invented.
 */
export function usageModel(usage) {
  const models = usage?.models
  if (Array.isArray(models)) {
    const named = models.filter((m) => typeof m === 'string' && m.trim())
    if (named.length) return named.join(' · ')
    return null
  }
  if (typeof usage?.model === 'string' && usage.model.trim()) return usage.model
  return null
}

/** Session cost in USD, or null when the contract field is absent. */
export function usageCost(usage) {
  return finiteOrNull(usage?.total_cost_usd)
}

/** Render helper: a known reading, or the honest em dash. */
export function orDash(value, format = (v) => String(v)) {
  return value === null || value === undefined ? '—' : format(value)
}

// Full tool args/result for an expanded chip. Objects pretty-print; strings
// pass through. Bounded because a tool result can be large and the transcript
// must stay scrollable.
export const DETAIL_MAX_CHARS = 2000

export function fmtDetail(value) {
  let text
  if (typeof value === 'string') text = value
  else {
    try {
      text = JSON.stringify(value, null, 2)
    } catch {
      text = String(value) // circular / non-serialisable: show what we can
    }
  }
  if (text === undefined) text = String(value) // JSON.stringify(undefined) === undefined
  if (text.length <= DETAIL_MAX_CHARS) return text
  return `${text.slice(0, DETAIL_MAX_CHARS)}\n… ${text.length - DETAIL_MAX_CHARS} more characters`
}
