import { expect, test } from '@playwright/test'
import { requireLocalReady } from './requireReady.mjs'
import { setRail } from './railFlag.mjs'

// The cross-scene continuity row (standardization slice 4b).
//
// SiteRoot owns the F-8 continuity rail and the AccountSignOut control above
// the scene ternary (site/ContinuityStore.jsx), so the /try <-> /app crossing
// no longer tears them down: the rail is the SAME element on both sides, it
// stays attached, its shared parts (the static label, the tenant catalog
// item) read the same on both sides, and the full label is byte-equal again
// once the round trip lands back on the stage. The
// first-run coach is NOT hoisted (recorded deviation, SURFACE-CONTRACT.md
// "What slice 4b changed"): its dismissal is a localStorage key, so a coach
// dismissed before the crossing must not be re-offered after it, without a
// SiteRoot-level mount.
//
// The crossing is the ROUTER's own in-app navigation, not a page load: a
// page.goto would build a new document and prove nothing about node
// identity. site/router.js navigate() is pushState + the 'leaf:navigate'
// event (its file header names both as the contract a sibling depends on),
// so the row drives exactly that pair.
//
// The coach only mounts for a visitor whose /api/session answers 401; the
// managed local stack runs auth-off (every visitor is active), so this row
// intercepts the session probe the way hp01-first-run-coach.spec.mjs does.
// Both scenes still render their nav under a signed-out session, which is
// what the rail rides in.
//
// Every row calls requireLocalReady first: under the managed runner
// (LEAF_E2E_MANAGED=1) a dead stack HARD-FAILS instead of skipping.
const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'

async function cross(page, path) {
  await page.evaluate((next) => {
    window.history.pushState({}, '', `${next}${window.location.search}${window.location.hash}`)
    window.dispatchEvent(new Event('leaf:navigate'))
  }, path)
}

async function routeSession401(page) {
  await page.route('**/api/session?*', (route) => route.fulfill({
    status: 401,
    contentType: 'application/json',
    body: JSON.stringify({ error: { message: 'sign in required' } }),
  }))
}

// A JS expando on the rail node: it survives exactly as long as the node
// object does, so reading it back after the crossing IS the identity proof.
// It touches no attribute, so the rendered markup stays byte-identical.
const MARK = '__leafContinuityMark'
const STATIC_LABEL = 'Carried across every profile'
const mark = (page, value) => page.evaluate(([key, v]) => {
  const el = document.querySelector('[data-testid="continuity-rail"]')
  el[key] = v
  const catalogItem = Array.from(el.querySelectorAll('.tc-continuity-item'))
    .map((item) => item.textContent)
    .find((text) => text.startsWith('catalog ·')) ?? null
  return { text: el.textContent, catalogItem }
}, [MARK, value])
const readMark = (page) => page.evaluate((key) => document.querySelector('[data-testid="continuity-rail"]')?.[key] ?? null, MARK)

for (const rail of ['1', '0']) {
  test(`/try -> /app -> /try keeps ONE continuity rail and a dismissed coach stays dismissed (rail ${rail === '1' ? 'ON' : 'OFF'})`, async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, rail)
    await routeSession401(page)

    // /try: the stage, signed out, so the coach is offered.
    await page.goto('/try')
    await expect(page.getByRole('main', { name: 'Leaf operator workspace' })).toBeVisible()
    const railEl = page.getByTestId('continuity-rail')
    await expect(railEl).toBeAttached()
    // The stage publishes its derivation on mount; wait for the live catalog
    // item so the captured label is the settled one, not the first paint.
    await expect(railEl).toContainText('catalog ·', { timeout: 30_000 })
    const coach = page.getByTestId('first-run-coach')
    await expect(coach).toBeVisible()
    const { text: label, catalogItem } = await mark(page, 'try')
    expect(label).toContain(STATIC_LABEL)
    expect(catalogItem).toMatch(/^catalog · \d+ (family|families) \/ \d+ tools$/)
    await page.getByTestId('first-run-coach-dismiss').click()
    await expect(coach).toHaveCount(0)

    // The crossing to /app: the console mounts (lazy), adopts the SAME node.
    await cross(page, '/app')
    await expect(page.locator('.app')).toBeAttached({ timeout: 60_000 })
    if (rail === '1') await expect(page.locator('.studio-shell[data-scene="app"]')).toHaveCount(1)
    else await expect(page.locator('.studio-shell')).toHaveCount(0)
    await expect(railEl).toBeAttached()
    expect(await readMark(page)).toBe('try')
    // Same label, for the parts both scenes derive from the same source: the
    // static label and the catalog item (both shells read the ONE tenant
    // catalog fold, F-7). The project item is each scene's OWN derivation
    // (F-9): on this stack the stage's operator identity starts with no
    // drawing while the console boots its drawing, so the two legitimately
    // differ there once the console has published. The carry of the stage's
    // value across the crossing window itself is a timing race in a browser,
    // so it is pinned deterministically in site/continuityHoist.test.jsx
    // rather than asserted here.
    await expect(railEl).toContainText(STATIC_LABEL)
    await expect(railEl).toContainText(catalogItem)
    // The console's own nav is where it now sits: the console's tabs, not a
    // leftover stage element (the stage unmounted with its scene).
    await expect(page.locator('.app .tc-product-nav [data-testid="continuity-rail"]')).toHaveCount(1)
    await expect(page.locator('.stage-root')).toHaveCount(0)
    await expect(page.getByTestId('first-run-coach')).toHaveCount(0)

    // Back to /try: still the same node, and the coach is not re-offered.
    await cross(page, '/try')
    await expect(page.getByRole('main', { name: 'Leaf operator workspace' })).toBeVisible()
    await expect(railEl).toBeAttached()
    expect(await readMark(page)).toBe('try')
    // Back on the stage the derivation is the stage's again, so the FULL
    // label is byte-equal to what was captured before the round trip.
    await expect(railEl).toHaveText(label)
    await expect(page.locator('.stage-root .tc-product-nav [data-testid="continuity-rail"]')).toHaveCount(1)
    await expect(page.getByTestId('first-run-coach')).toHaveCount(0)
    await expect.poll(() => page.evaluate(() => localStorage.getItem('leaf.coach.dismissed.v1'))).toBe('1')
    // Exactly one rail in the document at every point above, never two.
    await expect(page.getByTestId('continuity-rail')).toHaveCount(1)
  })
}
