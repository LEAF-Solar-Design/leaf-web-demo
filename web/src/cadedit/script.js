// W4g-7a SCRIPT: the reference's .scr, a file of command words with their
// operands, run line by line through the SAME parser the command line uses
// (lib/commandWords.js) and the SAME prompt grammar the ribbon shows
// (PROMPTS), so a script can do exactly what a drafter can type, no more.
//
// Grammar (the AutoCAD .scr convention, vocabulary only): one command per
// line, the word first, then one token per prompt operand in the prompt's
// own order; a point operand is one token in the point grammar ("x,y",
// "@dx,dy", "dist<angle"); a text operand may be double-quoted to carry
// spaces; a yes/no operand is yes|no|true|false|1|0; an operand left off
// keeps the prompt's default. Blank lines and lines starting with ";" are
// skipped. The first line that cannot be read stops the parse with its
// number, before anything runs.
//
// Fail-closed and bounded: MAX_SCRIPT_CHARS and MAX_SCRIPT_LINES are checked
// before any line is read; a token is at most MAX_TOKEN_CHARS; an unknown
// word, an operand that is not a point where a point is asked, an operand
// past the prompt's last slot, and an op the script cannot drive (a vertex
// edit needs the pane's index) are refusals naming the line.

export const MAX_SCRIPT_CHARS = 256 * 1024
export const MAX_SCRIPT_LINES = 5000
export const MAX_TOKEN_CHARS = 4096

/** The words a script may use beyond the prompted ones: they take no operand. */
export const BARE_OPS = Object.freeze(new Set(['delete', 'explode', 'copyClip', 'cutClip', 'undo', 'redo']))

const YES = new Set(['yes', 'y', 'true', '1', 'on'])
const NO = new Set(['no', 'n', 'false', '0', 'off'])
const POINT_RE = /^(@?-?[0-9.]+(,-?[0-9.]+|<-?[0-9.]+))$/

/** Split one line into tokens: whitespace-separated, a double-quoted run is one token. */
export function tokenize(line) {
  const out = []
  let i = 0
  const n = line.length
  while (i < n) {
    const c = line[i]
    if (c === ' ' || c === '\t') { i += 1; continue }
    if (c === '"') {
      const end = line.indexOf('"', i + 1)
      if (end < 0) return { refusal: 'an opening quote has no closing quote' }
      out.push(line.slice(i + 1, end))
      i = end + 1
      continue
    }
    let j = i
    while (j < n && line[j] !== ' ' && line[j] !== '\t') j += 1
    out.push(line.slice(i, j))
    i = j
  }
  for (const t of out) if (t.length > MAX_TOKEN_CHARS) return { refusal: `an operand is longer than ${MAX_TOKEN_CHARS} characters` }
  return { tokens: out }
}

// The coordinate pairs a prompt asks for, by field name: a point slot is an
// adjacent x-name then y-name pair of decimal fields (x,y / x2,y2 / x1,y1 /
// cx,cy / dx,dy / ex,ey), so ARRAY's row and column SPACING stay two
// numbers even though they are two decimal fields side by side.
const POINT_X = new Set(['x', 'x1', 'x2', 'cx', 'dx', 'ex'])
const POINT_Y = new Set(['y', 'y1', 'y2', 'cy', 'dy', 'ey'])

/**
 * The operand slots of a prompt, in order: a coordinate pair is ONE slot
 * written into its first field as a point expression; every other field is
 * one slot. `{ keys, kind }` per slot, kind 'point' | 'checkbox' | 'text' |
 * 'edge' | 'number'.
 */
export function promptSlots(prompt) {
  const slots = []
  for (const step of prompt.steps) {
    const fields = step.fields
    let k = 0
    while (k < fields.length) {
      const [key, , mode = 'decimal'] = fields[k]
      const next = fields[k + 1]
      const nextMode = next ? next[2] ?? 'decimal' : null
      if (mode === 'decimal' && nextMode === 'decimal' && POINT_X.has(key) && POINT_Y.has(next[0])) {
        slots.push({ keys: [key, next[0]], kind: 'point' })
        k += 2
        continue
      }
      slots.push({ keys: [key], kind: mode === 'checkbox' ? 'checkbox' : mode === 'text' ? 'text' : mode === 'edge' ? 'edge' : 'number' })
      k += 1
    }
  }
  return slots
}

/**
 * Parse a whole script. `parseWord(text)` is lib/commandWords' parser and
 * `prompts` the ribbon's PROMPTS table, passed in so this module stays pure.
 * Returns `{ lines: [{ line, word, group, op, verb, inputs }] }` or
 * `{ refusal, line }` naming the FIRST line that cannot be read.
 */
export function parseScript(text, parseWord, prompts) {
  if (typeof text !== 'string') return { refusal: 'the script is not text', line: 0 }
  if (text.length > MAX_SCRIPT_CHARS) return { refusal: `the script is longer than ${MAX_SCRIPT_CHARS} characters`, line: 0 }
  const rows = text.split(/\r\n|\r|\n/)
  if (rows.length > MAX_SCRIPT_LINES) return { refusal: `the script has more than ${MAX_SCRIPT_LINES} lines`, line: 0 }
  const lines = []
  for (let i = 0; i < rows.length; i += 1) {
    const number = i + 1
    const raw = rows[i].trim()
    if (!raw || raw.startsWith(';')) continue
    const tok = tokenize(raw)
    if (tok.refusal) return { refusal: `line ${number}: ${tok.refusal}`, line: number }
    const [head, ...operands] = tok.tokens
    const command = parseWord(head)
    if (!command) return { refusal: `line ${number}: "${head}" is not a command word`, line: number }
    const prompt = prompts[command.op] || null
    const inputs = {}
    if (!prompt) {
      if (!BARE_OPS.has(command.op)) return { refusal: `line ${number}: ${command.verb} cannot be driven from a script`, line: number }
      if (operands.length) return { refusal: `line ${number}: ${command.verb} takes no operand`, line: number }
      lines.push(Object.freeze({ line: number, word: head, group: command.group, op: command.op, verb: command.verb, inputs }))
      continue
    }
    const slots = promptSlots(prompt)
    if (operands.length > slots.length) return { refusal: `line ${number}: ${command.verb} takes at most ${slots.length} operand${slots.length === 1 ? '' : 's'}`, line: number }
    for (let s = 0; s < operands.length; s += 1) {
      const slot = slots[s]
      const value = operands[s]
      if (slot.kind === 'point') {
        if (!POINT_RE.test(value)) return { refusal: `line ${number}: ${command.verb} operand ${s + 1} must be a point (x,y or @dx,dy or dist<angle), got "${value}"`, line: number }
        inputs[slot.keys[0]] = value
        // The second field is emptied so the resolver's value wins, never a
        // stale default beside a fresh expression.
        inputs[slot.keys[1]] = ''
      } else if (slot.kind === 'checkbox') {
        const low = value.toLowerCase()
        if (YES.has(low)) inputs[slot.keys[0]] = 'true'
        else if (NO.has(low)) inputs[slot.keys[0]] = 'false'
        else return { refusal: `line ${number}: ${command.verb} operand ${s + 1} must be yes or no, got "${value}"`, line: number }
      } else {
        inputs[slot.keys[0]] = value
      }
    }
    lines.push(Object.freeze({ line: number, word: head, group: command.group, op: command.op, verb: command.verb, inputs }))
  }
  return { lines }
}
