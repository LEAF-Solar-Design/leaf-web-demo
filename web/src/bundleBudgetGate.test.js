// @vitest-environment node
import { spawnSync } from 'node:child_process'
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { gzipSync } from 'node:zlib'
import { expect, test } from 'vitest'

function runBudgetGate(assets, baseline) {
  const root = mkdtempSync(join(tmpdir(), 'bundle-gate-'))
  try {
    const scripts = join(root, 'scripts')
    const assetDirectory = join(root, 'dist', 'assets')
    mkdirSync(scripts, { recursive: true })
    mkdirSync(assetDirectory, { recursive: true })
    const script = join(scripts, 'check_bundle_budget.mjs')
    copyFileSync(resolve(process.cwd(), 'scripts', 'check_bundle_budget.mjs'), script)
    writeFileSync(join(scripts, 'bundle-baseline.json'), JSON.stringify(baseline))
    for (const [name, buffer] of Object.entries(assets)) {
      writeFileSync(join(assetDirectory, name), buffer)
    }
    return spawnSync(process.execPath, [script], { encoding: 'utf8', timeout: 30000 })
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
}

function committedBaseline() {
  return JSON.parse(readFileSync(resolve(process.cwd(), 'scripts', 'bundle-baseline.json'), 'utf8'))
}

test('passes when the gzip total equals the ceiling', () => {
  const buffer = Buffer.from('export const budgetFixture = "exact ceiling";\n')
  const gz = gzipSync(buffer, { level: 9 }).length
  const allowance = 16
  const result = runBudgetGate({ 'index-ABCDEFGH.js': buffer }, {
    total: gz - allowance,
    total_allowance_bytes: allowance,
    chunks: {},
    _ref: 'test',
  })
  expect(result.error).toBeUndefined()
  expect(result.status).toBe(0)
  expect(result.stdout).toContain('bundle budget ok')
})

test('fails closed one byte over the ceiling', () => {
  const buffer = Buffer.from('export const budgetFixture = "exact ceiling";\n')
  const gz = gzipSync(buffer, { level: 9 }).length
  const allowance = 16
  const result = runBudgetGate({ 'index-ABCDEFGH.js': buffer }, {
    total: gz - allowance - 1,
    total_allowance_bytes: allowance,
    chunks: {},
    _ref: 'test',
  })
  expect(result.error).toBeUndefined()
  expect(result.status).toBe(1)
  expect(result.stderr).toContain('bundle budget EXCEEDED')
})

test('folds hashed chunk names into one stem', () => {
  const first = Buffer.from('export const firstFixture = "first chunk";\n')
  const second = Buffer.from('export const secondFixture = "second chunk";\n')
  const gz = gzipSync(first, { level: 9 }).length + gzipSync(second, { level: 9 }).length
  const result = runBudgetGate({
    'index-ABCDEFGH.js': first,
    'index-IJKLMNOP.js': second,
  }, {
    total: gz,
    total_allowance_bytes: 4096,
    chunks: {},
    _ref: 'test',
  })
  expect(result.error).toBeUndefined()
  expect(result.status).toBe(0)
  const reportLines = result.stdout.split(/\r?\n/).filter((line) => /^\s*index\.js\s/.test(line))
  expect(reportLines).toHaveLength(1)
  const report = reportLines[0].match(/^\s*index\.js\s+(\d+)\s+gz\b/)
  expect(report).not.toBeNull()
  expect(Number(report[1])).toBe(gz)
  expect(result.stdout).not.toContain('index-ABCDEFGH')
  expect(result.stdout).not.toContain('index-IJKLMNOP')
})

test('committed baseline total equals the sum of its chunks', () => {
  const baseline = committedBaseline()
  expect(baseline.total).toBe(Object.values(baseline.chunks).reduce((sum, size) => sum + size, 0))
})

test('committed allowance never drops below the measured rebuild noise', () => {
  expect(committedBaseline().total_allowance_bytes).toBeGreaterThanOrEqual(4096)
})
