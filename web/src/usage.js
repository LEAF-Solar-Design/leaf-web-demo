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

/** Metered tokens first, with any API-rate dollar value labelled as an estimate. */
export function usageCostLabel(tokens, usd) {
  const tokenCount = finiteOrNull(tokens)
  const cost = finiteOrNull(usd)
  const parts = []
  if (tokenCount !== null && tokenCount >= 0) parts.push(`${tokenCount.toLocaleString()} tokens`)
  if (cost !== null && cost >= 0) {
    const usdText = cost > 0 && cost < 0.01 ? cost.toFixed(4) : cost.toFixed(3)
    parts.push(`~$${usdText} est`)
  }
  return parts.length ? parts.join(' · ') : orDash(null)
}

/** Render helper: a known reading, or the honest em dash. */
export function orDash(value, format = (v) => String(v)) {
  return value === null || value === undefined ? '·' : format(value)
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

// A display name for one model id. Known families read as people say them
// ("claude-sonnet-5-5" -> "Claude Sonnet 5.5", "qwen3.8-27b" -> "Qwen3.8-27B");
// anything else is shown as the id itself, never guessed.
export function modelDisplayName(id) {
  if (typeof id !== 'string' || !id.trim()) return null
  const value = id.trim()
  const claude = /^claude-([a-z]+)-(\d+(?:-\d+)*)$/i.exec(value)
  if (claude) return `Claude ${claude[1][0].toUpperCase()}${claude[1].slice(1).toLowerCase()} ${claude[2].replace(/-/g, '.')}`
  const qwen = /^qwen(\d+(?:\.\d+)*)-(.+)$/i.exec(value)
  if (qwen) return `Qwen${qwen[1]}-${qwen[2].replace(/(\d)b$/i, '$1B')}`
  return value
}

const BADGE_LABEL_MAX = 64

/**
 * Which model answered a turn, from that turn's own turn_usage (the runner's
 * real provider and model, never a constant). Returns null when the turn did
 * not report a model. `where` names a non-default provider: the label a
 * tenant's self-hosted provider reports (for example "local on Mac mini"), or
 * "OpenAI"; the default Claude path has none.
 */
export function modelBadge(usage) {
  const models = Array.isArray(usage?.models) ? usage.models : (usage?.model ? [usage.model] : [])
  const name = modelDisplayName(models.find((m) => typeof m === 'string' && m.trim()))
  if (!name) return null
  const provider = typeof usage?.provider === 'string' ? usage.provider : null
  const label = typeof usage?.provider_label === 'string' ? usage.provider_label.trim().slice(0, BADGE_LABEL_MAX) : ''
  const local = provider === 'openai_compatible'
  const where = local ? (label || 'self-hosted') : provider === 'openai' ? 'OpenAI' : null
  return { name, where, local, text: where ? `${name} · ${where}` : name }
}
