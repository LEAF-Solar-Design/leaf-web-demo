const object = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const text = value => typeof value === 'string' && value.length > 0 && value.length <= 256
const string = value => text(value) ? value : null
const time = value => text(value) && Number.isFinite(Date.parse(value)) ? value : null
const count = value => Number.isSafeInteger(value) && value >= 0 ? value : null
const dollars = value => typeof value === 'number' && Number.isFinite(value) ? value
  : typeof value === 'string' && /^-?\d{1,20}(?:\.\d{1,10})?(?![\s\S])/.test(value) ? value : null
const digest = value => typeof value === 'string' && /^(?:[a-f0-9]{40}|[a-f0-9]{64})(?![\s\S])/i.test(value) ? value : null

function trialOf(value) {
  if (!object(value) || !text(value.rollout_id) || !text(value.state)) return null
  const screen = value.screen
  return { rollout_id: value.rollout_id, state: value.state, at: time(value.at),
    screen: object(screen) ? { verdict: string(screen.verdict), reason: string(screen.reason),
      net_dollars: dollars(screen.net_dollars), episodes_completed: count(screen.episodes_completed),
      scope_count: count(screen.scope_count), kind: string(screen.kind),
      complete: typeof screen.complete === 'boolean' ? screen.complete : null } : null }
}

function keeperOf(value) {
  if (!object(value) || !text(value.feature) || !text(value.rollout_id)) return null
  const window = text(value.window) ? value.window : object(value.window)
    ? { start: time(value.window.start), end: time(value.window.end) } : null
  return { feature: value.feature, rollout_id: value.rollout_id, scope_count: count(value.scope_count),
    window, episodes_completed: count(value.episodes_completed), net_dollars: dollars(value.net_dollars), added_at: time(value.added_at) }
}

function latestOf(value) {
  if (!object(value) || !text(value.rollout_id) || !text(value.state)) return null
  return { rollout_id: value.rollout_id, kind: string(value.kind), feature: string(value.feature),
    snapshot_digest: digest(value.snapshot_digest), live_at: time(value.live_at), state: value.state,
    closed_at: time(value.closed_at), rollback_pending: value.rollback_pending === true, source_pending: value.source_pending === true }
}

function bounded(values, parse) {
  const result = []
  for (const value of values) {
    const parsed = parse(value)
    if (parsed) result.push(parsed)
    if (result.length === 20) break
  }
  return result
}

// Never fall back to an older controller report when the newest is malformed.
export function readController(provider) {
  try {
    const rows = provider?.rowsOf?.('evidence')
    if (!Array.isArray(rows)) return null
    let newest = null
    let newestAt = -Infinity
    for (const row of rows) {
      if (!object(row?.data) || row.data.kind !== 'controller') continue
      const at = time(row.data.received_at) ? Date.parse(row.data.received_at)
        : Number.isFinite(row.as_of) ? row.as_of : -Infinity
      if (at >= newestAt) { newest = row; newestAt = at }
    }
    const data = newest?.data
    if (!data || data.report_only !== true || !time(data.received_at)
      || !Array.isArray(data.screen_trials) || !Array.isArray(data.keepers) || !object(data.versions)) return null
    return { kind: 'controller', report_only: true, received_at: data.received_at,
      stale: newest.stale === true,
      truncated: newest.truncated === true || data.truncated === true || data.screen_trials.length > 20 || data.keepers.length > 20,
      keepers_unavailable: data.keepers_unavailable === true,
      screen_trials: bounded(data.screen_trials, trialOf), keepers: bounded(data.keepers, keeperOf),
      versions: { champion_digest: digest(data.versions.champion_digest), latest: latestOf(data.versions.latest) } }
  } catch { return null }
}
