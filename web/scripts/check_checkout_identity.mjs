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
  getSessionHolderId, claimHolderId, isLegacyHolder, lockState,
  HOLDER_STORAGE_KEY, CHECKOUT_RELOAD_HANDOFF_KEY,
  CHECKOUT_AUTH_RETURN_KEY, CHECKOUT_AUTH_COMPLETE_KEY, CHECKOUT_AUTH_RETURN_MAX_AGE_MS,
  stageCheckoutReloadHandoff, consumeCheckoutReloadHandoff,
  stageCheckoutAuthReturn, clearCheckoutAuthReturn, consumeCheckoutAuthReturn,
  completeCheckoutAuthReturn, consumeCheckoutAuthComplete,
  bootstrapCheckoutReloadHandoff, holdCheckoutReloadAuthority, remintSessionHolderId,
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

// --- reload handoff: exact, short-lived, one-use, and absent on duplication ---
{
  const storage = makeStorage()
  const holder = 'sess-reload-owner'
  const drawingId = 'rooftop_demo'
  const capability = 'lco1.secret-proof'
  if (!stageCheckoutReloadHandoff({
    capability, holder, drawingId, storage, now: () => 10_000,
  })) fail('(c1) could not stage a valid reload handoff')

  const restored = consumeCheckoutReloadHandoff({
    holder, drawingId, storage, now: () => 10_050,
  })
  if (restored?.capability !== capability) fail('(c1) exact reload did not recover its capability')
  else if (storage.getItem(CHECKOUT_RELOAD_HANDOFF_KEY) !== null) fail('(c1) consumed handoff remained in storage')
  else if (consumeCheckoutReloadHandoff({ holder, drawingId, storage, now: () => 10_060 }) !== null) {
    fail('(c1) reload handoff could be replayed')
  } else pass('(c1) exact reload recovers the capability once, then deletes it')

  for (const [label, candidateHolder, candidateDrawing, age] of [
    ['wrong holder', 'sess-other', drawingId, 10],
    ['wrong drawing', holder, 'other-drawing', 10],
    ['stale', holder, drawingId, 31_000],
    ['future timestamp', holder, drawingId, -1],
  ]) {
    const rejected = makeStorage()
    stageCheckoutReloadHandoff({ capability, holder, drawingId, storage: rejected, now: () => 20_000 })
    const got = consumeCheckoutReloadHandoff({
      holder: candidateHolder, drawingId: candidateDrawing, storage: rejected,
      now: () => 20_000 + age,
    })
    if (got !== null) fail(`(c1) ${label} handoff was accepted`)
    if (rejected.getItem(CHECKOUT_RELOAD_HANDOFF_KEY) !== null) fail(`(c1) ${label} handoff was not deleted`)
  }
  pass('(c1) mismatched, stale, and future handoffs fail closed and are deleted')

  // Duplicating a live tab copies ordinary sessionStorage, but no handoff exists
  // until beforeunload. This pin guards against writing the bearer capability at
  // normal acquire time, which would give a duplicate tab authority.
  const liveTab = makeStorage({ [HOLDER_STORAGE_KEY]: holder })
  const duplicate = makeStorage({ [HOLDER_STORAGE_KEY]: liveTab.getItem(HOLDER_STORAGE_KEY) })
  if (duplicate.getItem(CHECKOUT_RELOAD_HANDOFF_KEY) !== null) fail('(c1) duplicate inherited a checkout capability')
  else pass('(c1) a duplicated live tab has no checkout capability to inherit')

  const remintedStorage = makeStorage({ [HOLDER_STORAGE_KEY]: holder })
  const reminted = remintSessionHolderId(remintedStorage)
  if (reminted === holder || remintedStorage.getItem(HOLDER_STORAGE_KEY) !== reminted) {
    fail('(c1) a failed handoff redemption did not replace the copied holder id')
  } else pass('(c1) a failed handoff redemption remints and persists a distinct holder id')

  // Bootstrap consumption must survive React's speculative/double render. The
  // first call deletes storage, while every later call in this runtime returns
  // the same provisional value from memory.
  const runtimeHolder = 'sess-strict-render'
  const runtimeDrawing = 'strict-render-drawing'
  const runtimeStorage = makeStorage()
  stageCheckoutReloadHandoff({
    capability, holder: runtimeHolder, drawingId: runtimeDrawing,
    storage: runtimeStorage, now: () => 30_000,
  })
  const bootstrapA = bootstrapCheckoutReloadHandoff({
    holder: runtimeHolder, drawingId: runtimeDrawing,
    storage: runtimeStorage, now: () => 30_010, navigationType: 'reload',
  })
  const bootstrapB = bootstrapCheckoutReloadHandoff({
    holder: runtimeHolder, drawingId: runtimeDrawing,
    storage: runtimeStorage, now: () => 30_020, navigationType: 'reload',
  })
  if (bootstrapA?.capability !== capability || bootstrapB !== bootstrapA) {
    fail('(c1) speculative render did not reuse the consumed runtime handoff')
  } else if (runtimeStorage.getItem(CHECKOUT_RELOAD_HANDOFF_KEY) !== null) {
    fail('(c1) runtime bootstrap left the consumed handoff in storage')
  } else pass('(c1) speculative renders reuse one in-memory bootstrap after storage deletion')

  const duplicatedStorage = makeStorage()
  stageCheckoutReloadHandoff({
    capability, holder: 'sess-duplicate-nav', drawingId: 'duplicate-nav-drawing',
    storage: duplicatedStorage, now: () => 40_000,
  })
  const duplicateBootstrap = bootstrapCheckoutReloadHandoff({
    holder: 'sess-duplicate-nav', drawingId: 'duplicate-nav-drawing',
    storage: duplicatedStorage, now: () => 40_010, navigationType: 'navigate',
  })
  if (duplicateBootstrap !== null) fail('(c1) an unmarked navigation restored copied authority')
  else pass('(c1) unmarked navigation cannot restore checkout authority')

  const callbackNavigationStorage = makeStorage()
  stageCheckoutAuthReturn({ storage: callbackNavigationStorage, now: () => 50_000 })
  stageCheckoutReloadHandoff({
    capability, holder: 'sess-callback-nav', drawingId: 'callback-nav-drawing',
    storage: callbackNavigationStorage, now: () => 50_010,
  })
  const callbackNavigation = bootstrapCheckoutReloadHandoff({
    holder: 'sess-callback-nav', drawingId: 'default-drawing',
    storage: callbackNavigationStorage, now: () => 50_020,
    navigationType: 'navigate',
    deferForAuthCallback: true,
  })
  if (callbackNavigation !== null) fail('(c1) Auth0 callback redeemed authority before validation')
  else if (callbackNavigationStorage.getItem(CHECKOUT_AUTH_RETURN_KEY) === null) {
    fail('(c1) Auth0 callback destroyed its login marker before validation')
  } else if (callbackNavigationStorage.getItem(CHECKOUT_RELOAD_HANDOFF_KEY) === null) {
    fail('(c1) Auth0 callback destroyed authority before restoring the exact returnTo drawing')
  } else if (!completeCheckoutAuthReturn({ storage: callbackNavigationStorage, now: () => 50_030 })) {
    fail('(c1) validated Auth0 callback did not mint its clean-reload marker')
  } else {
    const restoredCustomDrawing = bootstrapCheckoutReloadHandoff({
      holder: 'sess-callback-nav', drawingId: 'callback-nav-drawing',
      storage: callbackNavigationStorage, now: () => 50_040,
      navigationType: 'reload',
    })
    if (restoredCustomDrawing?.capability !== capability) {
      fail('(c1) clean Auth0 reload did not restore authority for the exact returnTo drawing')
    } else pass('(c1) Auth0 callback defers authority until the exact returnTo drawing reloads')
  }

  const markerOnly = makeStorage()
  stageCheckoutAuthReturn({ storage: markerOnly, now: () => 80_000 })
  if (!consumeCheckoutAuthReturn({
    eligible: true, storage: markerOnly, now: () => 80_001,
  })) fail('(c1) a valid intentional-login marker was rejected')
  else if (consumeCheckoutAuthReturn({
    eligible: true, storage: markerOnly, now: () => 80_002,
  })) fail('(c1) intentional-login marker could be replayed')
  else pass('(c1) intentional-login marker is valid once and then deleted')

  stageCheckoutAuthReturn({ storage: markerOnly, now: () => 81_000 })
  clearCheckoutAuthReturn(markerOnly)
  if (markerOnly.getItem(CHECKOUT_AUTH_RETURN_KEY) !== null) {
    fail('(c1) failed Auth0 launch left its return marker behind')
  } else pass('(c1) failed Auth0 launch clears its return marker')

  const callbackReloadStorage = makeStorage()
  stageCheckoutAuthReturn({ storage: callbackReloadStorage, now: () => 90_000 })
  stageCheckoutReloadHandoff({
    capability, holder: 'sess-callback-reload', drawingId: 'callback-reload-drawing',
    storage: callbackReloadStorage, now: () => 90_010,
  })
  if (!completeCheckoutAuthReturn({ storage: callbackReloadStorage, now: () => 90_100 })) {
    fail('(c1) successful Auth0 callback did not mint its reload marker')
  }
  const callbackReload = bootstrapCheckoutReloadHandoff({
    holder: 'sess-callback-reload', drawingId: 'callback-reload-drawing',
    storage: callbackReloadStorage, now: () => 90_150,
    navigationType: 'reload',
  })
  if (callbackReload?.capability !== capability ||
      callbackReload?.authorityMaxAgeMs !== CHECKOUT_AUTH_RETURN_MAX_AGE_MS) {
    fail('(c1) successful Auth0 callback reload did not recover checkout authority')
  } else if (callbackReloadStorage.getItem(CHECKOUT_AUTH_COMPLETE_KEY) !== null) {
    fail('(c1) successful Auth0 callback reload marker could be replayed')
  } else pass('(c1) successful Auth0 callback carries authority through its clean reload once')

  const invalidCompletion = makeStorage()
  if (completeCheckoutAuthReturn({ storage: invalidCompletion, now: () => 91_000 }) ||
      consumeCheckoutAuthComplete({ eligible: true, storage: invalidCompletion, now: () => 91_001 })) {
    fail('(c1) Auth0 completion existed without an intentional login marker')
  } else pass('(c1) Auth0 completion cannot be minted without an intentional login')

  const waiters = []
  let held = false
  const fakeLocks = {
    request(_name, _options, callback) {
      return new Promise((resolve) => {
        const enter = async () => {
          held = true
          await callback({ name: _name })
          held = false
          resolve()
          waiters.shift()?.()
        }
        if (held) waiters.push(enter)
        else enter()
      })
    },
  }
  const acquired = []
  const handoff = {
    capability, holder: runtimeHolder, drawingId: runtimeDrawing, createdAtMs: 50_000,
  }
  const lockA = holdCheckoutReloadAuthority({
    handoff, locks: fakeLocks, now: () => 50_010, onAcquired: () => acquired.push('A'),
  })
  const lockB = holdCheckoutReloadAuthority({
    handoff, locks: fakeLocks, now: () => 50_010, onAcquired: () => acquired.push('B'),
  })
  await new Promise((resolve) => setTimeout(resolve, 10))
  if (lockB !== lockA || JSON.stringify(acquired) !== JSON.stringify(['A'])) {
    fail(`(c1) runtime did not keep one authority lock across repeated setup -> ${JSON.stringify(acquired)}`)
  } else pass('(c1) runtime lock survives repeated React setup without a release gap')
  lockA.stop()
  await Promise.all([lockA.done, lockB.done])

  const unsupported = holdCheckoutReloadAuthority({ handoff, locks: null })
  if (unsupported.active) fail('(c1) missing Web Locks did not fail closed')
  else pass('(c1) missing Web Locks fails closed')
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

// --- (d) DUPLICATED tabs must not keep the incumbent's id ---------------------
// Case (a) above misses this entirely: it models two tabs the user OPENED, which
// have independent storage. Duplicating a tab COPIES sessionStorage, so the new
// tab reads the incumbent's id and both believe they hold the lock. Storage alone
// cannot tell a duplicate from a reload, so the claim channel decides.
//
// The hub below delivers ASYNCHRONOUSLY, one task per message, like a real
// BroadcastChannel. An earlier version of this check injected a synchronous
// scheduler, which serialised the handshake and so could never produce the
// interleaving that actually broke: two duplicates claiming at once.
function makeAsyncHub() {
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
            for (const fn of other._listeners) setTimeout(() => fn({ data }), 0)
          }
        },
        _listeners: listeners,
      }
      peers.push(peer)
      return peer
    },
  }
}

const settle = async (ms = 60) => { await new Promise((r) => setTimeout(r, ms)) }

// Spin up N runtimes that all start from a COPY of one seeded store, each with a
// distinct claim age, and report the ids they end up live on.
async function runTabs(ages, seedId) {
  const hub = makeAsyncHub()
  const tabs = ages.map((age) => {
    const storage = makeStorage({ [HOLDER_STORAGE_KEY]: seedId })
    const id = getSessionHolderId(storage)
    const t = { age, storage, startId: id, liveId: id }
    t.claim = claimHolderId({
      id, storage, channel: hub.connect(),
      onRemint: (n) => { t.liveId = n }, now: () => age,
    })
    return t
  })
  // Busy Windows runners can delay a zero-delay BroadcastChannel task well
  // beyond a fixed 120 ms sleep. Poll the actual convergence condition so the
  // oracle measures the protocol instead of scheduler luck.
  const deadline = Date.now() + 2_000
  while (Date.now() < deadline && new Set(tabs.map((t) => t.liveId)).size < tabs.length) {
    await settle(20)
  }
  for (const t of tabs) t.claim.stop()
  return tabs
}

// (d1) two tabs: the OLDER keeps the id, the newer moves.
{
  const seed = 'sess-SEEDED-ID'
  const tabs = await runTabs([1000, 2000], seed)
  const [a, b] = tabs
  if (a.liveId === b.liveId) fail(`(d1) two duplicated tabs still share an id -> ${JSON.stringify(a.liveId)}`)
  else if (a.liveId !== seed) fail(`(d1) the incumbent gave up its id (${seed} -> ${JSON.stringify(a.liveId)})`)
  else if (b.liveId === seed) fail('(d1) the duplicate kept the incumbent id')
  else pass(`(d1) duplicate stepped aside: incumbent kept ${JSON.stringify(a.liveId)}, duplicate -> ${JSON.stringify(b.liveId)}`)
  if (b.storage.getItem(HOLDER_STORAGE_KEY) !== b.liveId) fail('(d1) the duplicate did not persist its reminted id')
}

// (d2) THREE tabs sharing one id. This is the interleaving that broke the
// previous implementation: it reminted on ANY `held`, so the middle tab answered
// the newest tab's claim while still holding the id, the incumbent obeyed that
// answer, and all three ended up on fresh ids. The server-side lock the incumbent
// actually took was then orphaned until expiry with no client able to release it.
{
  const seed = 'sess-THREE-WAY'
  const tabs = await runTabs([1000, 2000, 3000], seed)
  const live = tabs.map((t) => t.liveId)
  const unique = new Set(live)
  if (unique.size !== 3) fail(`(d2) three tabs did not resolve to three distinct ids -> ${JSON.stringify(live)}`)
  else if (live[0] !== seed) fail(`(d2) the OLDEST tab lost the id; nobody holds ${seed} -> ${JSON.stringify(live)}`)
  else pass(`(d2) three-way duplication resolved: oldest kept ${JSON.stringify(seed)}, others reminted`)
}

// (d3) SIMULTANEOUS claims (identical timestamps) must still resolve to one
// winner. A rule that cannot decide leaves two live runtimes sharing an id, which
// is the defect itself; the nonce tiebreak is what makes the order total.
{
  const seed = 'sess-SAME-MS'
  const tabs = await runTabs([5000, 5000, 5000], seed)
  const live = tabs.map((t) => t.liveId)
  const unique = new Set(live)
  if (unique.size !== 3) fail(`(d3) simultaneous claims left runtimes sharing an id -> ${JSON.stringify(live)}`)
  else if (!live.includes(seed)) fail(`(d3) simultaneous claims orphaned the lock: nobody kept ${seed} -> ${JSON.stringify(live)}`)
  else pass('(d3) simultaneous claims resolve to exactly one keeper, no sharing')
}

// (d4) a genuine FIRST tab (no peers) must keep its id and not remint.
{
  const hub = makeAsyncHub()
  const storage = makeStorage()
  const id = getSessionHolderId(storage)
  let live = id
  const claim = claimHolderId({
    id, storage, channel: hub.connect(), onRemint: (n) => { live = n }, now: () => 1000,
  })
  await settle(80)
  claim.stop()
  if (live !== id) fail(`(d4) a lone tab reminted with no peer present (${id} -> ${live})`)
  else pass('(d4) a lone tab keeps its id (reload stability preserved)')
}

// (d5) stop() must detach the listener, or a closed runtime keeps answering.
{
  const hub = makeAsyncHub()
  const peer = hub.connect()
  const storage = makeStorage()
  const id = getSessionHolderId(storage)
  const claim = claimHolderId({ id, storage, channel: peer, now: () => 1000 })
  await settle(30)
  const before = peer._listeners.length
  claim.stop()
  if (peer._listeners.length >= before && before > 0) {
    fail(`(d5) stop() left ${peer._listeners.length} listener(s) attached`)
  } else pass('(d5) stop() detaches the message listener')
}

// (d6) no BroadcastChannel -> the claim must degrade LOUDLY, not pretend.
{
  const noChan = claimHolderId({ id: 'sess-x', storage: makeStorage(), channel: null })
  if (noChan.active !== false) fail('(d6) claim with no channel did not report active: false')
  else pass('(d6) no BroadcastChannel -> active:false (documented degradation)')
}

// --- (e) the browser clock must NOT decide who may write ----------------------
// The bug: `expires <= Date.now()` used to free the lock, so an editor whose
// clock ran two hours fast re-enabled its own writes over a lease the server
// still considered live. Expiry now only OFFERS a Take, which the server settles.
{
  const iso = (ms) => new Date(ms).toISOString()
  const now = 1_700_000_000_000
  const skewedFast = now + 2 * 3600_000 // this client's clock is 2h ahead
  const liveLease = { holder: 'sess-other', expires: iso(now + 1800_000) }

  const sane = lockState({ mock: false, checkout: liveLease, unknown: false, ownHolder: 'sess-me', nowMs: now })
  const skewed = lockState({ mock: false, checkout: liveLease, unknown: false, ownHolder: 'sess-me', nowMs: skewedFast })

  if (!sane.writeLocked) fail('(e) a live lease held by another session did not suppress writes')
  else if (!skewed.writeLocked) {
    fail('(e) a 2h-fast clock re-enabled writes over a live lease (clock treated as authority)')
  } else if (!skewed.stale) {
    fail('(e) a lease that looks elapsed to this client offers no Take, so the UI can wedge')
  } else pass('(e) clock skew cannot enable a write; it only offers a Take for the server to settle')

  // Missing / malformed expires must be TAKEABLE, or the UI wedges forever.
  for (const bad of [undefined, null, '', 'not-a-date']) {
    const st = lockState({
      mock: false, checkout: { holder: 'legacy-tenant', expires: bad },
      unknown: false, ownHolder: 'sess-me', nowMs: now,
    })
    if (!st.writeLocked) fail(`(e) expires=${JSON.stringify(bad)} left writes enabled`)
    if (!st.stale) fail(`(e) expires=${JSON.stringify(bad)} offers no Take -> permanent client-side lock`)
    if (!st.legacy) fail(`(e) tenant-shaped holder with expires=${JSON.stringify(bad)} not flagged legacy`)
  }
  pass('(e) missing/malformed expires suppresses writes AND offers a Take (no wedge)')
}

// --- (f) legacy tenant-shaped holders are recognised --------------------------
{
  let ok = true
  for (const t of TENANTS) {
    if (!isLegacyHolder(t)) { fail(`(f) tenant-shaped holder not flagged legacy -> ${JSON.stringify(t)}`); ok = false }
  }
  if (isLegacyHolder('sess-abc')) { fail('(f) a session id was flagged legacy'); ok = false }
  if (isLegacyHolder('')) { fail('(f) blank holder flagged legacy'); ok = false }
  if (isLegacyHolder(null)) { fail('(f) null holder flagged legacy'); ok = false }
  if (ok) pass('(f) legacy tenant-shaped holders are distinguished from session ids')
}

// --- (g) the lock must FAIL CLOSED, tested as BEHAVIOUR -----------------------
// These were source regexes before, which an unreachable call satisfies as well
// as a correct one. They now exercise lockState directly.
{
  const me = 'sess-me'
  const cases = [
    // [label, args, expected writeLocked]
    ['unknown with no checkout yet (first load, request in flight)',
      { mock: false, checkout: null, unknown: true, ownHolder: me }, true],
    ['unknown after a failed read',
      { mock: false, checkout: null, unknown: true, ownHolder: me }, true],
    ['answered: nobody holds it',
      { mock: false, checkout: null, unknown: false, ownHolder: me }, false],
    ['another session holds it',
      { mock: false, checkout: { holder: 'sess-other' }, unknown: false, ownHolder: me }, true],
    ['we hold it',
      { mock: false, checkout: { holder: me }, unknown: false, ownHolder: me }, false],
    ['mock mode never locks',
      { mock: true, checkout: { holder: 'sess-other' }, unknown: true, ownHolder: me }, false],
    ['blank holder is not a lock',
      { mock: false, checkout: { holder: '   ' }, unknown: false, ownHolder: me }, false],
  ]
  let ok = true
  for (const [label, args, want] of cases) {
    const got = lockState(args).writeLocked
    if (got !== want) { fail(`(g) ${label}: writeLocked expected ${want}, got ${got}`); ok = false }
  }
  if (ok) pass(`(g) writeLocked fails closed across all ${cases.length} states`)

  // canTake must be offered for ANY other-held lock, whatever our clock says.
  // A slow clock previously judged an elapsed lease live and hid the button.
  {
    const now = 1_700_000_000_000
    const slow = now - 2 * 3600_000 // our clock runs 2h behind the server
    const elapsed = { holder: 'sess-other', expires: new Date(now - 60_000).toISOString() }
    const byslow = lockState({ mock: false, checkout: elapsed, unknown: false, ownHolder: 'sess-me', nowMs: slow })
    if (!byslow.writeLocked) fail('(g) a 2h-slow clock must still suppress writes')
    if (!byslow.canTake) fail('(g) a 2h-slow clock hid the Take offer -> the UI wedges with no action available')
    const live = { holder: 'sess-other', expires: new Date(now + 3600_000).toISOString() }
    if (!lockState({ mock: false, checkout: live, unknown: false, ownHolder: 'sess-me', nowMs: now }).canTake) {
      fail('(g) no Take offered for a live lock; the server should be the one to refuse it')
    }
    if (lockState({ mock: false, checkout: null, unknown: false, ownHolder: 'sess-me', nowMs: now }).canTake) {
      fail('(g) Take offered when nobody holds the lock')
    }
    pass('(g) Take is offered for any other-held lock regardless of clock skew')
  }

  // heldByUs must never be true for someone else's lock (the original tenant bug).
  if (lockState({ mock: false, checkout: { holder: 'sess-other' }, unknown: false, ownHolder: me }).heldByUs) {
    fail('(g) heldByUs true for another session -> a Release button for a lock we never took')
  } else pass('(g) heldByUs is false for another session (the original defect stays closed)')
}

// --- ONE controller, and no shell may re-derive the decision (W2c) -----------
//
// Until W2c the console owned a hand-rolled twin of the whole single-writer
// block, and the pins below were source greps for the twin's identifiers in
// App.jsx. The twin is gone: BOTH shells consume
// controllers/checkout/useCheckoutController.js, so the pins now say
// "delegates" (the shells) and "behaves" (the controller, exercised for real).
{
  const fs = await import('node:fs/promises')
  const app = await fs.readFile(new URL('../src/App.jsx', import.meta.url), 'utf8')
  const toolCast = await fs.readFile(new URL('../src/site/ToolCast.jsx', import.meta.url), 'utf8')
  const checkoutHook = await fs.readFile(new URL('../src/controllers/checkout/useCheckoutController.js', import.meta.url), 'utf8')
  const checkoutStore = await fs.readFile(new URL('../src/controllers/checkout/createCheckoutController.js', import.meta.url), 'utf8')
  const shells = [['App.jsx', app], ['ToolCast.jsx', toolCast]]

  if (/const\s+ownHolder\s*=\s*tenant\s*\|\|/.test(app)) {
    fail('App.jsx still derives ownHolder from the tenant')
  } else if (!/getSessionHolderId\(\)/.test(app)) {
    fail('App.jsx does not compute ownHolder from getSessionHolderId()')
  } else pass('App.jsx computes ownHolder from getSessionHolderId(), not the tenant')

  // EXACTLY ONE mount per shell. SiteRoot renders scene 'app' (App) and scenes
  // site|tool (StageScene -> ToolCast) in mutually exclusive arms of one
  // ternary and mounts none itself, so one mount per shell is one live
  // instance per page load. Two would be two capabilities, two holder ids and
  // two authorities over one lock.
  for (const [name, source] of shells) {
    const mounts = (source.match(/useCheckoutController\(/g) || []).length
    if (mounts !== 1) fail(`${name} mounts ${mounts} checkout controllers, not exactly 1`)
    else pass(`${name} mounts exactly one checkout controller`)
  }

  // The DECISION and the PROTOCOL live in one place. A shell that still names
  // any of these is re-deriving what the controller already owns — which is
  // precisely the drift W2c retired.
  const controllerOwned = [
    'lockState(',
    'claimHolderId(',
    'holdCheckoutReloadAuthority(',
    'bootstrapCheckoutReloadHandoff(',
    'stageCheckoutReloadHandoff(',
    'remintSessionHolderId(',
  ]
  for (const [name, source] of shells) {
    const stripped = source.replace(/\/\/[^\n]*/g, '').replace(/\/\*[\s\S]*?\*\//g, '')
    const forked = controllerOwned.filter((token) => stripped.includes(token))
    if (forked.length) fail(`${name} re-derives controller-owned checkout protocol: ${forked.join(', ')}`)
    else pass(`${name} delegates the whole write-lock decision and protocol to the controller`)
  }
  if (!/lockState\(/.test(checkoutStore)) {
    fail('createCheckoutController does not use lockState, so the tested decision is bypassed')
  } else pass('createCheckoutController delegates the write-lock decision to lockState')

  for (const required of [
    'bootstrapCheckoutReloadHandoff(',
    'holdCheckoutReloadAuthority(',
    'stageCheckoutReloadHandoff(',
    'claimHolderId(',
    'remintSessionHolderId(',
    'controller.restoreCapability(',
    'secureTakenCheckoutAuthority(',
  ]) {
    if (!checkoutHook.includes(required)) fail(`the shared checkout hook is missing ${required}`)
  }
  if (!/addEventListener\(['"]beforeunload['"]/.test(checkoutHook)) {
    fail('the shared checkout hook does not stage authority only at the reload lifecycle edge')
  } else pass('the shared checkout hook wires a one-use checkout capability handoff at beforeunload')
  if (/useRef\([^)]*\?\.capability/.test(checkoutHook)) {
    fail('the shared checkout hook installs the provisional handoff capability before lock ownership')
  } else pass('the shared checkout hook installs reload authority only inside an exclusive Web Lock')

  // The claim must not answer `held` while this runtime is unloading: the
  // reloading runtime would then remint away from the holder its staged
  // handoff names, and the redemption is lost.
  const stageBody = (checkoutHook.match(/const stage = \(\) => \{[\s\S]*?\n    \}/) || [''])[0]
  if (stageBody.indexOf('claimRef.current?.stop()') < 0 ||
      stageBody.indexOf('claimRef.current?.stop()') > stageBody.indexOf('stageCheckoutReloadHandoff(')) {
    fail('the reload handoff is staged while the claim is still answering `held`')
  } else pass('the claim stops before the reload handoff is staged')

  const identitySource = await fs.readFile(new URL('../src/checkoutIdentity.js', import.meta.url), 'utf8')
  if (!/runtimeReloadHandoff[\s\S]*bootstrapCheckoutReloadHandoff/.test(identitySource)) {
    fail('reload handoff consumption is not cached across speculative React renders')
  } else pass('reload handoff consumption is cached for the whole JavaScript runtime')

  const authSource = await fs.readFile(new URL('../src/auth.js', import.meta.url), 'utf8')
  const loginBody = (authSource.match(/export async function login\(\)[\s\S]*?\n}/) || [null])[0]
  const callbackBody = (authSource.match(/export async function handleRedirectCallback\(\)[\s\S]*?\n}/) || [''])[0]
  if (!/query\.has\(['"]state['"]\)[\s\S]*query\.has\(['"]code['"]\)[\s\S]*query\.has\(['"]error['"]\)/.test(authSource)) {
    fail('Auth0 callback detection does not include both success and error responses')
  } else if (!loginBody) {
    fail('could not find the Auth0 login body')
  } else if (loginBody.indexOf('stageCheckoutAuthReturn()') < 0 ||
             loginBody.indexOf('stageCheckoutAuthReturn()') > loginBody.indexOf('loginWithRedirect')) {
    fail('Auth0 login does not stage the one-use return marker before redirect')
  } else if (loginBody.indexOf('clearCheckoutAuthReturn()') < loginBody.indexOf('loginWithRedirect')) {
    fail('Auth0 launch failure does not clear its return marker')
  } else if (callbackBody.indexOf('completeCheckoutAuthReturn()') < callbackBody.indexOf('getTokenSilently')) {
    fail('Auth0 callback marks completion before it has a token')
  } else if (!/recoverableAuthError[\s\S]*completeCheckoutAuthReturn\(\)/.test(callbackBody)) {
    fail('Auth0 error callback cannot preserve its checkout fallback for a clean reload')
  } else pass('Auth0 login marks only its intentional redirect and clears a failed launch')

  // ONE sign-in path across both shells: end the lease first, then leave the
  // origin. The console used to converge its rendered lock state by hand here;
  // the controller's release re-reads /versions instead, so the control shows
  // the server's answer and a read that 401s on the way out fails CLOSED.
  const signInBodies = [
    ['App.jsx', (app.match(/const onLogin = useCallback\(async[\s\S]*?\n  \}, \[[^\]]*\]\)/) || [null])[0]],
    ['ToolCast.jsx', (toolCast.match(/const signInWithCheckoutRelease = useCallback\(async[\s\S]*?\n  \}, \[[^\]]*\]\)/) || [null])[0]],
  ]
  for (const [name, body] of signInBodies) {
    if (!body) fail(`${name} does not define the checkout-aware Auth0 login path`)
    else if (body.indexOf('checkout.actions.release()') < 0 ||
             body.indexOf('checkout.actions.release()') > body.indexOf('await login()')) {
      fail(`${name} leaves checkout authority live before Auth0 login`)
    } else pass(`${name} releases checkout authority through the controller before Auth0`)
  }

  // The decision must actually reach the control on BOTH surfaces, or it is
  // computed and thrown away. Removing a prop previously passed silently.
  const controlProps = [
    ['App.jsx', app, ['canTake={lock.canTake}', 'unknown={lock.unknown}', 'lockedByOther={otherHeldCheckout}']],
    ['ToolCast.jsx', toolCast, ['canTake={checkout.canTake}', 'unknown={checkout.unknown}', 'lockedByOther={checkout.lockedByOther}']],
  ]
  for (const [name, source, props] of controlProps) {
    for (const prop of props) {
      if (!source.includes(prop)) fail(`${name}: CheckoutControls is not given ${prop}`)
      else pass(`${name}: CheckoutControls receives ${prop}`)
    }
  }

  // The scope-reset contract reaches the shells as DATA, not as a comment.
  for (const [name, source] of shells) {
    if (!/checkoutScopeDrawingId\(\{[\s\S]{0,200}identityDrawingId:/.test(source)) {
      fail(`${name} scopes its checkout without the drawing identity: a tenant switch would keep the previous tenant's lock`)
    } else pass(`${name} scopes its checkout on the drawing identity (tenant switch voids it)`)
  }
}

// --- the controller BEHAVES, exercised against the real store ----------------
// These replace the source greps that used to scope themselves to App.jsx's
// `loadCheckout` body. A grep for a guard is satisfied by an unreachable one;
// each case below drives createCheckoutController for real.
{
  const { createCheckoutController, deriveCheckout, checkoutScopeDrawingId } =
    await import('../src/controllers/checkout/createCheckoutController.js')
  const idleServices = {
    loadVersions: async () => ({ checkout: null }),
    take: async () => null,
    release: async () => null,
  }

  // Unknown until answered. The old pin captured `useState(true)` out of
  // App.jsx; this asserts the STATE a first render actually sees.
  {
    const controller = createCheckoutController({ drawingId: 'demo', holder: 'sess-me', services: idleServices })
    const first = controller.getSnapshot()
    if (first.unknown !== true || first.writeLocked !== true) {
      fail(`the first snapshot is not fail-closed -> ${JSON.stringify({ unknown: first.unknown, writeLocked: first.writeLocked })}`)
    } else pass('the checkout starts unknown, so writes are suppressed while the first read is in flight')
    const mocked = createCheckoutController({ mock: true, drawingId: 'demo', holder: 'sess-me', services: idleServices })
    if (mocked.getSnapshot().unknown !== false || mocked.getSnapshot().writeLocked !== false) {
      fail('mock mode starts locked; the demo holds no locks at all')
    } else pass('mock mode starts with no lock and no unknown')
  }

  // A read goes unknown BEFORE awaiting, or the previous drawing's answer stays
  // authoritative for the whole new request and a write can be submitted before
  // any answer exists for the current drawing.
  {
    let releaseRead = null
    const gate = new Promise((resolve) => { releaseRead = resolve })
    const controller = createCheckoutController({
      drawingId: 'demo',
      holder: 'sess-me',
      services: { ...idleServices, loadVersions: async () => { await gate; return { checkout: null } } },
    })
    // Seed an answered state first, so "unknown" below can only come from the
    // read itself and not from the initial value.
    releaseRead({ checkout: null })
    await controller.refresh()
    if (controller.getSnapshot().unknown !== false) fail('an answered read did not clear unknown')
    let inFlight = null
    const slow = new Promise((resolve) => { inFlight = resolve })
    const controller2 = createCheckoutController({
      drawingId: 'demo',
      holder: 'sess-me',
      services: { ...idleServices, loadVersions: () => slow },
    })
    // Not awaited: the read is still IN FLIGHT, which is the whole point.
    controller2.refresh()
    await Promise.resolve()
    if (controller2.getSnapshot().unknown !== true || controller2.getSnapshot().writeLocked !== true) {
      fail('a read in flight left writes enabled')
    } else pass('a read marks the lock unknown BEFORE awaiting (no write-enabled window on drawing change)')
    inFlight({ checkout: null })
  }

  // The success path clears unknown, or the fail-closed design becomes a
  // permanent lock; the failure path re-arms it AND says the read failed.
  {
    const ok = createCheckoutController({ drawingId: 'demo', holder: 'sess-me', services: idleServices })
    await ok.refresh()
    if (ok.getSnapshot().unknown !== false || ok.getSnapshot().writeLocked !== false) {
      fail('a successful read left writes paused forever')
    } else pass('a successful read clears unknown')
    const bad = createCheckoutController({
      drawingId: 'demo',
      holder: 'sess-me',
      services: { ...idleServices, loadVersions: async () => { throw new Error('unavailable') } },
    })
    await bad.refresh()
    const snapshot = bad.getSnapshot()
    if (snapshot.unknown !== true || snapshot.readFailed !== true || snapshot.writeLocked !== true) {
      fail(`a failed read did not fail closed -> ${JSON.stringify(snapshot)}`)
    } else pass('a failed read marks unknown + readFailed and suppresses writes')
  }

  // The sequence fence: a stale response for the PREVIOUS drawing must never
  // replace the current drawing's answer. Both the success and the failure
  // path need one, so both are driven.
  {
    for (const stalePathFails of [false, true]) {
      let releaseOld = null
      let rejectOld = null
      const oldRead = new Promise((resolve, reject) => { releaseOld = resolve; rejectOld = reject })
      const controller = createCheckoutController({
        drawingId: 'old',
        holder: 'sess-me',
        services: {
          ...idleServices,
          loadVersions: (id) => (id === 'old' ? oldRead : Promise.resolve({ checkout: { holder: 'sess-current' } })),
        },
      })
      const stale = controller.refresh()
      controller.setScope({ drawingId: 'new', holder: 'sess-me', mock: false })
      await controller.refresh()
      if (stalePathFails) rejectOld(new Error('the old drawing read failed late'))
      else releaseOld({ checkout: { holder: 'sess-stale' } })
      await stale
      const snapshot = controller.getSnapshot()
      if (snapshot.drawingId !== 'new' || snapshot.checkout?.holder !== 'sess-current') {
        fail(`a stale ${stalePathFails ? 'failed' : 'successful'} read replaced the current drawing's checkout -> ${JSON.stringify(snapshot.checkout)}`)
      }
    }
    pass('a stale response for the previous drawing cannot replace the current answer (success and failure paths)')
  }

  // THE UNPROVEN-OWN-LOCK CORRECTION. `holder` is a PUBLIC label, so a matching
  // label is not proof: after a reload, a duplicated tab, or a redemption this
  // runtime lost, the stored holder can equal the lock's holder with no
  // capability behind it. That must read as somebody else's lock.
  {
    const record = { holder: 'sess-me' }
    const unproven = deriveCheckout(record, 'sess-me', Date.now(), false, false, false)
    const proven = deriveCheckout(record, 'sess-me', Date.now(), false, false, true)
    if (unproven.heldByUs) fail('a matching holder label with NO capability read as ours -> a Release for a lease we cannot prove')
    else if (!unproven.writeLocked) fail('a matching holder label with NO capability enabled writes')
    else if (unproven.lockedByOther !== record) fail('an unproven own lock is not surfaced as another holder')
    else if (!unproven.canTake) fail('an unproven own lock offers no Take, so the UI wedges with no action available')
    else if (!proven.heldByUs || proven.writeLocked) fail('a capability-backed own lock did not enable writes')
    else pass('an own-looking lock with no capability suppresses writes, offers a Take, and is never "ours"')
  }

  // THE TENANT SCOPE (ACCEPTANCE "Scope-reset contract", binding). A tenant
  // switch voids the drawing identity; the version chain does NOT reset with
  // it, so a scope read off drawingState alone keeps addressing the previous
  // tenant's drawing with the previous principal's capability.
  {
    if (checkoutScopeDrawingId({
      identityDrawingId: null,
      drawingState: { drawing_id: 'tenant-a-drawing' },
      requestedDrawingId: 'tenant-a-drawing',
    }) !== null) {
      fail('a voided drawing identity still produced a checkout scope')
    } else if (checkoutScopeDrawingId({
      identityDrawingId: 'demo',
      drawingState: { drawing_id: 'demo' },
      requestedDrawingId: 'demo',
    }) !== 'demo') {
      fail('the scope gate narrowed a LIVE identity; each shell must keep the drawing it addressed')
    } else pass('a voided drawing identity has no checkout scope, and a live one is unchanged')

    let releases = 0
    const controller = createCheckoutController({
      drawingId: 'tenant-a-drawing',
      holder: 'sess-tenant-a',
      services: {
        loadVersions: async () => ({ checkout: { holder: 'sess-tenant-a' } }),
        take: async () => ({ acquired: true, checkout_capability: 'tenant-a-proof' }),
        release: async () => { releases += 1; return { released: true } },
      },
    })
    await controller.take()
    if (controller.getCapability() !== 'tenant-a-proof') fail('the tenant-A take did not install its capability')
    controller.setScope({ drawingId: null, holder: 'sess-tenant-a', mock: false })
    await controller.refresh()
    const snapshot = controller.getSnapshot()
    if (controller.getCapability() !== null) {
      fail('a tenant switch kept the previous principal\'s bearer capability -> it can still AUTHORIZE a write')
    } else if (snapshot.checkout !== null || snapshot.lockedByOther) {
      fail('a tenant switch kept the previous tenant\'s lock record -> it can still GATE a write')
    } else if (releases !== 0) {
      fail('a tenant switch spent the NEW principal\'s credentials releasing the OLD principal\'s lease')
    } else pass('a tenant switch abandons the lease: no capability, no lock record, and no cross-principal release')
  }
}

if (failures) {
  console.error(`\n${failures} check(s) failed`)
  process.exit(1)
}
console.log('CHECKOUT_IDENTITY_OK')
