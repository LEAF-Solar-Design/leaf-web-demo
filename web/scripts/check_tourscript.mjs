// Node oracle for M5 — validates the guided-tour script against the REAL
// router (mockNlPrompt.matchPrompt) over the REAL tool registry.
//
// Nothing here is hardcoded from the tour: every routing assertion re-derives
// the lane/tool by running the step's own canned prompt through the shipped
// matcher, so a tour beat that would dead-end on stage (wrong tool, unknown
// tool, or the unwired `solve` lane) fails this gate.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

import { TOUR_STEPS } from '../src/demo/tourScript.js'
import { matchPrompt } from '../src/mock/mockNlPrompt.js'

const here = dirname(fileURLToPath(import.meta.url))
const registry = JSON.parse(readFileSync(join(here, '..', 'src', 'mock', 'registry.json'), 'utf8'))
const tools = registry.tools || []
const toolNames = new Set(tools.map((t) => t.name))

let failures = 0
function assert(cond, msg) {
  if (!cond) { console.error('FAIL:', msg); failures += 1 }
}

// --- shape -----------------------------------------------------------------
assert(Array.isArray(TOUR_STEPS), 'TOUR_STEPS must be an array')
assert(TOUR_STEPS.length >= 7, `TOUR_STEPS must have >= 7 steps (has ${TOUR_STEPS.length})`)

const ids = new Set()
for (const [i, s] of TOUR_STEPS.entries()) {
  assert(!!s && typeof s.id === 'string' && s.id.length > 0, `step ${i}: missing id`)
  assert(!ids.has(s.id), `step ${i}: duplicate id ${s.id}`)
  ids.add(s.id)
  assert(typeof s.body === 'string' && s.body.trim().length > 0, `step ${s.id}: missing body`)
  assert(s.target != null || s.action != null, `step ${s.id}: needs either a target or an action`)
  const words = String(s.body).trim().split(/\s+/).length
  assert(words <= 40, `step ${s.id}: body is ${words} words (keep it ~35 or under)`)
  if (s.tool != null) {
    assert(toolNames.has(s.tool), `step ${s.id}: tool "${s.tool}" is not in registry.json`)
  }
}

// --- required beats --------------------------------------------------------
const byAction = (a) => TOUR_STEPS.filter((s) => s.action === a)
assert(byAction('author').length === 1, `expected exactly one action:'author' step (got ${byAction('author').length})`)
const versionSteps = TOUR_STEPS.filter((s) => s.action === 'version' || s.tool === 'delete-marked-panel')
assert(versionSteps.length >= 1, "expected a version beat (action:'version' or tool:'delete-marked-panel')")
assert(byAction('exit').length === 1, `expected exactly one action:'exit' step (got ${byAction('exit').length})`)
assert(TOUR_STEPS[TOUR_STEPS.length - 1].action === 'exit', 'the last step must be the exit beat')

// --- routing, re-derived through the real matcher --------------------------
for (const s of TOUR_STEPS) {
  if (!s.prompt) continue
  const r = matchPrompt(s.prompt, tools)
  assert(r.lane !== 'solve', `step ${s.id}: prompt routes to the unwired solve lane`)
  if (s.action === 'author') {
    assert(r.lane === 'build', `step ${s.id}: author beat must route to lane:'build' (got ${r.lane})`)
  } else {
    assert(r.lane === 'run', `step ${s.id}: expected lane:'run' (got ${r.lane})`)
    assert(toolNames.has(r.tool), `step ${s.id}: router picked unknown tool ${r.tool}`)
    if (s.tool) {
      assert(r.tool === s.tool, `step ${s.id}: router picked ${r.tool}, script claims ${s.tool}`)
    }
  }
}

// The count beat is the demo's first impression — pin it explicitly.
const countStep = TOUR_STEPS.find((s) => s.id === 'count')
assert(!!countStep && !!countStep.prompt, 'missing the count beat (id:"count") with a canned prompt')
if (countStep && countStep.prompt) {
  const r = matchPrompt(countStep.prompt, tools)
  assert(r.lane === 'run', `count beat must route to lane:'run' (got ${r.lane})`)
  assert(r.tool === 'count-by-layer', `count beat must route to count-by-layer (got ${r.tool})`)
  assert(r.confidence >= 0.7, `count beat confidence too low (${r.confidence})`)
}

// No beat anywhere may reach the solve lane, prompt or not.
for (const s of TOUR_STEPS) {
  assert(s.lane !== 'solve' && s.action !== 'solve', `step ${s.id}: declares the solve lane`)
}

if (failures > 0) {
  console.error(`check_tourscript: ${failures} failure(s)`)
  process.exit(1)
}
console.log(`tour: ${TOUR_STEPS.length} steps · ${TOUR_STEPS.filter((s) => s.prompt).length} canned prompts re-routed through matchPrompt`)
console.log('TOURSCRIPT_OK')
