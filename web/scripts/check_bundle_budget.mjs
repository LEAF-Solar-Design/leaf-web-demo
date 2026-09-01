// Bundle-budget receipt (convergence plan W0/P0b). Fails closed when the
// built bundle's TOTAL gzip size exceeds the committed baseline plus its
// declared allowance, so a shell-convergence PR cannot silently regress the
// first load. Per-chunk numbers are reported (not gated) because chunk
// boundaries legitimately move during the convergence; the total is the
// contract. Assumes `npm run build` already ran (dist/assets exists).
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
// "index-CEIWaqwN.js" -> "index.js".
const stem = (name) => {
  const dot = name.lastIndexOf('.')
  const base = name.slice(0, dot)
  const ext = name.slice(dot)
  const dash = base.indexOf('-')
  return (dash === -1 ? base : base.slice(0, dash)) + ext
}

const chunks = {}
for (const name of readdirSync(DIST)) {
  if (!name.endsWith('.js') && !name.endsWith('.css')) continue
  const gz = gzipSync(readFileSync(join(DIST, name)), { level: 9 }).length
  const key = stem(name)
  chunks[key] = (chunks[key] || 0) + gz
}
const total = Object.values(chunks).reduce((a, b) => a + b, 0)

if (process.argv.includes('--write-baseline')) {
  let ref = 'unknown'
  try { ref = execSync('git rev-parse --short HEAD', { cwd: ROOT }).toString().trim() } catch {}
  const prev = JSON.parse(readFileSync(BASELINE_PATH, 'utf8'))
  writeFileSync(BASELINE_PATH, JSON.stringify({
    ...prev, _ref: ref, chunks, total,
  }, null, 2) + '\n')
  console.log(`bundle baseline reseeded at ${ref}: total ${total} gz bytes`)
  process.exit(0)
}

const baseline = JSON.parse(readFileSync(BASELINE_PATH, 'utf8'))
const ceiling = baseline.total + baseline.total_allowance_bytes
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
console.log('bundle budget ok')
