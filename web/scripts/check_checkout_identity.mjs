// Oracle: the single-writer checkout holder id must identify a SESSION, not a
// tenant. Recomputed against the REAL module, so nothing here is hardcoded.
//
// The bug this pins: ownHolder used to be `tenant || config.tenant || 'demo-tenant'`,
// so every teammate in one org produced the SAME holder id. Teammate B then read
// teammate A's lock as their own ("You hold the edit lock", with a live Release
// button) and could release a lock they never took.
//
// Run: node scripts/check_checkout_identity.mjs   (from web/)
import {
  getSessionHolderId, claimHolderId, isCheckoutActive, isLegacyHolder,
  HOLDER_STORAGE_KEY,
} from '../src/checkoutIdentity.js'

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

// --- (d) a DUPLICATED tab must not keep the incumbent's id --------------------
// The case (a) above misses entirely: it models two tabs the user OPENED, which
// have independent storage. Duplicating a tab COPIES sessionStorage, so the new
// tab reads the incumbent's id and both believe they hold the lock. Storage alone
// cannot tell a duplicate from a reload, so the claim channel decides.
{
  // A synchronous stand-in for BroadcastChannel: delivers to every peer except
  // the sender, which is the semantics claimHolderId relies on.
  const makeHub = () => {
    const peers = []
    return {
      connect() {
        const listeners = []
        const peer = {
          addEventListener: (_t, fn) => { listeners.push(fn) },
          removeEventListener: (_t, fn) => {
            const i = listeners.indexOf(fn); if (i >= 0) listeners.splice(i, 1)
          },
          close() { const i = peers.indexOf(peer); if (i >= 0) peers.splice(i, 1) },
          postMessage(data) {
            for (const other of peers) {
              if (other === peer) continue
              for (const fn of other._listeners) fn({ data })
            }
          },
          _listeners: listeners,
        }
        peers.push(peer)
        return peer
      },
    }
  }

  const hub = makeHub()
  const tabA = makeStorage()
  const idA = getSessionHolderId(tabA)
  let liveA = idA
  const claimA = claimHolderId({
    id: idA, storage: tabA, channel: hub.connect(),
    onRemint: (n) => { liveA = n }, now: () => 1000,
    schedule: (fn) => fn(), // sync: production defers a task (see claimHolderId)
  })

  // The duplication: tab B starts with a COPY of tab A's storage.
  const tabB = makeStorage({ [HOLDER_STORAGE_KEY]: tabA.getItem(HOLDER_STORAGE_KEY) })
  const idB = getSessionHolderId(tabB)
  if (idB !== idA) {
    fail(`(d) test setup wrong: a copied store should hand back the same id, got ${JSON.stringify(idB)}`)
  }
  let liveB = idB
  const claimB = claimHolderId({
    id: idB, storage: tabB, channel: hub.connect(),
    onRemint: (n) => { liveB = n }, now: () => 2000,
    schedule: (fn) => fn(),
  })

  if (!claimA.active || !claimB.active) {
    fail('(d) claimHolderId reported inactive with a channel provided')
  } else if (liveA === liveB) {
    fail(`(d) a duplicated tab still shares the incumbent's holder id -> ${JSON.stringify(liveA)}`)
  } else if (liveA !== idA) {
    fail(`(d) the incumbent gave up its id (${JSON.stringify(idA)} -> ${JSON.stringify(liveA)}); the duplicate should be the one to remint`)
  } else {
    pass(`(d) duplicated tab reminted: incumbent kept ${JSON.stringify(liveA)}, duplicate moved to ${JSON.stringify(liveB)}`)
  }

  // The remint must be persisted, or the next reload of the duplicate collides again.
  if (tabB.getItem(HOLDER_STORAGE_KEY) !== liveB) {
    fail('(d) the duplicate did not persist its reminted holder id')
  }

  claimA.stop(); claimB.stop()

  // With no channel available, the claim must degrade LOUDLY (active: false)
  // rather than pretending duplication detection is in place.
  const noChan = claimHolderId({ id: 'sess-x', storage: makeStorage(), channel: null })
  if (noChan.active !== false) fail('(d) claim with no channel did not report active: false')
  else pass('(d) no BroadcastChannel -> claim reports active:false (documented degradation)')
}

// --- (e) an EXPIRED lock is FREE (the server says so) -------------------------
{
  const iso = (ms) => new Date(ms).toISOString()
  const now = 1_700_000_000_000
  const cases = [
    [{ holder: 'sess-a', expires: iso(now + 60_000) }, true, 'unexpired lock is active'],
    [{ holder: 'sess-a', expires: iso(now - 1) }, false, 'lock that expired 1ms ago is free'],
    [{ holder: 'sess-a', expires: iso(now - 3600_000) }, false, 'long-expired lock is free'],
    [{ holder: 'sess-a' }, true, 'lock with no expires is treated as active'],
    [{ holder: 'sess-a', expires: 'not-a-date' }, true, 'unparseable expires does not silently free the lock'],
    [null, false, 'no checkout is not active'],
    [{ holder: '' }, false, 'checkout with a blank holder is not active'],
  ]
  let ok = true
  for (const [co, want, label] of cases) {
    const got = isCheckoutActive(co, now)
    if (got !== want) { fail(`(e) ${label}: expected ${want}, got ${got}`); ok = false }
  }
  if (ok) pass(`(e) expiry is honoured in all ${cases.length} cases`)
}

// --- (f) legacy tenant-shaped holders are recognised --------------------------
{
  let ok = true
  for (const t of TENANTS) {
    if (!isLegacyHolder(t)) { fail(`(f) tenant-shaped holder not flagged as legacy -> ${JSON.stringify(t)}`); ok = false }
  }
  if (isLegacyHolder('sess-abc')) { fail('(f) a session id was flagged as legacy'); ok = false }
  if (isLegacyHolder('')) { fail('(f) blank holder flagged as legacy'); ok = false }
  if (isLegacyHolder(null)) { fail('(f) null holder flagged as legacy'); ok = false }
  if (ok) pass('(f) legacy tenant-shaped holders are distinguished from session ids')
}

// --- the App must no longer derive the holder from the tenant -----------------
{
  const fs = await import('node:fs/promises')
  const app = await fs.readFile(new URL('../src/App.jsx', import.meta.url), 'utf8')

  if (/const\s+ownHolder\s*=\s*tenant\s*\|\|/.test(app)) {
    fail('App.jsx still derives ownHolder from the tenant')
  } else if (!/getSessionHolderId\(\)/.test(app)) {
    fail('App.jsx does not compute ownHolder from getSessionHolderId()')
  } else if (!/from\s+['"]\.\/checkoutIdentity\.js['"]/.test(app)) {
    fail('App.jsx does not import ./checkoutIdentity.js')
  } else {
    pass('App.jsx computes ownHolder from getSessionHolderId(), not the tenant')
  }

  // --- (g) the lock must FAIL CLOSED on a failed read -------------------------
  // Source-level because the behaviour lives in a React callback, and the exact
  // regression (an error mapping to "no lock") is a one-line edit away.
  if (!/setCheckoutUnknown\(true\)/.test(app)) {
    fail('(g) App.jsx never sets checkoutUnknown(true): a failed lock read cannot fail closed')
  } else if (!/const\s+writeLocked\s*=[^\n]*checkoutUnknown/.test(app)) {
    fail('(g) writeLocked does not consider checkoutUnknown, so a failed read re-enables writes')
  } else {
    pass('(g) a failed lock read sets checkoutUnknown and suppresses writes (fails closed)')
  }

  // The claim must actually be started, or duplicate detection is dead code.
  if (!/claimHolderId\(/.test(app)) {
    fail('(g) App.jsx never calls claimHolderId, so a duplicated tab is never detected')
  } else {
    pass('(g) App.jsx starts the holder claim')
  }

  // Expiry must be applied in the App, not just available in the module.
  if (!/isCheckoutActive\(/.test(app)) {
    fail('(g) App.jsx never calls isCheckoutActive, so an expired lock still suppresses writes')
  } else {
    pass('(g) App.jsx applies isCheckoutActive to the raw checkout')
  }

  // Out-of-order reads must be dropped.
  if (!/checkoutSeqRef/.test(app)) {
    fail('(g) no sequence guard on the checkout read: a stale response can overwrite fresh lock state')
  } else {
    pass('(g) checkout reads carry a sequence guard against out-of-order responses')
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
