import test from 'node:test'
import assert from 'node:assert/strict'
import { chromium } from '@playwright/test'
import { parseArgs, redact, makeTransport, buildReceipt } from './run_surface_config_proof_staging.mjs'

const sha = 'a'.repeat(40)
const required = ['--tenant-id', 'dedicated-fixture', '--expected-sha', sha, '--out', 'receipt.json']

test('parseArgs enforces each required input and probe relaxes them', () => {
  assert.throws(() => parseArgs([]), /tenant-id/)
  for (let i = 0; i < required.length; i += 2) {
    assert.throws(() => parseArgs(required.filter((_, index) => index !== i && index !== i + 1)), /required/)
  }
  const options = parseArgs(['--probe'])
  assert.equal(options.probe, true)
  assert.equal(options.baseUrl, 'https://platform-staging.leafdesign.ai')
  assert.equal(options.timeoutMs, 90000)
  assert.equal(options.headed, false)
  assert.equal(options.jwtStdin, false)
})

test('parseArgs validates SHA, option values, timeout and staging origin', () => {
  for (const invalid of ['A'.repeat(40), 'a'.repeat(39), 'g'.repeat(40)]) {
    assert.throws(() => parseArgs(['--probe', '--expected-sha', invalid]), /lowercase hex/)
  }
  for (const invalid of ['0', '-1', '1.5', 'Infinity', '9007199254740992']) {
    assert.throws(() => parseArgs(['--probe', '--timeout-ms', invalid]), /positive integer/)
  }
  assert.throws(() => parseArgs(['--probe', '--out']), /Missing option/)
  assert.throws(() => parseArgs(['--probe', '--jwt', 'do-not-echo']), /Unknown option/)
  assert.throws(() => parseArgs(['--probe', '--base-url', 'https://platform.leafdesign.ai']))
  assert.throws(() => parseArgs(['--probe', '--base-url', 'https://platform-staging.leafdesign.ai:4443']))
  const options = parseArgs([...required, '--headed', '--jwt-stdin', '--timeout-ms', '1234'],
    { LEAF_E2E_STAGING_JWT: '  secret-token\n' })
  assert.equal(options.tenantId, 'dedicated-fixture')
  assert.equal(options.expectedSourceSha, sha)
  assert.equal(options.out, 'receipt.json')
  assert.equal(options.headed, true)
  assert.equal(options.jwtStdin, true)
  assert.equal(options.timeoutMs, 1234)
  assert.equal(options.token, 'secret-token')
})

test('redact replaces every literal token occurrence and preserves other text', () => {
  assert.equal(redact('before a.b$ a.b$ after', 'a.b$'), 'before <redacted> <redacted> after')
  assert.equal(redact('ordinary text', 'secret'), 'ordinary text')
  assert.equal(redact('ordinary text', ''), 'ordinary text')
})

test('transport evaluates each method anew with its exact name and arguments', async () => {
  const calls = []
  const result = { observed: true }
  const page = { async evaluate(fn, payload) { calls.push({ fn, payload }); return result } }
  const transport = makeTransport(page)
  const cases = [['getSession', [false]], ['getSurfaceConfig', [false, { fresh: true }]],
    ['submitSurfaceConfig', [{ cad: { authoring: false } }]]]
  for (const [name, args] of cases) {
    assert.equal(await transport[name](...args), result)
    const call = calls.at(-1)
    assert.deepEqual(call.payload, [name, args])
    assert.match(String(call.fn), /window\.__leafSurfaceConfigProof\[name\]/)
  }
  await transport.getSession(false)
  assert.equal(calls.length, 4)
})

test('buildReceipt scrubs every field and key without changing the input', () => {
  const token = 'sensitive.jwt.token'
  const receipt = { ok: false, failure: token, nested: [{ [token]: `prefix ${token} ${token}` }] }
  const output = buildReceipt(receipt, { token, ranAt: token, baseUrl: token, expectedSourceSha: token })
  assert.equal(JSON.stringify(output).includes(token), false)
  assert.equal(output.driver, 'run_surface_config_proof_staging.mjs')
  assert.equal(output.ok, false)
  assert.equal(output.nested[0]['<redacted>'], 'prefix <redacted> <redacted>')
  assert.equal(receipt.failure, token)
  assert.equal(buildReceipt({ ok: true }, { ranAt: 'now', baseUrl: 'staging', expectedSourceSha: sha }).ok, true)
})

test('import has no browser launch, stdin, output or exit side effects', async () => {
  const launch = chromium.launch
  const log = console.log
  const error = console.error
  const stdinIterator = process.stdin[Symbol.asyncIterator]
  const exitCode = process.exitCode
  const unexpected = () => { throw new Error('Unexpected import side effect') }
  try {
    chromium.launch = unexpected
    console.log = unexpected
    console.error = unexpected
    process.stdin[Symbol.asyncIterator] = unexpected
    const imported = await import('./run_surface_config_proof_staging.mjs?import-only')
    assert.equal(typeof imported.makeTransport, 'function')
    assert.equal(process.exitCode, exitCode)
  } finally {
    chromium.launch = launch
    console.log = log
    console.error = error
    process.stdin[Symbol.asyncIterator] = stdinIterator
  }
})
