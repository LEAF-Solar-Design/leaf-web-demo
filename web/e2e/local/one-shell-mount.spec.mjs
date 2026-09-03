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
        // Slice D: on drafting surfaces the tool rail hides behind the band
        // (zero width); its inset is asserted only while it is visible.
        navHidden: !!document.querySelector('aside.nav[data-spine="hidden"]'),
        navWidth: document.querySelector('aside.nav').getBoundingClientRect().width,
        navLeft: document.querySelector('aside.nav').getBoundingClientRect().left,
        railRight: window.innerWidth - document.querySelector('aside.rail').getBoundingClientRect().right,
        headerBg: rgb(cs('header.top').backgroundColor),
        footerBg: rgb(cs('footer.foot-bar').backgroundColor),
      }
    })
    expect(chrome.navRadius).toBe('8px')
    if (chrome.navHidden) expect(chrome.navWidth).toBeLessThanOrEqual(1)
    else expect(chrome.navLeft).toBeGreaterThanOrEqual(12)
    expect(chrome.railRight).toBeGreaterThanOrEqual(12)
    // Dark chrome: the reference cockpit's own chrome is #2a2a2a (42) and
    // its recessed bands #232323 (35); anything lighter than 48 is paper.
    expect(Math.max(...chrome.headerBg)).toBeLessThan(48)
    expect(Math.max(...chrome.footerBg)).toBeLessThan(48)
    // The cockpit: view cluster in the window, status cluster in the footer.
    const view = page.getByTestId('cockpit-view')
    await expect(view).toBeVisible()
    await view.getByRole('button', { name: 'Zoom in' }).click()
    await view.getByRole('button', { name: 'Fit drawing to view' }).click()
    await expectOneCanvasIn(page, '.studio-ground')
    const status = page.getByTestId('cockpit-status')
    await expect(status).toBeVisible()
    await expect(status).toContainText(/entities/)
    // The canvas is the ground's box (W4e: the card window has no flow
    // height of its own any more; the ground is inset to the canvas).
    const box = await page.locator('.studio-shell .studio-ground').boundingBox()
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

    // CAD boots with the tool rail HIDDEN behind the band (Slice D seating:
    // the reference has no left rail; the ribbon carries the tool set, so an
    // expanded catalog beside it would be the duplication ACCEPTANCE named).
    // The aside stays in the DOM at zero width so the grid keeps its cells.
    const nav = page.locator('aside.nav[data-spine="hidden"]')
    await expect(nav).toHaveCount(1)
    expect(await nav.evaluate((el) => el.getBoundingClientRect().width)).toBeLessThanOrEqual(1)
    // The top band carries the rail's expand affordance while it is hidden
    // (W4e: a quick-access button on every tab; the Manage tab's panel too).
    await expect(page.getByTestId('cockpit-band').locator('[data-tool="rail-expand"]')).toHaveCount(1)

    // The ribbon renders the ACTIVE SURFACE'S fold from the REAL catalog,
    // one cluster per family (async load: wait for the first CATALOG cluster;
    // the fixed groups — Drawing, Modify, View, Version, Layers, Author —
    // are synchronous and would satisfy a bare .ribbon-cluster wait).
    const ribbon = page.getByTestId('drafting-ribbon')
    await expect(ribbon).toBeVisible()
    // W4e: the catalog's families are the Manage tab's panels.
    await page.getByRole('tab', { name: 'Manage' }).click()
    await expect(ribbon.locator('.ribbon-cluster[data-family]').first()).toBeVisible({ timeout: 20_000 })
    // No cluster paints over its neighbour: every pair of adjacent tool
    // boxes on the SAME ROW must be disjoint horizontally (the flex-shrink
    // overlap class). The band wraps (W4d), so a cluster that starts a new
    // row is not a neighbour of the one before it.
    const overlaps = await page.evaluate(() => {
      const boxes = [...document.querySelectorAll('.drafting-ribbon .ribbon-cluster')]
        .map((el) => el.getBoundingClientRect())
      let bad = 0
      for (let i = 1; i < boxes.length; i += 1) {
        const sameRow = Math.abs(boxes[i].top - boxes[i - 1].top) < 2
        if (sameRow && boxes[i].left < boxes[i - 1].right - 1) bad += 1
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
    await page.getByRole('tab', { name: 'Manage' }).click()
    const catalogTool = ribbon.locator('[data-family] .ribbon-tool').first()
    await expect(catalogTool).toBeVisible({ timeout: 20_000 })
    await catalogTool.click()
    // The strip appears; nothing auto-runs; Esc dismisses and never ejects.
    await expect(page.locator('.strip-decision')).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(page.locator('.strip-decision')).toHaveCount(0)
    await expect(page).toHaveURL(/\/app\?dev=1$/)

    // A family label in the band expands the rail AND opens that family
    // (the affordance the spine's monogram carried before Slice D).
    const familyLabel = ribbon.locator('[data-family] .ribbon-cluster-label.as-button').first()
    const famLabel = (await familyLabel.textContent()).replace(/^\S+\s*/, '').trim()
    await familyLabel.click()
    await expect(page.locator('aside.nav[data-spine]')).toHaveCount(0)
    await expect(
      page.locator('.section-head[aria-expanded="true"]').filter({ hasText: famLabel }),
    ).toHaveCount(1)

    // The collapse control hides the rail again; the band's expand tool
    // brings it back.
    await page.getByRole('button', { name: 'Collapse the tool rail to a spine' }).click()
    await expect(page.locator('aside.nav[data-spine="hidden"]')).toHaveCount(1)
    await ribbon.locator('[data-tool="rail-expand"]').click()
    await expect(page.locator('aside.nav[data-spine]')).toHaveCount(0)
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
    const candidates = intake.polylines.filter((entity) => (
      entity.handle && entity.closed === true && Array.isArray(entity.pts) && entity.pts.length >= 3
    ))
    expect(candidates.length, 'the sample drawing must carry a closed polyline').toBeGreaterThan(0)

    await setRail(page, '1')
    await page.goto('/app')
    await expect(page.locator('.studio-ground .viewer-canvas canvas')).toHaveCount(1, { timeout: 30_000 })

    const dock = page.getByTestId('properties-dock')
    await expect(dock).toBeVisible()
    await expect(dock.getByRole('button', { name: /Layers/ })).toBeVisible()
    // Empty selection: the readout's own hint renders INSIDE the dock (the
    // dock hosts the same element, never a re-implementation).
    await expect(dock.getByText('Click an entity to select it')).toBeVisible({ timeout: 20_000 })

    // Floating instruments (the dock, the result block, the view cluster)
    // sit ON the drawing, so pick the first closed polyline whose centroid
    // projects to a point the pointer chain delivers to the ground, not to
    // an instrument: the same rule a user's click follows.
    const pick = await page.evaluate((cands) => {
      const canvas = document.querySelector('.studio-ground .viewer-canvas')
      const ground = document.querySelector('.studio-ground')
      for (let i = 0; i < cands.length; i += 1) {
        const pts = cands[i].pts
        const cx = pts.reduce((a, p) => a + p[0] / pts.length, 0)
        const cy = pts.reduce((a, p) => a + p[1] / pts.length, 0)
        const pt = canvas.__cadviewer.project(cx, cy)
        if (!pt || pt.x < 0 || pt.y < 0 || pt.x > window.innerWidth || pt.y > window.innerHeight) continue
        const el = document.elementFromPoint(pt.x, pt.y)
        if (el && ground.contains(el)) return { index: i, x: pt.x, y: pt.y }
      }
      return null
    }, candidates.slice(0, 500))
    expect(pick, 'a closed polyline must be clickable on the drawing, clear of the instruments').toBeTruthy()
    const target = candidates[pick.index]
    await page.mouse.click(pick.x, pick.y)

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

  test('the right palette keeps the plan reachable before drawing intake exists', async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    await page.route('**/api/session?**', (route) => route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'fixture_no_intake' }),
    }))
    await page.goto('/app')
    await expect(page.locator(STUDIO)).toHaveCount(1)

    const dock = page.getByTestId('properties-dock')
    await expect(dock).toBeVisible()
    // The dock's Plan section folds by default (W4c-C: reference information
    // stays reachable without owning the viewport), and a folded section
    // renders no body — so "reachable" is proven as ONE click, not as a
    // panel already in the DOM. This row failed under the managed proof as
    // first written (count 0) because it asserted the latter.
    const planHead = dock.locator('.dock-section-head', { hasText: 'Plan' })
    await expect(planHead).toHaveAttribute('aria-expanded', 'false')
    await expect(page.locator('.ent-panel')).toHaveCount(0)
    await planHead.click()
    await expect(planHead).toHaveAttribute('aria-expanded', 'true')
    await expect(page.locator('.ent-panel')).toHaveCount(1)
    expect(await page.locator('.ent-panel').evaluate((el) => !!el.closest('.properties-dock'))).toBe(true)
  })

  test('<=980px: the entitlement gate renders inline in the console, with and without a drawing (narrow arm)', async ({ page, request }) => {
    // The dock arm is unreachable below 981px (wideViewport is false), so
    // the main-column arm MUST carry the gate there — with a drawing loaded
    // and in the honest-empty state alike. This is the responsive twin of
    // the wide-arm row above: the two arms use one condition, and this row
    // proves the narrow side of it never lets the panel vanish.
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    await page.setViewportSize({ width: 900, height: 640 })

    const expectInlineGate = async () => {
      await expect(page.locator(STUDIO)).toHaveCount(1)
      await expect(page.getByTestId('properties-dock')).toHaveCount(0)
      const ent = page.locator('.ent-panel')
      await expect(ent).toHaveCount(1)
      expect(await ent.evaluate((el) => !!el.closest('main.center-scroll'))).toBe(true)
      await ent.scrollIntoViewIfNeeded()
      await expect(ent).toBeVisible()
    }

    await page.goto('/app')
    await expectOneCanvasIn(page, '.studio-ground')
    await expectInlineGate()

    // Honest-empty: no intake at all (boot before intake, a failed load).
    await page.route('**/api/session?**', (route) => route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'fixture_no_intake' }),
    }))
    await page.goto('/app')
    await expectInlineGate()
  })

  test("the cockpit's actual tools (W4d Slice A): real groups, honest gating, one engine session", async ({ page, request }) => {
    test.setTimeout(180_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    await page.goto('/app')
    await expect(page.locator(STUDIO)).toHaveCount(1)
    await expectOneCanvasIn(page, '.studio-ground')
    const ribbon = page.getByTestId('drafting-ribbon')
    await expect(ribbon).toBeVisible()

    // W4e: the ribbon shows ONE tab's panels at a time. Draw is the
    // reference's eight-panel tab: the engine's Draw and Modify first (only
    // in a build with VITE_CAD_EDIT=1, every deployed artifact; a flag-off
    // build is a truthful state too, the panels ABSENT, not present-and-
    // dead, and this row says which build it proved), then Annotation,
    // Layers, Block, Properties, Groups, Clipboard. View carries the
    // viewer's and the version chain's commands; Manage the tool rail, the
    // catalog's families, and Author, in that order.
    const groupsOf = () => ribbon.locator('.ribbon-cluster').evaluateAll((els) => els.map((el) => el.dataset.group || `family:${el.dataset.family}`))
    let groups = await groupsOf()
    const cadEditOn = groups.includes('modify')
    const referenceTail = ['annotation', 'layers', 'block', 'properties', 'groups', 'clipboard']
    expect(groups).toEqual(cadEditOn ? ['draw', 'modify', ...referenceTail] : referenceTail)
    await page.getByRole('tab', { name: 'View' }).click()
    expect(await groupsOf()).toEqual(['view', 'version', 'layers'])
    await page.getByRole('tab', { name: 'Manage' }).click()
    groups = await groupsOf()
    expect(groups[0]).toBe('rail')
    expect(groups[groups.length - 1]).toBe('author')
    await page.getByRole('tab', { name: 'Draw' }).click()

    // SEATING (W4e), the reference's bands to the pixel at the 1600x1000
    // viewport: a 28px top band, the 95px ribbon, the 32px document tabs,
    // the canvas from (250, 155), the 250px properties pane, the viewport
    // strip at the canvas's top-left and the view cube at its top-right,
    // the 25px command line 35px off the bottom, the 31px status bar, and
    // the ribbon's opaque #2a2a2a.
    const seating = await page.evaluate(() => {
      const r = (sel) => {
        const el = document.querySelector(sel)
        if (!el) return null
        const b = el.getBoundingClientRect()
        return { x: Math.round(b.left), y: Math.round(b.top), w: Math.round(b.width), h: Math.round(b.height), bottom: Math.round(b.bottom) }
      }
      const shell = document.querySelector('.studio-shell').getBoundingClientRect()
      return {
        header: r('header.top'), band: r('.drafting-ribbon'), tabs: r('.viewer-toolbar'), pane: r('[data-testid="properties-dock"]'),
        strip: r('.cockpit-view'), cube: r('.cockpit-cube-wrap'), well: r('.bar.bar-command-line'), status: r('footer.foot-bar'),
        ground: r('.studio-ground'), shellW: Math.round(shell.width), shellH: Math.round(shell.height),
        glass: getComputedStyle(document.querySelector('.drafting-ribbon'), '::before').backgroundColor,
      }
    })
    expect(seating.header.h).toBe(28)
    expect([seating.band.y, seating.band.h]).toEqual([28, 95])
    expect([seating.tabs.y, seating.tabs.h]).toEqual([123, 32])
    expect([seating.ground.x, seating.ground.y]).toEqual([250, 155])
    expect([seating.pane.x, seating.pane.y, seating.pane.w]).toEqual([0, 155, 250])
    expect([seating.strip.x, seating.strip.y, seating.strip.h]).toEqual([250, 155, 26])
    expect(seating.cube.x).toBeGreaterThan(seating.shellW / 2)
    expect([seating.status.h, seating.status.bottom]).toEqual([31, seating.shellH])
    expect([seating.well.h, seating.shellH - seating.well.bottom]).toEqual([25, 35])
    expect(seating.glass).toBe('rgb(42, 42, 42)')
    test.info().annotations.push({ type: 'seating', description: JSON.stringify(seating) })

    // Slice E: the command well is the reference's one-line docked prompt on
    // drafting surfaces: "Command:" then the field, controls on the same row,
    // under 44px tall, and still the ONE Command bar the route matrix pins.
    const well = page.locator('.bar.bar-command-line')
    await expect(well).toHaveCount(1)
    await expect(well.locator('.bar-caret')).toHaveText('Command:')
    const wellBox = await well.boundingBox()
    expect(wellBox.height).toBeLessThanOrEqual(44)
    const inputBox = await well.locator('.bar-input').boundingBox()
    const controlsBox = await well.locator('.bar-controls').boundingBox()
    expect(Math.abs(inputBox.y - controlsBox.y)).toBeLessThan(12)
    await expect(page.getByLabel('Command bar')).toHaveCount(1)
    test.info().annotations.push({ type: 'cad_edit', description: cadEditOn ? 'VITE_CAD_EDIT=1: engine groups proven' : 'VITE_CAD_EDIT off in this build: engine groups absent by construction' })

    // Every group is VISIBLE at 1600 wide: the band wraps instead of hiding
    // half the tools behind a horizontal scroll (a group off-screen is a
    // group the operator cannot see, the opposite of surfacing it).
    const fit = await ribbon.evaluate((el) => {
      const band = el.getBoundingClientRect()
      const clusters = [...el.querySelectorAll('.ribbon-cluster')].map((c) => c.getBoundingClientRect())
      return {
        overflow: el.scrollWidth - el.clientWidth,
        outside: clusters.filter((c) => c.right > band.right + 1 || c.left < band.left - 1).length,
        rows: new Set(clusters.map((c) => Math.round(c.top))).size,
        bandHeight: Math.round(band.height),
        published: getComputedStyle(el.closest('.workspace-card')).getPropertyValue('--cockpit-ribbon-h').trim(),
      }
    })
    expect(fit.overflow).toBeLessThanOrEqual(1)
    expect(fit.outside).toBe(0)
    expect(fit.published).toBe(String(fit.bandHeight) + 'px')
    test.info().annotations.push({ type: 'ribbon', description: 'rows=' + fit.rows + ' height=' + fit.bandHeight })

    // The non-engine groups are live in every build (View tab: fit; Draw tab: layers).
    await page.getByRole('tab', { name: 'View' }).click()
    await expect(ribbon.locator('[data-tool="fit"]')).toBeEnabled()
    await page.getByRole('tab', { name: 'Draw' }).click()
    const layerToggleAny = ribbon.locator('[data-group="layers"] .ribbon-tool').first()
    await expect(layerToggleAny).toHaveAttribute('aria-pressed', 'true')
    if (!cadEditOn) return

    // Modify is unavailable on the console's own drawing (the engine edits
    // an imported DXF only) and SAYS SO: a visible note, and each tool's
    // reason in its accessible name — never a silently greyed group.
    const modify = ribbon.locator('[data-group="modify"]')
    await expect(modify.locator('.ribbon-note')).toHaveText('opens on an imported DXF')
    const draw = ribbon.locator('[data-group="draw"]')
    await expect(draw.locator('.ribbon-note')).toHaveText('opens on an imported DXF')
    // W4e: the reference's Draw column (rectangle, ellipse, point) and its
    // other six Modify tools are present and honestly off ("not in the
    // browser engine yet"); the engine's own four and six carry the
    // document reason. 4 + 3 and 6 + 6: the reference's grid.
    await expect(draw.locator('.ribbon-tool')).toHaveCount(7)
    await expect(draw.locator('[data-tool^="draw:create"]')).toHaveCount(4)
    for (const btn of await draw.locator('.ribbon-tool').all()) await expect(btn).toBeDisabled()
    const modifyTools = modify.locator('.ribbon-tool')
    await expect(modifyTools).toHaveCount(12)
    let modifyReal = 0
    for (const btn of await modifyTools.all()) {
      await expect(btn).toBeDisabled()
      const name = await btn.getAttribute('aria-label')
      if (name.includes('(unavailable: opens on an imported DXF)')) modifyReal += 1
      else expect(name).toContain('(unavailable: not in the browser engine yet)')
    }
    expect(modifyReal).toBe(6)

    // View drives the viewer: fit is live with a drawing loaded (View tab).
    await page.getByRole('tab', { name: 'View' }).click()
    await expect(ribbon.locator('[data-tool="fit"]')).toBeEnabled()
    // Version: the toolbar's exact gates, each disabled control naming why.
    const undo = ribbon.locator('[data-tool="undo"]')
    if (await undo.isDisabled()) expect(await undo.getAttribute('aria-label')).toMatch(/\(unavailable: /)
    // Layers: pressed toggles that drive the SAME visibility the dock's Legend shows.
    const layerToggle = ribbon.locator('[data-group="layers"] .ribbon-tool').first()
    await expect(layerToggle).toHaveAttribute('aria-pressed', 'true')
    await layerToggle.click()
    await expect(layerToggle).toHaveAttribute('aria-pressed', 'false')
    await layerToggle.click()
    await expect(layerToggle).toHaveAttribute('aria-pressed', 'true')
    // Author expands the rail and opens "Author a tool" where authoring is
    // live; where the stage is off (the local proof stack: R5 off) or the
    // plan lacks build, it is disabled WITH the reason, never grey and mute.
    await page.getByRole('tab', { name: 'Manage' }).click()
    const authorBtn = ribbon.locator('[data-tool="author-tool"]')
    if (await authorBtn.isEnabled()) {
      await authorBtn.click()
      await expect(page.locator('aside.nav[data-spine]')).toHaveCount(0)
      await expect(page.locator('.author-section .section-head[aria-expanded="true"]')).toHaveCount(1)
    } else {
      expect(await authorBtn.getAttribute('aria-label')).toMatch(/\(unavailable: /)
      test.info().annotations.push({ type: 'author', description: `disabled: ${await authorBtn.getAttribute('title')}` })
    }

    // Drawing: import-dxf opens the SAME import pane (aria-controls -> a
    // live element), the one place a document enters the engine.
    // (W4e: the File panel is the Insert tab; the same command is the
    // band's quick-access Open.)
    await page.getByRole('tab', { name: 'Insert' }).click()
    const importBtn = ribbon.locator('[data-tool="import-dxf"]')
    await expect(importBtn).toHaveAttribute('aria-expanded', 'false')
    await importBtn.click()
    await expect(importBtn).toHaveAttribute('aria-expanded', 'true')
    await expect(page.locator('#cockpit-import-pane[data-import-open="true"]')).toHaveCount(1)
    const fileInput = page.getByLabel('DXF file')
    await expect(fileInput).toBeVisible()
    // The pane floats on the DRAWING, below the band it opened from and the
    // drawing's own command band: never over the ribbon's second row.
    const clearance = await page.evaluate(() => {
      const band = document.querySelector('.drafting-ribbon').getBoundingClientRect()
      const bar = document.querySelector('.viewer-toolbar').getBoundingClientRect()
      const pane = document.querySelector('.cad-edit-workbench').getBoundingClientRect()
      return { paneTop: pane.top, bandBottom: band.bottom, barBottom: bar.bottom }
    })
    expect(clearance.paneTop).toBeGreaterThanOrEqual(clearance.bandBottom - 1)
    expect(clearance.paneTop).toBeGreaterThanOrEqual(clearance.barBottom - 1)

    // The engine half runs only where the compiled engine is served (dev
    // middleware from pkg-web, or a staged dist/engine). Without it the
    // import reports engine_unavailable by contract, which is not what this
    // row is about — so it stops here, honestly, rather than skipping the
    // whole receipt.
    const engine = await request.get('/engine/engine.js').catch(() => null)
    if (!engine || engine.status() !== 200) {
      test.info().annotations.push({ type: 'engine', description: 'compiled engine not served; import half not exercised' })
      return
    }
    const dxf = [
      '0', 'SECTION', '2', 'ENTITIES',
      '0', 'LINE', '8', 'Panels', '10', '0.0', '20', '0.0', '30', '0.0', '11', '100.0', '21', '50.0', '31', '0.0',
      '0', 'LWPOLYLINE', '8', 'Outline', '90', '3', '70', '0', '10', '0.0', '20', '0.0', '10', '50.0', '20', '5.0', '10', '80.0', '20', '40.0',
      '0', 'ENDSEC', '0', 'EOF',
    ].join('\n') + '\n'
    await fileInput.setInputFiles({ name: 'ribbon.dxf', mimeType: 'application/dxf', buffer: Buffer.from(dxf) })
    await expect(page.getByRole('status').filter({ hasText: /Loaded ribbon\.dxf/ })).toHaveCount(1, { timeout: 60_000 })
    // W4f slice A0: the canvas now shows the ENGINE document (the card is
    // stamped with its id through the viewer's applyVersion seam).
    await expect(page.locator('.workspace-card[data-engine-document="ribbon.dxf"]')).toHaveCount(1)
    await page.getByRole('tab', { name: 'Draw' }).click()
    // Loaded, nothing selected: the ribbon names the next missing thing.
    await expect(modify.locator('.ribbon-note')).toHaveText('select an entity in the imported DXF')
    await page.getByRole('radio').first().check()
    await expect(modify.locator('.ribbon-note')).toHaveCount(0)
    const del = ribbon.locator('[data-tool="modify:delete"]')
    await expect(del).toBeEnabled()
    await del.click()
    // The engine re-parsed its own written bytes and the pane shows the result.
    await expect(page.getByRole('status').filter({ hasText: /delete applied/ })).toHaveCount(1, { timeout: 60_000 })
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText('1')
    // The deleted entity's selection cleared with it (selection identity).
    await expect(modify.locator('.ribbon-note')).toHaveText('select an entity in the imported DXF')

    // W4d Slice B / W4e slice H: the Draw group creates real entities in
    // the imported document. A tool ARMS and the command line prompts for
    // its operands in the reference grammar ("LINE  Specify first point:"),
    // Enter runs; the selection lands on what was drawn, so Modify is live
    // on it at once; the count is the engine's re-parse of its own bytes.
    await expect(draw.locator('.ribbon-note')).toHaveCount(0)
    await expect(page.getByTestId('cockpit-prompt')).toHaveCount(0)
    const lineTool = ribbon.locator('[data-tool="draw:createLine"]')
    await lineTool.click()
    const promptRow = page.getByTestId('cockpit-prompt')
    await expect(promptRow).toHaveAttribute('data-op', 'createLine')
    await expect(promptRow).toContainText('LINE')
    await expect(promptRow).toContainText('Specify first point:')
    await expect(lineTool).toHaveAttribute('aria-expanded', 'true')
    // The prompt is the command line's upper line: the command input's own
    // left edge and width, seated directly on top of it.
    const seat = await page.evaluate(() => {
      const p = document.getElementById('cockpit-prompt').getBoundingClientRect()
      const b = document.querySelector('.bar.bar-command-line').getBoundingClientRect()
      return { dl: Math.abs(p.left - b.left), dw: Math.abs(p.width - b.width), gap: b.top - p.bottom }
    })
    expect(seat.dl).toBeLessThanOrEqual(1)
    expect(seat.dw).toBeLessThanOrEqual(1)
    expect(seat.gap).toBeGreaterThanOrEqual(-1)
    expect(seat.gap).toBeLessThanOrEqual(2)
    await page.getByLabel('ribbon x2').fill('40')
    await page.getByLabel('ribbon y2').fill('30')
    await page.getByLabel('ribbon y2').press('Enter')
    await expect(page.getByRole('status').filter({ hasText: /createLine applied: entity \d+ drawn/ })).toHaveCount(1, { timeout: 60_000 })
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText('2')
    await expect(modify.locator('.ribbon-note')).toHaveCount(0)
    await expect(del).toBeEnabled()
    await ribbon.locator('[data-tool="draw:createCircle"]').click()
    await expect(promptRow).toHaveAttribute('data-op', 'createCircle')
    await expect(lineTool).toHaveAttribute('aria-expanded', 'false')
    await page.getByLabel('ribbon r').fill('2.5')
    await page.getByTestId('cockpit-prompt-run').click()
    await expect(page.getByRole('status').filter({ hasText: /createCircle applied: entity \d+ drawn/ })).toHaveCount(1, { timeout: 60_000 })
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText('3')
    await expect(page.getByTestId('cad-edit-entity-list')).toContainText('CIRCLE on layer 0')
    // A degenerate create is refused as a sentence, and nothing changes.
    await page.getByLabel('ribbon r').fill('0')
    await page.getByLabel('ribbon r').press('Enter')
    await expect(page.getByRole('status').filter({ hasText: /Circle refused: r must be greater than 0/ })).toHaveCount(1)
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText('3')
    // Esc cancels the command; the prompt leaves with it.
    await page.getByLabel('ribbon r').press('Escape')
    await expect(page.getByTestId('cockpit-prompt')).toHaveCount(0)

    // W4f slice B: a typed command word on the Command bar arms the same
    // prompt and clears the bar; the natural-language router never sees it.
    const bar = page.getByLabel('Command bar')
    await bar.fill('circle')
    await bar.press('Enter')
    await expect(promptRow).toHaveAttribute('data-op', 'createCircle')
    await expect(promptRow).toContainText('CIRCLE')
    await expect(bar).toHaveValue('')
    await page.getByLabel('ribbon r').press('Escape')
    await expect(page.getByTestId('cockpit-prompt')).toHaveCount(0)

    // W4f slice F: `u` undoes the last engine edit (the circle), `redo`
    // brings it back; the band's Undo edit / Redo edit carry the depths.
    const undoQuick = page.locator('.cockpit-band [data-tool="quick-undo-edit"]')
    await expect(undoQuick).toBeEnabled()
    await bar.fill('u')
    await bar.press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText('2', { timeout: 60_000 })
    await expect(page.getByRole('status').filter({ hasText: /Undid createCircle/ })).toHaveCount(1)
    await expect(page.locator('.cockpit-band [data-tool="quick-redo-edit"]')).toBeEnabled()
    await bar.fill('redo')
    await bar.press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText('3', { timeout: 60_000 })
    await expect(page.getByTestId('cad-edit-entity-list')).toContainText('CIRCLE on layer 0')

    // W4f slice A1: the drawing answers the prompt. Arm LINE by its word,
    // click two points ON the drawing ground, the fields take the picked
    // coordinates, the caret moves on, the console's click-to-select stands
    // aside, Enter draws. The pixels come from the ground's own box (right
    // half, clear of the floating import card and the viewcube) and the
    // expected values from the viewer's own unproject of those pixels, so the
    // row holds at any viewport or framing; a pixel that lands on chrome
    // instead of the drawing fails here by name. (The proof's 1600x1000 frame
    // once put world (20,30) under the import card, and the click went to it.)
    await bar.fill('l')
    await bar.press('Enter')
    await expect(promptRow).toHaveAttribute('data-op', 'createLine')
    await expect(page.locator('.workspace-card[data-cockpit-picking="1"]')).toHaveCount(1)
    // A visible higher Esc rung owns the first key even when its opener keeps
    // focus outside the dialog. History closes and the armed command survives.
    // (exact: once the drawer is open its "Close version history" button
    // also matches the substring, and the focus assertion hit two elements
    // in the flagged proof on main a0820937)
    const historyWhileArmed = page.getByRole('button', { name: 'History', exact: true })
    await historyWhileArmed.click()
    await expect(historyWhileArmed).toBeFocused()
    const historyDialog = page.getByRole('dialog', { name: 'Version history' })
    await expect(historyDialog).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(historyDialog).toHaveCount(0)
    await expect(promptRow).toHaveAttribute('data-op', 'createLine')
    await expect(lineTool).toHaveAttribute('aria-expanded', 'true')
    const groundPick = (fx, fy) => page.evaluate(([px, py]) => {
      const ground = document.querySelector('.studio-ground')
      const box = ground.getBoundingClientRect()
      const x = Math.round(box.left + box.width * px)
      const y = Math.round(box.top + box.height * py)
      const hit = document.elementFromPoint(x, y)
      const world = document.querySelector('.studio-ground .viewer-canvas').__cadviewer.unproject(x, y)
      const name = hit ? `${hit.tagName.toLowerCase()}.${String(hit.className || '').split(' ')[0]}` : 'nothing'
      return { x, y, onGround: !!(hit && ground.contains(hit)), name, wx: world ? world.x : NaN, wy: world ? world.y : NaN }
    }, [fx, fy])
    // The same rounding the picker writes (pointPicking.js round3).
    const r3 = (v) => { const r = Math.round(v * 1000) / 1000; return Object.is(r, -0) ? '0' : String(r) }
    const a = await groundPick(0.62, 0.55)
    const b = await groundPick(0.8, 0.35)
    expect(a.onGround, `first pick pixel (${a.x},${a.y}) hit ${a.name}, not the drawing`).toBe(true)
    expect(b.onGround, `second pick pixel (${b.x},${b.y}) hit ${b.name}, not the drawing`).toBe(true)
    expect(Number.isFinite(a.wx) && Number.isFinite(a.wy) && Number.isFinite(b.wx) && Number.isFinite(b.wy)).toBe(true)
    await page.mouse.click(a.x, a.y)
    await expect(page.getByLabel('ribbon x', { exact: true })).toHaveValue(r3(a.wx))
    await expect(page.getByLabel('ribbon y', { exact: true })).toHaveValue(r3(a.wy))
    await expect(page.getByLabel('ribbon x2', { exact: true })).toBeFocused()
    await page.mouse.click(b.x, b.y)
    await expect(page.getByLabel('ribbon x2', { exact: true })).toHaveValue(r3(b.wx))
    await expect(page.getByLabel('ribbon y2', { exact: true })).toHaveValue(r3(b.wy))
    await expect(page.getByTestId('cockpit-prompt-run')).toBeFocused()
    await expect(page.locator('.selection-readout')).not.toContainText('Polyline')
    await page.keyboard.press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText('4', { timeout: 60_000 })
    // W4f-2: after the run the caret is back in the prompt's first field (the
    // Run button was disabled while the engine was busy and the browser had
    // dropped focus to the body), and while a point command is picking the
    // floating import card lets clicks through to the drawing under it.
    // W4f-3: LINE chains: the segment's end is the next segment's first
    // point and the caret waits in the next-point field.
    await expect(page.getByLabel('ribbon x', { exact: true })).toHaveValue(r3(b.wx))
    await expect(page.getByLabel('ribbon y', { exact: true })).toHaveValue(r3(b.wy))
    await expect(page.getByLabel('ribbon x2', { exact: true })).toBeFocused()
    // W4f-4: F8 turns ORTHO on (the prompt's chip is pressed); the next
    // pick, measured from the chain point, snaps to the axis of the larger
    // move: the pixel below is far to the right of b and a little down, so
    // x2 takes the pick's x and y2 holds b's y. F8 again turns it off.
    await page.keyboard.press('F8')
    await expect(page.getByTestId('cockpit-ortho')).toHaveAttribute('aria-pressed', 'true')
    const o = await groundPick(0.95, 0.42)
    expect(o.onGround, `ortho pick pixel (${o.x},${o.y}) hit ${o.name}, not the drawing`).toBe(true)
    expect(Math.abs(o.wx - b.wx)).toBeGreaterThan(Math.abs(o.wy - b.wy))
    await page.mouse.click(o.x, o.y)
    await expect(page.getByLabel('ribbon x2', { exact: true })).toHaveValue(r3(o.wx))
    await expect(page.getByLabel('ribbon y2', { exact: true })).toHaveValue(r3(b.wy))
    await page.keyboard.press('F8')
    await expect(page.getByTestId('cockpit-ortho')).toHaveAttribute('aria-pressed', 'false')
    // W4f-5: Enter draws that segment (the chain moves on), then F3 turns
    // OSNAP on and a click a few pixels off the imported polyline's corner
    // (50, 5) lands exactly on it. F3 again turns it off.
    await page.keyboard.press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText('5', { timeout: 60_000 })
    await page.keyboard.press('F3')
    await expect(page.getByTestId('cockpit-osnap')).toHaveAttribute('aria-pressed', 'true')
    const corner = await page.evaluate(() => {
      const canvas = document.querySelector('.studio-ground .viewer-canvas')
      const px = canvas.__cadviewer.project(50, 5)
      const x = Math.round(px.x) + 5
      const y = Math.round(px.y) - 4
      const hit = document.elementFromPoint(x, y)
      return { x, y, onGround: !!hit?.closest('.studio-ground') }
    })
    expect(corner.onGround, `snap pixel (${corner.x},${corner.y}) is not on the drawing`).toBe(true)
    await page.mouse.click(corner.x, corner.y)
    await expect(page.getByLabel('ribbon x2', { exact: true })).toHaveValue('50')
    await expect(page.getByLabel('ribbon y2', { exact: true })).toHaveValue('5')
    await page.keyboard.press('F3')
    await expect(page.getByTestId('cockpit-osnap')).toHaveAttribute('aria-pressed', 'false')
    const underCard = () => page.evaluate(() => {
      // The card is a full-width pass-through layer; the floating "Edit a
      // DXF drawing" workbench is the child that sits over the drawing.
      const bench = document.querySelector('#cockpit-import-pane .cad-edit-workbench')
      const box = bench.getBoundingClientRect()
      const hit = document.elementFromPoint(Math.round(box.left + box.width / 2), Math.round(box.top + box.height / 2))
      return { ground: !!hit?.closest('.studio-ground'), card: !!(hit && bench.contains(hit)) }
    })
    expect(await underCard()).toEqual({ ground: true, card: false })
    // A bare Esc with the focus on the body (the proof-3 situation), cancels
    // the armed command (W4f-2: the prompt's window rung), and the card takes
    // its clicks back.
    await page.evaluate(() => document.activeElement?.blur())
    await expect(page.getByLabel('ribbon x', { exact: true })).not.toBeFocused()
    await page.keyboard.press('Escape')
    await expect(page.locator('.workspace-card[data-cockpit-picking="1"]')).toHaveCount(0)
    expect(await underCard()).toEqual({ ground: false, card: true })
    // A sentence is still a sentence: it routes, it never arms. LAST in the
    // row on purpose: while its route decision is shown the Command bar's
    // Enter belongs to the decision strip, so a word typed after it would be
    // swallowed (the race that failed this row once).
    await bar.fill('draw a line across the roof')
    await bar.press('Enter')
    await expect(page.getByTestId('cockpit-prompt')).toHaveCount(0)
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

  test('solar depth never projects rooftop strings over the edit fixture (W4c-V3)', async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    await page.goto('/?fixture=edit&dev=1')
    await expect(page.locator(STUDIO)).toHaveCount(1)
    await page.getByLabel('Use mock data (off = live backend)').check()
    await expect(page.getByRole('button', { name: 'Preview pending edit' })).toBeVisible()
    await expect(page.locator('.viewer-canvas canvas')).toHaveCount(1, { timeout: 30_000 })

    await page.getByRole('tab', { name: 'Solar CAD' }).click()
    await expect(page.getByRole('tab', { name: 'Solar CAD' })).toHaveAttribute('aria-selected', 'true')
    // Give the bundled solve enough time to load on the pre-fix path. The
    // fixture must remain route-free after that same async boundary.
    await page.waitForTimeout(500)
    await expect(page.locator('.viewer-canvas[data-string-routes]')).toHaveCount(0)
  })

  test('the page dissolves into the viewport: no page-shaped block sits on the drawing (W4c-C)', async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    await page.goto('/app')
    await expect(page.locator(STUDIO)).toHaveCount(1)
    await expect(page.locator('.studio-ground .viewer-canvas canvas')).toHaveCount(1, { timeout: 30_000 })
    await expect(page.getByTestId('properties-dock')).toBeVisible()

    // THE regression this slice fixes: the operator's read of the first
    // cockpit was "that doesn't contain the cad cockpit" because page-shaped
    // blocks (a 1192x140 import slab, a 1194x228 white entitlements panel)
    // owned the drawing. Nothing large and light-backed may sit inside the
    // shell again - this is a computed-style check, not a class allowlist.
    const lightSlabs = await page.evaluate(() => [...document.querySelectorAll('.studio-shell *')]
      .filter((el) => {
        const m = getComputedStyle(el).backgroundColor.match(/rgba?\((\d+), (\d+), (\d+)(?:, ([\d.]+))?/)
        if (!m) return false
        if (m[4] !== undefined && Number(m[4]) < 0.2) return false
        const r = el.getBoundingClientRect()
        return Number(m[1]) > 200 && Number(m[2]) > 200 && Number(m[3]) > 200 && r.width * r.height > 30_000
      })
      .map((el) => `${el.tagName}.${el.className}`.slice(0, 60)))
    expect(lightSlabs, 'a light page-shaped block is sitting on the drawing').toEqual([])

    // The entitlements panel is hosted in the dock, not stacked in the column.
    const ent = page.locator('.ent-panel')
    if (await ent.count()) {
      expect(await ent.first().evaluate((el) => !!el.closest('.properties-dock'))).toBe(true)
    }
    // The result is a floating instrument, not a full-width band.
    const result = page.locator('.result-block')
    if (await result.count()) {
      // W4e: the instruments are viewport-fixed (the shell is a fixed host).
      expect(['absolute', 'fixed']).toContain(await result.evaluate((el) => getComputedStyle(el).position))
      // Idle (no result yet) the block is off the canvas entirely (W4e), which
      // is the strongest form of "no page-shaped block"; measured only when shown.
      const resultBox = await result.boundingBox()
      if (resultBox) expect(resultBox.width).toBeLessThan(600)
    }
    // Slice D seating: the tool rail is hidden behind the band and the job
    // monitor is a right-hand spine whose button fits its rail (44px, not
    // a sliver). One click expands it; its header collapses it again.
    await expect(page.locator('aside.nav[data-spine="hidden"]')).toHaveCount(1)
    const jobRail = page.locator('aside.rail[data-spine]')
    await expect(jobRail).toHaveCount(1)
    const rail = await jobRail.boundingBox()
    const btn = await jobRail.locator('.spine-btn').first().boundingBox()
    expect(rail.width).toBeGreaterThanOrEqual(40)
    expect(btn.width + 8).toBeLessThanOrEqual(rail.width)
    await jobRail.locator('.spine-btn').first().click()
    await expect(page.locator('aside.rail[data-spine]')).toHaveCount(0)
    await expect(page.getByRole('heading', { name: /Job monitor/ })).toBeVisible()
    await page.getByRole('button', { name: 'Collapse the job monitor to a spine' }).click()
    await expect(page.locator('aside.rail[data-spine]')).toHaveCount(1)
  })

  test('rail OFF keeps every block in flow: the cockpit changes nothing without the rail (W4c-C)', async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '0')
    await page.goto('/app')
    await expect(page.locator('.studio-shell')).toHaveCount(0)
    await expect(page.locator('.viewer-wrap .viewer-canvas canvas')).toHaveCount(1, { timeout: 30_000 })
    // The card grows no cockpit hooks, and the blocks keep their page flow.
    await expect(page.locator('.workspace-card[data-import-open]')).toHaveCount(0)
    await expect(page.locator('.workspace-card#cockpit-import-pane')).toHaveCount(0)
    await expect(page.getByTestId('properties-dock')).toHaveCount(0)
    // Slice E: the command well keeps its caret glyph and two-row well rail OFF.
    await expect(page.locator('.bar.bar-command-line')).toHaveCount(0)
    await expect(page.locator('.bar .bar-caret')).toHaveText('›')
    const result = page.locator('.result-block')
    if (await result.count()) {
      expect(await result.evaluate((el) => getComputedStyle(el).position)).toBe('static')
    }
    const ent = page.locator('.ent-panel')
    if (await ent.count()) {
      expect(await ent.first().evaluate((el) => !!el.closest('main.center-scroll'))).toBe(true)
    }
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
