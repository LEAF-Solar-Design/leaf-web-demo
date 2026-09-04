// TM1 time for the rails (slice 11a moved these out of JobRail.jsx so the
// BuildQueueCard can read them without a circular import; JobRail re-exports
// them for WorkspaceSummary and operator/SessionPanel, which import from
// there). Accepts epoch seconds (the server's REAL columns), epoch ms, or ISO
// strings. Bodies are byte-for-byte the JobRail originals.

function toDate(ts) {
  if (ts == null) return null
  if (typeof ts === 'number') return new Date(ts < 1e12 ? ts * 1000 : ts)
  const d = new Date(ts)
  return Number.isNaN(d.getTime()) ? null : d
}

export function fmtWhen(ts) {
  const d = toDate(ts)
  if (!d) return null
  const abs = d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
  const age = Date.now() - d.getTime()
  let rel
  if (age < 60_000) rel = 'now'
  else if (age < 3_600_000) rel = `${Math.floor(age / 60_000)} m`
  else if (age < 86_400_000) rel = `${Math.floor(age / 3_600_000)} h`
  else rel = d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
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
