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

// Every POSTing client must declare JSON: fetch defaults a string body to
// text/plain and FastAPI rejects it before the route runs (sol-critic PR #437
// round 1, finding 2 — the merge call shipped without the header).
for (const fn of ['proposePlatformChange', 'landPlatformChange', 'mergePlatformChange']) {
  const start = api.indexOf(`export async function ${fn}`)
  assert(start !== -1, `${fn} must exist`)
  const bodyEnd = api.indexOf('}\n', api.indexOf('body:', start))
  assert(api.slice(start, bodyEnd).includes("'Content-Type': 'application/json'"), `${fn} must send Content-Type: application/json`)
}

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

// Mount gating: strict entitlement (no permissive-unknown fallback) plus live
// mode. The query flag remains a deep link, but an entitled admin must also get
// a visible header entry so platform self-edit is discoverable.
assert(app.includes("entitlements?.entitlements?.platform_customize === true"), 'panel mount must gate on a STRICT platform_customize grant')
assert(app.includes("get('customize') === '1'"), 'panel must preserve the ?customize=1 deep link')
assert(app.includes('const canOpenCustomize = !mock && signedIn && platformCustomizeEntitled'), 'the shared gate must require live mode, an active token, and strict entitlement')
const exitGuard = app.match(/const customizeExit = useExit\(([^)\n]+)\)/)
assert(exitGuard?.[1].trim() === 'customizeOpen && canOpenCustomize', 'panel mount must use only the fail-closed open-and-authorized guard')
assert(app.includes('{canOpenCustomize && (') && app.includes('>Customize</button>'), 'an authorized admin must get a visible Customize entry')
assert(app.includes('onClick={() => setCustomizeOpen(true)}'), 'the visible Customize entry must open the guarded panel')
assert(app.includes('subscribeUnauthorized(() => {') && app.includes('setCustomizeOpen(false)'), 'a 401 must close the privileged drawer immediately')

// Guard strings alone cannot prove the mount is gated: a SECOND unconditional
// <CustomizePanel /> would keep every assert above green. Pin the mount count
// to exactly one, require that one to sit behind customizeExit.shown, and pin
// the component to App.jsx as its only consumer.
const mountSites = app.match(/<CustomizePanel\b/g) || []
assert(mountSites.length === 1, `App.jsx must mount CustomizePanel exactly once (found ${mountSites.length})`)
assert(/\{customizeExit\.shown && <CustomizePanel\b/.test(app), 'the single CustomizePanel mount must be behind customizeExit.shown')
// Exact-path allowlist, not endsWith: "OtherApp.jsx" must NOT be exempt.
const allowedConsumers = new Set([
  join(srcDir, 'App.jsx'),
  join(srcDir, 'components', 'CustomizePanel.jsx'),
])
for (const file of await walk(srcDir)) {
  if (allowedConsumers.has(file)) continue
  const text = await readFile(file, 'utf8')
  assert(!text.includes('CustomizePanel'), `CustomizePanel must have no consumer besides App.jsx: ${file}`)
}

// Landing ack: the exact recorded commit, never a re-read of the mutable ref.
assert(panel.includes('landPlatformChange(record.change_id, record.commit_sha)'), 'land must acknowledge the recorded commit sha')

// Merge ack (#422 Phase 3): same exact-commit discipline as land, and the
// button may only be OFFERED on a passed review with the PR still open —
// the server re-verifies, but the drawer must not invite a doomed approval.
assert(panel.includes('mergePlatformChange(record.change_id, record.commit_sha)'), 'merge must acknowledge the recorded commit sha')
assert(panel.includes("record.review?.state === 'passed' && record.review?.pr_state === 'open'"), 'the merge affordance must be gated on a passed review and an open PR')
assert((panel.match(/mergePlatformChange\(/g) || []).length === 1, 'exactly one merge call site')

// Calm-copy pins for the hold and handoff states.
assert(panel.includes('Awaiting co-sign'), 'awaiting_cosign must render as a calm hold')
assert(panel.includes('co-sign authority never enters the browser'), 'the drawer must state the co-sign boundary')
assert(panel.includes('merging needs your fresh approval of the exact commit'), 'the drawer must state the merge-approval boundary')
assert(panel.includes('nothing deploys from here'), 'the drawer must state the no-deploy boundary')

console.log('customize panel web checks passed')
