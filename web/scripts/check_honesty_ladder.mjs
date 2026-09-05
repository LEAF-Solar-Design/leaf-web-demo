// The HONESTY LADDER gate (standardization slice 13d).
//
// One rule, checked four ways: a control the product takes away must SAY WHY,
// and a slot the contract declares absent must say why in the doc. The rule is
// already written down — web/src/lib/ribbonClusters.js's file header calls it
// the HONESTY CONTRACT ("A greyed control with no reason is the gap ToolsPanel's
// lock-note closed, and the ribbon must never reopen it") — but until this file
// nothing enforced it. A reason map with a missing key renders an EMPTY reason,
// which is exactly the greyed-with-no-explanation control the contract forbids,
// and `undefined` is silent: no test, no build error, no console warning.
//
// The four checks:
//   1. REASON MAPS. Every `const *REASONS` under web/src (exported or not) is
//      Object.freeze'd and every value is a non-empty sentence (>= 12 chars, no
//      TODO/tbd/???). Every `SOMETHING_REASONS.key` reference anywhere under
//      web/src resolves to a key that map actually defines.
//   2. DISABLED RECORDS. Every object literal in ribbonClusters.js and
//      EngineRibbonClusters.jsx that declares `disabled:` also declares a
//      `reason` that holds up (`disabled: false` is exempt: a control that is
//      never taken away owes no explanation). A `reason` isn't just a key
//      that must exist — its value is judged by what it IS: a plain string
//      runs the same prose rule check 1 runs on a map entry (so `reason: ''`
//      or `reason: 'TODO'` fails exactly as a map entry would), a
//      `SOME_REASONS.key` reference is resolved the way check 1 resolves one
//      (a dangling key fails), and anything else — a template literal, a
//      function call, a ternary — is an expression this scanner declines to
//      evaluate, so it is counted under "unverifiable reason expressions",
//      never judged silently pass/fail, against a pinned BUDGET (today: 11)
//      that ratchets down only: a new one anywhere is a hard failure, and
//      fixing one must lower the pin in the same change.
//   3. ABSENT CONTRACT SLOTS. Every slot in productSurfaces.js's PRODUCT_SURFACES
//      contract that is absent — null, undefined, false, 'none', or an empty
//      array, every spelling a surface has used to mean "nothing renders
//      here" — on some surface has a ROW in docs/convergence/SURFACE-CONTRACT.md's
//      field table (anchored on the row's first cell, the backticked slot name,
//      and matching the header row's own column count — a row short or long a
//      cell cannot be trusted to hold its reason at the header-named index),
//      and that row's reason-column cell holds a real reason: non-empty, not a
//      bare `-` / `n/a` / `TBD` placeholder, at least as long as a map-entry
//      sentence (check 1's floor). A slot naming itself in the table with
//      nothing said about why, or a row missing the very column that would
//      say why, is exactly as silent as no row at all, and both used to pass.
//   4. POSITIVE CONTROL. Fixture sources with a disabled-and-reasonless record,
//      an unfrozen map, a placeholder reason and a dangling key each FAIL the
//      same functions checks 1-3 run. A gate nobody has watched go red is a
//      gate nobody knows works.
//
// STATIC ONLY, and honest about what that costs. Nothing here renders, and
// nothing here imports the ribbon modules (they pull in React and JSX). The
// parse is a light hand-written scanner over the source: comments and string
// bodies are blanked (`maskSource`), then braces are matched to find object
// literals and their depth-0 keys. Known limits, stated so nobody mistakes a
// pass for a proof:
//   - Regex literals are NOT masked. Detecting one needs the JS grammar, and
//     the `/` heuristic misfires on every self-closing JSX tag. A regex literal
//     holding an unbalanced quote or brace would confuse the scanner, so every
//     file this gate parses deeply must brace-balance after masking or it is
//     reported as unparseable (a violation, not a silent skip).
//   - Reason maps are matched by DECLARED NAME across the whole tree, not by
//     import graph. Two different files declaring the same map name would
//     collide, so the gate refuses that outright (check 1e).
//   - A REASON MAP value must be a plain quoted string; a computed or
//     templated value there is a hard violation, because a sentence a human
//     can read in the diff is the whole point. An INLINE `reason:` on a
//     disabled record is judged more leniently by design: a string or a
//     resolvable REASONS.key is verified the same way, but anything else
//     (a template literal included — interpolation makes "readable in the
//     diff" unprovable here) is only counted as unverifiable, never failed,
//     because this scanner cannot prove a computed inline reason is bad any
//     more than it can prove one is good. An "unverifiable" count is not a
//     free pass forever: it is pinned to a BUDGET (today: 11) that only ever
//     ratchets down, so a new one anywhere fails the gate and a fixed one
//     must lower the pin in the same change — see `UNVERIFIABLE_REASON_BUDGET`.
//   - Check 2 covers records written as object literals. A record assembled
//     field by field, or spread in from elsewhere, is invisible to it — the
//     scanner never sees the record come together, so it has nothing to
//     brace-match against.
//   - A field-table row's reason cell runs the SAME length floor and
//     placeholder check a REASONS map entry runs (check 1's prose rule), but
//     NOT its "opens with a letter" clause: this doc's own convention opens a
//     cell with a backticked file:line reference before the prose, and
//     rejecting that would fail the real table, not just a bad one. Which
//     cell IS the reason cell is never assumed by POSITION (`row[row.length -
//     1]` reads whichever cell happens to be last, including one some OTHER
//     column's text shifted into after a column got deleted): it is read
//     from the header row's own column count and the index of the column
//     whose heading names it (`fieldTableShape`), and a row whose cell count
//     does not match the header — too short, too long, or reduced to only
//     the slot's own name — is a violation before any cell is read by
//     position at all.
import assert from 'node:assert/strict'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join, relative, sep } from 'node:path'
import { describe, it } from 'node:test'

const SELF_PATH = fileURLToPath(import.meta.url)
const here = dirname(SELF_PATH)
const WEB = join(here, '..')
const REPO = join(WEB, '..')
const SRC = join(WEB, 'src')
const SURFACE_CONTRACT_DOC = join(REPO, 'docs', 'convergence', 'SURFACE-CONTRACT.md')

// The files that build ribbon/tool records. Check 2 parses these deeply; a
// new builder is added here. actionRegistry.js (slice 10a) is the third: its
// ribbonTool() assigns `disabled` and `reason` field by field today, which
// check 2 cannot see (the header's limits), so it contributes no literal
// record yet; a `disabled:` literal written there later is judged like the
// other two, and its four *REASONS maps are read by check 1 regardless.
const DISABLED_RECORD_FILES = [
  join(SRC, 'lib', 'ribbonClusters.js'),
  join(SRC, 'cadedit', 'EngineRibbonClusters.jsx'),
  join(SRC, 'lib', 'actionRegistry.js'),
]

// A reason is a sentence, not a token. 12 characters is roughly "three short
// words"; below that a value is a label, and a label does not tell a user what
// to do next.
const MIN_REASON_CHARS = 12
const PLACEHOLDER = /(\?\?\?|\b(?:TODO|TBD|FIXME|XXX|WIP|N\/A|LOREM)\b)/i

// NO EXEMPTIONS, and none is available. This gate has no allowlist and no
// escape hatch on purpose: a reason too short to be a sentence gets fixed in
// the file that owns the copy, never waived here. An earlier draft of this
// slice carved out 'engine busy' (11 chars) on the stated premise that the
// literal was pinned byte for byte by a test. No test pinned it, because every
// assertion compares against the constant (MODIFY_REASONS.busy and friends),
// so the copy was lengthened in its own file and the mechanism deleted with it.

// --- source scanning -------------------------------------------------------

/**
 * Blank the BODY of every comment and every quoted/backticked string, leaving
 * quotes, newlines and every other character in place at its original index.
 * Line numbers and offsets stay valid, so a violation can be reported at the
 * line it lives on. See the header for what this deliberately does not do.
 */
export function maskSource(src) {
  const out = src.split('')
  const n = src.length
  let i = 0
  const blank = (at) => { if (at < n && src[at] !== '\n') out[at] = ' ' }
  while (i < n) {
    const c = src[i]
    const d = i + 1 < n ? src[i + 1] : ''
    if (c === '/' && d === '/') {
      while (i < n && src[i] !== '\n') { out[i] = ' '; i += 1 }
      continue
    }
    if (c === '/' && d === '*') {
      out[i] = ' '; out[i + 1] = ' '; i += 2
      while (i < n && !(src[i] === '*' && src[i + 1] === '/')) { blank(i); i += 1 }
      if (i < n) { out[i] = ' '; out[i + 1] = ' '; i += 2 }
      continue
    }
    if (c === '"' || c === "'" || c === '`') {
      i += 1
      while (i < n) {
        if (src[i] === '\\') { blank(i); blank(i + 1); i += 2; continue }
        if (src[i] === c) break
        blank(i)
        i += 1
      }
      i += 1
      continue
    }
    i += 1
  }
  return out.join('')
}

/** 1-based line number of a character offset. */
export function lineOf(src, index) {
  let line = 1
  for (let i = 0; i < index && i < src.length; i += 1) if (src[i] === '\n') line += 1
  return line
}

/**
 * Net brace depth of a masked source, and whether it ever went negative.
 * Fail-closed guard: a file that does not balance was mis-masked, and every
 * conclusion drawn from it is worthless.
 */
export function braceBalance(masked) {
  let depth = 0
  let wentNegative = false
  for (const ch of masked) {
    if (ch === '{') depth += 1
    else if (ch === '}') { depth -= 1; if (depth < 0) wentNegative = true }
  }
  return { depth, wentNegative, balanced: depth === 0 && !wentNegative }
}

const IDENT_START = /[A-Za-z_$]/
const IDENT_PART = /[A-Za-z0-9_$]/
const OPEN = { '{': '}', '[': ']', '(': ')' }
const CLOSE = new Set(['}', ']', ')'])

/**
 * The depth-0 entries of an object-literal BODY (the text between its braces).
 * Returns `[{ key, valueStart, valueEnd }]`; a shorthand entry (`reason,`) has
 * a null value range. Spreads are skipped, not reported as keys.
 */
export function objectEntries(masked, bodyStart, bodyEnd) {
  const entries = []
  let i = bodyStart
  const skipGroup = () => {
    const stack = [OPEN[masked[i]]]
    i += 1
    while (i < bodyEnd && stack.length) {
      const ch = masked[i]
      if (OPEN[ch]) stack.push(OPEN[ch])
      else if (CLOSE.has(ch)) { if (ch === stack[stack.length - 1]) stack.pop(); else return }
      i += 1
    }
  }
  while (i < bodyEnd) {
    const ch = masked[i]
    if (/\s/.test(ch) || ch === ',' || ch === ';') { i += 1; continue }
    if (ch === '.' && masked.slice(i, i + 3) === '...') { // spread: skip its value
      i += 3
      while (i < bodyEnd) {
        const c2 = masked[i]
        if (c2 === ',') break
        if (OPEN[c2]) { skipGroup(); continue }
        i += 1
      }
      continue
    }
    if (OPEN[ch]) { skipGroup(); continue }
    if (!IDENT_START.test(ch) && ch !== '"' && ch !== "'") { i += 1; continue }
    // a key: bare identifier or quoted
    const keyStart = i
    let key = null
    if (ch === '"' || ch === "'") {
      i += 1
      while (i < bodyEnd && masked[i] !== ch) i += 1
      key = masked.slice(keyStart + 1, i)
      i += 1
    } else {
      while (i < bodyEnd && IDENT_PART.test(masked[i])) i += 1
      key = masked.slice(keyStart, i)
    }
    while (i < bodyEnd && /\s/.test(masked[i])) i += 1
    if (masked[i] === ':') {
      i += 1
      while (i < bodyEnd && /\s/.test(masked[i])) i += 1
      const valueStart = i
      while (i < bodyEnd) {
        const c2 = masked[i]
        if (c2 === ',') break
        if (OPEN[c2]) { skipGroup(); continue }
        i += 1
      }
      entries.push({ key, keyStart, valueStart, valueEnd: i })
    } else if (masked[i] === ',' || i >= bodyEnd || masked[i] === '}') {
      entries.push({ key, keyStart, valueStart: null, valueEnd: null }) // shorthand
    } else {
      // a method, a call, `key as T`: not an entry this gate reasons about.
      while (i < bodyEnd) {
        const c2 = masked[i]
        if (c2 === ',') break
        if (OPEN[c2]) { skipGroup(); continue }
        i += 1
      }
    }
  }
  return entries
}

/** Offset of the `}` matching the `{` at `open`, or -1. */
export function matchBrace(masked, open) {
  let depth = 0
  for (let i = open; i < masked.length; i += 1) {
    if (masked[i] === '{') depth += 1
    else if (masked[i] === '}') { depth -= 1; if (depth === 0) return i }
  }
  return -1
}

// --- check 1: reason maps --------------------------------------------------

// `REASONS` is itself a valid map name, so the capture cannot demand a prefix
// before the word; the suffix test happens on the captured name instead. The
// `export` keyword is optional: a `const FOO_REASONS = ...` a component keeps
// module-private is just as capable of rendering an empty reason as an
// exported one, and an unexported map used to be invisible to this gate.
const MAP_DECL = /(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=\s*/g

/**
 * Every exported `*REASONS` map in one source: its name, whether it is frozen,
 * and its key -> { value, line } entries. `value` is null when the entry is
 * not a plain quoted string (which is itself a violation).
 */
export function findReasonMaps(src) {
  const masked = maskSource(src)
  const maps = []
  MAP_DECL.lastIndex = 0
  let m
  while ((m = MAP_DECL.exec(masked)) !== null) {
    const name = m[1]
    if (!name.endsWith('REASONS')) continue
    const after = masked.slice(m.index + m[0].length)
    const frozen = /^Object\.freeze\s*\(\s*\{/.test(after)
    const brace = masked.indexOf('{', m.index + m[0].length)
    if (brace === -1) { maps.push({ name, frozen, line: lineOf(src, m.index), entries: [], parsed: false }); continue }
    const close = matchBrace(masked, brace)
    if (close === -1) { maps.push({ name, frozen, line: lineOf(src, m.index), entries: [], parsed: false }); continue }
    const entries = objectEntries(masked, brace + 1, close).map((e) => {
      let value = null
      if (e.valueStart != null) {
        const raw = src.slice(e.valueStart, e.valueEnd).trim()
        const q = raw[0]
        if ((q === '"' || q === "'") && raw.length >= 2 && raw[raw.length - 1] === q) {
          value = raw.slice(1, -1).replace(/\\(.)/g, '$1')
        }
      }
      return { key: e.key, value, line: lineOf(src, e.keyStart) }
    })
    maps.push({ name, frozen, line: lineOf(src, m.index), entries, parsed: true })
  }
  return maps
}

const REF = /\b([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)/g

/** Every `SOME_REASONS.key` member read in one source. */
export function findReasonReferences(src) {
  const masked = maskSource(src)
  const refs = []
  REF.lastIndex = 0
  let m
  while ((m = REF.exec(masked)) !== null) {
    if (!m[1].endsWith('REASONS')) continue
    refs.push({ map: m[1], key: m[2], line: lineOf(src, m.index) })
  }
  return refs
}

/** Why this reason value is not a sentence, or null when it is one. */
export function reasonProseViolation(value) {
  if (value === null) return 'not a plain quoted string (a reason must be readable in the diff)'
  const v = value.trim()
  if (v.length === 0) return 'empty'
  if (PLACEHOLDER.test(v)) return `placeholder text (${JSON.stringify(v)})`
  if (v.length < MIN_REASON_CHARS) return `${v.length} chars, under the ${MIN_REASON_CHARS}-char floor (${JSON.stringify(v)})`
  if (!/^[A-Za-z]/.test(v)) return `does not open with a letter (${JSON.stringify(v)})`
  return null
}

// --- check 2: disabled records --------------------------------------------

/**
 * Every object literal in one source that declares a `disabled` key, with the
 * depth-0 keys it declares beside it, plus whatever text the record wrote for
 * `reason` (`reasonRaw`, `null` when the key is absent or has no readable
 * value — e.g. shorthand `reason,`). `disabledIsFalse` marks the exempt case.
 */
export function findDisabledRecords(src) {
  const masked = maskSource(src)
  const found = []
  for (let i = 0; i < masked.length; i += 1) {
    if (masked[i] !== '{') continue
    const close = matchBrace(masked, i)
    if (close === -1) continue
    const entries = objectEntries(masked, i + 1, close)
    const disabled = entries.find((e) => e.key === 'disabled')
    if (!disabled) continue
    const value = disabled.valueStart == null ? '' : src.slice(disabled.valueStart, disabled.valueEnd).trim()
    const reasonEntry = entries.find((e) => e.key === 'reason')
    // Shorthand (`reason,`, no colon) has no value range to slice — its value
    // IS the identifier `reason` read from scope, so that identifier is its
    // own raw text: not a string, not a `MAP.key`, so it classifies as an
    // unverifiable expression rather than a false "no readable value".
    const reasonRaw = reasonEntry
      ? (reasonEntry.valueStart != null
          ? src.slice(reasonEntry.valueStart, reasonEntry.valueEnd).trim()
          : reasonEntry.key)
      : null
    found.push({
      line: lineOf(src, disabled.keyStart),
      keys: entries.map((e) => e.key),
      disabledValue: value,
      disabledIsFalse: value === 'false',
      hasReason: entries.some((e) => e.key === 'reason'),
      reasonLine: reasonEntry ? lineOf(src, reasonEntry.keyStart) : null,
      reasonRaw,
    })
  }
  return found
}

/**
 * What kind of value a `reason:` (or a REASONS map entry) holds, textually.
 * `'string'` carries the unescaped literal, `'ref'` a `SOME_REASONS.key`
 * member read, everything else is `'other'` — an expression this scanner
 * declines to evaluate (a template literal included: it could hold real
 * prose, but interpolation makes "readable in the diff" unprovable here).
 */
export function classifyReasonValue(raw) {
  if (raw == null) return { kind: 'missing' }
  const v = raw.trim()
  if (v.length === 0) return { kind: 'missing' }
  const q = v[0]
  if ((q === '"' || q === "'") && v.length >= 2 && v[v.length - 1] === q) {
    return { kind: 'string', value: v.slice(1, -1).replace(/\\(.)/g, '$1') }
  }
  // A template literal with NO interpolation is a plain string in a different
  // coat (an empty `` would render an empty reason), so it is judged as one.
  // Only an interpolated template stays 'other' (its prose is not readable here).
  if (q === '`' && v.length >= 2 && v[v.length - 1] === '`' && !v.includes('${')) {
    return { kind: 'string', value: v.slice(1, -1).replace(/\\(.)/g, '$1') }
  }
  const refMatch = /^([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)$/.exec(v)
  if (refMatch && refMatch[1].endsWith('REASONS')) {
    return { kind: 'ref', map: refMatch[1], key: refMatch[2] }
  }
  return { kind: 'other', raw: v }
}

/**
 * Why a disabled record's `reason` fails the honesty ladder, or `null` when
 * it holds up. A plain string runs the same prose rule as a map value; a
 * `REASONS.key` reference is resolved against `mapsByName` exactly as check 1
 * resolves one (an unresolvable map name is the same alias/shadow leniency
 * documented in the header, not a violation). An `'other'` expression is
 * deliberately NOT judged here — see `unverifiableReasonExpr`.
 */
export function disabledReasonViolation(rec, mapsByName = new Map()) {
  if (rec.disabledIsFalse) return null
  if (!rec.hasReason) return 'declares no `reason`'
  const cls = classifyReasonValue(rec.reasonRaw)
  if (cls.kind === 'missing') return 'declares `reason` with no readable value'
  if (cls.kind === 'string') {
    const why = reasonProseViolation(cls.value)
    return why ? `declares \`reason\` that is ${why}` : null
  }
  if (cls.kind === 'ref') {
    const map = mapsByName.get(cls.map)
    if (!map) return null // alias or a locally shadowed name; see the header's limits
    if (!map.entries.some((e) => e.key === cls.key)) {
      return `declares \`reason: ${cls.map}.${cls.key}\`, which is not a key of ${cls.map} — it reads as undefined`
    }
    return null
  }
  return null // 'other': unverifiable, counted separately, never silently passed
}

/**
 * The raw text of a disabled record's `reason` when it is an expression this
 * gate cannot verify (not a string literal, not a resolvable REASONS.key) —
 * the "count it, don't pass it silently" half of the fix. `null` when the
 * record is exempt, reasonless, or already judged by `disabledReasonViolation`.
 */
export function unverifiableReasonExpr(rec) {
  if (rec.disabledIsFalse || !rec.hasReason) return null
  const cls = classifyReasonValue(rec.reasonRaw)
  return cls.kind === 'other' ? cls.raw : null
}

/** Records that take a control away without a reason that holds up. */
export function reasonlessDisabled(src, mapsByName = new Map()) {
  return findDisabledRecords(src)
    .filter((r) => disabledReasonViolation(r, mapsByName) !== null)
}

// A budget, not a blanket allowance. "Unverifiable" (check 2's third bucket:
// not a plain string, not a resolvable REASONS.key — an identifier read from
// scope, a function call) used to be only WARNED and COUNTED, never asserted:
// the count could climb without limit and the gate would stay green forever,
// which is the same silent gap check 1-3 exist to close, just moved one
// bucket over. Pinned to the exact count this gate's own output reported on
// 2026-09-04 (Todo: 13d, round 2, rebased onto main): `6 reason maps ·
// 31 sentences · 128 key references · 12 disabled records ·
// 26 absent contract slots · 11 unverifiable reason expressions`. The pin
// only ever RATCHETS: a new unverifiable reason anywhere pushes the count
// over budget and fails the gate outright; giving an existing one a real
// string or REASONS.key lowers the live count below budget, which ALSO
// fails — on purpose, so the budget constant itself must drop in the same
// change, or nobody would ever notice the ratchet had room to tighten. (It
// already has, once: rebasing this change onto a main that had meanwhile
// resolved 5 of the original 16 dropped the live count to 11, and this
// pin caught it — see the git history of this line for the receipt.)
//
// Raised 11 -> 12 by W4g-5c, deliberately and with the gate's own sanction.
// The twelfth is the Clipboard cluster in EngineRibbonClusters.jsx, a THIRD
// engine group rendering `action.when(engineCtx)` exactly as the Draw and
// Modify clusters already do (both already inside this budget). The reason
// is not unverified, only statically unreadable from here: it comes from a
// pure ladder function with its own unit rows, and actionRegistry throws at
// runtime on any `when()` result outside KNOWN_REASON_VALUES, which is the
// check this gate cannot perform through a variable. A fourth engine group
// would land here the same way; anything else must earn its own line.
//
// Raised 12 -> 13 by W4g-5d: the Annotation cluster, the fourth engine
// group rendering `action.when(engineCtx)`, exactly as the line above
// said it would. Same verified ladder (drawReason), same runtime throw.
const UNVERIFIABLE_REASON_BUDGET = 13

/**
 * Whether an "unverifiable reason expressions" count holds against its
 * pinned budget, and why not when it doesn't — naming the exact delta and,
 * via `details` (each entry already carries `file:line: ...`), which file(s)
 * hold them. `null` when the count matches the budget exactly, the only
 * shape that passes: this ratchet has no slack in either direction.
 */
export function unverifiableReasonBudgetViolation(actualCount, budget, details = []) {
  if (actualCount === budget) return null
  const delta = actualCount - budget
  const direction = delta > 0 ? 'rose' : 'fell'
  const instruction = delta > 0
    ? `a new unverifiable reason expression was added without a plain-string or REASONS.key reason — give it one, or if it is deliberate, raise UNVERIFIABLE_REASON_BUDGET to ${actualCount} in this same change`
    : `an unverifiable reason expression was fixed — lower UNVERIFIABLE_REASON_BUDGET to ${actualCount} in this same change, or the pin is not tracking a number anyone trusts`
  const where = details.length ? ` Currently: ${details.join('; ')}` : ''
  return `unverifiable reason expressions ${direction} from the pinned budget of ${budget} to ${actualCount} `
    + `(${delta > 0 ? '+' : ''}${delta}): ${instruction}.${where}`
}

// --- check 3: absent contract slots ---------------------------------------

/**
 * Dotted paths in a contract tree whose value is absent: null, undefined,
 * false, 'none', or an empty array. Each of these renders nothing, exactly
 * like null did — a reader hunting for "why isn't X here" must find the same
 * answer regardless of which absent-spelling the surface picked.
 */
export function absentSlots(contract, prefix = '') {
  const out = []
  for (const [key, value] of Object.entries(contract)) {
    const path = prefix ? `${prefix}.${key}` : key
    const isEmptyArray = Array.isArray(value) && value.length === 0
    if (value && typeof value === 'object' && !Array.isArray(value)) out.push(...absentSlots(value, path))
    // Every spelling of "nothing here": null, undefined, false, 'none', '', 0, [].
    else if (value === null || value === undefined || value === false || value === 'none' || value === '' || value === 0 || isEmptyArray) out.push(path)
  }
  return out
}

/** The body of one `## ` section of a markdown doc. */
export function docSection(doc, heading) {
  const start = doc.indexOf(`\n${heading}\n`)
  if (start === -1) return ''
  const rest = doc.slice(start + heading.length + 2)
  const end = rest.search(/\n## /)
  return end === -1 ? rest : rest.slice(0, end)
}

/**
 * One markdown table row's cells: unescaped, trimmed, in order — or `null`
 * when the line is not a table row (does not open with `|`). A backslash-
 * escaped pipe (`\|`, this doc's own convention for a literal `|` inside a
 * union type like `` `'a' \| 'b'` ``) is kept as a literal character, never
 * read as a column boundary. A row that omits its closing pipe still yields
 * its final cell: the scan below only pushes a cell when it HITS a `|`, so a
 * row cut short at the end would otherwise drop whatever text follows the
 * last pipe on the floor — silently reporting one cell fewer than the row
 * actually has, which is exactly the wrong direction for a check that must
 * never UNDER-count a row's columns.
 */
export function splitTableRow(line) {
  const trimmed = line.trim()
  if (!trimmed.startsWith('|')) return null
  const cells = []
  let cur = ''
  for (let i = 1; i < trimmed.length; i += 1) {
    const c = trimmed[i]
    if (c === '\\' && trimmed[i + 1] === '|') { cur += '|'; i += 1; continue }
    if (c === '|') { cells.push(cur.trim()); cur = ''; continue }
    cur += c
  }
  // A well-formed row's closing `|` is its last character, so `cur` is empty
  // here and this adds nothing; only a row missing that closing pipe leaves
  // real text in `cur`, and that text is the row's last cell.
  if (cur.trim().length > 0) cells.push(cur.trim())
  return cells
}

/**
 * The cells of the field table's row for `slot` — ANCHORED: only a row whose
 * FIRST cell is exactly the backticked slot name (e.g. `` `tourAnchors` ``)
 * counts. A mention of the same token inside another row's prose (a cross-
 * reference, say) is not a row for that slot and must not be read as one;
 * `null` when no row's first cell matches.
 */
export function fieldTableRow(fieldTable, slot) {
  const wanted = `\`${slot}\``
  for (const line of fieldTable.split('\n')) {
    const cells = splitTableRow(line)
    if (cells && cells.length > 0 && cells[0] === wanted) return cells
  }
  return null
}

/**
 * The field table's HEADER row: the cells of the first table-shaped line in
 * the section (its `--- | --- | ---` separator, and then every slot row,
 * follow it). `null` when the section holds no table row at all — a section
 * that never turned into a table names nothing to anchor a column index on.
 */
export function fieldTableHeader(fieldTable) {
  for (const line of fieldTable.split('\n')) {
    const cells = splitTableRow(line)
    if (cells) return cells
  }
  return null
}

// The header cell that names the reason column, matched at the START of its
// text (never a substring match anywhere in it) so a real heading — this
// table's own "where it is read (and the literal it replaced)", or a plainer
// "reason" a future table might use — resolves, while a heading that merely
// mentions "reason" partway through some other label does not.
const REASON_HEADER = /^(?:reason\b|where it is read\b)/i

/**
 * The field table's shape, learned ONCE from its own header row: the total
 * column count every slot row must match, and the index of the column whose
 * heading names it as the reason column. `null` when the section has no
 * header row, or the header names no reason-shaped column — FAIL CLOSED:
 * every row check downstream must refuse to judge a row rather than guess
 * which cell holds the reason when the header itself does not say.
 *
 * This is what closes the short-row evasion a row-position read
 * (`row[row.length - 1]`) cannot: a row reduced to only its own slot name, or
 * shortened by deleting its reason column, no longer smuggles some OTHER
 * cell into the reason slot by shifting into the new "last" position — the
 * reason lives at a header-FIXED index, and a row whose cell count does not
 * match the header is rejected before any of its cells are read by position.
 */
export function fieldTableShape(fieldTable) {
  const header = fieldTableHeader(fieldTable)
  if (!header) return null
  const reasonIndex = header.findIndex((h) => REASON_HEADER.test(h.trim()))
  if (reasonIndex === -1) return null
  return { columnCount: header.length, reasonIndex }
}

/**
 * Why a slot's field-table row fails the honesty ladder on SHAPE alone
 * (before its reason cell's prose is ever read), or `null` when the row's
 * cell count matches the header's exactly. `shape` is `fieldTableShape(...)`;
 * a `null` shape (the header itself could not be read) fails every row
 * closed rather than silently passing them.
 */
export function fieldTableRowShapeViolation(row, shape, slot) {
  if (!shape) {
    return 'the field table has no header row naming a reason-shaped column '
      + '(expected a heading starting with "reason" or "where it is read") — '
      + `\`${slot}\`'s row cannot be judged without knowing which cell holds the reason`
  }
  if (row.length !== shape.columnCount) {
    return `row for \`${slot}\` has ${row.length} cells, expected ${shape.columnCount} `
      + "(the header row's column count) — a row missing or gaining a column cannot be "
      + 'trusted to hold its reason at the header-named position'
  }
  return null
}

// A lone dash (or a run of them) is markdown's own "nothing here" filler for
// an empty cell; it must be judged exactly like an empty cell, not like prose
// that happens to be short.
const BLANK_CELL = /^[-–—]+$/

/**
 * Why a field table's reason cell (the row's LAST cell, where every existing
 * row's rationale already lives) fails to hold a real reason, or `null` when
 * it holds up. Same length floor and placeholder list as `reasonProseViolation`
 * — see the header's "Known limits" for the one rule it deliberately skips.
 */
export function fieldTableReasonViolation(cell) {
  const v = (cell ?? '').trim()
  if (v.length === 0 || BLANK_CELL.test(v)) return 'empty (or a bare dash, markdown\'s own "nothing here")'
  if (PLACEHOLDER.test(v)) return `placeholder text (${JSON.stringify(v)})`
  if (v.length < MIN_REASON_CHARS) return `${v.length} chars, under the ${MIN_REASON_CHARS}-char floor (${JSON.stringify(v)})`
  return null
}

// --- file walk -------------------------------------------------------------

const SKIP_DIRS = new Set(['node_modules', 'dist', 'build', '.git', 'coverage'])

export function listSources(root) {
  const out = []
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      if (SKIP_DIRS.has(name)) continue
      const full = join(dir, name)
      if (statSync(full).isDirectory()) walk(full)
      else if (/\.(?:js|jsx|mjs)$/.test(name)) out.push(full)
    }
  }
  walk(root)
  return out
}

const rel = (p) => relative(REPO, p).split(sep).join('/')

// --- the gate --------------------------------------------------------------

const violations = []
const record = (file, line, message) => { violations.push(`${file}:${line}: ${message}`) }

const sources = listSources(SRC).map((path) => ({ path, file: rel(path), src: readFileSync(path, 'utf8') }))

// Only files that mention a REASONS token get parsed; the rest cannot define or
// read one, and parsing 270 files deeply buys nothing.
const reasonFiles = sources.filter((f) => f.src.includes('REASONS'))
const mapsByName = new Map()
const scannedCounts = { maps: 0, values: 0, refs: 0, records: 0, slots: 0, unverifiableReasons: 0 }
const unverifiableReasons = []

for (const f of reasonFiles) {
  const balance = braceBalance(maskSource(f.src))
  if (!balance.balanced) {
    record(f.file, 1, `braces do not balance after comment/string masking (net ${balance.depth}) — the scanner cannot trust this file, so it is reported rather than skipped`)
    continue
  }
  for (const map of findReasonMaps(f.src)) {
    scannedCounts.maps += 1
    if (mapsByName.has(map.name)) {
      record(f.file, map.line, `${map.name} is also exported by ${mapsByName.get(map.name).file} — this gate resolves reason references by name, so duplicate names are refused`)
      continue
    }
    mapsByName.set(map.name, { ...map, file: f.file })
    if (!map.parsed) { record(f.file, map.line, `${map.name}: could not read its object literal`); continue }
    if (!map.frozen) record(f.file, map.line, `${map.name} is not Object.freeze'd — a reason map a consumer can mutate is not a contract`)
    if (map.entries.length === 0) record(f.file, map.line, `${map.name} declares no reasons`)
    for (const entry of map.entries) {
      scannedCounts.values += 1
      const why = reasonProseViolation(entry.value)
      if (!why) continue
      record(f.file, entry.line, `${map.name}.${entry.key}: ${why}`)
    }
  }
}

for (const f of reasonFiles) {
  for (const ref of findReasonReferences(f.src)) {
    scannedCounts.refs += 1
    const map = mapsByName.get(ref.map)
    if (!map) continue // an alias or a locally shadowed name; see the header's limits
    if (!map.entries.some((e) => e.key === ref.key)) {
      record(f.file, ref.line, `${ref.map}.${ref.key} is not a key of ${ref.map} (${map.file}) — it reads as undefined, which renders a disabled control with NO reason`)
    }
  }
}

for (const path of DISABLED_RECORD_FILES) {
  const src = readFileSync(path, 'utf8')
  const file = rel(path)
  const balance = braceBalance(maskSource(src))
  if (!balance.balanced) {
    record(file, 1, `braces do not balance after comment/string masking (net ${balance.depth}) — disabled records cannot be checked here`)
    continue
  }
  const records = findDisabledRecords(src)
  scannedCounts.records += records.length
  for (const r of records) {
    const why = disabledReasonViolation(r, mapsByName)
    if (why) {
      record(file, r.reasonLine ?? r.line, `record sets \`disabled: ${r.disabledValue}\` and ${why} — the honesty contract in ribbonClusters.js forbids a greyed control with no sentence`)
      continue
    }
    const raw = unverifiableReasonExpr(r)
    if (raw) {
      scannedCounts.unverifiableReasons += 1
      unverifiableReasons.push(`${file}:${r.reasonLine}: reason is \`${raw}\` — not a plain string or a resolvable REASONS.key, so this gate cannot verify it and does not silently pass it`)
    }
  }
}

// The budget ratchet (blocker 2, round 2): "unverifiable" stops being a free
// pass the instant the count drifts off its pin, in EITHER direction.
{
  const why = unverifiableReasonBudgetViolation(unverifiableReasons.length, UNVERIFIABLE_REASON_BUDGET, unverifiableReasons)
  if (why) record(rel(SELF_PATH), 1, why)
}

const { PRODUCT_SURFACES } = await import(new URL('../src/site/productSurfaces.js', import.meta.url))
const doc = readFileSync(SURFACE_CONTRACT_DOC, 'utf8')
const fieldTable = docSection(doc, '## Field table')
const docFile = rel(SURFACE_CONTRACT_DOC)
const fieldTableShapeResult = fieldTableShape(fieldTable)
if (!fieldTableShapeResult) {
  record(docFile, 1, 'the field table has no header row naming a reason-shaped column (expected a heading '
    + 'starting with "reason" or "where it is read") — no absent slot\'s row can be judged without knowing '
    + 'which cell holds the reason')
}
const seenSlots = new Set()
for (const surface of PRODUCT_SURFACES) {
  for (const slot of absentSlots(surface.contract)) {
    if (seenSlots.has(slot)) continue
    seenSlots.add(slot)
    scannedCounts.slots += 1
    if (!doc.includes(`\`${slot}\``)) {
      record(docFile, 1, `\`${slot}\` is declared absent (null / undefined / false / 'none' / '' / 0 / []) on surface "${surface.id}" but the doc never names it`)
      continue
    }
    const row = fieldTableRow(fieldTable, slot)
    if (!row) {
      record(docFile, 1, `\`${slot}\` is declared absent on surface "${surface.id}" but has no row in the field table, where the rationale lives`)
      continue
    }
    if (!fieldTableShapeResult) continue // already recorded once, above; do not repeat per slot
    const shapeWhy = fieldTableRowShapeViolation(row, fieldTableShapeResult, slot)
    if (shapeWhy) {
      record(docFile, 1, `\`${slot}\` is declared absent on surface "${surface.id}": ${shapeWhy} — this is exactly how a `
        + 'name-only row or a row with its reason column deleted used to sneak past this gate')
      continue
    }
    const reasonCell = row[fieldTableShapeResult.reasonIndex]
    const why = fieldTableReasonViolation(reasonCell)
    if (why) {
      record(docFile, 1, `\`${slot}\` is declared absent on surface "${surface.id}" and has a field-table row, but its reason cell is ${why} — a slot cannot be absent with nothing said about why`)
    }
  }
}

// --- check 4: positive control --------------------------------------------
//
// Fixtures, not files. Each one is the exact shape the gate exists to catch.
const FIXTURES = {
  reasonlessDisabled: `
    const tool = { id: 'x', label: 'x', disabled: true, onClick: () => {} }
  `,
  reasonedDisabled: `
    const tool = { id: 'x', label: 'x', disabled: true, reason: 'no drawing loaded', onClick: () => {} }
  `,
  disabledFalse: `
    const tool = { id: 'x', label: 'x', disabled: false, onClick: () => {} }
  `,
  // The apostrophe and the brace live inside a comment and a string on purpose:
  // if masking regressed, this fixture would stop parsing.
  maskingTraps: `
    // don't trust a { brace } inside a comment
    const tool = { id: 'y', title: "a } brace and an apostrophe: don't", disabled: true, onClick: () => {} }
  `,
  unfrozenMap: `export const FAKE_REASONS = { locked: 'another session holds the edit lock' }`,
  placeholderReason: `export const FAKE_REASONS = Object.freeze({ locked: 'TODO: write this' })`,
  shortReason: `export const FAKE_REASONS = Object.freeze({ locked: 'nope' })`,
  computedReason: `export const FAKE_REASONS = Object.freeze({ locked: someLabel(x) })`,
  goodMap: `export const FAKE_REASONS = Object.freeze({ locked: 'another session holds the edit lock' })`,
  // Hole 3: a REASONS map the gate must catch even when nobody exports it.
  unexportedBadMap: `const FAKE_REASONS = Object.freeze({ locked: 'nope' })`,
  // Hole 1: an inline `reason:` that has the KEY but not a real sentence.
  emptyInlineReason: `
    const tool = { id: 'x', label: 'x', disabled: true, reason: '', onClick: () => {} }
  `,
  todoInlineReason: `
    const tool = { id: 'x', label: 'x', disabled: true, reason: 'TODO', onClick: () => {} }
  `,
  // Hole 1: an inline `reason: SOME_REASONS.key` must resolve like check 1.
  refInlineReasonBad: `
    const tool = { id: 'x', label: 'x', disabled: true, reason: FAKE_REASONS.missing, onClick: () => {} }
  `,
  refInlineReasonGood: `
    const tool = { id: 'x', label: 'x', disabled: true, reason: FAKE_REASONS.locked, onClick: () => {} }
  `,
  // Hole 1: anything else is unverifiable, not silently passed.
  computedInlineReason: `
    const tool = { id: 'x', label: 'x', disabled: true, reason: describeLock(x), onClick: () => {} }
  `,
  // A live pattern in ribbonClusters.js: `reason` built above and passed by
  // shorthand. It has no value range to read, so it must land in the same
  // unverifiable bucket as a computed reason, never in "no readable value".
  shorthandReason: `
    const reason = hasDrawing ? '' : REASONS.noDrawing
    const tool = { id: 'x', label: 'x', disabled: !hasDrawing, reason, onClick: () => {} }
  `,
}

describe('honesty ladder', () => {
  it('found the reason maps, records and slots it claims to guard', () => {
    assert.ok(scannedCounts.maps >= 4, `expected at least 4 *REASONS maps under web/src, found ${scannedCounts.maps}`)
    assert.ok(scannedCounts.values >= 20, `expected at least 20 reason sentences, found ${scannedCounts.values}`)
    assert.ok(scannedCounts.refs >= 20, `expected at least 20 REASONS.<key> references, found ${scannedCounts.refs}`)
    assert.ok(scannedCounts.records >= 8, `expected at least 8 disabled records in the ribbon builders, found ${scannedCounts.records}`)
    assert.ok(scannedCounts.slots >= 10, `expected at least 10 absent contract slots, found ${scannedCounts.slots}`)
    assert.ok(mapsByName.has('REASONS') && mapsByName.has('MODIFY_REASONS'), 'the two anchor maps were not found — the scan is looking in the wrong place')
  })

  describe('positive control (the gate goes red on purpose)', () => {
    it('flags a disabled record with no reason', () => {
      assert.equal(reasonlessDisabled(FIXTURES.reasonlessDisabled).length, 1)
    })
    it('passes a disabled record that carries one', () => {
      assert.equal(reasonlessDisabled(FIXTURES.reasonedDisabled).length, 0)
    })
    it('exempts `disabled: false`', () => {
      assert.equal(reasonlessDisabled(FIXTURES.disabledFalse).length, 0)
    })
    it('still parses past a brace in a comment and an apostrophe in a string', () => {
      assert.equal(reasonlessDisabled(FIXTURES.maskingTraps).length, 1)
    })
    it('flags an unfrozen reason map', () => {
      assert.equal(findReasonMaps(FIXTURES.unfrozenMap)[0].frozen, false)
      assert.equal(findReasonMaps(FIXTURES.goodMap)[0].frozen, true)
    })
    it('flags placeholder, too-short and computed reasons, and passes a real one', () => {
      const only = (fixture) => findReasonMaps(fixture)[0].entries[0].value
      assert.match(reasonProseViolation(only(FIXTURES.placeholderReason)) || '', /placeholder/)
      assert.match(reasonProseViolation(only(FIXTURES.shortReason)) || '', /char floor/)
      assert.match(reasonProseViolation(only(FIXTURES.computedReason)) || '', /not a plain quoted string/)
      assert.equal(reasonProseViolation(only(FIXTURES.goodMap)), null)
    })
    it('flags a reference to a key the map does not define', () => {
      const map = findReasonMaps(FIXTURES.goodMap)[0]
      const refs = findReasonReferences(`const r = FAKE_REASONS.locked; const bad = FAKE_REASONS.missing`)
      assert.equal(refs.length, 2)
      assert.deepEqual(refs.filter((x) => !map.entries.some((e) => e.key === x.key)).map((x) => x.key), ['missing'])
    })
    it('finds an absent slot and misses a present one', () => {
      assert.deepEqual(
        absentSlots({
          chrome: { productFrame: false, stageBranch: 'cad' },
          versions: 'none',
          dock: null,
          routes: ['one-shot'],
          emptyRoutes: [],
          unset: undefined,
        }),
        ['chrome.productFrame', 'versions', 'dock', 'emptyRoutes', 'unset'])
    })

    // Check 3's own hole (fix-forward, slice 13d, mutation-proven on main
    // twice): the field-table lookup used to test only "does this backticked
    // token appear ANYWHERE in the section", so a row reduced to
    // `| \`slot\` |  |  |  |` (the name present, the reason cell blank) or one
    // whose reason cell was a bare `-` both still passed. No fixture ever
    // drove this half of check 3 red on purpose before now — it was reached
    // only through the real doc, which happened to have real prose in every
    // row's last cell (`a11y` excepted — that row is fixed alongside this).
    //
    // Round 2's re-open (opus refuter): the fix above still read the reason
    // cell by POSITION (`row[row.length - 1]`), so a row that lost a cell
    // entirely — reduced to `| \`slot\` |` (just its own name) or to
    // `| \`slot\` | null | meaning |` (the reason column deleted) — read
    // some OTHER cell, or its own name, as if it were the reason, and both
    // still passed 23/23. The header-anchored shape check below closes that:
    // a row's cell count is checked against the header's BEFORE any cell is
    // read by position, so a short row fails on cell count, not on
    // whatever text happened to land in the wrong slot.
    describe('a field-table row must hold a real reason, not just its own name', () => {
      const miniFieldTable = [
        '| field | type | meaning | where it is read |',
        '| --- | --- | --- | --- |',
        '| `goodSlot` | null | a thing | undeclared: no per-surface support exists anywhere in the client |',
        '| `noReasonSlot` | null | a thing |  |',
        '| `dashSlot` | null | a thing | - |',
        '| `naSlot` | null | a thing | n/a |',
        "| `pipedType` | `'a' \\| 'b'` | a thing | a real sentence describing why this is absent here |",
        '| `otherSlot` | null | a thing | mentions `ghostSlot` in passing, but this row is otherSlot\'s, not ghostSlot\'s |',
        '| `nameOnlySlot` |',
        '| `reasonColumnDeletedSlot` | null | tour anchor ids that used to have a reason column |',
        '| `noClosingPipeSlot` | null | a thing | a real sentence with no closing pipe',
      ].join('\n')
      const shape = fieldTableShape(miniFieldTable)

      it('learns the header\'s column count and reason-column index from its own header row', () => {
        assert.deepEqual(shape, { columnCount: 4, reasonIndex: 3 })
      })

      it('locates a row by its anchored first cell, and only that cell', () => {
        assert.equal(fieldTableRow(miniFieldTable, 'missingSlot'), null)
        assert.deepEqual(
          fieldTableRow(miniFieldTable, 'goodSlot'),
          ['`goodSlot`', 'null', 'a thing', 'undeclared: no per-surface support exists anywhere in the client'])
      })

      // THE OLD BUG's exact shape: `ghostSlot` never owns a row, but its
      // backticked name appears inside another row's prose. The old check
      // (`fieldTable.includes(\`\\\`${slot}\\\`\`)`) would call that a match;
      // the anchored lookup must not.
      it('does not mistake a mention inside another row\'s prose for that slot\'s own row', () => {
        assert.equal(fieldTableRow(miniFieldTable, 'ghostSlot'), null)
      })

      it('keeps a backslash-escaped pipe inside a cell as a literal character, not a column split', () => {
        const row = fieldTableRow(miniFieldTable, 'pipedType')
        assert.equal(row[1], "`'a' | 'b'`")
        assert.equal(row.length, 4)
        assert.equal(fieldTableRowShapeViolation(row, shape, 'pipedType'), null)
      })

      it('passes a real sentence in the reason cell', () => {
        const row = fieldTableRow(miniFieldTable, 'goodSlot')
        assert.equal(fieldTableRowShapeViolation(row, shape, 'goodSlot'), null)
        assert.equal(fieldTableReasonViolation(row[shape.reasonIndex]), null)
      })

      // THE HOLE: a row present with an EMPTY reason cell used to pass,
      // because the old check only asked whether the slot's own backticked
      // name appeared anywhere in the field-table section.
      it('fails a row whose reason cell is empty', () => {
        const row = fieldTableRow(miniFieldTable, 'noReasonSlot')
        assert.equal(fieldTableRowShapeViolation(row, shape, 'noReasonSlot'), null)
        assert.match(fieldTableReasonViolation(row[shape.reasonIndex]) || '', /empty/)
      })

      it('fails a row whose reason cell is a bare dash', () => {
        const row = fieldTableRow(miniFieldTable, 'dashSlot')
        assert.match(fieldTableReasonViolation(row[shape.reasonIndex]) || '', /empty/)
      })

      it('fails a row whose reason cell is a bare "n/a"', () => {
        const row = fieldTableRow(miniFieldTable, 'naSlot')
        assert.match(fieldTableReasonViolation(row[shape.reasonIndex]) || '', /placeholder/)
      })

      // ROUND 2's exact evasion #1: a row reduced to ONLY the slot's own
      // backticked name. `row[row.length - 1]` would read `` `nameOnlySlot` ``
      // itself as the "reason" — 15 characters, opens with a backtick not a
      // placeholder, so the prose check alone waves it through. The shape
      // check must catch it on cell count before the prose check ever runs.
      it('fails a row reduced to only the slot\'s own name (no other cells at all)', () => {
        const row = fieldTableRow(miniFieldTable, 'nameOnlySlot')
        assert.deepEqual(row, ['`nameOnlySlot`'])
        assert.equal(fieldTableReasonViolation(row[row.length - 1]), null,
          'sanity: the slot\'s own backticked name reads as valid prose on its own — the shape check, not the prose check, is what must catch this')
        assert.match(fieldTableRowShapeViolation(row, shape, 'nameOnlySlot') || '', /row for `nameOnlySlot` has 1 cells, expected 4/)
      })

      // ROUND 2's exact evasion #2: the reason COLUMN deleted, leaving three
      // cells whose last one is the MEANING cell, not a reason — and long
      // enough prose to pass check 1's floor regardless.
      it('fails a row whose reason column was deleted, even though its remaining last cell reads like real prose', () => {
        const row = fieldTableRow(miniFieldTable, 'reasonColumnDeletedSlot')
        assert.equal(row.length, 3)
        assert.equal(fieldTableReasonViolation(row[row.length - 1]), null,
          'sanity: the deleted-column row\'s last cell (the meaning text) passes the prose rule on its own — the shape check, not the prose check, is what must catch this')
        assert.match(fieldTableRowShapeViolation(row, shape, 'reasonColumnDeletedSlot') || '', /row for `reasonColumnDeletedSlot` has 3 cells, expected 4/)
      })

      // The trailing-cell fix to `splitTableRow`: a row missing only its
      // closing pipe must still parse its real last cell, not be
      // misdiagnosed as short one column because the parser dropped it.
      it('a row missing only its closing pipe still parses its last cell and passes', () => {
        const row = fieldTableRow(miniFieldTable, 'noClosingPipeSlot')
        assert.deepEqual(row, ['`noClosingPipeSlot`', 'null', 'a thing', 'a real sentence with no closing pipe'])
        assert.equal(fieldTableRowShapeViolation(row, shape, 'noClosingPipeSlot'), null)
        assert.equal(fieldTableReasonViolation(row[shape.reasonIndex]), null)
      })

      // A header with no reason-shaped column at all must fail CLOSED, not
      // guess some column is the reason.
      it('a header naming no reason-shaped column fails closed instead of guessing an index', () => {
        const noReasonHeaderTable = [
          '| field | type | meaning |',
          '| --- | --- | --- |',
          '| `x` | null | nothing to see |',
        ].join('\n')
        assert.equal(fieldTableShape(noReasonHeaderTable), null)
        assert.match(
          fieldTableRowShapeViolation(fieldTableRow(noReasonHeaderTable, 'x'), fieldTableShape(noReasonHeaderTable), 'x') || '',
          /has no header row naming a reason-shaped column/)
      })

      it('a section with no table row at all fails closed the same way', () => {
        assert.equal(fieldTableShape('just some prose, no pipes anywhere in this section'), null)
      })

      // The real table, the real contract: every absent slot on every real
      // surface must resolve to a real row, of the header's own shape, with
      // a real reason at the header-named index. This is the targeted
      // check-3 assertion; the catch-all `violations.length === 0` test
      // below would also fail, but this one names the exact slot and cell
      // count.
      it('the real field table gives every absent slot a correctly-shaped row with a real reason', () => {
        const realShape = fieldTableShape(fieldTable)
        assert.ok(realShape, 'the real field table\'s header row must resolve a reason-shaped column')
        const seen = new Set()
        for (const surface of PRODUCT_SURFACES) {
          for (const slot of absentSlots(surface.contract)) {
            if (seen.has(slot)) continue
            seen.add(slot)
            const row = fieldTableRow(fieldTable, slot)
            assert.ok(row, `\`${slot}\` (absent on surface "${surface.id}") has no row in the field table`)
            const shapeWhy = fieldTableRowShapeViolation(row, realShape, slot)
            assert.equal(shapeWhy, null, shapeWhy)
            const why = fieldTableReasonViolation(row[realShape.reasonIndex])
            assert.equal(why, null, `\`${slot}\`'s field-table reason cell: ${why}`)
          }
        }
      })
    })

    describe('splitTableRow keeps a trailing cell when a row omits its closing pipe', () => {
      it('parses a well-formed row identically with or without checking the closing pipe', () => {
        assert.deepEqual(splitTableRow('| a | b | c |'), ['a', 'b', 'c'])
      })
      it('recovers the final cell of a row missing its closing pipe', () => {
        assert.deepEqual(splitTableRow('| a | b | c'), ['a', 'b', 'c'])
      })
      it('recovers a single-cell row missing its closing pipe', () => {
        assert.deepEqual(splitTableRow('| a'), ['a'])
      })
      it('does not manufacture a phantom trailing cell for a well-formed row ending exactly on its closing pipe', () => {
        assert.deepEqual(splitTableRow('| a | b |'), ['a', 'b'])
      })
    })

    // Hole 1a: check 2 used to test only that `reason` exists as a KEY. An
    // inline `reason: ''` or `reason: 'TODO'` used to pass; both must now run
    // through the same prose rule a map entry runs through.
    it('flags an inline reason that is empty or a placeholder, not just present', () => {
      assert.equal(reasonlessDisabled(FIXTURES.emptyInlineReason).length, 1)
      assert.equal(reasonlessDisabled(FIXTURES.todoInlineReason).length, 1)
      assert.match(
        disabledReasonViolation(findDisabledRecords(FIXTURES.emptyInlineReason)[0]) || '', /empty/)
      assert.match(
        disabledReasonViolation(findDisabledRecords(FIXTURES.todoInlineReason)[0]) || '', /placeholder/)
    })

    // Hole 1b: an inline `reason: SOME_REASONS.key` must resolve against the
    // map exactly as a bare `SOME_REASONS.key` reference does in check 1 — a
    // dangling key fails, a real one passes.
    it('resolves an inline REASONS.<key> reason the same way check 1 resolves a bare reference', () => {
      const map = findReasonMaps(FIXTURES.goodMap)[0]
      const mapsByName = new Map([[map.name, map]])
      const bad = findDisabledRecords(FIXTURES.refInlineReasonBad)[0]
      const good = findDisabledRecords(FIXTURES.refInlineReasonGood)[0]
      assert.match(disabledReasonViolation(bad, mapsByName) || '', /not a key of FAKE_REASONS/)
      assert.equal(disabledReasonViolation(good, mapsByName), null)
      // An unresolvable map name (alias/shadow) is check 1's documented
      // leniency, not a new violation — mirrored here, not re-litigated.
      assert.equal(disabledReasonViolation(bad), null)
    })

    // Hole 1c: anything that is neither a string nor a resolvable reference
    // must never pass SILENTLY — it is counted, not judged.
    it('counts a non-string non-ref inline reason as unverifiable instead of passing it silently', () => {
      const rec = findDisabledRecords(FIXTURES.computedInlineReason)[0]
      assert.equal(disabledReasonViolation(rec), null)
      assert.equal(unverifiableReasonExpr(rec), 'describeLock(x)')
      assert.equal(reasonlessDisabled(FIXTURES.computedInlineReason).length, 0)
    })
    // A shorthand `reason,` has no value range at all — it must not misread
    // as "declares reason with no readable value" (a false hard failure);
    // it is exactly the "any other expression" case, counted, not judged.
    it('treats shorthand `reason,` as unverifiable, not as a missing value', () => {
      const rec = findDisabledRecords(FIXTURES.shorthandReason)[0]
      assert.equal(rec.hasReason, true)
      assert.equal(disabledReasonViolation(rec), null)
      assert.equal(unverifiableReasonExpr(rec), 'reason')
      assert.equal(reasonlessDisabled(FIXTURES.shorthandReason).length, 0)
    })

    // Hole 3: an unexported `const *REASONS` used to be invisible to MAP_DECL.
    it('finds a REASONS map even when it is not exported, and still judges its values', () => {
      const maps = findReasonMaps(FIXTURES.unexportedBadMap)
      assert.equal(maps.length, 1)
      assert.equal(maps[0].name, 'FAKE_REASONS')
      assert.equal(maps[0].frozen, true)
      assert.match(reasonProseViolation(maps[0].entries[0].value) || '', /char floor/)
    })
  })

  // Round 2, blocker 2: "unverifiable reason expressions" used to be only
  // WARNED and COUNTED, never asserted — a new one could be added anywhere,
  // forever, without ever failing the gate. Pinned to a budget that ratchets
  // in both directions: over budget (a new one appeared) and under it (one
  // got fixed but the pin was not lowered) are both violations.
  describe('unverifiable reason expressions are asserted against a pinned budget, not silently counted', () => {
    it('matches the budget exactly: null', () => {
      assert.equal(unverifiableReasonBudgetViolation(16, 16), null)
    })

    it('a count above budget fails, naming the exact delta', () => {
      const why = unverifiableReasonBudgetViolation(17, 16, ['web/src/lib/ribbonClusters.js:999: reason is `newThing`'])
      assert.match(why, /rose from the pinned budget of 16 to 17 \(\+1\)/)
      assert.match(why, /raise UNVERIFIABLE_REASON_BUDGET to 17/)
      assert.match(why, /ribbonClusters\.js:999/, 'the violation must name the file the new one lives in')
    })

    it('a count below budget also fails — the ratchet must tighten, not just hold', () => {
      const why = unverifiableReasonBudgetViolation(15, 16, [])
      assert.match(why, /fell from the pinned budget of 16 to 15 \(-1\)/)
      assert.match(why, /lower UNVERIFIABLE_REASON_BUDGET to 15/)
    })

    it('the live tree sits exactly on its pinned budget today', () => {
      assert.equal(unverifiableReasons.length, UNVERIFIABLE_REASON_BUDGET,
        `the tree carries ${unverifiableReasons.length} unverifiable reason expressions; `
        + `UNVERIFIABLE_REASON_BUDGET must be updated to match in the same change:\n${unverifiableReasons.join('\n')}`)
    })

    // The wiring proof: one MORE identifier-style reason than the real tree
    // carries today, added to a SCRATCH COPY (a string, never written to
    // disk) of a live disabled-record file, must push the real total over
    // budget — proving the budget is wired to the same counts the running
    // gate computes, not just correct as a fixture in isolation.
    it('one added identifier-reason record in a scratch copy of a live file pushes the real tree over budget', () => {
      const livePath = DISABLED_RECORD_FILES[0]
      const liveSrc = readFileSync(livePath, 'utf8')
      const realCountInThisFile = findDisabledRecords(liveSrc)
        .filter((r) => unverifiableReasonExpr(r) != null).length
      const scratchSrc = `${liveSrc}\nconst __budgetScratchTool = { id: 'z', label: 'z', disabled: true, reason: describeScratchLock(x), onClick: () => {} }\n`
      const scratchCountInThisFile = findDisabledRecords(scratchSrc)
        .filter((r) => unverifiableReasonExpr(r) != null).length
      assert.equal(scratchCountInThisFile, realCountInThisFile + 1,
        'the injected record must be the only difference the scratch copy introduces')
      const projectedTreeTotal = unverifiableReasons.length - realCountInThisFile + scratchCountInThisFile
      assert.equal(projectedTreeTotal, unverifiableReasons.length + 1)
      const why = unverifiableReasonBudgetViolation(projectedTreeTotal, UNVERIFIABLE_REASON_BUDGET, unverifiableReasons)
      const expectRose = new RegExp(`rose from the pinned budget of ${UNVERIFIABLE_REASON_BUDGET} to ${projectedTreeTotal} \\(\\+1\\)`)
      assert.match(why, expectRose)
    })
  })

  // NIT (round 2 review): the production check-3 CALL SITE was unpinned — the
  // tests above all call the exported functions directly, so reverting the
  // GATE's own executed body back to a position-based reason read
  // (`row[row.length - 1]`) or a substring row scan (`fieldTable.includes`)
  // would stay green on the real doc while every test above kept passing.
  // This pin reads THIS FILE's own source and asserts the executed check-3
  // body is actually wired through the header-anchored shape, then proves
  // each assertion is load-bearing against hand-written text shaped exactly
  // like the two real regressions it exists to catch (mutation-checked: each
  // assertion is shown failing against the reverted shape it targets).
  describe('check 3\'s production call site stays wired to the header-anchored shape (source pin)', () => {
    const selfSrc = readFileSync(SELF_PATH, 'utf8')
    const bodyStart = selfSrc.indexOf('const { PRODUCT_SURFACES }')
    const bodyEnd = selfSrc.indexOf('// --- check 4', bodyStart)
    const check3Body = bodyStart !== -1 && bodyEnd !== -1 ? selfSrc.slice(bodyStart, bodyEnd) : ''

    const SHAPE_CALL = /fieldTableShape\s*\(\s*fieldTable\s*\)/
    const ROW_CALL = /fieldTableRow\s*\(\s*fieldTable\s*,\s*slot\s*\)/
    const SHAPE_VIOLATION_CALL = /fieldTableRowShapeViolation\s*\(\s*row\s*,\s*fieldTableShapeResult\s*,\s*slot\s*\)/
    const REASON_INDEX_READ = /row\[fieldTableShapeResult\.reasonIndex\]/
    const POSITIONAL_READ = /row\[row\.length\s*-\s*1\]/

    it('found check 3\'s executed body in this file (the markers did not move)', () => {
      assert.ok(check3Body.length > 200, 'could not isolate check 3\'s gate body between "const { PRODUCT_SURFACES }" and "// --- check 4" — the markers moved, and this pin is now checking nothing')
    })

    it('the real source calls fieldTableShape() once, looks up each row through the anchored fieldTableRow(), judges its shape via fieldTableRowShapeViolation(), and reads the reason cell at the header-learned index', () => {
      assert.match(check3Body, SHAPE_CALL)
      assert.match(check3Body, ROW_CALL)
      assert.match(check3Body, SHAPE_VIOLATION_CALL)
      assert.match(check3Body, REASON_INDEX_READ)
    })

    it('the real source never reads a field-table reason cell by position (row.length - 1) — that is exactly what let a short row smuggle the wrong cell through', () => {
      assert.doesNotMatch(check3Body, POSITIONAL_READ)
    })

    // Mutation check: hand-written text shaped exactly like the two real
    // regressions this pin exists to catch must fail the same assertions —
    // proving the regexes actually discriminate, not just happen to match
    // today's file by coincidence.
    it('mutation: a body that dropped the shape check (kept only the old positional read) fails this pin', () => {
      const reverted = `
        const row = fieldTableRow(fieldTable, slot)
        if (!row) { record(docFile, 1, 'no row'); continue }
        const reasonCell = row[row.length - 1]
        const why = fieldTableReasonViolation(reasonCell)
      `
      assert.match(reverted, ROW_CALL) // the row lookup alone is not enough...
      assert.doesNotMatch(reverted, SHAPE_CALL) // ...the shape call must ALSO be present
      assert.doesNotMatch(reverted, SHAPE_VIOLATION_CALL)
      assert.match(reverted, POSITIONAL_READ) // and the banned positional read is exactly what comes back
    })

    it('mutation: a body that reverted the anchored lookup to a substring scan fails this pin (recreates the original pre-13d bug)', () => {
      const reverted = "if (!fieldTable.includes(`\\`${slot}\\``)) { record(docFile, 1, 'no row'); continue }"
      assert.doesNotMatch(reverted, ROW_CALL)
      assert.doesNotMatch(reverted, SHAPE_CALL)
    })
  })

  it('every disabled or absent control carries a reason', () => {
    for (const v of violations) console.error(v)
    assert.equal(violations.length, 0,
      `${violations.length} honesty violation(s); each is printed above as file:line`)
  })
})

for (const line of unverifiableReasons) console.warn(`unverifiable reason expression: ${line}`)

console.log(
  `honesty ladder: ${scannedCounts.maps} reason maps · ${scannedCounts.values} sentences · `
  + `${scannedCounts.refs} key references · ${scannedCounts.records} disabled records · `
  + `${scannedCounts.slots} absent contract slots · `
  + `${scannedCounts.unverifiableReasons} unverifiable reason expressions`)
