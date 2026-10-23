import assert from 'node:assert/strict'
import test from 'node:test'
import { mkdtempSync, readFileSync, readdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { CANARY_INVENTORY, PROD_INVENTORY, InventoryRun, validateReceipt } from './prodInventory.mjs'
import ProdReporter from './prodReporter.mjs'
import { childEnvironment, launchProof } from '../../scripts/run_prod_readonly.mjs'

const WEB = fileURLToPath(new URL('../../', import.meta.url))
const read = (path) => readFileSync(join(WEB, path), 'utf8')
const collected = () => PROD_INVENTORY.map((row) => ({ ...row, expectedStatus: 'passed', annotations: [], repeatEachIndex: 0 }))

function exercise({ tests = collected(), mutate = () => {}, status = 'passed' } = {}) {
  const run = new InventoryRun()
  run.collect(tests)
  for (const item of tests) {
    run.begin(item, { retry: 0 })
    run.end(item, { retry: 0, status: 'passed', errors: [], attachments: [] })
  }
  mutate(run, tests)
  return run.finish(status)
}

test('required inventory rejects incomplete or nonpassing runs', () => {
  assert.equal(PROD_INVENTORY.length, 13)
  assert.ok(Object.isFrozen(PROD_INVENTORY))
  assert.ok(PROD_INVENTORY.every(Object.isFrozen))
  const pass = exercise()
  assert.equal(pass.status, 'passed')
  assert.deepEqual(validateReceipt(pass), pass)
  for (const tests of [[], collected().slice(1), [...collected(), collected()[0]], [...collected(), { ...collected()[0], title: 'extra' }], collected().map((row, index) => index ? row : { ...row, title: 'renamed' })]) assert.equal(exercise({ tests }).status, 'failed')
  for (const type of ['skip', 'fixme', 'fail']) assert.equal(exercise({ tests: collected().map((row, index) => index ? row : { ...row, annotations: [{ type }] }) }).status, 'failed')
  for (const expectedStatus of ['failed', 'skipped']) assert.equal(exercise({ tests: collected().map((row, index) => index ? row : { ...row, expectedStatus }) }).status, 'failed')
  assert.equal(exercise({ tests: collected().map((row, index) => index ? row : { ...row, repeatEachIndex: 1 }) }).status, 'failed')
  for (const status of ['failed', 'timedout', 'interrupted', 'skipped']) {
    assert.equal(exercise({ status }).status, 'failed')
    const run = new InventoryRun()
    const tests = collected()
    run.collect(tests)
    for (const [index, item] of tests.entries()) {
      run.begin(item, { retry: 0 })
      run.end(item, { retry: 0, status: index ? 'passed' : status })
    }
    assert.equal(run.finish('passed').status, 'failed')
  }
  for (const mutate of [
    (run) => run.error(),
    (run, tests) => run.begin(tests[0], { retry: 0 }),
    (run, tests) => run.begin(tests[0], { retry: 1 }),
    (run, tests) => run.end(tests[0], { retry: 1, status: 'passed' }),
    (run) => run.started.clear(),
    (run) => run.completed.clear(),
    (run, tests) => run.end(tests[0], { retry: 0, status: 'passed', errors: [{ message: 'synthetic' }] }),
    (run, tests) => run.end(tests[0], { retry: 0, status: 'passed', attachments: [{ name: 'synthetic' }] }),
  ]) assert.equal(exercise({ mutate }).status, 'failed')
  for (const bad of [{ ...pass, raw: 'secret' }, { ...pass, denials: { SECRET: 1 } }, { ...pass, denials: { METHOD: 1 } }, { ...pass, tests: [] }, { ...pass, tests: pass.tests.map((row) => ({ ...row, started: 2 })) }]) assert.throws(() => validateReceipt(bad), /^Error: PROD_RECEIPT_INVALID$/)
  const reporter = new ProdReporter()
  assert.equal(reporter.printsToStdio(), true)
  assert.equal(reporter.onStdOut, undefined)
  assert.equal(reporter.onStdErr, undefined)
  reporter.onBegin({}, { allTests() { throw new Error('synthetic collection error') } })
  assert.equal(reporter.run.finish('passed').status, 'failed')
  reporter.onError(new Error('synthetic global error'))
  assert.ok(reporter.run.errors >= 2)
  const successful = new ProdReporter()
  const cases = collected().map((row) => ({ ...row, location: { file: `/offline/${row.file}` } }))
  successful.onBegin({ projects: [{ retries: 0, repeatEach: 1, use: { browserName: 'chromium' } }] }, { allTests: () => cases })
  const sentinel = 'SYNTHETIC_REPORTER_SECRET_42'
  for (const item of cases) {
    successful.onTestBegin(item, { retry: 0 })
    successful.onTestEnd(item, { retry: 0, status: 'passed', stdout: [sentinel], stderr: [sentinel], errors: [], attachments: [] })
  }
  const lines = []
  const originalWrite = process.stdout.write
  try {
    process.stdout.write = (line) => { lines.push(line); return true }
    assert.deepEqual(successful.onEnd({ status: 'passed' }), { status: 'passed' })
    assert.deepEqual(reporter.onEnd({ status: 'passed' }), { status: 'failed' })
  } finally { process.stdout.write = originalWrite }
  assert.equal(lines.length, 2)
  assert.ok(!lines.join('').includes(sentinel))
  for (const line of lines) validateReceipt(JSON.parse(line.slice('PROD_READONLY_RECEIPT '.length)))
})

test('authenticated failure produces no raw artifacts or secrets', () => {
  const lock = JSON.parse(read('package-lock.json'))
  assert.equal(lock.packages['node_modules/@playwright/test'].version, '1.60.0')
  assert.equal(lock.packages['node_modules/playwright'].version, '1.60.0')
  const root = mkdtempSync(join(tmpdir(), 'leaf-prod-readonly-canary-'))
  const emitted = []
  const result = launchProof({ canary: true, outputDir: root, emit: (line) => emitted.push(line) })
  assert.equal(result.exitCode, 1)
  assert.ok(result.receipt, 'failure must produce a validated safe receipt')
  assert.deepEqual(validateReceipt(result.receipt, CANARY_INVENTORY), result.receipt)
  assert.equal(result.receipt.status, 'failed')
  assert.equal(result.receipt.tests[0].started, 1)
  assert.equal(result.receipt.tests[0].status, 'failed')
  // This count proves page setup and the attachment attempt actually ran.
  // A missing browser or fixture setup failure cannot satisfy the canary.
  assert.equal(result.receipt.denials.ARTIFACT, 1)
  assert.equal(result.receipt.denials.ESCAPE, 1)
  const sentinel = 'SYNTHETIC_CANARY_SECRET_42'
  assert.ok(!JSON.stringify(result).includes(sentinel))
  assert.ok(!emitted.join('').includes(sentinel))
  assert.equal(emitted.length, 1)
  assert.match(emitted[0], /^PROD_READONLY_RECEIPT /)
  const files = []
  function inspect(dir) {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      assert.ok(!entry.name.includes(sentinel))
      const path = join(dir, entry.name)
      if (entry.isDirectory()) inspect(path)
      else {
        files.push(entry.name)
        assert.ok(!readFileSync(path).includes(Buffer.from(sentinel)))
        assert.equal(entry.name, '.last-run.json', `unexpected raw artifact in ${root}`)
      }
    }
  }
  inspect(root)
  assert.ok(files.every((name) => !/\.(?:png|jpe?g|webm|mp4|zip|md)$/u.test(name)))
  // Keep the directory available to the operator; no post-capture deletion.
})

test('production entrypoints enforce the contract', () => {
  const scripts = JSON.parse(read('package.json')).scripts
  assert.equal(scripts['proof:prod'], 'node scripts/run_prod_readonly.mjs')
  assert.equal(scripts['proof:prod:readonly'], 'node scripts/run_prod_readonly.mjs')
  assert.equal(scripts['check:prod-readonly-contract'], 'node --test e2e/prod/prodConfig.test.mjs e2e/prod/prodGuard.test.mjs e2e/prod/prodContract.test.mjs')
  const actual = []
  for (const file of [...new Set(PROD_INVENTORY.map((row) => row.file))]) {
    const source = read(`e2e/prod/${file}`)
    assert.match(source, /import \{ expect, test \} from '\.\/prodFixtures\.mjs'/)
    assert.doesNotMatch(source, /@playwright\/test|test\.(?:skip|fixme|fail)|\.first\(\)|projects\[0\]|\.(?:route|routeFromHAR|routeWebSocket|newContext|newPage|newCDPSession|attach|screenshot|pdf)\s*\(/)
    assert.doesNotMatch(source, /prodOffline|_prodSession/)
    assert.doesNotMatch(source, /\b(?:browser|playwright)\b\s*[,}]/)
    for (const match of source.matchAll(/^test\('([^']+)'/gmu)) actual.push({ file, title: match[1] })
  }
  assert.deepEqual(actual, PROD_INVENTORY.map(({ file, title }) => ({ file, title })))
  const config = read('playwright.prod.config.mjs')
  assert.match(config, /const config = validateProdConfig\(\)/)
  assert.match(config, /testDir: '\.\/e2e\/prod'/)
  const match = config.match(/testMatch: (\/.*\/),/u)
  assert.ok(match)
  const matcher = new RegExp(match[1].slice(1, -1))
  for (const row of PROD_INVENTORY) assert.ok(matcher.test(row.file))
  for (const file of ['prodConfig.test.mjs', 'prodGuard.test.mjs', 'prodContract.test.mjs', 'artifact-failure.canary.mjs', 'other.spec.mjs']) assert.ok(!matcher.test(file))
  for (const key of ['video', 'screenshot', 'trace']) assert.match(config, new RegExp(`${key}: 'off'`))
  assert.match(config, /serviceWorkers: 'block'/)
  assert.match(config, /reporter: \[\['\.\/e2e\/prod\/prodReporter\.mjs'\]\]/)
  assert.doesNotMatch(config, /'html'|'list'|retries: [1-9]|repeatEach: [2-9]/)
  assert.match(read('e2e/prod-contract/playwright.canary.config.mjs'), /testMatch: '\*\*\/\*\.canary\.mjs'/)
  assert.doesNotMatch(read('e2e/prod-contract/artifact-failure.canary.mjs'), /\.goto\(|\.request\.|\.route\(|test\.skip/)
  const clean = childEnvironment({ PATH: 'synthetic', DEBUG: '*', PWDEBUG: '1', PW_TEST_REPORTER: 'html', NODE_OPTIONS: '--inspect', NODE_DEBUG: 'http', NODE_V8_COVERAGE: '/tmp/coverage', SSLKEYLOGFILE: '/tmp/keys', LEAF_E2E_PROD_ACCEPTANCE_TOKEN: 'SYNTHETIC_CHILD_SECRET' })
  assert.deepEqual(Object.keys(clean).sort(), ['CI', 'LEAF_E2E_PROD_ACCEPTANCE_TOKEN', 'NO_COLOR', 'PATH'])
  for (const arg of ['--grep', '--grep-invert', '--shard=1/2', '--list', '--reporter=html', '--project=chromium', '--repeat-each=2', '--retries=1', 'unified-prod-readonly']) {
    const lines = []
    const result = launchProof({ args: [arg], env: {}, emit: (line) => lines.push(line) })
    assert.equal(result.exitCode, 1)
    assert.deepEqual(lines, ['PROD_ARGUMENTS_FORBIDDEN\n'])
  }
})
