import test from 'node:test'
import assert from 'node:assert/strict'
import { chromium } from '@playwright/test'
import { parseArgs, redact, makeTransport, buildReceipt, runSeedOverlay } from './run_surface_config_proof_staging.mjs'

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

const seedArgs = ['--tenant-id', 'dedicated-fixture', '--expected-sha', sha,
  '--seed-overlay', 'overlay.json']
const seedOverlay = { cad: { authoring: true }, sheets: { chrome: { tab: 'My sheets' } } }

function seedFixture({ initial = { surfaces: {} },
  readback = { surfaces: seedOverlay, source: { sha256: 'b'.repeat(64) } },
  deployment = {}, session = {} } = {}) {
  const calls = []
  let reads = 0
  const page = {
    request: { async get(path) {
      calls.push(['ready', path])
      return { ok: () => true, status: () => 200,
        url: () => 'https://platform-staging.leafdesign.ai/api/ready',
        json: async () => ({ source_revision: sha, environment: 'staging', ready: true,
          degraded_mode: false, ...deployment }) }
    } },
    async evaluate(fn, [name, args]) {
      calls.push([name, ...args])
      assert.match(String(fn), /window\.__leafSurfaceConfigProof\[name\]/)
      if (name === 'getSession') return { tenant: 'dedicated-fixture', org: 'org', tier: 'pro', ...session }
      if (name === 'getSurfaceConfig') return reads++ === 0 ? initial : readback
      if (name === 'submitSurfaceConfig') return { sha256: 'b'.repeat(64) }
      assert.fail(`Unexpected bridge method: ${name}`)
    },
  }
  return { calls, options: { ...parseArgs(seedArgs), page,
    readOverlay: async (path) => {
      assert.equal(path, 'overlay.json')
      return JSON.stringify(seedOverlay)
    } } }
}

test('parseArgs accepts seed mode, requires tenant and SHA, and rejects conflicting modes', () => {
  const options = parseArgs([...seedArgs, '--seed-force'])
  assert.equal(options.seedOverlay, 'overlay.json')
  assert.equal(options.seedForce, true)
  assert.equal(options.out, undefined)
  for (const conflicting of [['--probe'], ['--out', 'receipt.json']]) {
    assert.throws(() => parseArgs([...seedArgs, ...conflicting]), /mutually exclusive/)
  }
  assert.throws(() => parseArgs(seedArgs.slice(2)), /tenant-id is required/)
  assert.throws(() => parseArgs(['--tenant-id', 'dedicated-fixture', ...seedArgs.slice(4)]), /expected-sha is required/)
  assert.throws(() => parseArgs(['--probe', '--seed-force']), /requires --seed-overlay/)
})

test('seed on empty readback submits the file overlay and verifies a fresh readback', async () => {
  const { options, calls } = seedFixture()
  const result = await runSeedOverlay(options)
  assert.deepEqual(result, { exitCode: 0, output: { seeded: true, sourcePresent: true,
    surfacesKeys: ['cad', 'sheets'], authoring: true } })
  assert.deepEqual(calls, [
    ['getSession', false], ['ready', '/api/ready'],
    ['getSurfaceConfig', false, { fresh: true }],
    ['submitSurfaceConfig', seedOverlay], ['getSurfaceConfig', false, { fresh: true }],
  ])
})

test('existing overlay exits 5 without reading the file or posting unless forced', async () => {
  const initial = { surfaces: {}, source: { sha256: 'a'.repeat(64) } }
  const refused = seedFixture({ initial })
  refused.options.readOverlay = () => assert.fail('Existing overlay must not be replaced')
  assert.deepEqual(await runSeedOverlay(refused.options), {
    exitCode: 5, output: { seeded: false, reason: 'overlay_exists' },
  })
  assert.equal(refused.calls.some(([name]) => name === 'submitSurfaceConfig'), false)
  const forced = seedFixture({ initial })
  const result = await runSeedOverlay({ ...forced.options, seedForce: true })
  assert.equal(result.exitCode, 0)
  assert.equal(result.output.seeded, true)
  assert.deepEqual(forced.calls.filter(([name]) => name === 'submitSurfaceConfig'),
    [['submitSurfaceConfig', seedOverlay]])
})

test('seed fails if readback lacks source or has different authoring', async () => {
  for (const readback of [{ surfaces: seedOverlay },
    { surfaces: { cad: { authoring: false } }, source: {} }]) {
    const { options } = seedFixture({ readback })
    const result = await runSeedOverlay(options)
    assert.equal(result.exitCode, 1)
    assert.equal(result.output.seeded, false)
    assert.equal(result.output.sourcePresent, Boolean(readback.source))
  }
})

test('seed refuses wrong deployment and tenant before reading or writing overlay', async () => {
  for (const deployment of [{ environment: 'production' }, { ready: false },
    { degraded_mode: true }, { source_revision: 'c'.repeat(40) }, { source_revision: null }]) {
    const { options, calls } = seedFixture({ deployment })
    assert.deepEqual(await runSeedOverlay(options), {
      exitCode: 1, output: { seeded: false, reason: 'deployment_mismatch' },
    })
    assert.equal(calls.some(([name]) => name === 'getSurfaceConfig' || name === 'submitSurfaceConfig'), false)
  }
  const { options, calls } = seedFixture({ session: { tenant: 'other-tenant' } })
  assert.deepEqual(await runSeedOverlay(options), {
    exitCode: 1, output: { seeded: false, reason: 'tenant_mismatch' },
  })
  assert.deepEqual(calls, [['getSession', false]])
})

test('seed output never includes token or overlay contents beyond allowed fields', async () => {
  const token = 'sensitive.jwt.token'
  const overlay = { cad: { authoring: true }, [token]: { chrome: { tab: token } } }
  const { options } = seedFixture({ readback: { surfaces: overlay, source: { private: token } } })
  options.token = token
  options.readOverlay = async () => JSON.stringify(overlay)
  const result = await runSeedOverlay(options)
  assert.equal(JSON.stringify(result.output).includes(token), false)
  assert.deepEqual(result.output, { seeded: true, sourcePresent: true,
    surfacesKeys: ['cad', '<redacted>'], authoring: true })
  const failing = seedFixture()
  failing.options.token = token
  failing.options.readOverlay = async () => { throw new Error(token) }
  assert.deepEqual(await runSeedOverlay(failing.options), {
    exitCode: 1, output: { seeded: false, reason: 'invalid_overlay' },
  })
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
