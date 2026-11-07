import test from 'node:test'
import assert from 'node:assert/strict'
import { existsSync, mkdtempSync, mkdirSync, readFileSync, writeFileSync, rmSync, symlinkSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'
import { MAX_INPUT_BYTES, MAX_RECEIPT_BYTES } from './report.mjs'

const cli = fileURLToPath(new URL('./personaReportCli.mjs', import.meta.url))
const identity = { deployment_identity: { commit: 'abc' }, fixture_version: 'fixture-1',
  catalog_version: 'catalog-1', browser_version: 'chromium-1', attempt: 1 }
const triple = { feature_id: 'action:open', state: 'ready', viewport: 'phone' }
const writeJson = (path, value) => writeFileSync(path, JSON.stringify(value))

function fixture(t) {
  const directory = mkdtempSync(join(tmpdir(), 'persona-cli-'))
  t.after(() => rmSync(directory, { recursive: true, force: true }))
  const featureMap = { catalog_version: identity.catalog_version, entries: [
    { id: triple.feature_id, states: ['ready', 'missing'], viewports: ['phone'] }] }
  const manifest = { schema: 'leaf.persona-inputs.v1', feature_map: 'feature-map.json', inputs: [] }
  for (const id of ['one', 'two']) {
    mkdirSync(join(directory, id, 'walk'), { recursive: true })
    writeJson(join(directory, id, 'walk/evidence.json'), { schema: 'leaf.walk-evidence.v1',
      featureId: triple.feature_id, state: triple.state, viewport: triple.viewport,
      steps: [], consoleErrors: [], pageErrors: [], failedRequests: [] })
    writeJson(join(directory, id, 'report.json'), { suites: [{ specs: [
      { title: 'action:open [ready] @phone', tests: [{ projectName: 'phone', results: [
        { status: 'passed', attachments: [{ name: 'walk-evidence', path: 'walk/evidence.json' }] },
      ] }] },
    ] }] })
    manifest.inputs.push({ id, report: `${id}/report.json`, artifacts_root: id, identity })
  }
  writeJson(join(directory, 'feature-map.json'), featureMap)
  writeJson(join(directory, 'receipt.json'), { schema: 'leaf.studio-walk.v1', ...identity,
    evidence: { cases: [{ ...triple, verdict: 'pass' }], coverage_gaps: [] } })
  const save = () => writeJson(join(directory, 'manifest.json'), manifest)
  save()
  const run = (args = ['--manifest', 'manifest.json', '--receipt', 'receipt.json', '--out', 'out.json']) =>
    spawnSync(process.execPath, [cli, ...args], { cwd: directory, encoding: 'utf8' })
  return { directory, manifest, save, run }
}

test('two accepted shards share one receipt denominator and retain distinct source references', (t) => {
  const { directory, run } = fixture(t)
  const result = run()
  assert.equal(result.status, 0, result.stderr)
  const text = readFileSync(join(directory, 'out.json'), 'utf8')
  assert.ok(text.endsWith('\n'))
  const report = JSON.parse(text)
  assert.equal(report.denominator, 20)
  assert.deepEqual(report.totals, { PASS: 0, FAIL: 0, UNKNOWN: 20, UNAVAILABLE: 0 })
  assert.ok(report.rows.every((row) => row.persona_verdict === 'UNKNOWN'))
  assert.deepEqual(report.evidence_bundles, [{ evidence_ref: 'persona-attempts/000001', sources: [
    'persona-sources/one/walk/evidence.json', 'persona-sources/two/walk/evidence.json',
  ] }])
  assert.equal(report.uncovered.filter((row) => row.reason === 'missing_case').length, 10)
})

test('inline attachments get deterministic shard-specific references', (t) => {
  const { directory, run } = fixture(t)
  const reportPath = join(directory, 'one/report.json')
  const report = JSON.parse(readFileSync(reportPath, 'utf8'))
  report.suites[0].specs[0].tests[0].results[0].attachments = [{ name: 'walk-evidence', body:
    Buffer.from(JSON.stringify({ schema: 'leaf.walk-evidence.v1' })).toString('base64') }]
  writeJson(reportPath, report)
  assert.equal(run().status, 0)
  const first = readFileSync(join(directory, 'out.json'), 'utf8')
  assert.match(first, /persona-sources\/one\/inline\/[a-f0-9]{64}\.json/)
  assert.equal(run().status, 0)
  assert.equal(readFileSync(join(directory, 'out.json'), 'utf8'), first)
})

test('native joined paths and backslash separators are accepted, but backslash traversal is refused', (t) => {
  const { directory, manifest, save, run } = fixture(t)
  manifest.inputs[0].report = 'one\\report.json'
  manifest.inputs[0].artifacts_root = 'one\\walk\\'
  const reportPath = join(directory, 'one', 'report.json')
  const report = JSON.parse(readFileSync(reportPath, 'utf8'))
  report.suites[0].specs[0].tests[0].results[0].attachments = [{ name: 'walk-evidence', body:
    Buffer.from(JSON.stringify({ schema: 'leaf.walk-evidence.v1' })).toString('base64') }]
  writeJson(reportPath, report)
  save()
  const args = ['--manifest', join(directory, 'manifest.json'), '--receipt', join(directory, 'receipt.json'),
    '--out', join(directory, 'out.json')]
  const result = run(args)
  assert.equal(result.status, 0, result.stderr)
  manifest.inputs[0].report = 'a\\..\\b'
  save()
  assert.match(run(args).stderr, /traversal/)
  assert.match(run(['--manifest', 'a\\..\\b', '--receipt', 'receipt.json', '--out', 'out.json']).stderr, /traversal/)
})

test('declared roots allow external shard inputs and feature maps but not receipts or output', (t) => {
  const { directory, manifest, save, run } = fixture(t)
  const outside = mkdtempSync(join(tmpdir(), 'persona-shards-'))
  t.after(() => rmSync(outside, { recursive: true, force: true }))
  mkdirSync(join(outside, 'shard', 'walk'), { recursive: true })
  writeFileSync(join(outside, 'shard', 'report.json'), readFileSync(join(directory, 'one', 'report.json')))
  writeFileSync(join(outside, 'shard', 'walk', 'evidence.json'), readFileSync(join(directory, 'one', 'walk', 'evidence.json')))
  writeFileSync(join(outside, 'feature-map.json'), readFileSync(join(directory, 'feature-map.json')))
  writeFileSync(join(outside, 'receipt.json'), readFileSync(join(directory, 'receipt.json')))
  manifest.inputs[0].report = join(outside, 'shard', 'report.json')
  manifest.inputs[0].artifacts_root = join(outside, 'shard')
  save()
  assert.match(run().stderr, /escapes input root/)
  manifest.roots = [outside]
  save()
  assert.equal(run().status, 0)
  manifest.feature_map = join(outside, 'feature-map.json')
  save()
  const accepted = run()
  assert.equal(accepted.status, 0, accepted.stderr)
  symlinkSync(outside, join(directory, 'shard-root'), 'dir')
  manifest.roots = [join(directory, 'shard-root')]
  save()
  const canonicalized = run()
  assert.equal(canonicalized.status, 0, canonicalized.stderr)
  const args = ['--manifest', 'manifest.json', '--receipt', 'receipt.json', '--out', join(outside, 'out.json')]
  assert.match(run(args).stderr, /escapes input root/)
  assert.match(run(['--manifest', 'manifest.json', '--receipt', join(outside, 'receipt.json'),
    '--out', 'out.json']).stderr, /escapes input root/)
  delete manifest.roots
  save()
  assert.match(run().stderr, /escapes input root/)
  manifest.feature_map = 'feature-map.json'
  manifest.inputs[0].report = 'one/report.json'
  save()
  assert.match(run().stderr, /escapes input root/)
})

test('roots must be an array of at most 17 existing absolute directories', (t) => {
  const { directory, manifest, save, run } = fixture(t)
  for (const roots of [['one'], [join(directory, 'missing')], [join(directory, 'receipt.json')],
    Array(18).fill(directory), null, directory]) {
    manifest.roots = roots
    save()
    const result = run()
    assert.notEqual(result.status, 0)
    assert.ok(result.stderr.trim())
  }
  for (const roots of [[], Array(17).fill(directory)]) {
    manifest.roots = roots
    save()
    const result = run()
    assert.equal(result.status, 0, result.stderr)
  }
})

test('live-style inline base64 evidence without paths leaves every lens unknown', (t) => {
  const { directory, run } = fixture(t)
  for (const id of ['one', 'two']) {
    const reportPath = join(directory, id, 'report.json')
    const report = JSON.parse(readFileSync(reportPath, 'utf8'))
    const evidence = readFileSync(join(directory, id, 'walk', 'evidence.json'))
    report.suites[0].specs[0].tests[0].results[0].attachments = [
      { name: 'walk-evidence', contentType: 'application/json', body: evidence.toString('base64') },
    ]
    writeJson(reportPath, report)
  }
  const result = run()
  assert.equal(result.status, 0, result.stderr)
  const report = JSON.parse(readFileSync(join(directory, 'out.json'), 'utf8'))
  assert.deepEqual(report.totals, { PASS: 0, FAIL: 0, UNKNOWN: 20, UNAVAILABLE: 0 })
  assert.ok(report.rows.every((row) => row.persona_verdict === 'UNKNOWN'))
  assert.equal(report.evidence_bundles[0].sources.length, 2)
  assert.ok(report.evidence_bundles[0].sources.every((source) =>
    /^persona-sources\/(one|two)\/inline\/[a-f0-9]{64}\.json$/.test(source)))
})

test('unknown, missing and duplicate flags fail closed', (t) => {
  const { run } = fixture(t)
  for (const args of [[], ['--manifest', 'manifest.json'],
    ['--unknown', 'manifest.json', '--receipt', 'receipt.json', '--out', 'out.json'],
    ['--manifest', 'manifest.json', '--receipt', 'receipt.json', '--out', 'out.json', '--out', 'again.json']]) {
    const result = run(args)
    assert.notEqual(result.status, 0)
    assert.match(result.stderr, /Usage:/)
  }
})

test('malformed manifest, ids, identities and JSON are rejected', (t) => {
  const { directory, manifest, save, run } = fixture(t)
  const original = structuredClone(manifest)
  for (const mutate of [
    (m) => { m.schema = 'wrong' }, (m) => { m.inputs = [] }, (m) => { m.inputs = Array(17).fill(m.inputs[0]) },
    (m) => { m.inputs[1].id = m.inputs[0].id }, (m) => { m.inputs[0].id = '../bad' },
    (m) => { delete m.inputs[0].identity }, (m) => { m.inputs[0].identity = { ...identity, attempt: 99 } },
    (m) => { delete m.feature_map }, (m) => { m.inputs[0].report = null },
  ]) {
    writeJson(join(directory, 'manifest.json'), original)
    const candidate = structuredClone(original)
    mutate(candidate)
    writeJson(join(directory, 'manifest.json'), candidate)
    const result = run()
    assert.notEqual(result.status, 0)
    assert.ok(result.stderr.trim())
  }
  save()
  writeFileSync(join(directory, 'one/report.json'), '{')
  assert.match(run().stderr, /not JSON/)
})

test('input files and aggregate decoded evidence enforce byte bounds', (t) => {
  const { directory, run } = fixture(t)
  const paths = ['manifest.json', 'receipt.json', 'one/report.json', 'feature-map.json']
  for (const path of paths) {
    const full = join(directory, path), original = readFileSync(full)
    const limit = path === 'feature-map.json' ? 8 * MAX_RECEIPT_BYTES : MAX_INPUT_BYTES
    writeFileSync(full, ' '.repeat(limit + 1))
    assert.match(run().stderr, /byte bound/)
    writeFileSync(full, original)
  }
  // JSON whitespace still counts toward decoded attachment bytes.
  for (const id of ['one', 'two']) writeFileSync(join(directory, id, 'walk/evidence.json'),
    JSON.stringify({ schema: 'leaf.walk-evidence.v1' }) + ' '.repeat(9 * MAX_RECEIPT_BYTES))
  assert.match(run().stderr, /Total decoded evidence exceeds byte bound/)
})

test('a persona output between 1 MiB and 16 MiB is written', (t) => {
  const { directory, run } = fixture(t)
  const path = join(directory, 'feature-map.json')
  const featureMap = JSON.parse(readFileSync(path, 'utf8'))
  featureMap.entries.push(...Array.from({ length: 700 }, (_, i) => ({ id: `action:feature-${i}`,
    states: ['ready'], viewports: ['phone'] })))
  writeJson(path, featureMap)
  const result = run()
  assert.equal(result.status, 0, result.stderr)
  const output = readFileSync(join(directory, 'out.json'))
  assert.ok(output.length > MAX_RECEIPT_BYTES)
  assert.ok(output.length <= 16 * 1024 * 1024)
  assert.equal(JSON.parse(output.toString('utf8')).denominator, 7020)
})

test('a persona output over 16 MiB is rejected before creating or overwriting output', (t) => {
  const { directory, run } = fixture(t)
  const path = join(directory, 'feature-map.json')
  const featureMap = JSON.parse(readFileSync(path, 'utf8'))
  featureMap.entries.push(...Array.from({ length: 6000 }, (_, i) => ({ id: `action:feature-${i}`,
    states: ['ready'], viewports: ['phone'] })))
  writeJson(path, featureMap)
  const refused = run()
  assert.equal(refused.status, 2, refused.stderr)
  assert.match(refused.stderr, /Persona output exceeds 16 MiB bound/)
  assert.equal(existsSync(join(directory, 'out.json')), false)
  writeFileSync(join(directory, 'out.json'), 'previous-output')
  const result = run()
  assert.equal(result.status, 2, result.stderr)
  assert.match(result.stderr, /Persona output exceeds 16 MiB bound/)
  assert.equal(readFileSync(join(directory, 'out.json'), 'utf8'), 'previous-output')
})

test('traversal and symlink escapes in files, roots, attachments and output fail closed', (t) => {
  const { directory, manifest, save, run } = fixture(t)
  const original = structuredClone(manifest)
  for (const [object, key] of [[manifest, 'feature_map'], [manifest.inputs[0], 'report'], [manifest.inputs[0], 'artifacts_root']]) {
    const value = object[key]
    object[key] = `../${value}`
    save()
    assert.match(run().stderr, /traversal/)
    object[key] = value
  }
  save()
  assert.match(run(['--manifest', 'manifest.json', '--receipt', '../receipt.json', '--out', 'out.json']).stderr, /traversal/)
  assert.match(run(['--manifest', 'manifest.json', '--receipt', 'receipt.json', '--out', '../out.json']).stderr, /traversal/)
  const outside = mkdtempSync(join(tmpdir(), 'persona-outside-'))
  t.after(() => rmSync(outside, { recursive: true, force: true }))
  writeJson(join(outside, 'foreign.json'), {})
  symlinkSync(outside, join(directory, 'escape'), 'dir')
  for (const mutate of [
    (m) => { m.feature_map = 'escape/foreign.json' },
    (m) => { m.inputs[0].report = 'escape/foreign.json' },
    (m) => { m.inputs[0].artifacts_root = 'escape' },
  ]) {
    const candidate = structuredClone(original)
    mutate(candidate)
    writeJson(join(directory, 'manifest.json'), candidate)
    assert.match(run().stderr, /escapes input root/)
  }
  save()
  assert.match(run(['--manifest', 'manifest.json', '--receipt', 'escape/foreign.json', '--out', 'out.json']).stderr, /escapes input root/)
  assert.match(run(['--manifest', 'manifest.json', '--receipt', 'receipt.json', '--out', 'escape/new.json']).stderr, /escapes input root/)
  symlinkSync(join(outside, 'not-created.json'), join(directory, 'dangling.json'))
  assert.notEqual(run(['--manifest', 'manifest.json', '--receipt', 'receipt.json', '--out', 'dangling.json']).status, 0)
  symlinkSync(join(outside, 'foreign.json'), join(directory, 'one/walk/escape.json'))
  const reportPath = join(directory, 'one/report.json')
  const report = JSON.parse(readFileSync(reportPath, 'utf8'))
  report.suites[0].specs[0].tests[0].results[0].attachments[0].path = 'walk/escape.json'
  writeJson(reportPath, report)
  assert.match(run().stderr, /escapes input root/)
})
