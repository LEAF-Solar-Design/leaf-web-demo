import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

const cli = fileURLToPath(new URL('./expectRed.mjs', import.meta.url))
const helper = fileURLToPath(new URL('./_harness.mjs', import.meta.url))
const config = fileURLToPath(new URL('../../playwright.regressions.config.mjs', import.meta.url))
const web = fileURLToPath(new URL('../../', import.meta.url))
const playwright = join(web, 'node_modules', '@playwright', 'test', 'cli.js')
const marker = 'W2A_ASSERTION_7b9e'
const imports = `import { test, expect, unsupportedLocal } from ${JSON.stringify(new URL('./_harness.mjs', import.meta.url).href)}\n`
const sha256 = async (path) => createHash('sha256').update(await readFile(path)).digest('hex')

async function run(script, args, extraEnv = {}) {
  const env = { ...process.env }
  delete env.LEAF_REGRESSION_SPEC
  delete env.LEAF_REGRESSION_REPORT
  const child = spawn(process.execPath, [script, ...args], {
    cwd: web, windowsHide: true, env: { ...env, ...extraEnv }, stdio: ['ignore', 'pipe', 'pipe'],
  })
  let stdout = ''
  let stderr = ''
  child.stdout.on('data', (chunk) => { stdout += chunk })
  child.stderr.on('data', (chunk) => { stderr += chunk })
  const code = await new Promise((accept, reject) => { child.once('error', reject); child.once('close', accept) })
  return { code, stdout, stderr }
}

test('expectRed preserves the frozen oracle and distinguishes every result', { timeout: 850_000 }, async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'leaf-regression-contract-'))
  try {
    const cases = [
      { name: 'marker failure', body: `test('red', async () => { expect(false, '${marker}').toBe(true) })`, code: 0, verdict: 'RED' },
      { name: 'passing spec', body: `test('green', async () => { expect(true).toBe(true) })`, code: 1, verdict: 'NOT_RED' },
      { name: 'wrong failure', body: `test('wrong', async () => { throw new Error('another assertion') })`, code: 2, verdict: 'WRONG_FAILURE' },
      { name: 'skipped spec', body: `test.skip('skip', async () => { throw new Error('${marker}') })`, code: 3, verdict: 'INVALID_SELECTION' },
      { name: 'empty selection', body: '', code: 3, verdict: 'INVALID_SELECTION' },
      { name: 'unsupported local', body: `test('unsupported', async () => { unsupportedLocal('private PostgreSQL required') })`, code: 4, verdict: 'UNSUPPORTED_LOCAL' },
      { name: 'stdout is not an oracle', body: `test('wrong', async () => { console.log('${marker}'); throw new Error('another assertion') })`, code: 2, verdict: 'WRONG_FAILURE' },
      { name: 'expected failure is not red', body: `test('expected', async () => { test.fail(); throw new Error('${marker}') })`, code: 2, verdict: 'WRONG_FAILURE' },
      { name: 'mixed red and skip', body: `test('red', async () => { throw new Error('${marker}') }); test.skip('skip', async () => {})`, code: 3, verdict: 'INVALID_SELECTION' },
      { name: 'queued admission', body: `test('queued', async ({ stack }) => { throw new Error('${marker}') })`, code: 75, verdict: 'QUEUED', admission: 'queued' },
      { name: 'stopped admission', body: `test('stopped', async ({ stack }) => { throw new Error('${marker}') })`, code: 76, verdict: 'STOPPED', admission: 'stopped' },
    ]
    for (const [index, example] of cases.entries()) {
      await t.test(example.name, async () => {
        const spec = join(root, `case-${index}.spec.mjs`)
        await writeFile(spec, imports + example.body + '\n')
        // Oracle-only specs request no stack/page. Refuse admission so an eager
        // browser option dependency cannot boot services and hide their verdict.
        const admission = example.admission || 'queued'
        const hook = join(root, `admission-${index}.mjs`)
        await writeFile(hook, `console.log(JSON.stringify({ status: '${admission}', reason: 'W2a admission probe' })); process.exit(${admission === 'stopped' ? 76 : 75})\n`)
        const env = { LEAF_WALK_ADMISSION_CMD: JSON.stringify([process.execPath, hook]) }
        const result = await run(cli, ['--spec', spec, '--marker', marker, '--config', config], env)
        assert.equal(result.code, example.code, result.stdout + result.stderr)
        const lines = result.stdout.trim().split(/\r?\n/)
        assert.equal(lines.length, 1, result.stdout)
        const receipt = JSON.parse(lines[0])
        assert.equal(receipt.verdict, example.verdict)
        assert.equal(receipt.exitCode, example.code)
        assert.equal(receipt.specSha256, await sha256(spec))
        assert.equal(receipt.helperSha256, await sha256(helper))
        if (example.code === 0) assert.ok(receipt.ran > 0)
        if (example.name === 'skipped spec') assert.equal(receipt.skipped, 1)
        if (example.name === 'empty selection') assert.equal(receipt.selected, 0)
        if (example.name === 'unsupported local') {
          assert.equal(receipt.selected, 1)
          assert.equal(receipt.ran, 1)
          assert.equal(receipt.skipped, 0)
          assert.match(result.stderr, /UNSUPPORTED_LOCAL: private PostgreSQL required/)
        }
      })
    }
    await t.test('the config itself rejects skips and zero selection', async () => {
      for (const [name, body] of [['skip', `test.skip('skip', async () => {})`], ['empty', '']]) {
        const spec = join(root, `direct-${name}.spec.mjs`)
        await writeFile(spec, imports + body + '\n')
        const result = await run(playwright, ['test', '--config', config], { LEAF_REGRESSION_SPEC: spec })
        assert.equal(result.code, 3, result.stdout + result.stderr)
      }
    })
    await t.test('canary passes through the real stack and proxy', { timeout: 600_000 }, async () => {
      const result = await run(playwright, ['test', '--config', config, '--grep', '@canary', '--workers=1'])
      assert.equal(result.code, 0, result.stdout + result.stderr)
      const report = JSON.parse(result.stdout.trim())
      assert.equal(report.selected, 1)
      assert.equal(report.results.length, 1)
      assert.equal(report.results[0].status, 'passed')
      assert.equal(report.status, 'passed')
    })
  } finally { await rm(root, { recursive: true, force: true }) }
})
