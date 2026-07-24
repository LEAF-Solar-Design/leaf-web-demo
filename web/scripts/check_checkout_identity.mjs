// Oracle: the single-writer checkout holder id must identify a SESSION, not a
// tenant. Recomputed against the REAL module, so nothing here is hardcoded.
//
// The bug this pins: ownHolder used to be `tenant || config.tenant || 'demo-tenant'`,
// so every teammate in one org produced the SAME holder id. Teammate B then read
// teammate A's lock as their own ("You hold the edit lock", with a live Release
// button) and could release a lock they never took.
//
// Run: node scripts/check_checkout_identity.mjs   (from web/)
import { getSessionHolderId, HOLDER_STORAGE_KEY } from '../src/checkoutIdentity.js'

let failures = 0
const fail = (msg) => { failures++; console.error('FAIL:', msg) }
const pass = (msg) => console.log('PASS:', msg)

// A sessionStorage stub. One stub == one browser tab.
function makeStorage(seed = {}) {
  const map = new Map(Object.entries(seed))
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => { map.set(k, String(v)) },
    removeItem: (k) => { map.delete(k) },
    get size() { return map.size },
  }
}

// Representative tenant/org strings: the values the OLD implementation would
// have produced. The holder must never be any of them.
const TENANTS = ['demo-tenant', 'demo', 'acme', 'acme-corp', 'leaf', 'tenant-1', 'org_abc123']

// --- (a) two distinct storages (two tabs) yield two DIFFERENT holder ids ------
{
  const tabA = makeStorage()
  const tabB = makeStorage()
  const a = getSessionHolderId(tabA)
  const b = getSessionHolderId(tabB)

  if (typeof a !== 'string' || !a.trim()) fail('(a) tab A produced no holder id')
  else if (typeof b !== 'string' || !b.trim()) fail('(a) tab B produced no holder id')
  else if (a === b) fail(`(a) two tabs share a holder id -> ${JSON.stringify(a)} (this is the tenant bug)`)
  else pass(`(a) two tabs get DIFFERENT holder ids -> ${JSON.stringify(a)} vs ${JSON.stringify(b)}`)

  // Each tab must also have persisted its id under the documented key.
  if (tabA.getItem(HOLDER_STORAGE_KEY) !== a) fail('(a) tab A did not persist its holder id')
  if (tabB.getItem(HOLDER_STORAGE_KEY) !== b) fail('(a) tab B did not persist its holder id')
}

// --- (b) one storage yields the SAME id across calls (survives reload) --------
{
  const tab = makeStorage()
  const first = getSessionHolderId(tab)
  const again = getSessionHolderId(tab)
  const third = getSessionHolderId(tab)

  if (first !== again || again !== third) {
    fail(`(b) holder id is not stable across calls -> ${JSON.stringify([first, again, third])}`)
  } else {
    pass(`(b) same tab keeps ONE holder id across reloads -> ${JSON.stringify(first)}`)
  }

  // A pre-seeded storage (the real reload case) must be read, not overwritten.
  const reloaded = makeStorage({ [HOLDER_STORAGE_KEY]: 'sess-preexisting-id' })
  const got = getSessionHolderId(reloaded)
  if (got !== 'sess-preexisting-id') fail(`(b) stored holder id was not reused -> ${JSON.stringify(got)}`)
}

// --- (c) the generated id is never a tenant string ----------------------------
{
  let clean = true
  for (let i = 0; i < 200; i++) {
    const id = getSessionHolderId(makeStorage())
    if (TENANTS.includes(id)) { fail(`(c) holder id equals a tenant string -> ${JSON.stringify(id)}`); clean = false; break }
    // Nor may a tenant string be embedded as the whole meaningful body of the id.
    for (const t of TENANTS) {
      if (id === t || id === `sess-${t}`) { fail(`(c) holder id is tenant-derived -> ${JSON.stringify(id)}`); clean = false; break }
    }
    if (!clean) break
  }
  if (clean) pass(`(c) holder id is independent of any tenant/org string (${TENANTS.length} representatives, 200 mints)`)
}

// --- robustness: no storage, and a hostile storage, must not throw ------------
{
  try {
    const ssr = getSessionHolderId(null)
    if (typeof ssr !== 'string' || !ssr.trim()) fail('null storage produced no holder id')
    else if (getSessionHolderId(null) !== ssr) fail('null-storage fallback id is not stable')
    else pass(`null storage falls back to a stable in-memory id -> ${JSON.stringify(ssr)}`)
  } catch (e) {
    fail(`null storage threw: ${e && e.message}`)
  }

  const hostile = {
    getItem() { throw new Error('SecurityError: storage disabled') },
    setItem() { throw new Error('SecurityError: storage disabled') },
  }
  try {
    const id = getSessionHolderId(hostile)
    if (typeof id !== 'string' || !id.trim()) fail('throwing storage produced no holder id')
    else pass(`throwing storage degrades to an in-memory id -> ${JSON.stringify(id)}`)
  } catch (e) {
    fail(`throwing storage was not contained: ${e && e.message}`)
  }

  // A blank/whitespace stored value must be replaced, not returned.
  const blank = makeStorage({ [HOLDER_STORAGE_KEY]: '   ' })
  const fixed = getSessionHolderId(blank)
  if (!fixed.trim() || fixed === '   ') fail('blank stored holder id was returned verbatim')
  else pass('blank stored holder id is re-minted')
}

// --- the App must no longer derive the holder from the tenant -----------------
{
  const fs = await import('node:fs/promises')
  const app = await fs.readFile(new URL('../src/App.jsx', import.meta.url), 'utf8')

  if (/const\s+ownHolder\s*=\s*tenant\s*\|\|/.test(app)) {
    fail('App.jsx still derives ownHolder from the tenant')
  } else if (!/const\s+ownHolder\s*=\s*useMemo\(\s*\(\)\s*=>\s*getSessionHolderId\(\)/.test(app)) {
    fail('App.jsx does not compute ownHolder from getSessionHolderId()')
  } else if (!/from\s+['"]\.\/checkoutIdentity\.js['"]/.test(app)) {
    fail('App.jsx does not import ./checkoutIdentity.js')
  } else {
    pass('App.jsx computes ownHolder from getSessionHolderId(), not the tenant')
  }

  // Purity: node imported it, but be explicit so a future edit can't sneak a
  // bundler-only dependency into the module (mirrors check_errors.mjs).
  const src = await fs.readFile(new URL('../src/checkoutIdentity.js', import.meta.url), 'utf8')
  if (/import\.meta/.test(src)) fail('checkoutIdentity.js references import.meta')
  if (/from ['"]react/.test(src)) fail('checkoutIdentity.js imports React')
  if (/\.json['"]/.test(src)) fail('checkoutIdentity.js imports JSON')
}

if (failures) {
  console.error(`\n${failures} check(s) failed`)
  process.exit(1)
}
console.log('CHECKOUT_IDENTITY_OK')
