import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { copyFileSync, mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { gzipSync } from 'node:zlib'
import { beforeReference, isolatedChunks, stem, stemFailures } from './check_bundle_budget.mjs'

test('the CLI enforces the ceiling when invoked through an aliased directory', () => {
  const temporary = mkdtempSync(join(tmpdir(), 'bundle-gate-alias-'))
  try {
    const root = join(temporary, 'real')
    const alias = join(temporary, 'alias')
    const scripts = join(root, 'scripts')
    const assets = join(root, 'dist', 'assets')
    mkdirSync(scripts, { recursive: true })
    mkdirSync(assets, { recursive: true })
    copyFileSync(fileURLToPath(new URL('./check_bundle_budget.mjs', import.meta.url)), join(scripts, 'check_bundle_budget.mjs'))
    symlinkSync(root, alias, 'junction')
    const buffer = Buffer.from('export const budgetFixture = "aliased directory";\n')
    const bytes = gzipSync(buffer, { level: 9 }).length
    writeFileSync(join(assets, 'index-ABCDEFGH.js'), buffer)
    const baseline = { chunks: {}, total: bytes, total_allowance_bytes: 0, _ref: 'test' }
    const run = () => spawnSync(process.execPath, [join(alias, 'scripts', 'check_bundle_budget.mjs')], { encoding: 'utf8', timeout: 30000 })
    writeFileSync(join(scripts, 'bundle-baseline.json'), JSON.stringify(baseline))
    const exact = run()
    assert.equal(exact.error, undefined)
    assert.equal(exact.status, 0, exact.stderr)
    assert.match(exact.stdout, /bundle budget ok/)
    writeFileSync(join(scripts, 'bundle-baseline.json'), JSON.stringify({ ...baseline, total: bytes - 1 }))
    const over = run()
    assert.equal(over.error, undefined)
    assert.equal(over.status, 1, over.stderr)
    assert.match(over.stderr, /bundle budget EXCEEDED/)
  } finally { rmSync(temporary, { recursive: true, force: true }) }
})

test('the lazy ELK chunk is charged to the full canvas dependency cap', () => {
  const chunks = {}
  for (const [name, bytes] of [['LabCanvas-Abc_1234.js', 40], ['elk.bundled-aB_-1234.js', 61]]) {
    const key = stem(name)
    chunks[key] = (chunks[key] || 0) + bytes
  }
  assert.deepEqual(chunks, { 'LabCanvas.js': 40, 'elk.bundled.js': 61 })
  assert.match(stemFailures(chunks, { stem_caps: { 'LabCanvas.js': 100 } }).join('\n'), /hard stem cap/)
  assert.deepEqual(stemFailures(chunks, { stem_caps: { 'LabCanvas.js': 101 } }), [])
  assert.match(stemFailures({ 'elk.bundled.js': 61 }, { stem_caps: { 'LabCanvas.js': 101 } }).join('\n'), /LabCanvas.js missing/)
  assert.match(stemFailures(chunks, { stem_caps: { 'LabCanvas.js': 40, 'elk.bundled.js': 60 } }).join('\n'), /elk.bundled.js.*hard stem cap/)
  assert.match(stemFailures({ 'elk.bundled.js': 61 }, { stem_caps: {} }).join('\n'), /no hard cap/)
  assert.equal(stem('LabCanvas-Abc_1234.css'), 'LabCanvas.css')
  assert.equal(stem('vendor-react-Abc_1234.js'), 'vendor-react.js')
})

test('before overlay uses the merge-base or an explicit ref, with no HEAD fallback', () => {
  assert.equal(beforeReference([], 'merge-base-sha'), 'merge-base-sha')
  assert.equal(beforeReference(['--before', 'original-canvas-parent'], 'merge-base-sha'), 'original-canvas-parent')
  assert.throws(() => beforeReference([], null), /merge-base.*--before/)
  assert.throws(() => beforeReference(['--before'], 'merge-base-sha'), /requires a git ref/)
})

test('identical before and after bundles cannot reseed, even with chunk order changed', () => {
  assert.throws(() => isolatedChunks({ chunks: {} },
    { chunks: { 'index.js': 12, 'index.css': 3 } },
    { chunks: { 'index.css': 3, 'index.js': 12 } }), /identical.*refusing/)
})

test('isolates only the canvas delta and requires both lazy stems when reseeding', () => {
  const result = isolatedChunks({ chunks: { 'index.js': 10 }, required_stems: ['index.js'] },
    { chunks: { 'index.js': 20 } },
    { chunks: { 'index.js': 20, 'LabCanvas.js': 100, 'LabCanvas.css': 5 } })
  assert.deepEqual(result.chunks, { 'index.js': 10, 'LabCanvas.js': 100, 'LabCanvas.css': 5 })
  assert.deepEqual(result.required_stems, ['index.js', 'LabCanvas.js', 'LabCanvas.css'])
})

test('every capped stem must be present even without required_stems', () => {
  const baseline = { stem_caps: { 'LabCanvas.js': 100, 'LabCanvas.css': 10 } }
  assert.match(stemFailures({}, baseline).join('\n'), /LabCanvas.js missing/)
  assert.match(stemFailures({ 'LabCanvas.js': 90 }, baseline).join('\n'), /LabCanvas.css missing/)
  assert.deepEqual(stemFailures({ 'LabCanvas.js': 100, 'LabCanvas.css': 10 }, baseline), [])
  assert.match(stemFailures({ 'LabCanvas.js': 101, 'LabCanvas.css': 10 }, baseline).join('\n'), /hard stem cap/)
})
