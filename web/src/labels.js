// Humanize a snake_case result/param key for display. A trailing unit suffix
// becomes a parenthesized unit, so "distance_in" reads as a phrase ("distance
// (in)") instead of the broken-English "distance in". Keys with no unit suffix
// are unchanged, so pinned demo copy (panels_near_edge, panels_total) does not
// shift.
//
// PURE module: no React, no import.meta — node can import it headless (mirrors
// the errorHumanize.js precedent).

const UNITS = { in: 'in', sqft: 'sqft', ft: 'ft', deg: 'deg', ms: 'ms', pct: '%' }

export function humanKey(k) {
  const parts = String(k).split('_')
  const last = parts[parts.length - 1]
  if (parts.length > 1 && Object.prototype.hasOwnProperty.call(UNITS, last)) {
    return `${parts.slice(0, -1).join(' ')} (${UNITS[last]})`
  }
  return parts.join(' ')
}

export default humanKey
