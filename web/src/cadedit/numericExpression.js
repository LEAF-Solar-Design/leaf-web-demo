// CAD scalar fields: a leading sign is absolute; only '@' measures from current.
const MAX_EXPRESSION_CHARS = 64
// Integer micrometres keep exact ratios such as ft/in free of conversion noise.
const UNIT_SCALE = Object.freeze({ mm: 1000, cm: 10000, m: 1000000, in: 25400, ft: 304800 })
const UNIT_MARKS = Object.freeze({ "'": 'ft', '"': 'in', '′': 'ft', '″': 'in' })

const isDigit = (char) => char >= '0' && char <= '9'
const isLetter = (char) => (char >= 'a' && char <= 'z') || (char >= 'A' && char <= 'Z')
const has = (record, key) => Object.prototype.hasOwnProperty.call(record, key)
const refuse = (reason) => ({ ok: false, reason })

function normalizeUnit(unit) {
  if (typeof unit !== 'string') return null
  const name = unit.trim().toLowerCase()
  if (has(UNIT_MARKS, name)) return UNIT_MARKS[name]
  return has(UNIT_SCALE, name) ? name : null
}

// Consume every character, including suffixes: no permissive numeric-prefix parse.
function tokenize(text) {
  const tokens = []
  let i = 0
  while (i < text.length) {
    const char = text[i]
    if (/\s/.test(char)) { i += 1; continue }
    if ('@+-*/'.includes(char)) {
      tokens.push({ kind: 'operator', value: char })
      i += 1
    } else if (isDigit(char) || char === '.') {
      const start = i
      let digits = 0
      while (i < text.length && isDigit(text[i])) { i += 1; digits += 1 }
      if (text[i] === '.') {
        i += 1
        while (i < text.length && isDigit(text[i])) { i += 1; digits += 1 }
      }
      if (!digits) return null
      tokens.push({ kind: 'number', value: Number(text.slice(start, i)) })
    } else if (isLetter(char)) {
      const start = i
      while (i < text.length && isLetter(text[i])) i += 1
      tokens.push({ kind: 'unit', value: text.slice(start, i) })
    } else if (has(UNIT_MARKS, char)) {
      tokens.push({ kind: 'unit', value: char })
      i += 1
    } else return null
  }
  return tokens
}

/** Detect the relative marker, including malformed expressions needing a refusal. */
export function isRelativeDimension(raw) {
  return typeof raw === 'string' && raw.length <= MAX_EXPRESSION_CHARS && raw.trim().startsWith('@')
}

/**
 * Parse a signed decimal with an optional unit, or @+n, @-n, @*n, @/n.
 * Relative operands are unsigned decimals; + and - may carry a distance unit,
 * while * and / take unitless factors. Canonical is String(value) in field units,
 * with negative zero normalized to zero and no display rounding.
 */
export function parseDimension(raw, { current, unit } = {}) {
  if (typeof raw !== 'string') return refuse('Enter a dimension as text.')
  if (raw.length > MAX_EXPRESSION_CHARS) return refuse('A dimension must be at most 64 characters.')
  const fieldUnit = normalizeUnit(unit)
  if (!fieldUnit) return refuse('The drawing unit is unknown; use mm, cm, m, in or ft.')
  const tokens = tokenize(raw)
  if (!tokens || !tokens.length) return refuse('Use a decimal dimension or @ followed by +, -, * or / and a decimal.')

  let i = 0
  const relative = tokens[i].value === '@'
  let operator = '+'
  if (relative) {
    i += 1
    if (tokens[i]?.kind !== 'operator' || !'+-*/'.includes(tokens[i].value)) {
      return refuse('A relative dimension needs @+, @-, @* or @/ followed by a decimal.')
    }
    operator = tokens[i++].value
  } else if (tokens[i].kind === 'operator' && '+-'.includes(tokens[i].value)) {
    operator = tokens[i++].value
  }
  if (tokens[i]?.kind !== 'number') return refuse('Enter a decimal after the sign or relative operator.')
  let operand = tokens[i++].value
  if (tokens[i]?.kind === 'unit') {
    const inputUnit = normalizeUnit(tokens[i++].value)
    if (!inputUnit) return refuse('The dimension unit is unknown; use mm, cm, m, in or ft.')
    if (relative && (operator === '*' || operator === '/')) {
      return refuse('Multiplication and division need a unitless decimal factor.')
    }
    operand *= UNIT_SCALE[inputUnit] / UNIT_SCALE[fieldUnit]
  }
  if (i !== tokens.length) return refuse('Use one decimal and an optional unit, with one relative operator after @.')
  if (!Number.isFinite(operand)) return refuse('The dimension must resolve to a finite number.')
  if (relative && !Number.isFinite(current)) return refuse('A relative dimension needs a finite current value.')
  if (relative && operator === '/' && operand === 0) return refuse('A dimension cannot be divided by zero.')

  let value
  if (!relative) value = operator === '-' ? -operand : operand
  else if (operator === '+') value = current + operand
  else if (operator === '-') value = current - operand
  else if (operator === '*') value = current * operand
  else value = current / operand
  if (!Number.isFinite(value)) return refuse('The dimension must resolve to a finite number.')
  if (Object.is(value, -0)) value = 0
  return { ok: true, value, canonical: String(value) }
}
