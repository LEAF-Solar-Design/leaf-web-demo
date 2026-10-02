import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { PNG } from 'pngjs'
import { computeJourneyMetrics, createJourneyStepCapture, captureTargetGeometry, diffAxe, diffPixels } from './uxMetrics.mjs'

// Fresh worktree prerequisite: npm ci in web. No binary fixtures are written.
const json = (path) => JSON.parse(readFileSync(new URL(path, import.meta.url), 'utf8'))
const baseline = json('./axe-baseline.json')
const good = json('./fixtures/ux-journey.good.json')
const bad = json('./fixtures/ux-journey.bad.json')
const pixels = json('./fixtures/ux-pixel-provenance.json')
const copy = (v) => structuredClone(v)
const metricNames = ['extra_steps', 'time_to_task_ms', 'dead_ends', 'scroll_needed_steps', 'recovery']
const action = (e, index = 0) => e.steps.filter((s) => s.measurements?.kind === 'user-action')[index].measurements
function assertUnknown(value) {
  assert.equal(value.status, 'unknown')
  assert.equal(value.value, null)
  assert.ok(value.reason)
}
function image(width = pixels.dimensions.width, height = pixels.dimensions.height, changes = []) {
  const png = new PNG({ width, height })
  png.data.fill(255)
  for (const [x, y] of changes) {
    const offset = (y * width + x) * 4
    png.data[offset] = 0; png.data[offset + 1] = 0; png.data[offset + 2] = 0
  }
  return PNG.sync.write(png)
}
function compare(options = {}) {
  return diffPixels({ before: image(), after: image(), provenance: copy(pixels.provenance), masks: copy(pixels.masks), ...options })
}

test('synthetic good/bad traces flip every metric against independent authored answers', () => {
  for (const fixture of [good, bad]) {
    const result = computeJourneyMetrics(fixture, baseline)
    for (const name of metricNames) assert.equal(result[name].status, 'known', name)
    for (const name of metricNames.filter((n) => n !== 'recovery')) assert.equal(result[name].value, fixture.expected[name], name)
    assert.deepEqual(result.recovery.value.map(({ raw, status, ...answer }) => answer), fixture.expected.recovery)
    assert.equal(result.user_action_count.value, fixture === good ? 3 : 5)
    assert.equal(result.elapsed_ms.value, fixture.elapsedMs)
    assert.deepEqual(result.raw, fixture)
    assert.deepEqual(result.baseline, baseline)
  }
  // Bad trace: 5 - 3 = 2; fault-one: 300 - 200 = 100;
  // fault-two: 700 - 400 = 300, with two independently linked actions.
  const result = computeJourneyMetrics(bad, baseline)
  assert.equal(result.extra_steps.value, 2)
  assert.equal(result.recovery.value[0].elapsed_ms, 100)
  assert.equal(result.recovery.value[1].elapsed_ms, 300)
  assert.equal(result.scroll_needed_steps.value, 2, 'multiple scrolls still count one action')
})

test('real first-run v1 step shape uses timeToTaskMs and cannot imply action count', () => {
  const source = readFileSync(new URL('./journeys/first-run-open.spec.mjs', import.meta.url), 'utf8')
  const names = [...source.matchAll(/await step\('([^']+)'/g)].map((match) => match[1])
  assert.deepEqual(names, ['Land as a first-time visitor', 'Dismiss the first-run coach', 'Open the offered sample rooftop', 'See the drawing in the viewer'])
  assert.match(source, /walkEvidence\.timeToTaskMs = performance\.now\(\) - started/)
  // Representative values, actual source field names; this is not a live run.
  const evidence = { schema: 'leaf.walk-evidence.v1', test: 'first-run-open @desktop',
    steps: [{ phase: 'setup', kind: 'navigate' }, ...names.map((name) => ({ name, elapsedMs: 100 }))],
    timeToTaskMs: 1234, elapsedMs: 2000, accessibility: { violations: [], incomplete: [] } }
  const result = computeJourneyMetrics(evidence, baseline)
  assert.equal(result.time_to_task_ms.value, 1234)
  assert.equal(result.elapsed_ms.value, 2000)
  for (const name of ['extra_steps', 'dead_ends', 'scroll_needed_steps', 'recovery']) assertUnknown(result[name])
})

test('setup, assertions, console errors and teardown cannot inflate successful metrics', () => {
  const e = copy(good)
  e.steps.push({ name: 'Assert', phase: 'assertion' }, { name: 'Prepare', phase: 'setup' })
  e.consoleErrors = ['irrelevant to explicit dead ends']
  e.elapsedMs = 9999
  e.timeToTaskMs = 8888
  const result = computeJourneyMetrics(e, baseline)
  assert.equal(result.user_action_count.value, 3)
  assert.equal(result.dead_ends.value, 0)
  assert.equal(result.time_to_task_ms.value, 300)
  assert.equal(result.elapsed_ms.value, 9999)
  const shorter = copy(good)
  shorter.steps.splice(1, 1)
  assert.equal(computeJourneyMetrics(shorter, baseline).extra_steps.value, 0)
})

test('failed and incomplete tasks remain unknown while failed elapsedMs is preserved', () => {
  for (const mutate of [
    (e) => { e.failure = { message: 'failed after elapsed time' } },
    (e) => { e.measurements.task.success = false },
    (e) => { delete e.measurements.task.oracle },
    (e) => { delete e.measurements.task.startedMs },
    (e) => { e.measurements.task.completedMs = 0 },
  ]) {
    const e = copy(good); mutate(e)
    const result = computeJourneyMetrics(e, baseline)
    for (const name of metricNames) assertUnknown(result[name])
    assert.equal(result.elapsed_ms.value, 600)
  }
  for (const mutate of [
    (e) => { delete e.measurements.captureComplete },
    (e) => { delete action(e).outcome },
    (e) => { delete action(e).startedMs },
    (e) => { action(e, 1).startedMs = 0 },
    (e) => { delete action(e).kind },
    (e) => { e.steps.push(null) },
  ]) {
    const e = copy(good); mutate(e)
    const result = computeJourneyMetrics(e, baseline)
    for (const name of ['extra_steps', 'dead_ends', 'scroll_needed_steps', 'recovery']) assertUnknown(result[name])
  }
  assertUnknown(computeJourneyMetrics({}, baseline).time_to_task_ms)
  assertUnknown(computeJourneyMetrics({}, baseline).elapsed_ms)
})

test('golden baseline identities and scope must match', () => {
  assertUnknown(computeJourneyMetrics(good).extra_steps)
  for (const mutate of [
    (b) => { b.id = 'another' }, (b) => { b.goldenPathVersion = '2' },
    (b) => { b.scope.catalogVersion = 'other' }, (b) => { delete b.scope.fixtureVersion },
    (b) => { delete b.goldenUserActionCount },
  ]) {
    const b = copy(baseline); mutate(b)
    assertUnknown(computeJourneyMetrics(good, b).extra_steps)
  }
})

test('pre-action geometry survives scrolling and invalid geometry is unknown', () => {
  const e = copy(bad)
  action(e).postAction = { targetBounds: { x: 0, y: 0, width: 1, height: 1 } }
  assert.equal(computeJourneyMetrics(e, baseline).scroll_needed_steps.value, 2)
  const explicit = copy(good)
  delete action(explicit).preAction
  action(explicit).scrollRequired = true
  assert.equal(computeJourneyMetrics(explicit, baseline).scroll_needed_steps.value, 1)
  for (const mutate of [
    (m) => { delete m.preAction }, (m) => { m.preAction.targetBounds.width = -1 },
    (m) => { m.preAction.viewport.width = '4' }, (m) => { m.scrollEvents = [{ required: true }] },
  ]) {
    const trace = copy(good); mutate(action(trace))
    assertUnknown(computeJourneyMetrics(trace, baseline).scroll_needed_steps)
  }
})

test('fault inventory, linkage and completion cannot silently become zero', () => {
  const missing = copy(good); delete missing.measurements.faults
  assertUnknown(computeJourneyMetrics(missing, baseline).recovery)
  const unlinked = copy(bad); action(unlinked, 1).recoveryFor = 'not-injected'
  assertUnknown(computeJourneyMetrics(unlinked, baseline).recovery)
  const duplicate = copy(bad); duplicate.measurements.faults[1].id = 'fault-one'
  assertUnknown(computeJourneyMetrics(duplicate, baseline).recovery)
  const incomplete = copy(bad); delete incomplete.measurements.faults[0].completedMs
  assertUnknown(computeJourneyMetrics(incomplete, baseline).recovery.value[0])
  const noAttempt = copy(good)
  noAttempt.measurements.faults = [{ id: 'unused', injectedMs: 110, completedMs: 200, outcome: 'unrecovered' }]
  assert.deepEqual(computeJourneyMetrics(noAttempt, baseline).recovery.value[0], {
    faultId: 'unused', status: 'known', attempted: false, recovered: false, actions_needed: 0, elapsed_ms: 90,
    raw: noAttempt.measurements.faults[0],
  })
})

test('bounded capture records geometry before action, monotonic timings and explicit outcomes', async () => {
  let tick = 0
  const e = { steps: [] }
  const capture = createJourneyStepCapture(e, { baseline, clock: () => (tick += 10) })
  const order = []
  capture.injectFault('fault')
  await capture.capture({ name: 'Recover', actionType: 'click', featureId: 'recover', recoveryFor: 'fault', outcome: 'success',
    readGeometry: async () => { order.push('geometry'); return copy(action(good).preAction) } }, async () => { order.push('action'); return 7 })
  capture.completeFault('fault', true)
  capture.finishTask('visible', true)
  assert.deepEqual(order, ['geometry', 'action'])
  assert.equal(e.steps[0].name, 'Recover')
  assert.equal(e.steps[0].elapsedMs, 10)
  assert.equal(e.timeToTaskMs, 50)
  const result = computeJourneyMetrics(e, baseline)
  assert.equal(result.time_to_task_ms.value, 50)
  assert.equal(result.recovery.value[0].elapsed_ms, 30)
  assert.equal(result.recovery.value[0].actions_needed, 1)
  await assert.rejects(capture.capture({ name: 'After oracle' }, async () => {}), /already completed/)
  const page = {}
  const geometry = await captureTargetGeometry(page, { evaluate: async (callback) => {
    assert.match(callback.toString(), /getBoundingClientRect/)
    return copy(action(good).preAction)
  } })
  assert.deepEqual(geometry, action(good).preAction)
})

test('capture overflow, thrown action and backwards clock remain explicit', async () => {
  let tick = 0
  const e = { steps: [] }, c = createJourneyStepCapture(e, { baseline, maxSteps: 1, clock: () => tick++ })
  await assert.rejects(c.capture({ name: 'Failed', outcome: 'success' }, async () => { throw new Error('action failed') }), /action failed/)
  assert.equal(e.steps[0].measurements.outcome, 'failed')
  await assert.rejects(c.capture({ name: 'Overflow' }, async () => {}), /limit exceeded/)
  c.finishTask('failed assertion', false)
  assert.equal(e.measurements.captureComplete, false)
  assertUnknown(computeJourneyMetrics(e, baseline).time_to_task_ms)
  const times = [10, 0]
  const backwards = createJourneyStepCapture({ steps: [] }, { clock: () => times.shift() })
  assert.throws(() => backwards.injectFault('clock'), /monotonic/)
  assert.throws(() => createJourneyStepCapture({}, { maxSteps: 0 }), /bounds/)
})

test('axe separates introduced, unchanged, resolved and incomplete instances', () => {
  const clean = diffAxe(good, baseline)
  assert.equal(clean.status, 'known')
  assert.equal(clean.introduced.length, 0)
  assert.equal(clean.unchanged.length, 2)
  assert.equal(clean.resolved.length, 0)
  assert.equal(clean.incomplete.status, 'known')
  const result = diffAxe(bad, baseline)
  assert.equal(result.status, 'known')
  assert.deepEqual(result.introduced.map((i) => i.ruleId), ['button-name'])
  assert.deepEqual(result.unchanged.map((i) => i.ruleId), ['color-contrast'])
  assert.deepEqual(result.resolved.map((i) => i.ruleId), ['label'])
  assertUnknown(result.incomplete)
  assert.equal(result.incomplete.raw.current.length, 1)
  const moved = copy(good); moved.accessibility.violations[0].nodes[0].target = ['#another-context']
  assert.equal(diffAxe(moved, baseline).introduced.length, 1)
})

test('axe missing, incompatible or malformed baselines cannot become empty comparisons', () => {
  assertUnknown(diffAxe({ accessibility: { violations: [], incomplete: [] } }, undefined))
  assertUnknown(diffAxe(good, undefined))
  const emptyScope = copy(good), emptyBaseline = copy(baseline)
  emptyScope.scope.viewport = {}; emptyBaseline.scope.viewport = {}
  assertUnknown(diffAxe(emptyScope, emptyBaseline))
  for (const mutate of [
    (b) => { b.scope.axeVersion = 'other' }, (b) => { delete b.scope.state },
    (b) => { delete b.accessibility }, (b) => { delete b.accessibility.incomplete },
    (b) => { b.accessibility.violations[0].nodes[0].target = [] },
  ]) {
    const b = copy(baseline); mutate(b)
    assertUnknown(diffAxe(good, b))
  }
  const reordered = copy(good)
  reordered.accessibility.violations.reverse()
  assert.deepEqual(diffAxe(good, baseline).unchanged, diffAxe(reordered, baseline).unchanged)
})

test('generated PNGs compare unchanged, unmasked and canvas-only changes with provenance', () => {
  for (const [changes, expected] of [[[], 0], [[[0, 0]], 1], [[[1, 1], [2, 1]], 0]]) {
    const result = compare({ after: image(4, 3, changes) })
    assert.equal(result.status, 'known')
    assert.equal(result.comparedPixelCount, 10)
    assert.equal(result.changedPixelCount, expected)
    assert.equal(result.ratio, expected / 10)
    assert.equal(result.disposition, 'review-evidence')
    assert.deepEqual(result.raw.provenance, pixels.provenance)
    assert.deepEqual(result.raw.masks, pixels.masks)
    assert.equal(result.raw.threshold, 0.1)
    for (const hash of Object.values(result.raw.imageHashes)) assert.match(hash, /^[a-f0-9]{64}$/)
    assert.equal(result.raw.imageHashes.before === result.raw.imageHashes.after, changes.length === 0)
  }
  const overlap = compare({ masks: [pixels.masks[0], pixels.masks[0]] })
  assert.equal(overlap.comparedPixelCount, 10, 'overlapping masks count each pixel once')
})

test('pixel scope, size, corrupt PNG, mask and fully masked comparisons refuse success', () => {
  for (const key of ['journey', 'state', 'fixtureVersion', 'catalogVersion', 'viewport', 'deviceScale', 'browser', 'captureConditions']) {
    const provenance = copy(pixels.provenance); provenance.after[key] = 'incompatible'
    assertUnknown(compare({ provenance }))
    const missing = copy(pixels.provenance); delete missing.before[key]
    assertUnknown(compare({ provenance: missing }))
  }
  const missingDeployment = copy(pixels.provenance); delete missingDeployment.after.deploymentId
  assertUnknown(compare({ provenance: missingDeployment }))
  const badThreshold = copy(pixels.provenance); badThreshold.threshold = -1
  assertUnknown(compare({ provenance: badThreshold }))
  assertUnknown(compare({ after: image(5, 3) }))
  assertUnknown(compare({ before: image(5, 3), after: image(5, 3) }))
  assertUnknown(compare({ after: Buffer.from('corrupt PNG') }))
  assertUnknown(compare({ before: null }))
  assertUnknown(compare({ masks: undefined }))
  for (const mask of [
    { x: -1, y: 0, width: 1, height: 1, source: 'bad' },
    { x: 4, y: 0, width: 1, height: 1, source: 'bad' },
    { x: 0, y: 0, width: 0, height: 1, source: 'bad' },
    { x: 0.5, y: 0, width: 1, height: 1, source: 'bad' },
    { x: 0, y: 0, width: 1, height: 1 },
    { x: 0, y: 0, width: 4, height: 3, source: 'fully masked' },
  ]) assertUnknown(compare({ masks: [mask] }))
  assert.equal(compare({ masks: [] }).comparedPixelCount, 12)
})

test('all computations are deterministic and preserve inputs', () => {
  const e = copy(bad), b = copy(baseline), p = copy(pixels)
  const snapshots = [copy(e), copy(b), copy(p)]
  assert.deepEqual(computeJourneyMetrics(e, b), computeJourneyMetrics(e, b))
  assert.deepEqual(diffAxe(e, b), diffAxe(e, b))
  const before = image(), after = image(4, 3, [[0, 0]]), original = Buffer.from(after)
  const input = { before, after, provenance: p.provenance, masks: p.masks }
  assert.deepEqual(diffPixels(input), diffPixels(input))
  assert.deepEqual(after, original)
  assert.deepEqual([e, b, p], snapshots)
})
