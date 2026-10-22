// Bundle-budget receipt (convergence plan W0/P0b). Fails closed when the
// built bundle's TOTAL gzip size exceeds the committed baseline plus its
// declared allowance, so a shell-convergence PR cannot silently regress the
// first load. Canvas stems additionally have hard caps, including the full
// ELK and XYFlow dependency code. Normal checks read npm run build output.
//
// Usage:
//   node scripts/check_bundle_budget.mjs                  # gate against baseline
//   node scripts/check_bundle_budget.mjs --write-baseline # reseed after a deliberate change
import { readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { gzipSync } from 'node:zlib'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { execSync } from 'node:child_process'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const DIST = join(ROOT, 'dist', 'assets')
const BASELINE_PATH = join(ROOT, 'scripts', 'bundle-baseline.json')

// Hash-stripped stem so content-hash churn never fails the gate:
// "index-CEIWaqwN.js" -> "index.js". Neither first-dash (merged
// "vendor-viewer" into "vendor"; panel W0) nor last-dash (Rollup hashes may
// CONTAIN dashes, so the split lands mid-hash) is stable: strip the
// fixed-width trailing "-<8 hash chars>" instead.
const stem = (name) => {
  const dot = name.lastIndexOf('.')
  return name.slice(0, dot).replace(/-[A-Za-z0-9_-]{8}$/, '') + name.slice(dot)
}

if (process.argv.includes('--write-baseline')) {
  // Isolate SEL-102 from unrelated drift since the older total baseline. Both
  // builds use identical dependencies/configuration; only the original graph
  // entry and shell styles are overlaid in memory for the before build.
  // No checkout edits, dist writes or temporary files are needed.
  const { build } = await import('vite')
  const previous = JSON.parse(readFileSync(BASELINE_PATH, 'utf8'))
  const original = new Map(['src/lab/canvas/GraphView.jsx', 'src/lab/lab.css'].map(path => [
    join(ROOT, path).replaceAll('\\', '/'),
    execSync(`git show HEAD:web/${path}`, { cwd: ROOT, encoding: 'utf8' }),
  ]))
  const measure = async before => {
    const result = await build({ root: ROOT, logLevel: 'warn',
      build: { write: false, emptyOutDir: false },
      plugins: before ? [{ name: 'sel102-before-overlay', enforce: 'pre',
        load(id) { return original.get(id.replaceAll('\\', '/')) ?? null },
      }] : [],
    })
    const chunks = {}
    for (const bundle of Array.isArray(result) ? result : [result]) for (const asset of bundle.output) {
      if (!/\.(js|css)$/.test(asset.fileName)) continue
      const name = stem(asset.fileName.split('/').at(-1))
      const content = asset.type === 'chunk' ? asset.code : asset.source
      chunks[name] = (chunks[name] || 0) + gzipSync(content, { level: 9 }).length
    }
    return { chunks, total: Object.values(chunks).reduce((sum, bytes) => sum + bytes, 0) }
  }
  const before = await measure(true)
  const after = await measure(false)
  const chunks = { ...previous.chunks }
  const deltas = {}
  for (const name of new Set([...Object.keys(before.chunks), ...Object.keys(after.chunks)])) {
    const delta = (after.chunks[name] || 0) - (before.chunks[name] || 0)
    if (!delta) continue
    deltas[name] = delta
    chunks[name] = (chunks[name] || 0) + delta
    if (chunks[name] < 0) throw new Error(`Cannot isolate ${name} from the old baseline`)
    if (!chunks[name]) delete chunks[name]
  }
  const canvas = Object.entries(after.chunks).filter(([name]) => /^(LabCanvas|elk[^.]*|xyflow[^.]*)\.(js|css)$/.test(name))
  if (!canvas.some(([name]) => name === 'LabCanvas.js')) {
    throw new Error(`Lazy LabCanvas.js chunk missing; wire LabWorkbench into the production app before reseeding. Observed before ${before.total}, after ${after.total} gzip bytes.`)
  }
  const ref = execSync('git rev-parse --short HEAD', { cwd: ROOT, encoding: 'utf8' }).trim()
  const total = Object.values(chunks).reduce((sum, bytes) => sum + bytes, 0)
  writeFileSync(BASELINE_PATH, JSON.stringify({ ...previous, _ref: ref, chunks, total,
    stem_caps: { ...previous.stem_caps, ...Object.fromEntries(canvas.map(([name, bytes]) => [name, bytes + 4096])) },
    isolated_canvas_measurement: {
      method: 'gzip-9, complete JS/CSS, Vite in-memory builds; HEAD graph entry and shell CSS before, working tree after',
      source_ref: ref, before_total: before.total, after_total: after.total,
      delta: after.total - before.total, deltas,
      baseline_before_total: previous.total,
      unrelated_drift_bytes: before.total - previous.total,
      canvas_dependency_gzip_bytes: canvas.reduce((sum, [, bytes]) => sum + bytes, 0),
      cap_headroom_bytes: 4096,
    },
  }, null, 2) + '\n')
  console.log(`SEL-102 isolated: before ${before.total}, after ${after.total}, delta ${after.total - before.total} gzip bytes`)
  console.log(`bundle baseline ${previous.total} -> ${total}; unrelated drift ${before.total - previous.total} bytes was not reseeded`)
  console.log(`canvas hard caps: ${JSON.stringify(Object.fromEntries(canvas.map(([name, bytes]) => [name, bytes + 4096])))}`)
  process.exit(0)
}

const chunks = {}
for (const name of readdirSync(DIST)) {
  if (!name.endsWith('.js') && !name.endsWith('.css')) continue
  const gz = gzipSync(readFileSync(join(DIST, name)), { level: 9 }).length
  const key = stem(name)
  chunks[key] = (chunks[key] || 0) + gz
}
const total = Object.values(chunks).reduce((a, b) => a + b, 0)

const baseline = JSON.parse(readFileSync(BASELINE_PATH, 'utf8'))
const ceiling = baseline.total + baseline.total_allowance_bytes
let failed = false
for (const key of baseline.required_stems || []) {
  if (!Object.hasOwn(chunks, key)) {
    console.error(`bundle budget EXCEEDED: required dependency stem ${key} missing from the production build`)
    failed = true
  }
}
for (const [key, cap] of Object.entries(baseline.stem_caps || {})) {
  if (!Number.isSafeInteger(cap) || cap < 0 || (chunks[key] || 0) > cap) {
    console.error(`bundle budget EXCEEDED: ${key} ${chunks[key] || 0} > hard stem cap ${cap}`)
    failed = true
  }
}
for (const key of Object.keys(chunks)) {
  if (/^(LabCanvas|elk[^.]*|xyflow[^.]*)\.(js|css)$/.test(key) && !Object.hasOwn(baseline.stem_caps || {}, key)) {
    console.error(`bundle budget EXCEEDED: canvas stem ${key} has no hard cap`)
    failed = true
  }
}
for (const [key, size] of Object.entries(chunks).sort()) {
  const was = baseline.chunks[key]
  const delta = was == null ? ' (new)' : ` (${size - was >= 0 ? '+' : ''}${size - was})`
  console.log(`  ${key.padEnd(18)} ${String(size).padStart(8)} gz${delta}`)
}
console.log(`  TOTAL              ${String(total).padStart(8)} gz (baseline ${baseline.total}, ceiling ${ceiling})`)
if (total > ceiling) {
  console.error(`bundle budget EXCEEDED: total ${total} > ceiling ${ceiling} (baseline ${baseline.total} @ ${baseline._ref} + allowance ${baseline.total_allowance_bytes}). Either shrink the bundle or, for a DELIBERATE budgeted increase, reseed with --write-baseline in the same PR and say why in its body.`)
  process.exit(1)
}
if (failed) process.exit(1)
console.log('bundle budget ok')
