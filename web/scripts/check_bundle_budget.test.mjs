import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { copyFileSync, mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { gzipSync } from 'node:zlib'
import { beforeOverlay, beforeReference, isolatedChunks, measurementReceipt, stem, stemFailures } from './check_bundle_budget.mjs'

function overlayFixture({ diff = [], status = [], original = {} } = {}) {
  const root = join(tmpdir(), 'bundle-overlay-fixture', 'web')
  const calls = []
  const git = (command, args, options) => {
    assert.equal(command, 'git')
    assert.equal(options.cwd, dirname(root))
    calls.push(args)
    if (args[0] === 'diff') {
      assert.deepEqual(args, ['diff', '--name-only', '--no-renames', '-z', 'before-sha', '--', 'web/src'])
      return diff.join('\0') + '\0'
    }
    if (args[0] === 'status') {
      assert.deepEqual(args, ['status', '--porcelain', '-z', '--untracked-files=all', '--', 'web/src'])
      return status.join('\0') + '\0'
    }
    if (args[0] === 'ls-tree') {
      assert.deepEqual(args, ['ls-tree', '-r', '--name-only', '-z', 'before-sha', '--', 'web/src'])
      return Object.keys(original).join('\0') + '\0'
    }
    assert.equal(args[0], 'show')
    const path = args[1].slice('before-sha:'.length)
    assert.ok(Object.hasOwn(original, path), `unexpected before source ${path}`)
    return original[path]
  }
  return { root, calls, create: () => beforeOverlay('before-sha', root, git) }
}

test('before overlay unions committed, staged, unstaged and untracked source paths', () => {
  const fixture = overlayFixture({
    diff: ['web/src/LabGround.jsx', 'web/src/staged.js'],
    status: [' M web/src/LabGround.jsx', 'M  web/src/staged.js', ' M web/src/dirty.js',
      '?? web/src/embed/new.js', 'R  web/src/renamed.js', 'web/src/old.js'],
    original: { 'web/src/LabGround.jsx': 'export const before = true', 'web/src/dirty.js': 'old dirty source' },
  })
  const { paths, plugin } = fixture.create()
  assert.deepEqual(paths, ['web/src/LabGround.jsx', 'web/src/dirty.js', 'web/src/embed/new.js',
    'web/src/old.js', 'web/src/renamed.js', 'web/src/staged.js'])
  assert.equal(plugin.name, 'before-overlay')
  assert.equal(plugin.enforce, 'pre')
  assert.equal(plugin.load(join(fixture.root, 'src', 'LabGround.jsx')), 'export const before = true')
  assert.equal(plugin.load(join(fixture.root, 'src', 'dirty.js') + '?query'), 'old dirty source')
  assert.equal(plugin.load(join(fixture.root, 'src', 'unchanged.js')), null)
})

test('a source file added since the before ref loads as an empty module', () => {
  const fixture = overlayFixture({ diff: ['web/src/embed/added.js'] })
  const { plugin } = fixture.create()
  assert.equal(plugin.load(join(fixture.root, 'src', 'embed', 'added.js')), 'export {}')
  assert.equal(fixture.calls.some(args => args[0] === 'show'), false)
})

test('a deleted source import resolves to its before content, including extensionless imports', () => {
  const fixture = overlayFixture({
    diff: ['web/src/embed/deleted.js', 'web/src/embed/deleted.css'],
    status: [' D web/src/embed/deleted.js'],
    original: { 'web/src/embed/deleted.js': 'export const restored = true', 'web/src/embed/deleted.css': '.before {}' },
  })
  const { paths, plugin } = fixture.create()
  assert.equal(paths.length, 2)
  const importer = join(fixture.root, 'src', 'main.jsx')
  const id = plugin.resolveId('./embed/deleted.js', importer)
  assert.match(id, /\?before-overlay$/)
  assert.equal(plugin.load(id), 'export const restored = true')
  assert.equal(plugin.resolveId('./embed/deleted', importer), id)
  assert.equal(plugin.resolveId('/src/embed/deleted.js', importer), id)
  assert.equal(plugin.resolveId(join(fixture.root, 'src', 'embed', 'deleted.js'), importer), id)
  assert.equal(plugin.load(plugin.resolveId('./embed/deleted.css', importer)), '.before {}')
  assert.equal(plugin.resolveId('react', importer), null)
  assert.equal(plugin.resolveId('./unchanged.js', importer), null)
  assert.equal(plugin.resolveId('./deleted.js', id), id)
})

test('before overlay permits 400 distinct paths and refuses 401 before loading sources', () => {
  const diff = Array.from({ length: 400 }, (_, index) => `web/src/file${index}.js`)
  assert.equal(overlayFixture({ diff, status: [` M ${diff[0]}`] }).create().paths.length, 400)
  const fixture = overlayFixture({ diff, status: ['?? web/src/extra.js'] })
  assert.throws(() => fixture.create(), /too many changed files to isolate/)
  assert.deepEqual(fixture.calls.map(args => args[0]), ['diff', 'status'])
})

test('isolated measurement retains its legacy alias and records the first 20 overlay paths', () => {
  const paths = Array.from({ length: 25 }, (_, index) => `web/src/file${index}.js`)
  const receipt = measurementReceipt({ sourceRef: 'before-sha', before: { total: 110 },
    after: { total: 140 }, previous: { total: 100 }, canvas: [['LabCanvas.js', 20], ['LabCanvas.css', 5]],
    deltas: { 'index.js': 30 }, paths })
  assert.equal(receipt.isolated_canvas_measurement, receipt.isolated_measurement)
  assert.deepEqual(receipt.isolated_measurement, {
    method: 'gzip-9, complete JS/CSS, Vite in-memory builds; merge-base or explicit --before changed web/src files before, working tree after',
    source_ref: 'before-sha', before_total: 110, after_total: 140, delta: 30,
    deltas: { 'index.js': 30 }, baseline_before_total: 100, unrelated_drift_bytes: 10,
    canvas_dependency_gzip_bytes: 25, cap_headroom_bytes: 4096,
    overlaid_paths: 25, overlaid_sample: paths.slice(0, 20),
  })
})

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
