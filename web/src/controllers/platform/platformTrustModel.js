export const PLATFORM_TRUST_AUTH_SOURCE = 'platform-trust'

export function isUnauthorized(error) {
  return error?.status === 401 || / -> 401$/.test(String(error?.message || ''))
}

// Capabilities whose UI affordance also requires a rollout-availability flag
// from the same /api/entitlements body. Policy (the tier holds the capability)
// and availability (this deployment has the stage open to this tenant) are
// distinct: `build` may be entitled while the R5 authoring stage is off, and
// showing an enabled Generate that 404s is the bug this mapping closes.
const AVAILABILITY_FIELD = { build: 'author_stage' }

export function entitlementAllowed(entitlements, key, fallback = true) {
  const value = entitlements?.entitlements?.[key]
  const allowed = value == null ? fallback : value !== false
  if (!allowed) return false
  const availField = AVAILABILITY_FIELD[key]
  if (!availField) return true
  const avail = entitlements?.availability?.[availField]
  // Absent availability (old server / mock) resolves permissive, mirroring the
  // entitlement fallback — behavior without the field is unchanged.
  if (avail == null) return true
  return avail !== false
}

export function classifyQuotaResult(result) {
  if (!result || result.ok !== false) return null
  if (result.quota_kind === 'daily_runs') {
    return {
      kind: 'daily_runs',
      error: result.error,
      limit: Number.isFinite(Number(result.limit)) ? Number(result.limit) : null,
      used: Number.isFinite(Number(result.used)) ? Number(result.used) : null,
    }
  }

  if (result?.error?.error_code !== 'quota_exceeded') return null
  return { kind: 'spend', error: result.error, limit: null, used: null }
}

export function resolveQuotaStatus({ result, conditionAt = 0, usage, usageAt = 0 } = {}) {
  const condition = classifyQuotaResult(result)
  if (!condition) return { condition: null, freshUsage: null, cleared: false, visible: null }

  // A usage read that completed before or at the rejection cannot clear it.
  const freshUsage = conditionAt > 0 && usageAt > conditionAt ? usage : null
  let cleared = false
  if (condition.kind === 'spend') {
    cleared = !!(freshUsage?.cap?.enabled
      && typeof freshUsage.cap.remaining === 'number'
      && freshUsage.cap.remaining > 0)
  } else if (condition.limit != null) {
    cleared = !!(freshUsage?.today
      && Number(freshUsage.today.runs || 0) < condition.limit)
  }

  return { condition, freshUsage, cleared, visible: cleared ? null : condition }
}

export function classifyHealth(health) {
  if (!health) return { status: 'unknown', degraded: false }
  const degraded = health.degraded_mode === true || health.ok === false
  return { status: degraded ? 'degraded' : 'healthy', degraded }
}
