import { expect, test } from '@playwright/test'
import { requireLocalReady } from './requireReady.mjs'

// The W3 one-shell mount proof (docs/convergence/ACCEPTANCE.md): every
// route-matrix row the local stack can exercise, with the rail ON and OFF,
// plus the sol-required ROLLBACK walk (rail off restores the old shell with
// no stale storage, URL state, or provider duplication).
//
// The rail is RUNTIME: /runtime-flags.js is a same-origin static file loaded
// synchronously before the bundle, so per-test route interception is the
// exact production mechanism (the container entrypoint rewrites the same
// file). addInitScript is NOT sufficient — the real file executes after init
// scripts and would overwrite the flag back to '0'.
const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'

async function setRail(page, value) {
  await page.route('**/runtime-flags.js', (route) => route.fulfill({
    contentType: 'text/javascript',
    body: `window.__LEAF_FLAGS = { oneShell: '${value}' }`,
  }))
}

const STUDIO = '.studio-shell[data-scene="app"][data-mode="console"]'

// One canvas, and it lives where the mode says: the studio ground when the
// rail is on, the console's inline wrap when it is off.
async function expectOneCanvasIn(page, containerSelector) {
  await expect(page.locator('.viewer-canvas canvas')).toHaveCount(1, { timeout: 30_000 })
  await expect(page.locator(`${containerSelector} .viewer-canvas canvas`)).toHaveCount(1)
}

test.describe('route matrix, rail ON', () => {
  test('/app boots studio mode console: one canvas in the ground, one controller, one command bar', async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    await page.goto('/app')
    await expect(page.locator(STUDIO)).toHaveCount(1)
    await expectOneCanvasIn(page, '.studio-ground')
    // No duplicate-instance regressions: one checkout stamp, one session
    // surface, one Command bar, one main landmark (the console's own).
    expect(await page.locator('[data-checkout-instance]').count()).toBe(1)
    await expect(page.getByLabel('Command bar')).toHaveCount(1)
    await expect(page.locator('main')).toHaveCount(1)
    // No stage furniture leaked into console mode.
    await expect(page.locator('.tc-operator-rail')).toHaveCount(0)
    await expect(page.locator('main.stage-root')).toHaveCount(0)
  })

  test('/ty is the same console mode; deep-link queries boot it off any path', async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    await page.goto('/ty')
    await expect(page.locator(STUDIO)).toHaveCount(1)

    await page.goto('/?drawing=cat-panels')
    await expect(page.locator(STUDIO)).toHaveCount(1)

    await page.goto('/?ops=1')
    await expect(page.locator(STUDIO)).toHaveCount(1)

    await page.goto('/?demo=1')
    await expect(page.locator(STUDIO)).toHaveCount(1)
  })

  test('/try stays the operator stage — never console mode', async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    await page.goto('/try')
    await expect(page.locator('main.stage-root[data-scene="tool"]')).toHaveCount(1)
    await expect(page.locator(STUDIO)).toHaveCount(0)
    // ?demo on /try stays operator mode (route matrix, ?demo row).
    await page.goto('/try?demo=1')
    await expect(page.locator('main.stage-root[data-scene="tool"]')).toHaveCount(1)
    await expect(page.locator(STUDIO)).toHaveCount(0)
  })

  test('Esc at top level in console mode never leaves /app (route matrix, Esc row)', async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    await page.goto('/app')
    await expect(page.locator(STUDIO)).toHaveCount(1)
    await page.keyboard.press('Escape')
    await page.keyboard.press('Escape')
    await expect(page).toHaveURL(/\/app$/)
    await expect(page.locator(STUDIO)).toHaveCount(1)
  })
})

test.describe('route matrix, rail OFF + rollback', () => {
  test('rail off is byte-for-byte the old shell: inline canvas, no studio DOM', async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '0')
    await page.goto('/app')
    await expect(page.locator('.studio-shell')).toHaveCount(0)
    await expect(page.locator('.studio-ground')).toHaveCount(0)
    await expectOneCanvasIn(page, '.viewer-wrap')
    expect(await page.locator('[data-checkout-instance]').count()).toBe(1)
  })

  test('ROLLBACK: on -> off restores the old shell with no stale storage, URL state, or provider duplication', async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)

    await setRail(page, '1')
    await page.goto('/app')
    await expect(page.locator(STUDIO)).toHaveCount(1)
    await expectOneCanvasIn(page, '.studio-ground')
    const storageOn = await page.evaluate(() => Object.keys({ ...localStorage, ...sessionStorage }))

    // The flip: the same file the container entrypoint rewrites, then a
    // reload — exactly the production rollback (env flip + task restart).
    await page.unroute('**/runtime-flags.js')
    await setRail(page, '0')
    await page.reload()

    await expect(page.locator('.studio-shell')).toHaveCount(0)
    await expect(page.locator('.studio-ground')).toHaveCount(0)
    await expectOneCanvasIn(page, '.viewer-wrap')
    expect(await page.locator('[data-checkout-instance]').count()).toBe(1)
    await expect(page.getByLabel('Command bar')).toHaveCount(1)
    await expect(page).toHaveURL(/\/app$/)

    // No storage residue from the studio session: the mount writes nothing,
    // so the key set after rollback must not have grown any shell-shaped key.
    const storageOff = await page.evaluate(() => Object.keys({ ...localStorage, ...sessionStorage }))
    const grown = storageOff.filter((k) => !storageOn.includes(k))
    expect(grown.filter((k) => /shell|studio/i.test(k))).toEqual([])
  })
})
