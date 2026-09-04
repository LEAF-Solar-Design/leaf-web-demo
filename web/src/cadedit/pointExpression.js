// W4f-8: the reference command line's point grammar, typed into a point
// step's FIRST field instead of a plain number:
//   "x,y"          an absolute pair
//   "@dx,dy"       relative to the anchor (the previous point)
//   "dist<angle"   polar from the origin, angle in degrees counter-clockwise from +x
//   "@dist<angle"  relative polar from the anchor
// Pure and bounded: at most MAX_EXPRESSION_CHARS, every number must be a
// finite decimal literal (no parseFloat leniency: "10abc" is refused), a
// relative form without an anchor resolves to nothing, and the result is
// rounded to three decimals (the store parses strings; 6.1e-17 noise never
// reaches a field). Anything else is null; the caller keeps the store's own
// refusal for that.
export const MAX_EXPRESSION_CHARS = 64

const DEG = Math.PI / 180
const NUMBER = /^[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?$/

const num = (text) => {
  const t = String(text).trim()
  if (!NUMBER.test(t)) return null
  const n = Number(t)
  return Number.isFinite(n) ? n : null
}

const round3 = (v) => {
  const r = Math.round(v * 1000) / 1000
  return Object.is(r, -0) ? 0 : r
}

/** True when the text is shaped like a point expression rather than a number: a pair or a polar form. */
export function isPointExpression(raw) {
  if (typeof raw !== 'string') return false
  const t = raw.trim()
  if (!t || t.length > MAX_EXPRESSION_CHARS) return false
  return t.includes(',') || t.includes('<') || t.startsWith('@')
}

/**
 * The parsed shape, or null: { relative, polar, a, b } where (a, b) is
 * (x, y) for a pair and (dist, angleDeg) for a polar form.
 */
export function parsePointExpression(raw) {
  if (!isPointExpression(raw)) return null
  let t = raw.trim()
  let relative = false
  if (t.startsWith('@')) { relative = true; t = t.slice(1).trim() }
  if (!t) return null
  const lt = t.indexOf('<')
  if (lt >= 0) {
    if (t.indexOf('<', lt + 1) >= 0 || t.includes(',')) return null
    const a = num(t.slice(0, lt))
    const b = num(t.slice(lt + 1))
    if (a === null || b === null) return null
    return Object.freeze({ relative, polar: true, a, b })
  }
  const comma = t.indexOf(',')
  if (comma < 0 || t.indexOf(',', comma + 1) >= 0) return null
  const a = num(t.slice(0, comma))
  const b = num(t.slice(comma + 1))
  if (a === null || b === null) return null
  return Object.freeze({ relative, polar: false, a, b })
}

/**
 * The point the expression names, as [x, y] rounded to three decimals, or
 * null when it does not parse, when a relative form has no anchor, or when
 * the anchor is not a finite pair.
 */
export function resolvePointExpression(raw, anchor = null) {
  const p = parsePointExpression(raw)
  if (!p) return null
  let x = p.polar ? p.a * Math.cos(p.b * DEG) : p.a
  let y = p.polar ? p.a * Math.sin(p.b * DEG) : p.b
  if (p.relative) {
    if (!Array.isArray(anchor) || anchor.length !== 2 || !Number.isFinite(anchor[0]) || !Number.isFinite(anchor[1])) return null
    x += anchor[0]
    y += anchor[1]
  }
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null
  return [round3(x), round3(y)]
}

/** Why an expression resolved to nothing, in the drafter's words, or '' when it resolved. */
export function pointExpressionRefusal(raw, anchor = null) {
  if (!isPointExpression(raw)) return ''
  const p = parsePointExpression(raw)
  if (!p) return `"${String(raw).trim()}" is not a point: use x,y, @dx,dy, dist<angle or @dist<angle.`
  if (p.relative && resolvePointExpression(raw, anchor) === null) return '"@" needs a previous point to measure from.'
  return resolvePointExpression(raw, anchor) === null ? `"${String(raw).trim()}" does not resolve to a finite point.` : ''
}
