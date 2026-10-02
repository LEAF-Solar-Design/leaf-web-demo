import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { lenses, predicates, rubricSections, evaluateLenses } from './lenses/index.mjs'

const load = (path) => JSON.parse(readFileSync(new URL(path, import.meta.url), 'utf8'))
const good = load('./lenses/fixtures/good.json').cases
const bad = load('./lenses/fixtures/bad.json').cases
const receiptSchema = load('../../walk/fixtures/leaf.studio-walk.v1.schema.json')
const clone = (value) => structuredClone(value)
const find = (id) => predicates.find((entry) => entry.id === id)
function remove(object, path) {
  const keys = path.split('.')
  const parent = keys.slice(0, -1).reduce((value, key) => value[key], object)
  delete parent[keys.at(-1)]
}
function set(object, path, value) {
  const keys = path.split('.')
  const parent = keys.slice(0, -1).reduce((item, key) => item[key], object)
  parent[keys.at(-1)] = value
}

test('exactly ten unique lenses map all eight exact rubric sections', () => {
  assert.equal(lenses.length, 10)
  assert.deepEqual(lenses.map((entry) => entry.name), [
    'accessibility', 'end-user', 'mobile-touch', 'ops-sre', 'performance',
    'qc-dev', 'red-hat', 'support', 'ui-ux-dev', 'unfamiliar',
  ])
  assert.equal(new Set(lenses.map((entry) => entry.name)).size, 10)
  assert.equal(new Set(predicates.map((entry) => entry.id)).size, predicates.length)
  assert.deepEqual(new Set(lenses.flatMap((entry) => entry.dimensions)), new Set(Object.keys(rubricSections)))
  assert.deepEqual(rubricSections, { Copy: [34, 52], 'Ease of use': [59, 77], Layout: [84, 110],
    Chronology: [117, 134], Streamlinedness: [141, 155], Automation: [162, 178],
    Familiarity: [185, 200], Enjoyment: [207, 221] })
  for (const entry of predicates) {
    assert.ok(entry.requiredEvidenceFields.length > 0)
    for (const section of entry.rubric) {
      assert.equal(section.source, 'ui-loop/rubric.md')
      assert.deepEqual(section.lines, rubricSections[section.dimension])
    }
  }
  assert.deepEqual(Object.keys(good).sort(), predicates.map((entry) => entry.id).sort())
  assert.deepEqual(Object.keys(bad).sort(), Object.keys(good).sort())
})

for (const entry of predicates) {
  test(`${entry.id}: good=pass bad=finding missing=unknown`, () => {
    assert.equal(entry.evaluate(good[entry.id]).status, 'pass')
    const result = entry.evaluate(bad[entry.id])
    assert.equal(result.status, 'finding')
    assert.equal(result.finding.assertion_id, entry.id)
    assert.notDeepEqual(good[entry.id].measurements, bad[entry.id].measurements)
    for (const path of entry.requiredEvidenceFields) {
      const incomplete = clone(good[entry.id])
      remove(incomplete, path)
      const unknown = entry.evaluate(incomplete)
      assert.equal(unknown.status, 'unknown', `missing ${path}`)
      assert.ok(unknown.missing.includes(path))
      const invalid = clone(good[entry.id])
      set(invalid, path, null)
      assert.equal(entry.evaluate(invalid).status, 'unknown', `null ${path}`)
    }
  })

  test(`${entry.id}: finding conforms to receipt fields and rubric five parts`, () => {
    const finding = entry.evaluate(bad[entry.id]).finding
    const allowed = Object.keys(receiptSchema.properties.findings.items.properties)
    assert.deepEqual(Object.keys(finding).sort(), allowed.sort())
    assert.match(finding.feature_id, new RegExp(receiptSchema.$defs.id.pattern))
    assert.match(finding.assertion_id, new RegExp(receiptSchema.$defs.id.pattern))
    assert.ok(finding.feature_id.length <= receiptSchema.$defs.id.maxLength)
    for (const part of ['observation', 'mechanism', 'consequence', 'desired_outcome',
      'smallest_correction', 'reevaluation_check', 'reference']) {
      assert.equal(typeof finding.evidence[part], 'string')
      assert.ok(finding.evidence[part].length > 0)
    }
    assert.ok(Object.keys(finding.evidence.evidence).length > 0)
    assert.deepEqual(finding.evidence.context, { viewport: 'phone', state: 'ready', journey_id: 'first-run-open' })
    assert.ok(['measured', 'inferred'].includes(finding.evidence.provenance))
    assert.equal(typeof finding.severity, 'string')
  })
}

test('combined measured evidence gives pass or finding for all ten lens evaluators', () => {
  for (const [cases, expected] of [[good, 'pass'], [bad, 'finding']]) {
    const evidence = clone(Object.values(cases)[0])
    evidence.measurements = {}
    for (const sample of Object.values(cases)) for (const [key, value] of Object.entries(sample.measurements)) {
      evidence.measurements[key] = value && typeof value === 'object' && !Array.isArray(value)
        ? { ...evidence.measurements[key], ...clone(value) } : clone(value)
    }
    const results = evaluateLenses(evidence)
    assert.equal(results.length, 10)
    for (const result of results) assert.equal(result.status, expected, result.lens)
  }
})

test('real v1 runner shape without measurements yields unknown for every lens', () => {
  // Same fields as fixtures.mjs walkEvidence, including runProbe context/result.
  const evidence = { schema: 'leaf.walk-evidence.v1', test: 'Open drawing', viewport: 'phone',
    consoleErrors: [], pageErrors: [], failedRequests: [], abortedRequests: [], httpErrors: [],
    responses: [], steps: [], accessibility: null, startedAt: '2026-10-01T23:00:00.000Z',
    featureId: 'drawing-edit', state: 'ready', result: { result: 'passed' } }
  for (const result of evaluateLenses(evidence)) {
    assert.equal(result.status, 'unknown')
    assert.ok(result.results.every((item) => item.status === 'unknown'))
  }
  assert.ok(evaluateLenses(undefined).every((entry) => entry.status === 'unknown'))
})

test('mouse-only, non-coarse, and non-phone evidence cannot certify phone touch targets', () => {
  const entry = find('mobile-touch.reachable-target')
  for (const [path, value] of [['measurements.input.touch', false],
    ['measurements.input.coarsePointer', false], ['viewport', 'desktop'], ['viewport', 'tablet']]) {
    const evidence = clone(good[entry.id])
    set(evidence, path, value)
    assert.equal(entry.evaluate(evidence).status, 'unknown')
  }
  for (const [path, value] of [['measurements.control.rect.height', 43],
    ['measurements.control.rect.x', 380], ['measurements.control.rect.x', -1], ['measurements.control.clipped', true],
    ['measurements.control.reachable', false]]) {
    const evidence = clone(good[entry.id])
    set(evidence, path, value)
    assert.equal(entry.evaluate(evidence).status, 'finding')
  }
})

test('performance requires a supplied baseline, never a universal duration rule', () => {
  const entry = find('performance.declared-baseline')
  const evidence = clone(good[entry.id])
  delete evidence.measurements.baseline
  assert.equal(entry.evaluate(evidence).status, 'unknown')
  evidence.measurements.baseline = { reference: 'slow-task-study', maxDurationMs: 100000, maxRequiredSteps: 3 }
  evidence.measurements.task.durationMs = 90000
  assert.equal(entry.evaluate(evidence).status, 'pass')
  evidence.measurements.baseline.maxDurationMs = 80000
  assert.equal(entry.evaluate(evidence).status, 'finding')
})

test('wait explanations and recovery outcomes are separate observed requirements', () => {
  const entry = find('ops-sre.wait-recovery')
  const evidence = clone(good[entry.id])
  evidence.measurements.wait.progress = ''
  assert.equal(entry.evaluate(evidence).status, 'finding')
  evidence.measurements.wait.reason = 'Waiting for provider response'
  assert.equal(entry.evaluate(evidence).status, 'pass')
  evidence.measurements.recovery.outcome = ''
  assert.equal(entry.evaluate(evidence).status, 'finding')
  evidence.measurements.wait.observed = false
  evidence.measurements.recovery.failureObserved = false
  assert.equal(entry.evaluate(evidence).status, 'unknown')
})

test('completion proxy remains inferred; human reaction is unknown without supplied feedback', () => {
  const proxy = find('ui-ux-dev.completion-proxy')
  const reaction = find('ui-ux-dev.human-reaction')
  assert.equal(proxy.evaluate(good[proxy.id]).provenance, 'inferred')
  const finding = proxy.evaluate(bad[proxy.id]).finding
  assert.equal(finding.evidence.provenance, 'inferred')
  assert.match(finding.evidence.consequence, /not measured dissatisfaction/)
  assert.equal(reaction.evaluate(good[proxy.id]).status, 'unknown')
  assert.equal(reaction.evaluate(bad[reaction.id]).finding.evidence.provenance, 'measured')
  const inventedReaction = clone(good[reaction.id])
  delete inventedReaction.measurements.humanFeedback.quote
  assert.equal(reaction.evaluate(inventedReaction).status, 'unknown')
  const incompleteRectangle = clone(good[proxy.id])
  delete incompleteRectangle.measurements.layout.controls[0].width
  assert.equal(proxy.evaluate(incompleteRectangle).status, 'unknown')
})

test('runtime-assembled credentials trigger findings and are redacted from finding evidence', () => {
  const entry = find('red-hat.failure-refusal')
  const jwt = ['eyJ' + 'hbGciOiJIUzI1NiJ9', 'cGF5bG9hZA', 'c2lnbmF0dXJl'].join('.')
  const bearer = ['Bear', 'er', ' ', 'test-', 'credential'].join('')
  const basicHeader = ['Author', 'ization', ': ', 'Basic ', 'dGVzdDp0ZXN0'].join('')
  for (const credential of [jwt, bearer, basicHeader]) {
    const evidence = clone(good[entry.id])
    evidence.measurements.failure.visibleText = `Request failed: ${credential}`
    const result = entry.evaluate(evidence)
    assert.equal(result.status, 'finding')
    assert.ok(!JSON.stringify(result).includes(credential))
    assert.match(result.finding.evidence.evidence['failure.visibleText'], /\[REDACTED\]/)
  }
})

test('hostname, URL, build marker, deployment SHA and title do not change verdicts', () => {
  for (const cases of [good, bad]) for (const evidence of Object.values(cases)) {
    const changed = clone(evidence)
    Object.assign(changed, { hostname: 'different.example', url: 'https://different.example/other',
      buildMarker: 'other-build', deploymentSha: 'other-sha', pageTitle: 'Other title',
      stack: { baseURL: 'https://different.example' } })
    assert.deepEqual(evaluateLenses(changed), evaluateLenses(evidence))
  }
})

test('deterministic evaluations preserve input and never grant executable or scoring authority', () => {
  const forbidden = /^(?:scores?|ranking|average|averaging|commands?|selectors?|file_paths?|owned(?:Files|_files)?|owned-file-grants)$/i
  function inspect(value) {
    if (!value || typeof value !== 'object') return
    for (const [key, child] of Object.entries(value)) {
      assert.ok(!forbidden.test(key), `forbidden authority or score field: ${key}`)
      inspect(child)
    }
  }
  for (const cases of [good, bad]) for (const sample of Object.values(cases)) {
    const evidence = clone(sample)
    evidence.measurements.command = 'arbitrary prose is not executable'
    evidence.measurements.ownedFiles = ['foreign-file']
    evidence.measurements.score = 99
    const before = clone(evidence)
    const first = evaluateLenses(evidence)
    assert.deepEqual(first, evaluateLenses(evidence))
    assert.deepEqual(evidence, before)
    assert.deepEqual(first, evaluateLenses(sample))
    inspect(first)
  }
})
