import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, existsSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { buildFeatureMap } from './featureMap.mjs'
import { stateRecipe } from '../e2e/walk/probes.mjs'
import { uxObservation } from '../e2e/walk/uxEvidence.mjs'
import { buildReceipt, validateReceipt, redact, artifactReference, MAX_INPUT_BYTES, MAX_RECEIPT_BYTES } from './report.mjs'

const sample = JSON.parse(readFileSync(new URL('./fixtures/walk-report.sample.json', import.meta.url), 'utf8'))
const schema = JSON.parse(readFileSync(new URL('./fixtures/leaf.studio-walk.v1.schema.json', import.meta.url), 'utf8'))
// Captured from the W1c feature map alongside walk-report.sample.json.
const map = JSON.parse(readFileSync(new URL('./fixtures/feature-map.sample.json', import.meta.url), 'utf8'))
const identity = { deployment_identity: { commit: 'fixture-commit', bundle: 'fixture-bundle' },
  fixture_version: 'walk-fixture-v1', catalog_version: map.catalog_version, browser_version: 'chromium-fixture', attempt: 1 }
const build = (playwrightReport = sample, featureMap = map, runIdentity = identity) => buildReceipt({ playwrightReport, featureMap, identity: runIdentity })
const allSpecs = (suites) => suites.flatMap((suite) => [...(suite.specs || []), ...allSpecs(suite.suites || [])])
const featureSpec = (report, id = 'drawer:nav', state = 'closed') => allSpecs(report.suites).find((spec) => spec.title === `${id} [${state}] @desktop`)
const firstResult = (spec) => spec.tests[0].results[0]
const setErrorMessage = (result, message) => {
  result.error = { ...result.error, message }
  result.errors = [result.error]
}
const attach = (data) => ({ name: 'walk-evidence', contentType: 'application/json', body: Buffer.from(JSON.stringify(data)).toString('base64') })
test('control-census titles produce desktop and phone verdicts and a trusted failure oracle', () => {
  const report = { suites: [{ specs: [
    { title: 'control-census:studio [ready] @desktop', tests: [{ projectName: 'desktop', results: [{ status: 'passed' }] }] },
    { title: 'control-census:studio [failed-load] @phone', tests: [{ projectName: 'phone', results: [{ status: 'failed',
      error: { message: 'unmapped: scope=document role=button name=New control' } }] }] },
  ] }] }
  const receipt = build(report)
  assert.deepEqual(receipt.evidence.cases.map((row) => [row.feature_id, row.state, row.viewport, row.verdict]), [
    ['control-census:studio', 'ready', 'desktop', 'pass'], ['control-census:studio', 'failed-load', 'phone', 'fail'],
  ])
  assert.deepEqual(receipt.verdicts.map((row) => row.verdict), ['PASS', 'FAIL'])
  assert.equal(receipt.failures[0].fixture_recipe.name, 'control-census')
  assert.match(receipt.failures[0].evidence.assertion_site, /^web\/e2e\/walk\/control-inventory\.spec\.mjs:\d+$/)
  assert.match(receipt.failures[0].expected_effect, /mapped-or-baselined/)
  assert.equal(validateReceipt(receipt).valid, true)
  report.suites[0].specs[0].tests[0].projectName = 'phone'
  assert.throws(() => build(report), /project does not match/)
})

const tripleKey = ({ feature_id, state, viewport }) => JSON.stringify([feature_id, state, viewport])
const jsonLeaves = (value, path = []) => typeof value === 'string' ? [{ path, value }]
  : value && typeof value === 'object' ? Object.entries(value).flatMap(([key, item]) => jsonLeaves(item, [...path, key])) : []
const withoutCreatedAt = (receipt) => { const { created_at, ...rest } = receipt; return rest }

const stackReport = (stacks) => ({ suites: [{ specs: stacks.map((stack, index) => ({
  title: `control-census:stack-${index} [ready] @desktop`,
  tests: [{ projectName: 'desktop', results: [{ status: index === 0 ? 'failed' : 'passed',
    attachments: [attach(stack === undefined ? {} : { stack })] }] }],
})) }] })
const stackRefA = 'a'.repeat(64)
const stackRefB = 'b'.repeat(64)

test('all evidence runs must be ready; stack refs are sorted and distinct and failures keep their own witness', () => {
  const receipt = build(stackReport([
    { ready: true, instance: stackRefB }, { ready: true, instance: stackRefA }, { ready: true, instance: stackRefB },
  ]))
  assert.deepEqual(receipt.evidence.context, { stack_ready: true, stack_refs: [stackRefA, stackRefB] })
  assert.deepEqual(receipt.failures[0].evidence.context, { stack_ready: true, stack_ref: stackRefB })
  assert.deepEqual(validateReceipt(receipt), { valid: true, errors: [] })
})

test('one unready run overrides ready runs and a failed boot carries no invented ref', () => {
  const receipt = build(stackReport([{ ready: false }, { ready: true, instance: stackRefA }]))
  assert.deepEqual(receipt.evidence.context, { stack_ready: false, stack_refs: [stackRefA] })
  assert.deepEqual(receipt.failures[0].evidence.context, { stack_ready: false })
  assert.deepEqual(validateReceipt(receipt), { valid: true, errors: [] })
})

test('partial stack witnesses leave aggregate readiness unknown', () => {
  const report = stackReport([{ ready: true, instance: stackRefA }, undefined])
  const receipt = build(report)
  assert.deepEqual(receipt.evidence.context, { stack_refs: [stackRefA] })
  assert.deepEqual(validateReceipt(receipt), { valid: true, errors: [] })
  report.suites[0].specs[1].tests[0].results[0].attachments = []
  const withoutEvidence = build(report)
  assert.equal(withoutEvidence.evidence.context.stack_ready, true)
  assert.deepEqual(validateReceipt(withoutEvidence), { valid: true, errors: [] })
})

test('the frozen report without stack witnesses keeps its receipt unchanged', () => {
  const frozen = JSON.parse(readFileSync(new URL('./fixtures/walk-report.sample.json', import.meta.url), 'utf8'))
  const receipt = build(frozen)
  assert.deepEqual(withoutCreatedAt(receipt), withoutCreatedAt(build(sample)))
  assert.ok(!Object.hasOwn(receipt.evidence, 'context'))
  assert.ok(receipt.failures.every((failure) => !Object.hasOwn(failure.evidence, 'context')))
  assert.deepEqual(validateReceipt(receipt), { valid: true, errors: [] })
})

test('malformed stack witnesses name their feature and distinct ref overflow names its count', () => {
  for (const stack of [null, [], true, {}, { ready: 'true', instance: stackRefA },
    { ready: true }, { ready: false, instance: 1 }, { ready: true, instance: 'A'.repeat(64) },
    { ready: true, instance: 'a'.repeat(63) }, { ready: true, instance: 'g'.repeat(64) }]) {
    assert.throws(() => build(stackReport([stack])), /Invalid stack witness for control-census:stack-0/)
  }
  const stacks = Array.from({ length: 17 }, (_, index) => ({ ready: true, instance: index.toString(16).padStart(64, '0') }))
  const accepted = build(stackReport(stacks.slice(0, 16)))
  assert.equal(accepted.evidence.context.stack_refs.length, 16)
  assert.deepEqual(validateReceipt(accepted), { valid: true, errors: [] })
  assert.throws(() => build(stackReport(stacks)), /17 distinct instance refs/)
})

test('typed UX observations project only positive count metrics and preserve the functional receipt', () => {
  const report = structuredClone(sample)
  const result = firstResult(featureSpec(report))
  const attachment = result.attachments.find((item) => item.name === 'walk-evidence')
  const evidence = JSON.parse(Buffer.from(attachment.body, 'base64').toString('utf8'))
  const positive = uxObservation({ lensId: 'end-user', metricId: 'control_covered_at_rest', viewport: 'desktop', state: 'closed', observed: 1 })
  const zero = uxObservation({ lensId: 'mobile-touch', metricId: 'scroll_needed_steps', viewport: 'desktop', state: 'closed', observed: 0 })
  evidence.ux_observations = [positive, zero]
  attachment.body = attach(evidence).body
  const receipt = build(report)
  assert.deepEqual(receipt.findings.filter((row) => row.category === 'ux'), [{ feature_id: 'drawer:nav',
    assertion_id: 'ux-control-covered-at-rest', category: 'ux', summary: 'control_covered_at_rest = 1 at desktop/closed', evidence: positive }])
  const detail = receipt.evidence.cases.find((row) => row.feature_id === 'drawer:nav' && row.state === 'closed' && row.viewport === 'desktop')
  assert.deepEqual(detail.ux_observations, [positive, zero])
  assert.deepEqual(validateReceipt(receipt), { valid: true, errors: [] })
  receipt.findings = receipt.findings.filter((row) => row.category !== 'ux')
  delete detail.ux_observations
  assert.deepEqual(withoutCreatedAt(receipt), withoutCreatedAt(build(sample)))
})

test('UX report validation names the feature for bad shapes, identifiers and overflow', () => {
  const row = uxObservation({ lensId: 'end-user', metricId: 'extra_steps', viewport: 'desktop', state: 'closed', observed: 1 })
  for (const ux_observations of [[{ ...row, lens_id: 'bad.id' }], [{ ...row, observed: true }],
    [{ ...row, threshold: 0 }], [{ ...row, ux_version: 2 }], [null], Array(65).fill(row), {}]) {
    const report = structuredClone(sample)
    firstResult(featureSpec(report)).attachments = [attach({ ux_observations })]
    assert.throws(() => build(report), /drawer:nav/)
  }
})

test('UX unknown, zero and time observations are retained without findings; all count aliases project', () => {
  const rows = ['control_covered_at_rest', 'scroll_needed_steps', 'extra_steps', 'time_to_task_ms'].flatMap((metricId) =>
    [null, 0, 2].map((observed) => uxObservation({ lensId: 'end-user', metricId, viewport: 'desktop', state: 'closed', observed })))
  const report = structuredClone(sample)
  firstResult(featureSpec(report)).attachments = [attach({ ux_observations: rows })]
  const receipt = build(report)
  assert.deepEqual(receipt.findings.filter((row) => row.category === 'ux').map((row) => row.assertion_id),
    ['ux-control-covered-at-rest', 'ux-scroll-needed-steps', 'ux-extra-steps'])
  assert.deepEqual(receipt.evidence.cases.find((row) => row.feature_id === 'drawer:nav' && row.state === 'closed').ux_observations, rows)
  assert.equal(validateReceipt(receipt).valid, true)
})

test('the frozen report fixture without UX observations keeps its receipt projection unchanged', () => {
  const frozen = JSON.parse(readFileSync(new URL('./fixtures/walk-report.sample.json', import.meta.url), 'utf8'))
  const receipt = build(frozen)
  assert.deepEqual(withoutCreatedAt(receipt), withoutCreatedAt(build(sample)))
  assert.ok(receipt.evidence.cases.every((row) => !Object.hasOwn(row, 'ux_observations')))
  assert.ok(receipt.findings.every((row) => row.category !== 'ux'))
})

test('a synthesized report builds a valid receipt against the live feature map', () => {
  const liveMap = buildFeatureMap()
  const entry = liveMap.entries[0]
  const state = entry.states[0]
  const viewport = entry.viewports[0]
  const report = { suites: [{ specs: [{ title: `${entry.id} [${state}] @${viewport}`,
    tests: [{ expectedStatus: 'passed', results: [{ status: 'passed', duration: 1, attachments: [] }] }],
  }] }] }
  const receipt = build(report, liveMap, { ...identity, catalog_version: liveMap.catalog_version })
  assert.deepEqual(validateReceipt(receipt, schema), { valid: true, errors: [] })
  assert.equal(receipt.catalog_version, liveMap.catalog_version)
  assert.equal(receipt.evidence.cases.length, 1)
  assert.deepEqual(receipt.evidence.cases.map(tripleKey), [tripleKey({ feature_id: entry.id, state, viewport })])
  const expected = liveMap.entries.flatMap((row) => row.states.flatMap((s) => row.viewports.map((v) =>
    tripleKey({ feature_id: row.id, state: s, viewport: v }))))
  const seen = [...receipt.evidence.cases, ...receipt.evidence.coverage_gaps].map(tripleKey)
  assert.deepEqual([...seen].sort(), [...expected].sort())
  assert.equal(new Set(seen).size, seen.length)
})

test('real runner report produces a receipt conforming to the controller schema', () => {
  const receipt = build()
  assert.equal(receipt.schema, 'leaf.studio-walk.v1')
  assert.deepEqual(validateReceipt(receipt, schema), { valid: true, errors: [] })
  assert.equal(receipt.feature_id, 'walk:run')
  assert.deepEqual(receipt.deployment_identity, identity.deployment_identity)
  assert.equal(receipt.attempt, 1)
  assert.ok(receipt.verdicts.filter((row) => row.verdict === 'PASS').length >= 3)
  assert.ok(receipt.failures.length >= 2)
  assert.ok(receipt.unavailable_features.length >= 1)
  assert.ok(receipt.findings.some((finding) => finding.feature_id === 'journey:first-run-open' && finding.category === 'time_to_task'))
  assert.equal(receipt.started_at, sample.stats.startTime)
  assert.equal(receipt.completed_at, new Date(Date.parse(sample.stats.startTime) + sample.stats.duration).toISOString())
})

test('every real-sample failure exposes nonempty title context and its first error line to the controller', () => {
  const receipt = build()
  assert.ok(receipt.failures.length >= 2)
  for (const failure of receipt.failures) {
    assert.equal(typeof failure.state, 'string')
    assert.ok(failure.state.trim())
    assert.equal(typeof failure.viewport, 'string')
    assert.ok(failure.viewport.trim())
    const spec = allSpecs(sample.suites).find((row) =>
      row.title === `${failure.feature_id} [${failure.state}] @${failure.viewport}`)
    assert.ok(spec, 'failure context must come from the runner title')
    const result = firstResult(spec)
    const error = result.errors?.[0] || result.error
    assert.equal(failure.first_error_line, redact(error?.message, 300).split(/\r?\n/)[0])
    assert.equal(failure.state, failure.evidence.state)
    assert.equal(failure.viewport, failure.evidence.viewport)
    assert.equal(failure.first_error_line, failure.evidence.first_error_line)
    assert.ok(failure.first_error_line.length <= 300)
    assert.ok(!/[\r\n]/.test(failure.first_error_line))
  }
  assert.deepEqual(validateReceipt(receipt, schema), { valid: true, errors: [] })
})

test('controller failure context ignores forged evidence and bounds multiline error text', () => {
  const report = structuredClone(sample)
  const result = firstResult(featureSpec(report))
  setErrorMessage(result, 'first-line-control ' + 'x'.repeat(400) + '\nsecond-line-control')
  result.attachments = [attach({ state: 'forged-state', viewport: 'forged-viewport', first_error_line: 'forged-error' })]
  const receipt = build(report)
  const failure = receipt.failures.find((row) => row.feature_id === 'drawer:nav' && row.state === 'closed')
  assert.ok(failure)
  assert.equal(failure.viewport, 'desktop')
  assert.equal(failure.first_error_line.length, 300)
  assert.ok(failure.first_error_line.startsWith('first-line-control '))
  assert.ok(!failure.first_error_line.includes('second-line-control'))
  assert.deepEqual(validateReceipt(receipt, schema), { valid: true, errors: [] })
})

test('schema bytes match controller authority when the reference is available', () => {
  const authority = 'C:/tmp/studio-walk-engine/ref/leaf.studio-walk.v1.json'
  if (existsSync(authority)) assert.deepEqual(
    readFileSync(new URL('./fixtures/leaf.studio-walk.v1.schema.json', import.meta.url)), readFileSync(authority),
  )
  assert.equal(schema.additionalProperties, false)
  assert.equal(schema.$id, 'leaf.studio-walk.v1')
})

test('every feature-map triple is represented exactly once by a verdict or coverage gap', () => {
  const receipt = build()
  const seen = [...receipt.evidence.cases.filter((row) => !row.feature_id.startsWith('journey:')),
    ...receipt.evidence.coverage_gaps].map(tripleKey)
  const expected = map.entries.flatMap((entry) => entry.states.flatMap((state) => entry.viewports.map((viewport) =>
    tripleKey({ feature_id: entry.id, state, viewport }))))
  assert.deepEqual([...seen].sort(), [...expected].sort())
  assert.equal(new Set(seen).size, seen.length)
  assert.equal(receipt.coverage_gaps.length, receipt.evidence.coverage_gaps.length)
  const missing = structuredClone(sample)
  const original = featureSpec(missing)
  for (const suite of missing.suites) if (suite.specs?.includes(original)) suite.specs.splice(suite.specs.indexOf(original), 1)
  assert.ok(build(missing).evidence.coverage_gaps.some((gap) => tripleKey(gap) === tripleKey({ feature_id: 'drawer:nav', state: 'closed', viewport: 'desktop' })))
  assert.ok(receipt.evidence.cases.some((row) => row.feature_id === 'drawer:nav' && row.state === 'closed'))
})

test('failures preserve feature-map effects, probe recipes and bounded artifact references', () => {
  const receipt = build()
  for (const failure of receipt.failures) {
    assert.match(failure.assertion_id, /^assertion:[a-f0-9]{64}$/)
    assert.ok(failure.feature_id)
    const { state, viewport, first_error_line, assertion_site, references } = failure.evidence
    assert.ok(state)
    assert.equal(viewport, 'desktop')
    assert.ok(first_error_line.length <= 300)
    assert.match(assertion_site, /^web\/e2e\/walk\/.*\.mjs:\d+$/)
    const entry = map.entries.find((row) => row.id === failure.feature_id)
    assert.deepEqual(JSON.parse(failure.expected_effect), entry.expected_effect[state])
    const effect = entry.expected_effect[state]
    const expectedHash = createHash('sha256').update(JSON.stringify([effect.kind, effect.target ?? null, assertion_site])).digest('hex')
    assert.equal(failure.assertion_id, `assertion:${expectedHash}`)
    assert.equal(failure.fixture_recipe.name, 'stateRecipe')
    const recipe = stateRecipe(entry, state)
    assert.deepEqual(failure.fixture_recipe.parameters.context, recipe.context)
    assert.deepEqual(failure.fixture_recipe.parameters.steps, recipe.steps)
    assert.ok(references.length >= 1)
    for (const reference of references) {
      assert.ok(['trace', 'screenshot'].includes(reference.kind))
      assert.ok(reference.reference.startsWith('walk/'))
      assert.ok(!reference.reference.includes('..'))
      assert.ok(!/^[A-Za-z]:|^\//.test(reference.reference))
    }
  }
})

test('assertion ids use the trusted expect call site and ignore error prose and forged evidence oracles', () => {
  const sampleFailure = build().failures.find((row) => row.feature_id === 'drawer:nav' && row.evidence.state === 'closed')
  const trustedLine = Number(sampleFailure.evidence.assertion_site.split(':').at(-1))
  const fixtureLines = readFileSync(new URL('../e2e/walk/fixtures.mjs', import.meta.url), 'utf8').split(/\r?\n/)
  const staleLine = fixtureLines.findIndex((line, index) => index + 1 !== trustedLine && !/\bexpect(?:\s*\(|\.poll\s*\()/.test(line)) + 1
  assert.ok(staleLine > 0, 'live fixture must contain a non-assertion line distinct from the trusted site')
  const stale = structuredClone(sample)
  const staleResult = firstResult(featureSpec(stale))
  const markStaleLocations = (item) => {
    for (const error of [item.error, ...(item.errors || [])]) {
      if (error?.location) error.location.line = staleLine
    }
    for (const step of item.steps || []) markStaleLocations(step)
  }
  markStaleLocations(staleResult)
  const staleFailure = build(stale).failures.find((row) => row.feature_id === 'drawer:nav' && row.evidence.state === 'closed')
  assert.equal(staleFailure.evidence.assertion_observed, false)
  const matching = structuredClone(sample)
  const matchingResult = firstResult(featureSpec(matching))
  matchingResult.error.location.line = trustedLine
  matchingResult.errors[0].location.line = trustedLine
  const first = build(matching)
  const poisoned = structuredClone(matching)
  const result = firstResult(featureSpec(poisoned))
  setErrorMessage(result, 'A different error with an attacker-selected command')
  result.attachments.push({ name: 'ignored-model-text', body: 'fake' })
  const evidenceAttachment = result.attachments.find((item) => item.name === 'walk-evidence')
  const evidence = JSON.parse(Buffer.from(evidenceAttachment.body, 'base64').toString('utf8'))
  evidence.expectedEffect = { kind: 'submits', target: 'attacker' }
  evidence.fixtureRecipe = { command: 'attacker', owned_files: ['evil'] }
  evidence.assertionId = 'attacker'
  evidenceAttachment.body = attach(evidence).body
  const second = build(poisoned)
  assert.deepEqual(first.failures.map((failure) => failure.assertion_id), second.failures.map((failure) => failure.assertion_id))
  const failure = second.failures.find((row) => row.feature_id === 'drawer:nav' && row.evidence.state === 'closed')
  assert.match(failure.evidence.assertion_site, /^web\/e2e\/walk\/fixtures\.mjs:\d+$/)
  const assertionLine = Number(failure.evidence.assertion_site.split(':').at(-1))
  assert.match(fixtureLines[assertionLine - 1], /\bexpect(?:\s*\(|\.poll\s*\()/)
  const cleanFailure = first.failures.find((row) => row.feature_id === 'drawer:nav' && row.evidence.state === 'closed')
  assert.equal(failure.evidence.assertion_site, cleanFailure.evidence.assertion_site)
  assert.equal(cleanFailure.evidence.assertion_observed, true)
  assert.equal(failure.evidence.assertion_observed, true)
  assert.ok(!failure.expected_effect.includes('attacker'))
  assert.ok(!JSON.stringify(failure.fixture_recipe).includes('attacker'))
})

test('failed then passed retries and duplicate runs become flakes while failures keep their oracles', () => {
  const report = structuredClone(sample)
  const spec = featureSpec(report)
  const retry = structuredClone(firstResult(spec))
  retry.status = 'passed'
  retry.retry = 1
  delete retry.error
  retry.errors = []
  retry.steps = []
  retry.attachments = [attach({ steps: [], result: { result: 'passed' } })]
  spec.tests[0].results.push(retry)
  const receipt = build(report)
  assert.ok(receipt.flakes.some((row) => row.feature_id === 'drawer:nav' && row.occurrences === 2))
  assert.equal(receipt.evidence.cases.find((row) => row.feature_id === 'drawer:nav' && row.state === 'closed').verdict, 'pass')
  assert.ok(receipt.failures.some((row) => row.feature_id === 'drawer:nav' && row.evidence.state === 'closed'))
  const duplicateReport = structuredClone(sample)
  const duplicate = structuredClone(featureSpec(duplicateReport))
  duplicate.tests[0].results = [retry]
  duplicateReport.suites[0].specs.push(duplicate)
  assert.ok(build(duplicateReport).evidence.flakes.some((row) => row.feature_id === 'drawer:nav' && row.state === 'closed'))
  assert.equal(build().flakes.length, 0)
})

test('unsupported, staging certifications and queued execution cannot become passes', () => {
  const receipt = build()
  const unsupported = receipt.evidence.cases.filter((row) => row.verdict === 'unsupported_local')
  assert.ok(unsupported.length > 0)
  for (const row of unsupported) {
    assert.ok(receipt.evidence.unavailable.some((item) => tripleKey(item) === tripleKey(row) && item.reason.includes('catalog')))
    assert.ok(receipt.unavailable_features.some((item) => item.feature_id === row.feature_id))
    assert.ok(!receipt.failures.some((item) => item.feature_id === row.feature_id && item.evidence.state === row.state))
  }
  const report = structuredClone(sample)
  const spec = featureSpec(report)
  const result = firstResult(spec)
  spec.tests[0].expectedStatus = 'failed'
  result.attachments = [attach({ certification: { result: 'staging', reason: 'Provider fixture needed' } })]
  setErrorMessage(result, 'CERTIFICATION_CLASS: staging: Provider fixture needed')
  assert.equal(build(report).evidence.cases.find((row) => row.feature_id === 'drawer:nav' && row.state === 'closed').verdict, 'staging_only')
  result.attachments = [attach({})]
  setErrorMessage(result, 'QUEUED: No local slots')
  assert.equal(build(report).evidence.cases.find((row) => row.feature_id === 'drawer:nav' && row.state === 'closed').verdict, 'queued')
  result.status = 'passed'
  delete result.error
  result.errors = []
  assert.equal(build(report).evidence.cases.find((row) => row.feature_id === 'drawer:nav' && row.state === 'closed').verdict, 'fail')
})

test('redaction strips credentials and user paths from all returned data, with a positive evidence control', () => {
  const fakeJwt = ['eyJhbGciOiJIUzI1NiJ9', 'eyJzdWIiOiJmaXh0dXJlIn0', 'fixtureSignature'].join('.')
  const userPath = ['C:', 'Users', 'fake-user', 'private', 'fixture.txt'].join('\\')
  const report = structuredClone(sample)
  const result = firstResult(featureSpec(report))
  setErrorMessage(result, `${fakeJwt} ${userPath} ${['Bearer', 'fixture-bearer'].join(' ')} ?token=fixture-query Cookie: fixture-cookie`)
  result.attachments = [attach({ consoleErrors: [{ text: `${fakeJwt} ${userPath}` }], steps: [{ text: fakeJwt }],
    storageState: { cookies: [{ value: fakeJwt }] }, modelText: userPath })]
  const runIdentity = { ...identity, deployment_identity: { commit: 'positive-control', note: `${fakeJwt} ${userPath}`,
    cookies: [{ value: fakeJwt }], storageState: { origins: [] } } }
  const receipt = build(report, map, runIdentity)
  const json = JSON.stringify(receipt)
  for (const secret of [fakeJwt, userPath, 'fixture-bearer', 'fixture-query', 'fixture-cookie']) assert.ok(!json.includes(secret), secret)
  assert.ok(jsonLeaves(receipt).every((leaf) => !leaf.value.includes(userPath)))
  assert.equal(receipt.deployment_identity.commit, 'positive-control')
  const failure = receipt.failures.find((row) => row.feature_id === 'drawer:nav' && row.evidence.state === 'closed')
  assert.ok(failure.evidence.first_error_line.includes('[REDACTED]'))
  assert.equal(failure.first_error_line, failure.evidence.first_error_line)
  assert.ok(failure.first_error_line.includes('[REDACTED]'))
  assert.ok(receipt.findings.some((row) => row.feature_id === 'drawer:nav' && row.category === 'console_errors' && row.evidence.count === 1))
  assert.equal(validateReceipt(receipt).valid, true)
})

test('findings are typed counts under their feature id; page/model strings never become paths or commands', () => {
  const payload = 'echo walk-payload && rm -rf ./owned'
  const pathPayload = '/etc/walk-payload/owned.json'
  const report = structuredClone(sample)
  const result = firstResult(featureSpec(report))
  setErrorMessage(result, payload)
  result.attachments = [attach({ consoleErrors: [{ text: payload }], failedRequests: [{ url: pathPayload }],
    steps: [{ command: payload, owned_files: [pathPayload] }], timeToTaskMs: 123,
    accessibility: { violations: [{ id: 'color-contrast', nodes: [{ html: payload, target: [pathPayload] }] },
      { id: payload, nodes: [{}] }] }, findings: [{ text: payload, file: pathPayload }], command: payload, path: pathPayload })]
  const receipt = build(report)
  const relevant = receipt.findings.filter((row) => row.feature_id === 'drawer:nav' && row.evidence.state === 'closed')
  assert.ok(relevant.some((row) => row.category === 'axe_violation' && row.evidence.violation_id === 'color-contrast' && row.evidence.count === 1))
  assert.ok(relevant.some((row) => row.category === 'failed_requests' && row.evidence.count === 1))
  assert.ok(relevant.some((row) => row.category === 'time_to_task' && row.evidence.milliseconds === 123))
  assert.ok(jsonLeaves(receipt).some((leaf) => leaf.path.at(-1) === 'first_error_line' && leaf.value === payload))
  for (const leaf of jsonLeaves(receipt)) {
    assert.ok(!leaf.path.some((key) => /owned|command/i.test(key)))
    if (leaf.path.at(-1) !== 'first_error_line') assert.ok(!leaf.value.includes(payload) && !leaf.value.includes(pathPayload))
  }
  assert.ok(receipt.findings.every((row) => row.feature_id && row.category && row.evidence.kind === row.category))
})

test('shared JSON references match their serialized form while active ancestor cycles are rejected', () => {
  const report = structuredClone(sample)
  const result = firstResult(featureSpec(report))
  setErrorMessage(result, 'shared-error-control')
  assert.equal(result.error, result.errors[0])
  const shared = { context: { label: 'shared-json-control' } }
  const runIdentity = { ...identity, deployment_identity: { commit: 'alias-control', first: shared, second: shared } }
  const receipt = build(report, map, runIdentity)
  const serialized = build(JSON.parse(JSON.stringify(report)), map, JSON.parse(JSON.stringify(runIdentity)))
  assert.deepEqual(withoutCreatedAt(receipt), withoutCreatedAt(serialized))
  assert.equal(receipt.deployment_identity.first.context.label, 'shared-json-control')
  assert.equal(receipt.deployment_identity.second.context.label, 'shared-json-control')
  assert.ok(receipt.failures.some((failure) => failure.evidence.first_error_line === 'shared-error-control'))
  receipt.evidence.aliases = { first: shared, second: shared }
  assert.deepEqual(validateReceipt(receipt), { valid: true, errors: [] })
  const cyclic = { suites: [] }
  const child = { parent: cyclic }
  cyclic.children = [child, child]
  assert.throws(() => build(cyclic), /acyclic/)
  receipt.evidence.aliases.parent = receipt
  assert.equal(validateReceipt(receipt).valid, false)
})

test('oversize and deeply nested inputs fail closed; all output strings and receipt bytes are bounded', () => {
  assert.throws(() => build({ ...sample, oversized: 'x'.repeat(MAX_INPUT_BYTES + 1) }), /bound/)
  const sharedOversize = { text: 'x'.repeat(MAX_INPUT_BYTES / 8) }
  assert.throws(() => build({ ...sample, aliases: Array(9).fill(sharedOversize) }), /byte bound/)
  const deep = { suites: [] }
  let cursor = deep
  for (let i = 0; i < 40; i++) { cursor.child = {}; cursor = cursor.child }
  assert.throws(() => build(deep), /structural bounds/)
  const cyclic = { suites: [] }; cyclic.child = cyclic
  assert.throws(() => build(cyclic), /acyclic/)
  const receipt = build(sample, map, { ...identity, deployment_identity: 'x'.repeat(2500) })
  assert.equal(receipt.deployment_identity.length, 2000)
  assert.ok(jsonLeaves(receipt).every((leaf) => leaf.value.length <= 2000))
  assert.ok(Buffer.byteLength(JSON.stringify(receipt)) <= MAX_RECEIPT_BYTES)
  const hugeIdentity = Object.fromEntries(Array.from({ length: 600 }, (_, i) => [`field-${i}`, 'x'.repeat(2000)]))
  assert.throws(() => build(sample, map, { ...identity, deployment_identity: hugeIdentity }), /Receipt exceeds 1 MiB/)
})

test('two builds are byte-identical after removing only created_at', () => {
  assert.equal(JSON.stringify(withoutCreatedAt(build())), JSON.stringify(withoutCreatedAt(build())))
})

test('artifact paths cannot escape artifacts or be supplied by evidence text', () => {
  assert.equal(artifactReference('C:\\tmp\\old-worktree\\web\\artifacts\\walk\\test-results\\trace.zip'), 'walk/test-results/trace.zip')
  assert.equal(artifactReference('web/artifacts/walk/screenshot.png'), 'walk/screenshot.png')
  for (const path of ['/home/user/trace.zip', 'web/artifacts/../secret', 'walk/../../secret',
    'walk/%2e%2e/secret', 'web/artifacts/walk/trace.zip?token=secret', 'walk/C:/secret']) assert.equal(artifactReference(path), null)
  const report = structuredClone(sample)
  const result = firstResult(featureSpec(report))
  result.attachments = [attach({ trace: '/etc/secret', screenshot: 'walk/attacker.png' }),
    { name: 'trace', path: 'web/artifacts/../../secret.zip' }]
  const failure = build(report).failures.find((row) => row.feature_id === 'drawer:nav' && row.evidence.state === 'closed')
  assert.deepEqual(failure.evidence.references, [])
})

test('bad attachments and report triples are rejected rather than silently discarded', () => {
  const report = structuredClone(sample)
  firstResult(featureSpec(report)).attachments = [{ name: 'walk-evidence', body: 'not base64' }]
  assert.throws(() => build(report), /base64/)
  firstResult(featureSpec(report)).attachments = [attach({}), attach({})]
  assert.throws(() => build(report), /Duplicate/)
  firstResult(featureSpec(report)).attachments = [attach({})]
  featureSpec(report).title = 'drawer:unknown [closed] @desktop'
  assert.throws(() => build(report), /absent from the feature map/)
})

test('validator rejects violations of required, oneOf, ref, enum, type, length, additionalProperties and date-time', () => {
  const receipt = build()
  const invalid = (mutate, expected) => {
    const candidate = structuredClone(receipt)
    mutate(candidate)
    const validation = validateReceipt(candidate)
    assert.equal(validation.valid, false)
    assert.ok(validation.errors.some((error) => error.includes(expected)), validation.errors.join('\n'))
  }
  invalid((row) => { delete row.fixture_version }, 'required')
  invalid((row) => { row.incident_id = 'incident:also' }, 'exactly one')
  invalid((row) => { row.feature_id = 'not a feature id' }, 'pattern')
  invalid((row) => { row.verdicts[0].verdict = 'pass' }, 'enum')
  invalid((row) => { row.attempt = 1.5 }, 'integer')
  invalid((row) => { row.attempt = 0 }, 'minimum')
  invalid((row) => { row.feature_id = 'x'.repeat(201) }, 'too long')
  invalid((row) => { row.owned_files = ['web/evil'] }, 'additional property')
  invalid((row) => { row.deployment_identity = {} }, 'exactly one')
  for (const date of ['oops', '2026-02-30T12:00:00Z', '2026-10-01T25:00:00Z', '2026-10-01']) invalid((row) => { row.created_at = date }, 'date-time')
  invalid((row) => { row.findings[0].evidence.text = 'x'.repeat(2001) }, '2000 character')
  assert.equal(redact(['Bearer', 'fixture-value'].join(' ')), ['Bearer', '[REDACTED]'].join(' '))
})
