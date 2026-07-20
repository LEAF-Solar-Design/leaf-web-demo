// check_integration.mjs — browser-free END-TO-END oracle for the golden path.
//
// Loads the real sample rooftop intake, routes each runbook beat through the
// REAL matchPrompt, executes it through the REAL mockEngine (and mockVersions
// for the write/undo beat), and asserts the recomputed values equal the pinned
// numbers in demoBeats.expected.json. Nothing here is hardcoded: every number
// compared comes out of the engine at runtime, so drift in the geometry, the
// matcher, the registry, or the intake fixture fails this check.
//
// Node discipline: no import.meta, no React, no JSON import assertions.
// Run from web/:  node test/check_integration.mjs

import { readFileSync, existsSync } from 'node:fs'
import { join, resolve, dirname } from 'node:path'

import { matchPrompt } from '../src/mock/mockNlPrompt.js'
import { runMock } from '../src/mock/mockEngine.js'
import * as versions from '../src/mock/mockVersions.js'

function repoRoot() {
  let dir = resolve(process.cwd())
  for (let i = 0; i < 6; i++) {
    if (existsSync(join(dir, 'web', 'public', 'sample.intake.json')) &&
        existsSync(join(dir, 'web', 'test', 'demoBeats.expected.json'))) return dir
    const up = dirname(dir)
    if (up === dir) break
    dir = up
  }
  throw new Error('check_integration: could not locate repo root from ' + process.cwd())
}

const ROOT = repoRoot()
const readJson = (p) => JSON.parse(readFileSync(p, 'utf8'))

const fail = (msg) => { console.error('INTEGRATION_FAIL: ' + msg); process.exit(1) }
const eq = (actual, expect, what) => {
  if (actual !== expect) fail(`${what}: expected ${JSON.stringify(expect)}, got ${JSON.stringify(actual)}`)
}

// ---- real inputs ----------------------------------------------------------
const intake = readJson(join(ROOT, 'web', 'public', 'sample.intake.json'))
const expected = readJson(join(ROOT, 'web', 'test', 'demoBeats.expected.json'))
const tools = (readJson(join(ROOT, 'web', 'src', 'mock', 'registry.json')).tools) || []

if (!Array.isArray(intake.polylines) || intake.polylines.length === 0) fail('sample.intake.json has no polylines')

const beatById = {}
for (const b of expected.beats || []) beatById[b.id] = b
const need = (id) => beatById[id] || fail(`demoBeats.expected.json is missing beat '${id}'`)

// Route a prompt through the real matcher and execute it on the real engine.
function execute(beat, onIntake) {
  const m = matchPrompt(beat.prompt, tools)
  if (m.lane === expected.forbidden_lane) fail(`beat ${beat.id} routed to the forbidden ${expected.forbidden_lane} lane`)
  eq(m.lane, beat.lane, `beat ${beat.id} lane`)
  eq(m.tool ?? null, beat.tool ?? null, `beat ${beat.id} tool`)
  if (m.lane !== 'run') return { match: m, env: null }
  const tool = tools.find((t) => t.name === m.tool)
  if (!tool) fail(`beat ${beat.id}: router named tool '${m.tool}' which is not in registry.json`)
  const env = runMock(tool, m.params, onIntake || intake)
  if (!env.ok) fail(`beat ${beat.id}: engine returned error ${env.error}`)
  return { match: m, env }
}

// ---- beat 1: count --------------------------------------------------------
{
  const b = need('count')
  const { env } = execute(b)
  eq(env.result.counts.Panels, b.expect.counts.Panels, 'count: counts.Panels')
  eq(env.result.total, b.expect.total, 'count: total')
  console.log(`  ok  count      -> counts.Panels=${env.result.counts.Panels} (recomputed from ${intake.polylines.length} polylines)`)
}

// ---- beat 2: measure ------------------------------------------------------
{
  const b = need('measure')
  const { env } = execute(b)
  eq(env.result.panels, b.expect.panels, 'measure: panels')
  eq(env.result.total_area_sqft, b.expect.total_area_sqft, 'measure: total_area_sqft')
  if (!(env.overlay && Array.isArray(env.overlay.markers) && env.overlay.markers.length === 1)) {
    fail('measure: expected exactly one largest-panel marker in the overlay')
  }
  console.log(`  ok  measure    -> total_area_sqft=${env.result.total_area_sqft}, largest marker "${env.overlay.markers[0].label}"`)
}

// ---- beat 3: edge ring ----------------------------------------------------
{
  const b = need('edge')
  const { match, env } = execute(b)
  eq(match.params.distance_in, b.params.distance_in, 'edge: parsed params.distance_in')
  eq(env.result.distance_in, b.expect.distance_in, 'edge: distance_in')
  eq(env.result.panels_near_edge, b.expect.panels_near_edge, 'edge: panels_near_edge')
  eq(env.result.panels_total, b.expect.panels_total, 'edge: panels_total')
  eq(env.overlay.highlight_handles.length, b.expect.panels_near_edge, 'edge: highlighted handle count')
  console.log(`  ok  edge       -> ${env.result.panels_near_edge} of ${env.result.panels_total} within ${env.result.distance_in} in`)
}

// ---- beat 4: build lane (authoring, no engine run) ------------------------
{
  const b = need('build')
  const { match } = execute(b)
  if (!(match.confidence >= (b.min_confidence ?? 0.7))) fail(`build: confidence ${match.confidence} below ${b.min_confidence ?? 0.7}`)
  console.log(`  ok  build      -> lane=build conf=${match.confidence} (authors a reusable tool, not a one-off answer)`)
}

// ---- beat 5: write -> v2, then undo -> v1 --------------------------------
{
  const b = need('write')
  const { match, env } = execute(b)
  eq(match.params.handle, b.params.handle, 'write: parsed params.handle')
  eq(env.result.removed, b.expect.removed, 'write: removed handle')
  eq(env.result.panels_before, b.expect.panels_before, 'write: panels_before')
  eq(env.result.panels_after, b.expect.panels_after, 'write: panels_after')
  eq(env.result.new_version.version, b.expect.new_version.version, 'write: new_version.version')
  eq(env.result.new_version.parent, b.expect.new_version.parent, 'write: new_version.parent')
  console.log(`  ok  write      -> removed ${env.result.removed}, ${env.result.panels_before}->${env.result.panels_after}, v${env.result.new_version.version} (parent v${env.result.new_version.parent})`)

  // The version chain the UI actually walks.
  versions.reset()
  versions.seedBase(intake)
  const stamp = versions.applyDelete(match.params.handle)
  eq(stamp.version, b.expect.new_version.version, 'versions: applyDelete version')
  eq(stamp.parent, b.expect.new_version.parent, 'versions: applyDelete parent')
  eq(stamp.removed, b.expect.removed, 'versions: applyDelete removed')
  eq(stamp.intake.polylines.length, b.expect.panels_after, 'versions: v2 polyline count')

  const after = versions.undo()
  eq(after.head, expected.undo.head, 'undo: head')
  eq(after.latest, expected.undo.latest, 'undo: latest')
  eq(after.intake.polylines.length, expected.undo.panels_at_head, 'undo: polyline count at head')

  const hist = versions.list()
  const vs = hist.versions.map((r) => r.v)
  eq(JSON.stringify(vs), JSON.stringify(expected.history.versions), 'history: version list')
  eq(hist.versions[1].tool, expected.history.v2_tool, 'history: v2 tool')
  eq(hist.head, expected.undo.head, 'history: head after undo')
  console.log(`  ok  undo       -> head=v${after.head} latest=v${after.latest}, ${after.intake.polylines.length} panels restored; History ${vs.join('->')} via ${hist.versions[1].tool}`)
  versions.reset()
}

console.log('INTEGRATION_OK')
