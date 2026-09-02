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
//
// Every test here calls requireLocalReady first: under the managed runner
// (LEAF_E2E_MANAGED=1) a dead stack HARD-FAILS instead of skipping, so a
// green receipt from this file must come from that runner and quote its
// executed/skipped counts (the unmanaged `proof:local` skips silently).
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

// Storage keys telemetry.js owns (`leaf.telemetry.session` sid and the
// per-event `leaf.telemetry.cap.*` counters), written LAZILY on the first
// tracked event by BOTH shells — the one NAMED exclusion from the rollback
// residue diff, because they are event-driven rather than shell-driven (an
// idle boot writes none; the first click writes the sid). The baseline walk
// below performs the SAME interaction as the studio walk anyway, so the diff
// is symmetric even without the exclusion. Anything else that grows during
// or after a studio session is stale storage the rollback contract forbids.
const TELEMETRY_OWNED = (k) => k === 'leaf.telemetry.session' || k.startsWith('leaf.telemetry.cap.')
function residue(baseline, keys) {
  return keys.filter((k) => !baseline.includes(k) && !TELEMETRY_OWNED(k))
}

const storageKeys = (page) => page.evaluate(() => Object.keys({ ...localStorage, ...sessionStorage }))

// The one interaction both walks perform: hit the card's viewer window (the
// pan/select path through the pointer chain under the rail; the inline canvas
// without it) and walk the top-level Esc rung.
async function interact(page) {
  const box = await page.locator('.viewer-wrap').boundingBox()
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2)
  await page.keyboard.press('Escape')
  await expect(page).toHaveURL(/\/app$/)
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
    // The ground is a named landmark once attached (never aria-hidden with a
    // canvas inside it).
    await expect(page.locator('.studio-ground[role="region"][aria-label="Drawing"]')).toHaveCount(1)
    await expect(page.locator('.studio-ground[aria-hidden="true"]')).toHaveCount(0)
  })

  test('the pointer chain punches through the card window to the ground canvas', async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    await page.goto('/app')
    await expectOneCanvasIn(page, '.studio-ground')
    // Every link in the chain computes to `none` (a specificity defeat at
    // any link silently kills pan/zoom/select through the card window).
    const chain = await page.evaluate(() => (
      ['.studio-shell .app', '.studio-shell .center-col', '.studio-shell main.center-scroll', '.studio-shell .workspace-card', '.studio-shell .viewer-wrap']
        .map((sel) => [sel, getComputedStyle(document.querySelector(sel)).pointerEvents])
    ))
    for (const [sel, value] of chain) expect(value, sel).toBe('none')
    // Painted panes keep their events; a pre-existing pointer-transparent
    // overlay (the drawer layer) keeps ITS declared `none` — the restore
    // rules must not out-specify it into a click shield.
    const painted = await page.evaluate(() => (
      ['header.top', '.viewer-toolbar', '.bar-dock']
        .map((sel) => [sel, document.querySelector(sel) ? getComputedStyle(document.querySelector(sel)).pointerEvents : 'absent'])
    ))
    for (const [sel, value] of painted) expect(['auto', 'absent'], sel).toContain(value)
    const drawerLayer = await page.evaluate(() => {
      const el = document.querySelector('.studio-shell .app > .drawer-layer')
      return el ? getComputedStyle(el).pointerEvents : 'absent'
    })
    expect(['none', 'absent']).toContain(drawerLayer)
    // The receipt that matters: hit-testing the center of the card's viewer
    // window lands INSIDE the ground subtree (the portaled canvas), not on
    // console furniture.
    const hit = await page.evaluate(() => {
      const r = document.querySelector('.studio-shell .viewer-wrap').getBoundingClientRect()
      const el = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
      return { tag: el?.tagName, inGround: !!el?.closest('.studio-ground') }
    })
    expect(hit.inGround, `elementFromPoint hit <${hit.tag}> outside the ground`).toBe(true)
  })

  test('/app/* and /ty/* sub-paths, and every boot query off /try, are the same console mode', async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    for (const path of ['/ty', '/app/deep/link', '/ty/deep', '/?drawing=cat-panels', '/?ops=1', '/?demo=1', '/?dev=1', '/?fixture=edit']) {
      await page.goto(path)
      await expect(page.locator(STUDIO), path).toHaveCount(1)
    }
  })

  test('each tab has its own ground: drawing for CAD and Solar CAD, the project board for Browser, the device stage for iOS', async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    await page.goto('/app')
    await expectOneCanvasIn(page, '.studio-ground')
    const viewer = page.locator('.studio-ground .studio-ground-viewer')
    const board = page.locator('.studio-ground [data-ground="browser"]')
    const device = page.locator('.studio-ground [data-ground="ios"]')
    // Both non-drawing grounds are MOUNTED from the start (one mount, toggled
    // by `hidden`), and hidden while the drawing shows.
    await expect(board).toHaveCount(1)
    await expect(device).toHaveCount(1)
    await expect(viewer).toBeVisible()
    await expect(board).toBeHidden()
    await expect(device).toBeHidden()

    await page.getByRole('tab', { name: 'Browser' }).click()
    await expect(board).toBeVisible()
    await expect(viewer).toBeHidden()
    await expect(device).toBeHidden()
    // The canvas is still there (hidden), never torn down by a tab switch.
    await expect(page.locator('.studio-ground .viewer-canvas canvas')).toHaveCount(1)
    await expect(board.locator('[data-tile="drawing"]')).toContainText(/polylines/)
    await expect(board.locator('[data-tile="catalog"]')).toContainText(/famil/)

    await page.getByRole('tab', { name: 'iOS' }).click()
    await expect(device).toBeVisible()
    await expect(board).toBeHidden()
    await expect(viewer).toBeHidden()
    await expect(device.locator('.device-frame')).toHaveCount(1)
    await expect(device.locator('[data-testid="device-state"]')).not.toBeEmpty()

    await page.getByRole('tab', { name: 'Solar CAD' }).click()
    await expect(viewer).toBeVisible()
    await expect(board).toBeHidden()
    await expect(device).toBeHidden()
    await expectOneCanvasIn(page, '.studio-ground')

    // Deep link straight into a non-drawing surface boots that ground.
    await page.goto('/app?surface=browser')
    await expect(page.locator(STUDIO)).toHaveCount(1)
    await expect(page.locator('.studio-ground [data-ground="browser"]')).toBeVisible()
    await expect(page.locator('.studio-ground .studio-ground-viewer')).toBeHidden()
  })

  test('/try stays the operator stage; /sheets and unknown paths never mount the studio', async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    await page.goto('/try')
    await expect(page.locator('main.stage-root[data-scene="tool"]')).toHaveCount(1)
    await expect(page.locator(STUDIO)).toHaveCount(0)
    // ?demo on /try stays operator mode (route matrix, ?demo row — the BOOT
    // consumer; the drawing-selection consumer is a separate owed test).
    await page.goto('/try?demo=1')
    await expect(page.locator('main.stage-root[data-scene="tool"]')).toHaveCount(1)
    await expect(page.locator(STUDIO)).toHaveCount(0)
    // The studio branch lives ONLY in the scene-app arm.
    await page.goto('/sheets')
    await expect(page.locator(STUDIO)).toHaveCount(0)
    await expect(page.locator('.studio-ground')).toHaveCount(0)
    await page.goto('/definitely-not-a-route')
    await expect(page.locator(STUDIO)).toHaveCount(0)
    await expect(page.locator('main.stage-root[data-scene="tool"]')).toHaveCount(0)
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

  test('<=980px: the shell is the scroll surface and the ground stays pinned', async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    await page.setViewportSize({ width: 900, height: 640 })
    await page.goto('/app')
    await expectOneCanvasIn(page, '.studio-ground')
    const before = await page.evaluate(() => {
      const shell = document.querySelector('.studio-shell')
      return { scrollHeight: shell.scrollHeight, clientHeight: shell.clientHeight, overflowY: getComputedStyle(shell).overflowY }
    })
    expect(before.overflowY).toBe('auto')
    expect(before.scrollHeight, 'stacked console must overflow the viewport').toBeGreaterThan(before.clientHeight)
    const after = await page.evaluate(() => {
      const shell = document.querySelector('.studio-shell')
      shell.scrollTop = 400
      const ground = document.querySelector('.studio-ground').getBoundingClientRect()
      return { scrollTop: shell.scrollTop, groundTop: ground.top, groundHeight: ground.height, viewport: window.innerHeight }
    })
    expect(after.scrollTop).toBeGreaterThan(0)
    expect(after.groundTop).toBe(0)
    expect(after.groundHeight).toBe(after.viewport)
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
    // No surface ground exists without the shell — on any tab.
    await expect(page.locator('[data-ground]')).toHaveCount(0)
    await page.getByRole('tab', { name: 'Browser' }).click()
    await expect(page.locator('[data-ground]')).toHaveCount(0)
    await expect(page.locator('#product-surface-panel')).toHaveCount(1)
  })

  test('ROLLBACK: on -> off restores the old shell with no stale storage, URL state, or provider duplication', async ({ page, request }) => {
    test.setTimeout(180_000)
    await requireLocalReady(request, test, API_BASE)

    // BASELINE FIRST, rail OFF: the key set the old shell owns after a fresh
    // boot AND the same interaction the studio walk performs, captured
    // BEFORE any studio session exists. (A baseline taken after the studio
    // boots grandfathers every key the studio writes; an idle baseline
    // misattributes event-driven keys to the studio.)
    await setRail(page, '0')
    await page.goto('/app')
    await expectOneCanvasIn(page, '.viewer-wrap')
    await interact(page)
    const baseline = await storageKeys(page)

    // The studio session, interacted with — not idle.
    await page.unroute('**/runtime-flags.js')
    await setRail(page, '1')
    await page.reload()
    await expect(page.locator(STUDIO)).toHaveCount(1)
    await expectOneCanvasIn(page, '.studio-ground')
    await interact(page)
    expect(residue(baseline, await storageKeys(page)), 'the studio session wrote storage').toEqual([])

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
    await expect(page.locator('main')).toHaveCount(1)
    await expect(page).toHaveURL(/\/app$/)

    // No storage residue from the studio session: nothing the old shell did
    // not already own on its own fresh boot may survive the rollback.
    expect(residue(baseline, await storageKeys(page)), 'stale storage survived rollback').toEqual([])
  })
})
