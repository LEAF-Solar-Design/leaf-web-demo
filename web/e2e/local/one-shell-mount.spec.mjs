import { expect, test } from '@playwright/test'
import { requireLocalReady } from './requireReady.mjs'
import { setRail } from './railFlag.mjs'

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

// setRail moved to railFlag.mjs (W4c-0): the checkout-ownership twin and
// every future rail-ON row arm the flag through the same interception.

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
    // No duplicate-instance regressions: one checkout stamp, one WORKSPACE
    // controller stamp (W4c-0 debt: a duplicated WorkspaceControllerProvider
    // would be a second converse session, invisible to the checkout stamp),
    // one Command bar, one main landmark (the console's own).
    expect(await page.locator('[data-checkout-instance]').count()).toBe(1)
    expect(await page.locator('[data-controller-instance]').count()).toBe(1)
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

  test('floating rails, dark chrome, and the drawing cockpit (W4b)', async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    await page.goto('/app')
    await expectOneCanvasIn(page, '.studio-ground')
    // The surface hook exists only under the rail, and CAD drops the page
    // furniture (the command bar is the prompt).
    await expect(page.locator('.app[data-surface="cad"]')).toHaveCount(1)
    await expect(page.locator('.home-q')).toBeHidden()
    await expect(page.locator('.tc-continuity')).toBeHidden()
    // Rails float: inset with rounded corners; header and footer are dark.
    const chrome = await page.evaluate(() => {
      const cs = (sel) => getComputedStyle(document.querySelector(sel))
      const rgb = (v) => v.match(/\d+/g).slice(0, 3).map(Number)
      return {
        navRadius: cs('aside.nav').borderRadius,
        navLeft: document.querySelector('aside.nav').getBoundingClientRect().left,
        railRight: window.innerWidth - document.querySelector('aside.rail').getBoundingClientRect().right,
        headerBg: rgb(cs('header.top').backgroundColor),
        footerBg: rgb(cs('footer.foot-bar').backgroundColor),
      }
    })
    expect(chrome.navRadius).toBe('8px')
    expect(chrome.navLeft).toBeGreaterThanOrEqual(12)
    expect(chrome.railRight).toBeGreaterThanOrEqual(12)
    expect(Math.max(...chrome.headerBg)).toBeLessThan(40)
    expect(Math.max(...chrome.footerBg)).toBeLessThan(40)
    // The cockpit: view cluster in the window, status cluster in the footer.
    const view = page.getByTestId('cockpit-view')
    await expect(view).toBeVisible()
    await view.getByRole('button', { name: 'Zoom in' }).click()
    await view.getByRole('button', { name: 'Fit drawing to view' }).click()
    await expectOneCanvasIn(page, '.studio-ground')
    const status = page.getByTestId('cockpit-status')
    await expect(status).toBeVisible()
    await expect(status).toContainText(/entities/)
    const box = await page.locator('.studio-shell .viewer-wrap').boundingBox()
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
    await page.mouse.move(box.x + box.width / 2 + 5, box.y + box.height / 2 + 5)
    await expect(status.locator('.cockpit-coord b').first()).not.toHaveText('—')
    await expect(status.locator('.cockpit-scale b')).toContainText(/1px = /)
    // Browser keeps its page furniture (the frame is the page there).
    await page.getByRole('tab', { name: 'Browser' }).click()
    await expect(page.locator('.app[data-surface="browser"]')).toHaveCount(1)
    await expect(page.locator('.home-q')).toBeVisible()
    await expect(page.getByTestId('cockpit-view')).toHaveCount(0)
    await expect(page.getByTestId('cockpit-status')).toHaveCount(0)
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

  test('the drafting cockpit on the REAL stack: spine by default, per-surface ribbon, rail adaptation (W4c-V1)', async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    await page.goto('/app')
    await expect(page.locator(STUDIO)).toHaveCount(1)

    // CAD boots in the spine posture: the ribbon carries the tool set, so an
    // expanded catalog beside it would be the duplication ACCEPTANCE named.
    const nav = page.locator('aside.nav[data-spine]')
    await expect(nav).toHaveCount(1)
    expect((await nav.boundingBox()).width).toBeLessThanOrEqual(48)
    // W4b chrome row invariants survive the spine: radius + the 12px inset.
    expect(await nav.evaluate((el) => getComputedStyle(el).borderRadius)).toBe('8px')
    expect((await nav.boundingBox()).x).toBeGreaterThanOrEqual(12)

    // The ribbon renders the ACTIVE SURFACE'S fold from the REAL catalog,
    // one cluster per family (async load: wait for the first cluster).
    const ribbon = page.getByTestId('drafting-ribbon')
    await expect(ribbon).toBeVisible()
    await expect(ribbon.locator('.ribbon-cluster').first()).toBeVisible({ timeout: 20_000 })
    // No cluster paints over its neighbour: every pair of adjacent tool
    // boxes must be disjoint horizontally (the flex-shrink overlap class).
    const overlaps = await page.evaluate(() => {
      const boxes = [...document.querySelectorAll('.drafting-ribbon .ribbon-cluster')]
        .map((el) => el.getBoundingClientRect())
      let bad = 0
      for (let i = 1; i < boxes.length; i += 1) {
        if (boxes[i].left < boxes[i - 1].right - 1) bad += 1
      }
      return bad
    })
    expect(overlaps).toBe(0)

    // Browser is not a drafting surface: expanded rail, folded families, no
    // ribbon, no spine - the rail populates per application.
    await page.getByRole('tab', { name: 'Browser' }).click()
    await expect(page.getByTestId('drafting-ribbon')).toHaveCount(0)
    await expect(page.locator('aside.nav[data-spine]')).toHaveCount(0)
    await expect(page.locator('.fam-title')).toBeVisible()
  })

  test('a ribbon click arms the SAME confirm ladder as the rail (mock walk, W4c-V1)', async ({ page, request }) => {
    // Mock mode exercises the identical CLIENT path (commitCatalogDecision ->
    // armDecision -> the decision strip) without the real stack's canonical-
    // version context, which plain /app does not stage for direct catalog
    // runs (armDecision fails closed there by design - same as the rail).
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    await page.goto('/app?dev=1')
    await expect(page.locator(STUDIO)).toHaveCount(1)
    // Deterministic mock: the signed-in local stack classifies ?demo=1 as
    // liveDemo, so flip the dev Mock switch instead of trusting the query.
    await page.getByLabel('Use mock data (off = live backend)').check()
    const ribbon = page.getByTestId('drafting-ribbon')
    await expect(ribbon.locator('.ribbon-tool').first()).toBeVisible({ timeout: 20_000 })
    await ribbon.locator('.ribbon-tool').first().click()
    // The strip appears; nothing auto-runs; Esc dismisses and never ejects.
    await expect(page.locator('.strip-decision')).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(page.locator('.strip-decision')).toHaveCount(0)
    await expect(page).toHaveURL(/\/app\?dev=1$/)

    // A spine monogram expands the rail AND opens that family.
    const monogram = page.locator('.nav-spine .spine-btn').nth(1)
    const famLabel = await monogram.getAttribute('title')
    await monogram.click()
    await expect(page.locator('aside.nav[data-spine]')).toHaveCount(0)
    await expect(
      page.locator('.section-head[aria-expanded="true"]').filter({ hasText: famLabel }),
    ).toHaveCount(1)

    // The collapse control restores the spine.
    await page.getByRole('button', { name: 'Collapse the tool rail to a spine' }).click()
    await expect(page.locator('aside.nav[data-spine]')).toHaveCount(1)
  })

  test('the right palette: dock hosts Layers + Selection, geometry from a real pick (W4c-V2)', async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    // A real closed polyline from the live session intake, picked via the
    // viewer's DEV projection hook - the same gesture viewer-interaction
    // proves rail-OFF; this row proves it lands in the DOCK under the rail.
    const sessionResponse = await request.get(`${API_BASE}/api/session?dwg=rooftop_demo`, {
      headers: { 'X-Tenant-Id': 'demo-tenant' },
    })
    expect(sessionResponse.status()).toBe(200)
    const intake = (await sessionResponse.json()).intake
    const target = intake.polylines.find((entity) => (
      entity.handle && entity.closed === true && Array.isArray(entity.pts) && entity.pts.length >= 3
    ))
    expect(target, 'the sample drawing must carry a closed polyline').toBeTruthy()

    await setRail(page, '1')
    await page.goto('/app')
    await expect(page.locator('.studio-ground .viewer-canvas canvas')).toHaveCount(1, { timeout: 30_000 })

    const dock = page.getByTestId('properties-dock')
    await expect(dock).toBeVisible()
    await expect(dock.getByRole('button', { name: /Layers/ })).toBeVisible()
    // Empty selection: the readout's own hint renders INSIDE the dock (the
    // dock hosts the same element, never a re-implementation).
    await expect(dock.getByText('Click an entity to select it')).toBeVisible({ timeout: 20_000 })

    const centroid = target.pts.reduce((acc, pt) => [acc[0] + pt[0] / target.pts.length, acc[1] + pt[1] / target.pts.length], [0, 0])
    const clientPoint = await page.evaluate(([wx, wy]) => (
      document.querySelector('.studio-ground .viewer-canvas').__cadviewer.project(wx, wy)
    ), centroid)
    await page.mouse.click(clientPoint.x, clientPoint.y)

    await expect(dock.locator('.selection-readout')).toContainText('Polyline', { timeout: 10_000 })
    await expect(dock.locator('.selection-readout')).toContainText(target.handle)
    const geometry = dock.getByTestId('dock-geometry')
    await expect(geometry).toContainText('Vertices')
    await expect(geometry).toContainText('Perimeter')
    await expect(geometry).toContainText('Area')
    // Fail-closed formatting: the rows never render NaN or -0.00.
    const text = await geometry.textContent()
    expect(text).not.toMatch(/NaN|-0\.00/)
    // Deselect clears the geometry with the selection (scope-reset shape).
    await dock.getByRole('button', { name: 'Deselect' }).click()
    await expect(dock.getByTestId('dock-geometry')).toHaveCount(0)
  })

  test('solar depth: real solved strings on the Solar tab only, honesty-gated (W4c-V3)', async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    await page.goto('/app?dev=1')
    await expect(page.locator(STUDIO)).toHaveCount(1)
    // Mock: the bundled solve was computed against these exact drawing
    // bytes; the live demo drawing is mutable and gets NO overlay.
    await page.getByLabel('Use mock data (off = live backend)').check()
    await expect(page.locator('.viewer-canvas canvas')).toHaveCount(1, { timeout: 30_000 })

    // CAD first: no routes on a non-solar surface, ever.
    await expect(page.locator('.viewer-canvas[data-string-routes]')).toHaveCount(0)

    // The expected count comes from the BUNDLE itself (one of the 135
    // solved strings is a degenerate <2-point path the viewer honestly
    // refuses to draw), so the row cannot drift from the artifact.
    const payload = await (await page.request.get('/demo-solve.json')).json()
    const expected = payload.solve.strings.filter((route) => Array.isArray(route.pts) && route.pts.length >= 2).length
    expect(expected).toBeGreaterThan(100)

    await page.getByRole('tab', { name: 'Solar CAD' }).click()
    await expect(page.locator(`.viewer-canvas[data-string-routes="${expected}"]`)).toHaveCount(1, { timeout: 20_000 })

    // Back to CAD: the overlay leaves with the surface.
    await page.getByRole('tab', { name: 'CAD', exact: true }).click()
    await expect(page.locator('.viewer-canvas[data-string-routes]')).toHaveCount(0)
  })

  test('Esc ladder, history rung under the rail: an open drawer owns Esc, the route never moves', async ({ page, request }) => {
    // W4c-0 debt (ACCEPTANCE "Esc LADDER rungs"): the terminal row above
    // proves Esc never LEAVES /app; this rung proves an owned surface
    // (the Version history drawer, role=dialog) consumes Esc FIRST, closes,
    // and leaves the studio standing. The route rung (a live routing panel)
    // is owed with the first rail-ON run walk.
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    await page.goto('/app?drawing=cat-panels')
    await expect(page.locator(STUDIO)).toHaveCount(1)
    const history = page.getByRole('button', { name: 'History' })
    await expect(history).toBeVisible({ timeout: 20_000 })
    await history.click()
    const drawer = page.getByRole('dialog', { name: 'Version history' })
    await expect(drawer).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(drawer).toHaveCount(0)
    await expect(page).toHaveURL(/\/app\?drawing=cat-panels$/)
    await expect(page.locator(STUDIO)).toHaveCount(1)
    // And the now-unowned Esc still refuses to eject the console.
    await page.keyboard.press('Escape')
    await expect(page).toHaveURL(/\/app\?drawing=cat-panels$/)
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
    expect(await page.locator('[data-controller-instance]').count()).toBe(1)
    // No surface ground, no surface hook, no cockpit without the shell.
    await expect(page.locator('[data-ground]')).toHaveCount(0)
    await expect(page.locator('.app[data-surface]')).toHaveCount(0)
    await expect(page.getByTestId('cockpit-view')).toHaveCount(0)
    await expect(page.getByTestId('cockpit-status')).toHaveCount(0)
    // W4c-V1 furniture is studio-only: none of it may exist rail-OFF.
    await expect(page.getByTestId('drafting-ribbon')).toHaveCount(0)
    await expect(page.locator('aside.nav[data-spine]')).toHaveCount(0)
    await expect(page.locator('.nav-spine')).toHaveCount(0)
    await expect(page.locator('.spine-collapse')).toHaveCount(0)
    await expect(page.getByTestId('properties-dock')).toHaveCount(0)
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
    expect(await page.locator('[data-controller-instance]').count()).toBe(1)
    await expect(page.getByLabel('Command bar')).toHaveCount(1)
    await expect(page.locator('main')).toHaveCount(1)
    await expect(page).toHaveURL(/\/app$/)

    // No storage residue from the studio session: nothing the old shell did
    // not already own on its own fresh boot may survive the rollback.
    expect(residue(baseline, await storageKeys(page)), 'stale storage survived rollback').toEqual([])
  })
})
