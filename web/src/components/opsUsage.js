// Ops usage scoreboard — the wire body of GET /api/operator/tenants reduced to
// the two scopes the drawer renders: this profile, and the platform.
//
// PROVENANCE. Re-derived 2026-09-01 from leaf_website `a18df33 feat(ops): add
// profile usage scoreboard`, which was written against a console/ copy of an
// OpsDrawer that predates the account-controls section. That original was
// dropped from leaf_website PR #246 rather than merged, because reconciling the
// two component bodies is a rewrite and not a conflict resolution. This is the
// rewrite, against:
//   web/src/components/OpsDrawer.jsx blob 46db3672 (last changed by 58d03fd9,
//   "move browser ops behind operator grants" #855), leaf-web-demo main 496ab659.
// If you are reading this after the drawer has moved on again, the binding
// check is opsUsage.test.js — it renders the real component, so it fails rather
// than rots when the scoreboard stops being wired.
//
// ABSENT IS NOT ZERO. The single defect carried by the original: it ran every
// field through `Number(value) || 0`, so a backend that could not read its
// ledger reported a confident $0.000 of spend. That is the one reading that
// inverts an operator's judgement, so this module keeps `null` for unknown all
// the way to the em dash, exactly as usage.js does for the turn strip.
import { finiteOrNull } from '../usage.js'

/**
 * Sum that refuses to guess. One unknown contributor makes the whole total
 * unknown: summing only the known half reports a real number that is lower
 * than the truth, which reads as fact and is worse than admitting the gap.
 */
function sumStrict(values) {
  let total = 0
  for (const value of values) {
    if (value === null) return null
    total += value
  }
  return total
}

function readTenant(tenant) {
  return {
    ...tenant,
    runs: finiteOrNull(tenant?.runs),
    usd_est: finiteOrNull(tenant?.usd_est),
    llm_turns: finiteOrNull(tenant?.llm_turns),
    llm_cost_tokens: finiteOrNull(tenant?.llm_cost_tokens),
    llm_usd_est: finiteOrNull(tenant?.llm_usd_est),
  }
}

/**
 * Platform scope. The server sends its own `platform` block, which is
 * authoritative because it can see tenants that spent LLM turns without ever
 * reaching the broker. Only when that block is absent (an older backend) do we
 * fall back to what the listed rows can actually prove.
 */
function readPlatform(platform, tenants) {
  if (platform && typeof platform === 'object') {
    return {
      profiles: finiteOrNull(platform.profiles),
      autocad_backend: {
        runs: finiteOrNull(platform.autocad_backend?.runs),
        usd_est: finiteOrNull(platform.autocad_backend?.usd_est),
      },
      llm: {
        turns: finiteOrNull(platform.llm?.turns),
        cost_tokens: finiteOrNull(platform.llm?.cost_tokens),
        usd_est: finiteOrNull(platform.llm?.usd_est),
      },
    }
  }
  return {
    profiles: tenants.length,
    autocad_backend: {
      runs: sumStrict(tenants.map((t) => t.runs)),
      usd_est: sumStrict(tenants.map((t) => t.usd_est)),
    },
    llm: {
      turns: sumStrict(tenants.map((t) => t.llm_turns)),
      cost_tokens: sumStrict(tenants.map((t) => t.llm_cost_tokens)),
      usd_est: sumStrict(tenants.map((t) => t.llm_usd_est)),
    },
  }
}

export function normalizeOpsUsage(body) {
  const tenants = Array.isArray(body?.tenants) ? body.tenants.map(readTenant) : []
  return { tenants, platform: readPlatform(body?.platform, tenants) }
}

// One formatter instance, not one per render: Intl constructors are the
// expensive part and this runs for every metric on every drawer paint.
const COMPACT = new Intl.NumberFormat('en-US', {
  notation: 'compact',
  maximumFractionDigits: 1,
})

/** A count for a scoreboard tile, or the em dash when it is not known. */
export function formatUsageCount(value) {
  return value === null || value === undefined ? '—' : COMPACT.format(value)
}

/** An estimated spend, or the em dash. Never "$0.000" for an unknown. */
export function formatUsageUsd(value) {
  return value === null || value === undefined ? '—' : `$${Number(value).toFixed(3)}`
}
