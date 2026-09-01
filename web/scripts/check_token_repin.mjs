// Token re-pin parity ratchet (convergence plan W0/P0b, amended per the
// adversarial critique: COLOR-subset parity with an explicit global-token
// allowlist, ratcheted against committed known gaps).
//
// The dark subtrees (.stage-root in landing.css, .sheets-root in sheets.css)
// keep their identity ONLY by re-declaring the paper token contract; a :root
// color token they fail to re-pin is a light-literal-on-dark hole waiting for
// a consumer (four shipped in PR #849's review round). This gate:
//   - REQUIRES: every non-alias :root declaration carrying a literal color,
//     minus the global allowlist (fonts, motion, measured-height vars) —
//     aliases (`var(...)` values) resolve inside the subtree and are exempt.
//   - FAILS on any required token missing from a dark block UNLESS it is in
//     the committed known-gaps file (token-repin-gaps.json). The gaps are
//     DEBT: W1's tokens.css restructure burns them to zero; a PR may only
//     shrink the file, never grow it.
//   - VALIDATES .operator-console as a subset (every key it declares must
//     exist at :root), since it deliberately re-pins only what operator.css
//     consumes.
import { readFileSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const read = (p) => readFileSync(join(ROOT, p), 'utf8')

const declRe = /(--[a-zA-Z0-9-]+)\s*:\s*([^;]+);/g
const decls = (text) => {
  const out = {}
  for (const m of text.matchAll(declRe)) out[m[1]] = m[2]
  return out
}
// Merge EVERY occurrence of `selector { ... }` — layout blocks and re-pin
// blocks share selectors, and reading only the first was exactly the bug
// that made a stage gap of 15 look like 53 during P0b development.
const merged = (css, selector) => {
  const out = {}
  let idx = 0
  for (;;) {
    const i = css.indexOf(selector, idx)
    if (i === -1) break
    const j = css.indexOf('{', i)
    if (j === -1) break
    let depth = 1
    let k = j + 1
    while (depth && k < css.length) {
      if (css[k] === '{') depth += 1
      else if (css[k] === '}') depth -= 1
      k += 1
    }
    Object.assign(out, decls(css.slice(j + 1, k - 1)))
    idx = k
  }
  return out
}

const ALLOW_PREFIX = ['--font-', '--m-', '--mo-', '--recast-', '--drawer-', '--studio-']
const COLORISH = /#[0-9a-fA-F]|rgba?\(|oklch\(|oklab\(|color-mix\(/

const styles = read('src/styles.css')
const landing = read('src/site/landing.css')
const sheets = read('src/site/sheets.css')
const gaps = JSON.parse(read('scripts/token-repin-gaps.json'))

const root = merged(styles, ':root')
const required = Object.entries(root)
  .filter(([k, v]) =>
    !ALLOW_PREFIX.some((p) => k.startsWith(p)) &&
    !v.trim().startsWith('var(') &&
    COLORISH.test(v))
  .map(([k]) => k)

let failed = false
const gate = (name, block, knownGaps) => {
  const missing = required.filter((k) => !(k in block))
  const newHoles = missing.filter((k) => !knownGaps.includes(k))
  const paidDown = knownGaps.filter((k) => k in block)
  if (newHoles.length) {
    failed = true
    console.error(`${name}: NEW re-pin hole(s) — a :root color token this dark subtree does not re-declare: ${newHoles.join(', ')}`)
  }
  if (paidDown.length) {
    console.error(`${name}: ${paidDown.length} known gap(s) now re-pinned — REMOVE them from token-repin-gaps.json in this PR: ${paidDown.join(', ')}`)
    failed = true
  }
  console.log(`${name}: ${required.length - missing.length}/${required.length} required color tokens re-pinned (${missing.length} known-debt)`)
}
gate('.stage-root (landing.css)', merged(landing, '.stage-root'), gaps.stage)
gate('.sheets-root (sheets.css)', merged(sheets, '.sheets-root'), gaps.sheets)

const oc = merged(styles, '.operator-console')
const orphans = Object.keys(oc).filter((k) => k.startsWith('--') && !(k in root))
if (orphans.length) {
  failed = true
  console.error(`.operator-console declares token(s) that do not exist at :root: ${orphans.join(', ')}`)
} else {
  console.log(`.operator-console: subset valid (${Object.keys(oc).length} keys)`)
}

if (failed) process.exit(1)
console.log('token re-pin parity ok')
