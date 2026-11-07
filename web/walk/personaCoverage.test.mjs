import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { buildPersonaCoverage } from './personaCoverage.mjs'
import { evaluateLenses, lenses } from '../e2e/walk/lenses/index.mjs'

const names = ['accessibility', 'end-user', 'mobile-touch', 'ops-sre', 'performance',
  'qc-dev', 'red-hat', 'support', 'ui-ux-dev', 'unfamiliar']
const identity = { deployment_identity: { commit: 'abc', stack: { name: 'studio', version: 1 } },
  fixture_version: 'fixture-1', catalog_version: 'catalog-1', browser_version: 'chromium-1', attempt: 2 }
const context = { feature_id: 'action:open', state: 'ready', viewport: 'desktop' }
const entry = { id: 'action:open', states: ['ready'], viewports: ['desktop'] }
const evaluation = (overrides = {}) => ({ ...context, evidence_ref: 'walk/task-1.json',
  identity: structuredClone(identity), lenses: names.map((lens) => ({ lens, status: 'pass' })), ...overrides })
function input() {
  return { featureMap: { catalog_version: 'catalog-1', entries: [structuredClone(entry)] },
    walkReceipt: { schema: 'leaf.studio-walk.v1', ...structuredClone(identity), coverage_gaps: [],
      evidence: { cases: [{ ...context, verdict: 'pass' }], coverage_gaps: [] } }, evaluations: [evaluation()] }
}
const build = (value = input()) => buildPersonaCoverage(value)
const expectedRow = { ...context, persona: 'accessibility', case_verdict: 'PASS', persona_verdict: 'PASS',
  verdict: 'PASS', evidence_ref: 'walk/task-1.json' }

test('complete matching evidence uses all ten real lenses and receipt identity layout', () => {
  assert.deepEqual(lenses.map(({ name }) => name), names)
  const result = build()
  assert.equal(result.schema, 'leaf.studio-persona-coverage.v1')
  for (const field of Object.keys(identity)) assert.deepEqual(result[field], identity[field])
  assert.equal(result.denominator, 10)
  assert.deepEqual(result.totals, { PASS: 10, FAIL: 0, UNKNOWN: 0, UNAVAILABLE: 0 })
  assert.deepEqual(result.rows[0], expectedRow)
  assert.deepEqual(result.rows.map(({ persona }) => persona), [...names].sort())
  assert.deepEqual(result.uncovered, [])
  assert.deepEqual(JSON.parse(JSON.stringify(result)), result)
})

test('state viewport overrides define the denominator without exemptions or extra cases', () => {
  const value = input()
  value.featureMap.entries[0] = { ...entry, states: ['ready', 'drawer-open'], viewports: ['desktop', 'phone'],
    state_viewports: { 'drawer-open': ['phone'] } }
  value.featureMap.exemptions = [{ id: 'action:exempt' }]
  value.walkReceipt.evidence.cases.push(
    { feature_id: 'journey:first-run-open', state: 'first-run', viewport: 'desktop', verdict: 'pass' },
    { feature_id: 'control-census:ribbon', state: 'ready', viewport: 'desktop', verdict: 'pass' })
  const result = build(value)
  assert.equal(result.denominator, 30)
  assert.deepEqual(result.totals, { PASS: 10, FAIL: 0, UNKNOWN: 20, UNAVAILABLE: 0 })
  assert.deepEqual([...new Set(result.rows.map((row) => `${row.state}/${row.viewport}`))],
    ['drawer-open/phone', 'ready/desktop', 'ready/phone'])
  assert.equal(result.uncovered.length, 20)
  assert.ok(result.uncovered.every((row) => row.reason === 'missing_case'))
})

test('findings fail only the reported persona and never copy arbitrary evaluator prose', () => {
  const value = input()
  value.evaluations[0].lenses[0] = { lens: 'accessibility', status: 'finding', finding: { secret: 'PRIVATE-PROSE' } }
  const result = build(value)
  assert.deepEqual(result.rows[0], { ...expectedRow, persona_verdict: 'FAIL', verdict: 'FAIL' })
  assert.deepEqual(result.totals, { PASS: 9, FAIL: 1, UNKNOWN: 0, UNAVAILABLE: 0 })
  assert.deepEqual(result.uncovered, [])
  assert.ok(!JSON.stringify(result).includes('PRIVATE-PROSE'))
})

test('missing and unknown persona evidence and missing base cases stay unknown', () => {
  const value = input()
  value.evaluations[0].lenses = [{ lens: 'accessibility', status: 'unknown' }]
  let result = build(value)
  assert.deepEqual(result.totals, { PASS: 0, FAIL: 0, UNKNOWN: 10, UNAVAILABLE: 0 })
  assert.deepEqual(result.uncovered[0], { ...expectedRow, persona_verdict: 'UNKNOWN', verdict: 'UNKNOWN', reason: 'unknown_persona' })
  assert.equal(result.uncovered[1].reason, 'missing_persona')
  assert.equal(result.rows[1].evidence_ref, null)
  value.evaluations = [evaluation()]
  value.walkReceipt.evidence.cases = []
  result = build(value)
  assert.equal(result.rows[0].persona_verdict, 'PASS')
  assert.equal(result.rows[0].case_verdict, 'UNKNOWN')
  assert.equal(result.rows[0].verdict, 'UNKNOWN')
  assert.ok(result.uncovered.every((row) => row.reason === 'missing_case'))
  delete value.evaluations
  assert.equal(build(value).rows[0].persona_verdict, 'UNKNOWN')
})

test('base mapping and conservative duplicate observation precedence cannot fabricate persona findings', () => {
  for (const [statuses, expected] of [
    [['fail', 'pass'], 'FAIL'], [['pass', 'fail'], 'FAIL'],
    [['unsupported_local', 'pass'], 'UNAVAILABLE'], [['staging_only', 'queued', 'pass'], 'UNAVAILABLE'],
    [['queued', 'pass'], 'UNKNOWN'], [['unexpected', 'pass'], 'UNKNOWN'], [[undefined, 'pass'], 'UNKNOWN'],
    [['fail', 'unsupported_local', 'queued', 'pass'], 'FAIL'],
  ]) {
    const value = input()
    value.walkReceipt.evidence.cases = statuses.map((verdict) => ({ ...context, verdict }))
    const result = build(value)
    assert.equal(result.rows[0].case_verdict, expected)
    assert.equal(result.rows[0].verdict, expected)
    assert.equal(result.rows[0].persona_verdict, 'PASS')
    assert.equal(result.totals[expected], 10)
    assert.deepEqual(result.uncovered, [])
  }
})

test('explicit coverage gaps override contradictory passes and retain uncovered rows', () => {
  const value = input()
  value.walkReceipt.evidence.coverage_gaps = [{ ...context }]
  value.walkReceipt.coverage_gaps = [{ feature_id: context.feature_id, reason: 'No test result: ready @desktop' }]
  let result = build(value)
  assert.equal(result.totals.UNKNOWN, 10)
  assert.equal(result.uncovered.length, 10)
  assert.ok(result.uncovered.every((row) => row.reason === 'coverage_gap' && row.verdict === 'UNKNOWN'))
  value.walkReceipt.evidence.coverage_gaps = []
  assert.equal(build(value).totals.UNKNOWN, 10, 'wire-only gaps cannot be ignored')
  value.walkReceipt.evidence.cases[0].verdict = 'fail'
  result = build(value)
  assert.equal(result.totals.FAIL, 10)
  assert.equal(result.rows[0].persona_verdict, 'PASS')
})

test('base failures and unavailable cases dominate persona results without inventing findings', () => {
  const value = input()
  value.evaluations[0].lenses[0].status = 'finding'
  value.walkReceipt.evidence.cases[0].verdict = 'staging_only'
  assert.deepEqual(build(value).rows[0], { ...expectedRow, case_verdict: 'UNAVAILABLE',
    persona_verdict: 'FAIL', verdict: 'UNAVAILABLE' })
  value.evaluations = []
  value.walkReceipt.evidence.cases[0].verdict = 'fail'
  assert.deepEqual(build(value).rows[0], { ...expectedRow, case_verdict: 'FAIL',
    persona_verdict: 'UNKNOWN', verdict: 'FAIL', evidence_ref: null })
})

test('lossless gap triples stay narrow when the wire gap has only a feature id', () => {
  const value = input()
  value.featureMap.entries[0].states.push('open')
  value.walkReceipt.evidence.coverage_gaps = [{ ...context, state: 'open' }]
  value.walkReceipt.coverage_gaps = [{ feature_id: context.feature_id, reason: 'No result for open' }]
  const result = build(value)
  assert.deepEqual(result.totals, { PASS: 10, FAIL: 0, UNKNOWN: 10, UNAVAILABLE: 0 })
  assert.equal(result.uncovered.length, 10)
  assert.ok(result.uncovered.every((row) => row.state === 'open' && row.reason === 'coverage_gap'))
})

test('catalog and all five evaluation identity fields must match structurally', () => {
  const value = input()
  value.evaluations[0].identity = { attempt: 2, browser_version: 'chromium-1', catalog_version: 'catalog-1',
    fixture_version: 'fixture-1', deployment_identity: { stack: { version: 1, name: 'studio' }, commit: 'abc' } }
  assert.equal(build(value).totals.PASS, 10)
  for (const field of Object.keys(identity)) {
    const candidate = input()
    candidate.evaluations[0].identity[field] = 'different'
    assert.throws(() => build(candidate), /identity/)
    delete candidate.evaluations[0].identity[field]
    assert.throws(() => build(candidate), /identity/)
  }
  value.featureMap.catalog_version = 'other'
  assert.throws(() => build(value), /catalog_version/)
  const unknown = input()
  delete unknown.walkReceipt.browser_version
  unknown.evaluations = []
  assert.equal(build(unknown).browser_version, null)
})

test('duplicate and foreign inventory/evaluation triples, lenses and statuses are rejected', () => {
  const invalid = (mutate, pattern) => {
    const value = input()
    mutate(value)
    assert.throws(() => build(value), pattern)
  }
  invalid((v) => v.featureMap.entries.push(structuredClone(entry)), /Duplicate inventory triple/)
  invalid((v) => v.featureMap.entries[0].states.push('ready'), /Duplicate inventory triple/)
  invalid((v) => v.evaluations.push(evaluation()), /Duplicate triple\/lens/)
  invalid((v) => v.evaluations[0].lenses.push({ lens: 'accessibility', status: 'pass' }), /Duplicate triple\/lens/)
  invalid((v) => { v.evaluations[0].feature_id = 'action:foreign' }, /unknown inventory triple/)
  invalid((v) => { v.evaluations[0].viewport = 'phone' }, /unknown inventory triple/)
  invalid((v) => { v.evaluations[0].lenses[0].lens = 'invented' }, /Unknown lens/)
  invalid((v) => { v.evaluations[0].lenses[0].status = 'PASS' }, /Invalid lens status/)
  invalid((v) => { v.evaluations[0].evidence_ref = '' }, /evidence_ref/)
  invalid((v) => { v.evaluations[0].evidence_ref = '../private' }, /evidence_ref/)
})

test('malformed required arrays and identifiers fail descriptively', () => {
  for (const [mutate, pattern] of [
    [(v) => { v.featureMap.entries = {} }, /entries.*array/],
    [(v) => { v.featureMap.entries[0].states = [] }, /states.*array/],
    [(v) => { v.featureMap.entries[0].states = [null] }, /state/],
    [(v) => { v.featureMap.entries[0].viewports = null }, /viewports.*array/],
    [(v) => { v.featureMap.entries[0].id = 'action:*' }, /feature id/],
    [(v) => { v.featureMap.entries[0].state_viewports = { ready: [] } }, /array/],
    [(v) => { v.featureMap.entries[0].state_viewports = { foreign: ['phone'] } }, /unknown state/],
    [(v) => { v.walkReceipt.evidence.cases = null }, /cases.*array/],
    [(v) => { v.walkReceipt.evidence.coverage_gaps = null }, /coverage_gaps.*array/],
    [(v) => { v.walkReceipt.schema = 'other' }, /leaf.studio-walk.v1/],
    [(v) => { v.evaluations = null }, /evaluations.*array/],
    [(v) => { v.evaluations[0].lenses = null }, /lenses.*array/],
  ]) {
    const value = input()
    mutate(value)
    assert.throws(() => build(value), pattern)
  }
})

test('real evaluateLenses output is compatible, including unknown modality gaps', () => {
  const good = JSON.parse(readFileSync(new URL('../e2e/walk/lenses/fixtures/good.json', import.meta.url), 'utf8')).cases
  const evidence = structuredClone(Object.values(good)[0])
  evidence.measurements = {}
  for (const sample of Object.values(good)) for (const [key, value] of Object.entries(sample.measurements)) {
    evidence.measurements[key] = value && typeof value === 'object' && !Array.isArray(value)
      ? { ...evidence.measurements[key], ...structuredClone(value) } : structuredClone(value)
  }
  const value = input()
  value.featureMap.entries[0].viewports = ['phone']
  value.walkReceipt.evidence.cases[0].viewport = 'phone'
  value.evaluations = [evaluation({ viewport: 'phone', lenses: evaluateLenses(evidence) })]
  assert.deepEqual(build(value).totals, { PASS: 10, FAIL: 0, UNKNOWN: 0, UNAVAILABLE: 0 })
  value.evaluations[0].lenses = evaluateLenses({ schema: 'leaf.walk-evidence.v1', featureId: 'action:open', state: 'ready', viewport: 'phone' })
  assert.deepEqual(build(value).totals, { PASS: 0, FAIL: 0, UNKNOWN: 10, UNAVAILABLE: 0 })
})

function freeze(value) {
  if (value && typeof value === 'object') {
    Object.values(value).forEach(freeze)
    Object.freeze(value)
  }
  return value
}

test('output order is deterministic, inputs stay immutable and identity objects are detached', () => {
  const value = input()
  value.featureMap.entries.push({ id: 'action:z', states: ['z', 'A'], viewports: ['phone', 'desktop'] })
  const snapshot = structuredClone(value)
  freeze(value)
  const result = build(value)
  assert.deepEqual(value, snapshot)
  assert.equal(result.denominator, 50)
  assert.equal(Object.values(result.totals).reduce((sum, n) => sum + n, 0), 50)
  assert.deepEqual(result.rows[10], { feature_id: 'action:z', state: 'A', viewport: 'desktop', persona: 'accessibility',
    case_verdict: 'UNKNOWN', persona_verdict: 'UNKNOWN', verdict: 'UNKNOWN', evidence_ref: null })
  const reordered = structuredClone(snapshot)
  reordered.featureMap.entries.reverse()
  reordered.featureMap.entries[0].states.reverse()
  reordered.featureMap.entries[0].viewports.reverse()
  reordered.evaluations[0].lenses.reverse()
  assert.equal(JSON.stringify(build(reordered)), JSON.stringify(result))
  result.deployment_identity.stack.name = 'changed'
  assert.equal(value.walkReceipt.deployment_identity.stack.name, 'studio')
})
