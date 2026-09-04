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
//   1. REASON MAPS. Every `export const *REASONS` under web/src is
//      Object.freeze'd and every value is a non-empty sentence (>= 12 chars, no
//      TODO/tbd/???). Every `SOMETHING_REASONS.key` reference anywhere under
//      web/src resolves to a key that map actually defines.
//   2. DISABLED RECORDS. Every object literal in ribbonClusters.js and
//      EngineRibbonClusters.jsx that declares `disabled:` also declares
//      `reason` (`disabled: false` is exempt: a control that is never taken
//      away owes no explanation).
//   3. ABSENT CONTRACT SLOTS. Every slot in productSurfaces.js's PRODUCT_SURFACES
//      contract that is null / false / 'none' on some surface is named in
//      docs/convergence/SURFACE-CONTRACT.md's field table, so the reader can
//      find out why it is absent.
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
//   - Reason maps are matched by EXPORT NAME across the whole tree, not by
//     import graph. Two different files exporting the same map name would
//     collide, so the gate refuses that outright (check 1e).
//   - A reason value must be a plain quoted string. A computed or templated
//     value is a violation, because a sentence a human can read in the diff is
//     the whole point.
//   - Check 2 covers records written as object literals. A record assembled
//     field by field, or spread in from elsewhere, is invisible to it.
import assert from 'node:assert/strict'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join, relative, sep } from 'node:path'
import { describe, it } from 'node:test'

const here = dirname(fileURLToPath(import.meta.url))
const WEB = join(here, '..')
const REPO = join(WEB, '..')
const SRC = join(WEB, 'src')
const SURFACE_CONTRACT_DOC = join(REPO, 'docs', 'convergence', 'SURFACE-CONTRACT.md')

// The two files that build ribbon/tool records as object literals. Check 2
// parses these deeply; adding a third builder means adding it here.
const DISABLED_RECORD_FILES = [
  join(SRC, 'lib', 'ribbonClusters.js'),
  join(SRC, 'cadedit', 'EngineRibbonClusters.jsx'),
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
// before the word; the suffix test happens on the captured name instead.
const MAP_DECL = /export\s+const\s+([A-Za-z_$][\w$]*)\s*=\s*/g

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
 * depth-0 keys it declares beside it. `disabledIsFalse` marks the exempt case.
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
    found.push({
      line: lineOf(src, disabled.keyStart),
      keys: entries.map((e) => e.key),
      disabledValue: value,
      disabledIsFalse: value === 'false',
    })
  }
  return found
}

/** Records that take a control away without saying why. */
export function reasonlessDisabled(src) {
  return findDisabledRecords(src)
    .filter((r) => !r.disabledIsFalse && !r.keys.includes('reason'))
}

// --- check 3: absent contract slots ---------------------------------------

/** Dotted paths in a contract tree whose value is null, false or 'none'. */
export function absentSlots(contract, prefix = '') {
  const out = []
  for (const [key, value] of Object.entries(contract)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (value && typeof value === 'object' && !Array.isArray(value)) out.push(...absentSlots(value, path))
    else if (value === null || value === false || value === 'none') out.push(path)
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
const scannedCounts = { maps: 0, values: 0, refs: 0, records: 0, slots: 0 }

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
  scannedCounts.records += findDisabledRecords(src).length
  for (const r of reasonlessDisabled(src)) {
    record(file, r.line, `record sets \`disabled: ${r.disabledValue}\` and declares no \`reason\` — the honesty contract in ribbonClusters.js forbids a greyed control with no sentence`)
  }
}

const { PRODUCT_SURFACES } = await import(new URL('../src/site/productSurfaces.js', import.meta.url))
const doc = readFileSync(SURFACE_CONTRACT_DOC, 'utf8')
const fieldTable = docSection(doc, '## Field table')
const docFile = rel(SURFACE_CONTRACT_DOC)
const seenSlots = new Set()
for (const surface of PRODUCT_SURFACES) {
  for (const slot of absentSlots(surface.contract)) {
    if (seenSlots.has(slot)) continue
    seenSlots.add(slot)
    scannedCounts.slots += 1
    if (!doc.includes(`\`${slot}\``)) {
      record(docFile, 1, `\`${slot}\` is declared absent (null / false / 'none') on surface "${surface.id}" but the doc never names it`)
    } else if (!fieldTable.includes(`\`${slot}\``)) {
      record(docFile, 1, `\`${slot}\` is declared absent on surface "${surface.id}" but has no row in the field table, where the rationale lives`)
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
        absentSlots({ chrome: { productFrame: false, stageBranch: 'cad' }, versions: 'none', dock: null, routes: ['one-shot'] }),
        ['chrome.productFrame', 'versions', 'dock'])
    })
  })

  it('every disabled or absent control carries a reason', () => {
    for (const v of violations) console.error(v)
    assert.equal(violations.length, 0,
      `${violations.length} honesty violation(s); each is printed above as file:line`)
  })
})

console.log(
  `honesty ladder: ${scannedCounts.maps} reason maps · ${scannedCounts.values} sentences · `
  + `${scannedCounts.refs} key references · ${scannedCounts.records} disabled records · `
  + `${scannedCounts.slots} absent contract slots`)
