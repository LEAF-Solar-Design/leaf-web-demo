// Bundle-budget receipt (convergence plan W0/P0b). Fails closed when the
// built bundle's TOTAL gzip size exceeds the committed baseline plus its
// declared allowance, so a shell-convergence PR cannot silently regress the
// first load. Canvas stems additionally have hard caps, including the full
// ELK and XYFlow dependency code. Normal checks read npm run build output.
//
// Usage:
//   node scripts/check_bundle_budget.mjs                  # gate against baseline
//   node scripts/check_bundle_budget.mjs --write-baseline # reseed after a deliberate change
//   node scripts/check_bundle_budget.mjs --write-baseline --before <ref> # explicit before source
import { existsSync, readdirSync, readFileSync, realpathSync } from 'node:fs'
import { gzipSync } from 'node:zlib'
import { join, dirname, isAbsolute, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { execFileSync } from 'node:child_process'

import { bundleGraph } from './bundle-assets.mjs'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const DIST = join(ROOT, 'dist', 'assets')
const BASELINE_PATH = join(ROOT, 'scripts', 'bundle-baseline.json')

// Hash-stripped stem so content-hash churn never fails the gate:
// "index-CEIWaqwN.js" -> "index.js". Neither first-dash (merged
// "vendor-viewer" into "vendor"; panel W0) nor last-dash (Rollup hashes may
// CONTAIN dashes, so the split lands mid-hash) is stable: strip the
// fixed-width trailing "-<8 hash chars>" instead.
export const stem = (name) => {
  const dot = name.lastIndexOf('.')
  return name.slice(0, dot).replace(/-[A-Za-z0-9_-]{8}$/, '') + name.slice(dot)
}
const isCanvasStem = name => /^(LabCanvas|elk[^/]*|xyflow[^/]*)\.(js|css)$/.test(name)

export function beforeReference(args, mergeBase) {
  const index = args.indexOf('--before')
  if (index !== -1) {
    if (!args[index + 1] || args[index + 1].startsWith('--')) throw new Error('--before requires a git ref')
    return args[index + 1]
  }
  if (!mergeBase) throw new Error('Cannot resolve merge-base with origin/main; supply --before <ref>')
  return mergeBase
}

export function beforeOverlay(sourceRef, root = ROOT, git = execFileSync) {
  const run = args => git('git', args, { cwd: dirname(root), encoding: 'utf8' })
  const paths = new Set(run(['diff', '--name-only', '--no-renames', '-z', sourceRef, '--', 'web/src']).split('\0').filter(Boolean))
  const status = run(['status', '--porcelain', '-z', '--untracked-files=all', '--', 'web/src']).split('\0')
  for (let index = 0; index < status.length; index++) {
    const entry = status[index]
    if (!entry) continue
    paths.add(entry.slice(3))
    if (/[RC]/.test(entry.slice(0, 2))) paths.add(status[++index])
  }
  const overlaidPaths = [...paths].filter(path => path?.startsWith('web/src/')).sort()
  if (overlaidPaths.length > 400) throw new Error('too many changed files to isolate')
  const beforePaths = new Set(run(['ls-tree', '-r', '--name-only', '-z', sourceRef, '--', 'web/src']).split('\0'))
  const original = new Map(overlaidPaths.map(path => [
    resolve(dirname(root), path).replaceAll('\\', '/'),
    beforePaths.has(path) ? run(['show', `${sourceRef}:${path}`]) : 'export {}',
  ]))
  const deleted = new Set([...original.keys()].filter(path => !existsSync(path)))
  const cleanId = id => id.split('?')[0].replaceAll('\\', '/')
  return { paths: overlaidPaths, plugin: {
    name: 'before-overlay', enforce: 'pre',
    resolveId(source, importer) {
      if (!source.startsWith('.') && !isAbsolute(source)) return null
      if (source.startsWith('.') && !importer) return null
      const path = cleanId(source.startsWith('/src/') ? resolve(root, `.${source}`) :
        resolve(importer ? dirname(cleanId(importer)) : root, source))
      const candidates = [path, ...['.js', '.jsx', '.ts', '.tsx', '.mjs', '.json', '.css'].map(ext => path + ext),
        ...['/index.js', '/index.jsx', '/index.ts', '/index.tsx'].map(entry => path + entry)]
      const match = candidates.find(candidate => deleted.has(candidate))
      return match ? `${match}?before-overlay` : null
    },
    load(id) { return original.get(cleanId(id)) ?? null },
  } }
}

export function measurementReceipt({ sourceRef, before, after, previous, canvas, deltas, paths }) {
  const measurement = {
    method: 'gzip-9, complete JS/CSS, Vite in-memory builds; merge-base or explicit --before changed web/src files before, working tree after',
    source_ref: sourceRef, before_total: before.total, after_total: after.total,
    delta: after.total - before.total, deltas,
    baseline_before_total: previous.total,
    unrelated_drift_bytes: before.total - previous.total,
    canvas_dependency_gzip_bytes: canvas.reduce((sum, [, bytes]) => sum + bytes, 0),
    cap_headroom_bytes: 4096,
    overlaid_paths: paths.length,
    overlaid_sample: paths.slice(0, 20),
  }
  return { isolated_measurement: measurement, isolated_canvas_measurement: measurement }
}

export function isolatedChunks(previous, before, after) {
  const names = new Set([...Object.keys(before.chunks), ...Object.keys(after.chunks)])
  if ([...names].every(name => before.chunks[name] === after.chunks[name])) {
    throw new Error('Before and after bundles are identical; refusing to reseed')
  }
  const chunks = { ...previous.chunks }
  const deltas = {}
  for (const name of names) {
    const delta = (after.chunks[name] || 0) - (before.chunks[name] || 0)
    if (!delta) continue
    deltas[name] = delta
    chunks[name] = (chunks[name] || 0) + delta
    if (chunks[name] < 0) throw new Error(`Cannot isolate ${name} from the old baseline`)
    if (!chunks[name]) delete chunks[name]
  }
  return { chunks, deltas,
    required_stems: [...new Set([...(previous.required_stems || []), 'LabCanvas.js', 'LabCanvas.css'])] }
}

export function stemFailures(chunks, baseline) {
  const failures = []
  // Older baselines include ELK inside LabCanvas.js. Until an explicit reseed
  // caps the new lazy stem separately, enforce that same combined byte ceiling.
  const sharedElkCap = Object.hasOwn(baseline.stem_caps || {}, 'LabCanvas.js') &&
    !Object.hasOwn(baseline.stem_caps || {}, 'elk.bundled.js')
  for (const key of new Set([...(baseline.required_stems || []), ...Object.keys(baseline.stem_caps || {})])) {
    if (!Object.hasOwn(chunks, key)) failures.push(`required dependency stem ${key} missing from the production build`)
  }
  for (const [key, cap] of Object.entries(baseline.stem_caps || {})) {
    const size = chunks[key] + (key === 'LabCanvas.js' && sharedElkCap ? (chunks['elk.bundled.js'] || 0) : 0)
    if (!Number.isSafeInteger(cap) || cap < 0 || size > cap) failures.push(`${key} ${size} > hard stem cap ${cap}`)
  }
  for (const key of Object.keys(chunks)) {
    if (isCanvasStem(key) && !Object.hasOwn(baseline.stem_caps || {}, key) &&
      !(key === 'elk.bundled.js' && sharedElkCap)) {
      failures.push(`canvas stem ${key} has no hard cap`)
    }
  }
  return failures
}

async function main() {
if (process.argv.includes('--write-baseline')) {
  throw new Error('Automatic Vite reseeding is retired. Supply a reviewed before/after Next artifact measurement before changing the committed baseline.')
}
const nextDist = join(ROOT, 'dist', '_next', 'static')
if (existsSync(nextDist)) {
  const graph = bundleGraph(join(ROOT, 'dist'))
  const baseline = JSON.parse(readFileSync(BASELINE_PATH, 'utf8'))
  const bytes = file => gzipSync(graph.assets.get(file), { level: 9 }).length
  const files = [...graph.assets.keys()].filter(file => file.startsWith('_next/static/') && /\.(js|css)$/.test(file))
  // Comparable to the old budget: sum gzip-9 of every emitted JS/CSS file,
  // including lazy and worker chunks, once each. Engine glue/wasm was never
  // part of dist/assets and is still outside this metric.
  const total = files.reduce((sum, file) => sum + bytes(file), 0)
  // Startup is a separate diagnostic, not a replacement for the total cap:
  // HTML script/link roots plus their eager import closure; dynamic imports
  // are excluded. Framework/runtime bytes count just like application bytes.
  const startup = [...graph.startup].reduce((sum, file) => sum + bytes(file), 0)
  const ceiling = baseline.total + baseline.total_allowance_bytes
  if (total > ceiling) throw new Error(`bundle budget EXCEEDED: all Next JS/CSS gzip-9 ${total} > ${ceiling}`)
  // React.lazy need not appear in Next's react-loadable manifest. Its
  // unique emitted markup identifies the chunk, and emitted dependency
  // edges bind the lazy closure without trusting a producer claim.
  const canvasFiles = new Set([...graph.assets].filter(([file, code]) =>
    file.endsWith('.js') && code.includes('lab-canvas-editor')).map(([file]) => file))
  if (!canvasFiles.size) throw new Error('No emitted lazy LabCanvas dependency evidence')
  for (const file of canvasFiles) if (!graph.reachable.has(file)) throw new Error(`Unreachable lazy canvas asset: ${file}`)
  const closure = graph.closure([...canvasFiles])
  const js = [...closure].filter(file => file.endsWith('.js') && !graph.startup.has(file))
  const css = files.filter(file => file.endsWith('.css') && graph.assets.get(file).includes('.react-flow'))
  // Webpack may merge ELK with its consumer. Preserve the old combined hard
  // ceiling rather than requiring Rollup's old filenames or adding headroom.
  const jsCap = baseline.stem_caps['LabCanvas.js'] + baseline.stem_caps['elk.bundled.js']
  const cssCap = baseline.stem_caps['LabCanvas.css']
  const jsBytes = js.reduce((sum, file) => sum + bytes(file), 0)
  const cssBytes = css.reduce((sum, file) => sum + bytes(file), 0)
  if (!Number.isSafeInteger(jsCap) || jsBytes > jsCap) throw new Error(`lazy canvas/ELK gzip-9 ${jsBytes} > ${jsCap}`)
  if (!Number.isSafeInteger(cssCap) || !css.length || cssBytes > cssCap) throw new Error(`lazy canvas CSS gzip-9 ${cssBytes} > ${cssCap}; missing CSS also fails`)
  if ([...canvasFiles].some(file => graph.startup.has(file))) throw new Error('Lazy canvas has entered the startup script closure')
  const initial = [...graph.startup].map(file => graph.assets.get(file)).join('\n')
  if (/__THREE__|three\.(?:module|core)|node_modules[/\\\\]three[/\\\\]/.test(initial)) throw new Error('Viewer dependency in startup scripts')
  console.log(`bundle budget ok: complete Next JS/CSS ${total} gzip-9 bytes (ceiling ${ceiling}); startup ${startup}; lazy canvas/ELK ${jsBytes} JS + ${cssBytes} CSS`)
  return
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
const failures = stemFailures(chunks, baseline)
for (const failure of failures) console.error(`bundle budget EXCEEDED: ${failure}`)
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
if (failures.length) process.exit(1)
console.log('bundle budget ok')
}

// Node resolves module URLs through symlinks, while argv can retain an alias
// such as macOS /var instead of /private/var. Compare the same real paths.
if (process.argv[1] && realpathSync(resolve(process.argv[1])) === realpathSync(fileURLToPath(import.meta.url))) {
  main().catch(error => { console.error(error.message); process.exitCode = 1 })
}
