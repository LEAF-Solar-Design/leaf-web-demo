// check_routes.mjs — ROUTER-BOUND validator for the golden-path runbook.
//
// It does NOT hardcode routing outcomes: it lifts every literal prompt out of
// docs/DEMO-GOLDEN-PATH.md (the `- **Type:** \`...\`` lines), cross-checks that
// set against the mirrored array in web/test/demoBeats.expected.json, then runs
// each prompt through the REAL matchPrompt over the REAL registry.json tools
// and asserts the lane/tool/params/confidence the runbook claims. Any beat that
// routes to the honest dead-end `solve` lane is a hard failure.
//
// Node discipline: no import.meta, no React, no JSON import assertions.
// Run from web/:  node test/check_routes.mjs

import { readFileSync, existsSync } from 'node:fs'
import { join, resolve, dirname } from 'node:path'

import { matchPrompt } from '../src/mock/mockNlPrompt.js'

// ---- locate the repo root (works from web/ or from the repo root) ----------
function repoRoot() {
  let dir = resolve(process.cwd())
  for (let i = 0; i < 6; i++) {
    if (existsSync(join(dir, 'docs', 'DEMO-GOLDEN-PATH.md')) &&
        existsSync(join(dir, 'web', 'src', 'mock', 'registry.json'))) return dir
    const up = dirname(dir)
    if (up === dir) break
    dir = up
  }
  throw new Error('check_routes: could not locate repo root from ' + process.cwd())
}

const ROOT = repoRoot()
const readJson = (p) => JSON.parse(readFileSync(p, 'utf8'))

const fail = (msg) => { console.error('ROUTES_FAIL: ' + msg); process.exit(1) }
const eq = (actual, expected, what) => {
  if (actual !== expected) fail(`${what}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`)
}

// ---- inputs ---------------------------------------------------------------
const runbook = readFileSync(join(ROOT, 'docs', 'DEMO-GOLDEN-PATH.md'), 'utf8')
const expected = readJson(join(ROOT, 'web', 'test', 'demoBeats.expected.json'))
const registry = readJson(join(ROOT, 'web', 'src', 'mock', 'registry.json'))
const tools = registry.tools || []
if (tools.length === 0) fail('registry.json has no tools')

// ---- (1) extract the literal prompts the runbook tells the presenter to type
const docPrompts = []
for (const line of runbook.split(/\r?\n/)) {
  const m = line.match(/^\s*[-*]\s*\*\*Type:\*\*\s*`([^`]+)`/)
  if (m) docPrompts.push(m[1])
}
if (docPrompts.length === 0) fail('no `- **Type:** `prompt`` beats found in docs/DEMO-GOLDEN-PATH.md')

// ---- (2) the doc and the mirrored array must agree, in order ---------------
const beats = expected.beats || []
const mirrored = beats.map((b) => b.prompt)
if (docPrompts.length !== mirrored.length) {
  fail(`runbook names ${docPrompts.length} typed prompts but demoBeats.expected.json mirrors ${mirrored.length}\n` +
       `  runbook:  ${JSON.stringify(docPrompts)}\n  mirrored: ${JSON.stringify(mirrored)}`)
}
docPrompts.forEach((p, i) => eq(p, mirrored[i], `beat ${i} prompt (runbook vs mirrored array)`))

// ---- (3) route every prompt through the REAL matcher ----------------------
let runBeats = 0
for (const beat of beats) {
  const m = matchPrompt(beat.prompt, tools)
  const tag = `beat ${beat.beat} (${beat.id}) "${beat.prompt}"`

  if (m.lane === expected.forbidden_lane) fail(`${tag} routes to the forbidden ${expected.forbidden_lane} lane`)
  eq(m.lane, beat.lane, `${tag} lane`)
  eq(m.tool ?? null, beat.tool ?? null, `${tag} tool`)

  const min = beat.min_confidence ?? 0.7
  if (!(typeof m.confidence === 'number' && m.confidence >= min)) {
    fail(`${tag} confidence ${m.confidence} < ${min}`)
  }

  if (beat.params) {
    for (const [k, v] of Object.entries(beat.params)) {
      eq(m.params?.[k], v, `${tag} params.${k}`)
    }
  }

  // The tool the router names must actually exist in the catalog.
  if (beat.tool && !tools.some((t) => t.name === beat.tool)) fail(`${tag} names tool '${beat.tool}' absent from registry.json`)

  if (m.lane === 'run') runBeats++
  console.log(`  ok  ${tag} -> lane=${m.lane} tool=${m.tool} conf=${m.confidence}` +
              (beat.params ? ` params=${JSON.stringify(m.params)}` : ''))
}

if (runBeats < 4) fail(`expected at least 4 run-lane beats, got ${runBeats}`)
if (!beats.some((b) => b.lane === 'build')) fail('no build-lane beat in the runbook — the differentiator beat is missing')

// ---- (4) belt-and-braces: nothing anywhere in the beat set hits solve -----
for (const beat of beats) {
  if (beat.lane === expected.forbidden_lane) fail(`beat ${beat.id} declares the forbidden lane`)
}

console.log(`ROUTES_OK (${beats.length} beats, ${runBeats} run-lane, 0 solve-lane)`)
