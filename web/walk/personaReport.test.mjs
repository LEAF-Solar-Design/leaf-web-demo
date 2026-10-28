import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { evaluateLenses, lenses } from '../e2e/walk/lenses/index.mjs'
import { buildPersonaCoverage } from './personaCoverage.mjs'
import { buildPersonaReport } from './personaReport.mjs'

// Synthetic evaluator fixtures exercise the adapter, not live Studio coverage.
const fixtures = Object.fromEntries(['good', 'bad'].map((name) => [name,
  JSON.parse(readFileSync(new URL(`../e2e/walk/lenses/fixtures/${name}.json`, import.meta.url), 'utf8')).cases]))
const identity = { deployment_identity: { commit: 'abc', stack: { name: 'studio', version: 1 } },
  fixture_version: 'fixture-1', catalog_version: 'catalog-1', browser_version: 'chromium-1', attempt: 2 }
const context = { feature_id: 'action:open', state: 'ready', viewport: 'phone' }
function evidence(kind = 'good', triple = context) {
  const result = { schema: 'leaf.walk-evidence.v1', featureId: triple.feature_id,
    state: triple.state, viewport: triple.viewport, measurements: {} }
  for (const sample of Object.values(fixtures[kind])) for (const [key, value] of Object.entries(sample.measurements)) {
    result.measurements[key] = value && typeof value === 'object' && !Array.isArray(value)
      ? { ...result.measurements[key], ...structuredClone(value) } : structuredClone(value)
  }
  return result
}
const run = (kind = 'good', triple = context, evidence_ref = 'shard-1/attempt-1.json') => ({ ...triple,
  identity: structuredClone(identity), evidence_ref, evidence: evidence(kind, triple) })
function input() {
  return { featureMap: { catalog_version: 'catalog-1', entries: [
    { id: context.feature_id, states: ['ready'], viewports: ['phone'] }] },
  walkReceipt: { schema: 'leaf.studio-walk.v1', ...structuredClone(identity), coverage_gaps: [],
    evidence: { cases: [{ ...context, verdict: 'pass' }], coverage_gaps: [] } }, runs: [run()] }
}
const build = (value = input()) => buildPersonaReport(value)
const verdicts = { pass: 'PASS', finding: 'FAIL', unknown: 'UNKNOWN' }
function assertRealResults(result, sample) {
  const expected = new Map(evaluateLenses(sample).map(({ lens, status }) => [lens, verdicts[status]]))
  assert.equal(result.rows.length, lenses.length)
  for (const row of result.rows) {
    assert.equal(row.persona_verdict, expected.get(row.persona))
    assert.equal(row.verdict, expected.get(row.persona))
  }
}

test('real pass, finding and unknown results are adapted without evidence or finding prose', () => {
  for (const kind of ['good', 'bad', 'unknown']) {
    const value = input()
    if (kind === 'bad') value.runs = [run('bad')]
    if (kind === 'unknown') delete value.runs[0].evidence.measurements
    value.runs[0].evidence.private = 'PRIVATE-EVIDENCE'
    if (kind === 'bad') value.runs[0].evidence.measurements.humanFeedback.quote = 'PRIVATE-FEEDBACK'
    const result = build(value)
    assertRealResults(result, value.runs[0].evidence)
    assert.equal(result.totals[verdicts[kind === 'good' ? 'pass' : kind === 'bad' ? 'finding' : 'unknown']], 10)
    assert.ok(!JSON.stringify(result).includes('PRIVATE-'))
    assert.ok(!JSON.stringify(result).includes('finding'))
    assert.deepEqual(JSON.parse(JSON.stringify(result)), result)
  }
})

test('ordinary runner evidence without measurements leaves every lens unknown', () => {
  const value = input()
  value.runs[0].evidence = { schema: 'leaf.walk-evidence.v1', featureId: context.feature_id,
    state: context.state, viewport: context.viewport, steps: [], consoleErrors: [], pageErrors: [],
    failedRequests: [], httpErrors: [], accessibility: null }
  assertRealResults(build(value), value.runs[0].evidence)
  assert.equal(build(value).totals.UNKNOWN, 10)
})

test('missing evidence context and measurement fields are never filled from controller metadata', () => {
  for (const field of ['featureId', 'state', 'viewport']) {
    const value = input()
    delete value.runs[0].evidence[field]
    const snapshot = structuredClone(value)
    assert.equal(build(value).totals.UNKNOWN, 10)
    assert.deepEqual(value, snapshot)
    assert.ok(!Object.hasOwn(value.runs[0].evidence, field))
  }
  const value = input()
  delete value.runs[0].evidence.measurements.reference
  assert.equal(build(value).totals.UNKNOWN, 10)
  const partial = input()
  delete partial.runs[0].evidence.measurements.focus.actualReturnTarget
  const result = build(partial)
  assert.equal(result.rows.find((row) => row.persona === 'accessibility').persona_verdict, 'UNKNOWN')
  assert.equal(result.totals.PASS, 9)
})

test('malformed runs, schemas, supplied mismatches and foreign triples throw', () => {
  for (const mutate of [
    (v) => { v.runs = null }, (v) => { v.runs = {} }, (v) => { v.runs = [null] },
    (v) => { v.runs = [[]] }, (v) => { delete v.runs[0].evidence },
    (v) => { v.runs[0].evidence = [] }, (v) => { delete v.runs[0].evidence.schema },
    (v) => { v.runs[0].evidence.schema = 'other' },
    ...['feature_id', 'state', 'viewport'].map((field) => (v) => { delete v.runs[0][field] }),
    ...['featureId', 'state', 'viewport'].map((field) => (v) => { v.runs[0].evidence[field] = 'foreign' }),
    (v) => { v.runs[0] = run('good', { ...context, feature_id: 'action:foreign' }) },
    (v) => { v.runs[0] = run('good', { ...context, state: 'foreign' }) },
    (v) => { v.runs[0] = run('good', { ...context, viewport: 'desktop' }) },
  ]) {
    const value = input()
    mutate(value)
    assert.throws(() => build(value))
  }
})

test('all five receipt identity fields match structurally and catalog mismatch is not repaired', () => {
  const value = input()
  value.runs[0].identity = { attempt: 2, browser_version: 'chromium-1', catalog_version: 'catalog-1',
    fixture_version: 'fixture-1', deployment_identity: { stack: { version: 1, name: 'studio' }, commit: 'abc' } }
  assert.equal(build(value).totals.PASS, 10)
  for (const field of Object.keys(identity)) for (const mode of ['different', 'missing']) {
    const candidate = input()
    if (mode === 'missing') delete candidate.runs[0].identity[field]
    else candidate.runs[0].identity[field] = 'foreign'
    assert.throws(() => build(candidate), /identity/)
  }
  for (const malformed of [null, [], { ...identity, extra: true }]) {
    const candidate = input()
    candidate.runs[0].identity = malformed
    assert.throws(() => build(candidate), /identity/)
  }
  value.featureMap.catalog_version = 'foreign'
  const snapshot = structuredClone(value)
  assert.throws(() => build(value), /catalog_version/)
  assert.deepEqual(value, snapshot)
  value.runs = []
  assert.throws(() => build(value), /catalog_version/)
})

test('every source reference is validated, including a retry that cannot change findings', () => {
  for (const evidence_ref of ['', ' ', '../private', 'a/../b', 'https://example.com/a', '/absolute',
    'a\\b', 'a?token=secret', 'a\nprivate', 'a'.repeat(2001), null, 42]) {
    for (const reverse of [false, true]) {
      const value = input()
      value.runs = [run('bad'), run('good', context, evidence_ref)]
      if (reverse) value.runs.reverse()
      assert.throws(() => build(value), /evidence_ref/)
    }
  }
  const value = input()
  value.runs.push(run('good'))
  delete value.runs[1].evidence_ref
  assert.throws(() => build(value), /evidence_ref/)
})

test('retry reduction keeps finding before unknown before pass in either attempt order', () => {
  for (const kinds of [['bad', 'good'], ['unknown', 'good'], ['bad', 'unknown'], ['good', 'unknown', 'bad']]) {
    const value = input()
    value.runs = kinds.map((kind, i) => {
      const attempt = run(kind === 'bad' ? 'bad' : 'good', context, `shard-${i}/trace.json`)
      if (kind === 'unknown') delete attempt.evidence.measurements
      return attempt
    })
    const expected = kinds.includes('bad') ? 'FAIL' : 'UNKNOWN'
    const result = build(value)
    assert.equal(result.denominator, 10)
    assert.equal(result.totals[expected], 10)
    value.runs.reverse()
    assert.deepEqual(build(value), result)
  }
  const value = input()
  const partial = run('good', context, 'retry/partial.json')
  delete partial.evidence.measurements.focus
  value.runs.push(partial)
  const result = build(value)
  assert.equal(result.totals.UNKNOWN, 1)
  assert.equal(result.totals.PASS, 9, 'reduction is per lens rather than per attempt')
})

test('disjoint shard runs use one final receipt, global denominator, base verdicts and gaps', () => {
  const value = input()
  const second = { feature_id: 'action:save', state: 'ready', viewport: 'phone' }
  value.featureMap.entries.push({ id: second.feature_id, states: ['ready', 'missing'], viewports: ['phone'] })
  value.walkReceipt.evidence.cases.push({ ...second, verdict: 'pass' })
  const shardOne = [run()]
  const shardTwo = [run('good', second, 'shard-2/trace.json')]
  value.runs = [...shardOne, ...shardTwo]
  let result = build(value)
  assert.equal(result.denominator, 30)
  assert.deepEqual(result.totals, { PASS: 20, FAIL: 0, UNKNOWN: 10, UNAVAILABLE: 0 })
  assert.ok(result.uncovered.every((row) => row.state === 'missing' && row.reason === 'missing_case'))
  value.walkReceipt.evidence.coverage_gaps.push({ ...second })
  result = build(value)
  assert.equal(result.totals.UNKNOWN, 20)
  assert.ok(result.uncovered.filter((row) => row.state === 'ready').every((row) => row.reason === 'coverage_gap'))
  for (const [base, expected] of [['fail', 'FAIL'], ['staging_only', 'UNAVAILABLE'], ['queued', 'UNKNOWN']]) {
    value.walkReceipt.evidence.cases[0].verdict = base
    result = build(value)
    assert.ok(result.rows.filter((row) => row.feature_id === context.feature_id)
      .every((row) => row.case_verdict === expected && row.verdict === expected && row.persona_verdict === 'PASS'))
  }
})

test('no runs returns the ordinary uncovered companion with no bundles', () => {
  const value = input()
  delete value.runs
  assert.deepEqual(build(value), { ...buildPersonaCoverage(value), evidence_bundles: [] })
  assert.equal(build(value).totals.UNKNOWN, 10)
  assert.ok(build(value).uncovered.every((row) => row.reason === 'missing_persona'))
})

function freeze(value) {
  if (value && typeof value === 'object') {
    Object.values(value).forEach(freeze)
    Object.freeze(value)
  }
  return value
}

test('bundle IDs follow code-point triple order and preserve sorted unique sources immutably', () => {
  const value = input()
  value.featureMap.entries[0].states = ['ready', 'A']
  value.featureMap.entries[0].viewports = ['phone', 'desktop']
  const triples = [context, { ...context, state: 'A' }, { ...context, state: 'A', viewport: 'desktop' }]
  value.runs = triples.map((triple, i) => run('good', triple, `sources/${i}.json`))
  value.runs.push(run('good', context, 'sources/Z.json'), run('good', context, 'sources/a.json'), run())
  value.runs.push(run())
  const snapshot = structuredClone(value)
  freeze(value)
  const result = build(value)
  assert.deepEqual(value, snapshot)
  assert.deepEqual(result.evidence_bundles, [
    { evidence_ref: 'persona-attempts/000001', sources: ['sources/2.json'] },
    { evidence_ref: 'persona-attempts/000002', sources: ['sources/1.json'] },
    { evidence_ref: 'persona-attempts/000003', sources: ['shard-1/attempt-1.json', 'sources/0.json', 'sources/Z.json', 'sources/a.json'] },
  ])
  for (const row of result.rows) {
    const expected = row.state === 'A' ? (row.viewport === 'desktop' ? 'persona-attempts/000001' : 'persona-attempts/000002')
      : row.viewport === 'phone' ? 'persona-attempts/000003' : null
    assert.equal(row.evidence_ref, expected)
  }
  const reordered = structuredClone(snapshot)
  reordered.runs.reverse()
  reordered.featureMap.entries.reverse()
  reordered.featureMap.entries[0].states.reverse()
  reordered.featureMap.entries[0].viewports.reverse()
  reordered.walkReceipt.evidence.cases.reverse()
  assert.equal(JSON.stringify(build(reordered)), JSON.stringify(result))
  result.deployment_identity.stack.name = 'changed'
  result.evidence_bundles[0].sources.push('other/source')
  assert.deepEqual(value, snapshot)
})
