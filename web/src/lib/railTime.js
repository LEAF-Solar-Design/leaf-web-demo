// TM1 time for the rails (slice 11a moved these out of JobRail.jsx so the
// BuildQueueCard can read them without a circular import; JobRail re-exports
// them for WorkspaceSummary and operator/SessionPanel, which import from
// there). Accepts epoch seconds (the server's REAL columns), epoch ms, or ISO
// strings. The rail's absolute clock and day grouping retain their original
// presentation; elapsed-time wording is shared with the other panels.

function toDate(ts) {
  if (ts == null) return null
  if (typeof ts === 'number') return new Date(ts < 1e12 ? ts * 1000 : ts)
  const d = new Date(ts)
  return Number.isNaN(d.getTime()) ? null : d
}

// These shared formatters take epoch milliseconds; callers parsing an API
// timestamp do so at the boundary rather than guessing seconds versus ms.
export function relativeTime(ms, now = Date.now()) {
  if (!Number.isFinite(ms) || !Number.isFinite(now)) return 'Time unavailable'
  const d = new Date(ms)
  if (Number.isNaN(d.getTime())) return 'Time unavailable'
  const age = now - ms
  if (age < 60_000) return 'now'
  if (age < 3_600_000) return `${Math.floor(age / 60_000)} m`
  if (age < 86_400_000) return `${Math.floor(age / 3_600_000)} h`
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

export function absoluteWithZone(ms, { timeZone = 'America/Chicago' } = {}) {
  if (!Number.isFinite(ms)) return undefined
  const d = new Date(ms)
  if (Number.isNaN(d.getTime())) return undefined
  return d.toLocaleString('en-US', {
    timeZone, month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'shortGeneric',
  }).replace(/ (?:GMT|UTC)(?:\+0)?$/, ' UTC')
}

// A lease horizon counts down, rather than describing an event's age. Keep
// its existing rounding and suppress the horizon once the lease has elapsed.
export function relativeUntil(ms, now = Date.now()) {
  if (!Number.isFinite(ms) || !Number.isFinite(now)) return null
  const d = new Date(ms)
  if (Number.isNaN(d.getTime())) return null
  const remainingMs = ms - now
  if (remainingMs <= 0) return null
  const mins = Math.max(Math.round(remainingMs / 60_000), 1)
  if (mins >= 1440) return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  if (mins >= 60) return `~${Math.round(mins / 60)} h`
  return `~${mins} m`
}

export function fmtWhen(ts) {
  const d = toDate(ts)
  if (!d) return null
  const abs = d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
  const rel = relativeTime(d.getTime())
  const clock = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
  return { rel, abs, clock, day: d.toDateString(), date: d }
}

// 26px day-boundary label ("Today · Jul 16") for the ledger. Exported so
// other panels rendering the same TM1 day-grouped ledger convention (e.g.
// operator/SessionPanel's transcript) don't reimplement the "Today"/
// "Yesterday" boundary text.
export function dayLabel(d) {
  const md = d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  if (d.toDateString() === new Date().toDateString()) return `Today · ${md}`
  if (d.toDateString() === new Date(Date.now() - 86_400_000).toDateString()) return `Yesterday · ${md}`
  return md
}
