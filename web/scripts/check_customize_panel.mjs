import { readFile } from 'node:fs/promises'
import { readdir } from 'node:fs/promises'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = new URL('../', import.meta.url)
const api = await readFile(new URL('src/api.js', root), 'utf8')
const app = await readFile(new URL('src/App.jsx', root), 'utf8')
const panel = await readFile(new URL('src/components/CustomizePanel.jsx', root), 'utf8')

function assert(ok, message) {
  if (!ok) throw new Error(message)
}

// Routes: the browser client speaks only the three tenant-JWT routes.
assert(api.includes("'/api/platform/customize'"), 'self-edit client must use the canonical propose route')
assert(api.includes('/api/platform/customize/${'), 'self-edit client must use the status/land routes by change id')

// The co-sign approval authority must NEVER enter the browser: no client for
// the secret-authenticated internal routes, no approval-secret header, in ANY
// web source file (not just the two we know about today).
const srcDir = fileURLToPath(new URL('src/', root))
async function walk(dir) {
  const out = []
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const p = join(dir, entry.name)
    if (entry.isDirectory()) out.push(...await walk(p))
    else if (/\.(jsx?|css)$/.test(entry.name)) out.push(p)
  }
  return out
}
for (const file of await walk(srcDir)) {
  const text = await readFile(file, 'utf8')
  assert(!text.includes('/internal/platform-customize'), `co-sign routes must not appear in the browser bundle: ${file}`)
  assert(!text.includes('X-Approval-Secret'), `approval secret header must not appear in the browser bundle: ${file}`)
}

// Mount gating: strict entitlement (no permissive-unknown fallback), plus the
// hidden ?customize=1 flag, plus live mode. All three, verbatim.
assert(app.includes("entitlements?.entitlements?.platform_customize === true"), 'panel mount must gate on a STRICT platform_customize grant')
assert(app.includes("get('customize') === '1'"), 'panel must stay behind the ?customize=1 flag')
assert(app.includes('customizeFlag && !mock && !customizeDismissed && platformCustomizeEntitled'), 'panel mount must require flag AND live mode AND strict entitlement')

// Guard strings alone cannot prove the mount is gated: a SECOND unconditional
// <CustomizePanel /> would keep every assert above green. Pin the mount count
// to exactly one, require that one to sit behind customizeExit.shown, and pin
// the component to App.jsx as its only consumer.
const mountSites = app.match(/<CustomizePanel\b/g) || []
assert(mountSites.length === 1, `App.jsx must mount CustomizePanel exactly once (found ${mountSites.length})`)
assert(/\{customizeExit\.shown && <CustomizePanel\b/.test(app), 'the single CustomizePanel mount must be behind customizeExit.shown')
for (const file of await walk(srcDir)) {
  if (file.endsWith('App.jsx') || file.endsWith('CustomizePanel.jsx')) continue
  const text = await readFile(file, 'utf8')
  assert(!text.includes('CustomizePanel'), `CustomizePanel must have no consumer besides App.jsx: ${file}`)
}

// Landing ack: the exact recorded commit, never a re-read of the mutable ref.
assert(panel.includes('landPlatformChange(record.change_id, record.commit_sha)'), 'land must acknowledge the recorded commit sha')

// Calm-copy pins for the hold and handoff states.
assert(panel.includes('Awaiting co-sign'), 'awaiting_cosign must render as a calm hold')
assert(panel.includes('co-sign authority never enters the browser'), 'the drawer must state the co-sign boundary')
assert(panel.includes('nothing merges or deploys from here'), 'the drawer must state the branch-only boundary')

console.log('customize panel web checks passed')
