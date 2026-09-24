import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
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

test('J1 row1, J1 row2, J1 row4, J1 row5, J1 row8: served Browser panes, first run, material and drawing-profile continuity', async ({ page, request }) => {
  test.setTimeout(120_000)
  await requireLocalReady(request, test, API_BASE)
  await setRail(page, '1')
  await page.setViewportSize({ width: 1920, height: 1080 })
  // A local presentation fixture, never a credential. Workspace and upload
  // writes are intercepted; the rest of the managed stack keeps its real IO.
  await page.addInitScript(() => {
    localStorage.setItem('leaf.jwt', 'j1-presentation-fixture')
    if (!sessionStorage.getItem('j1-initialized')) {
      localStorage.removeItem('leaf.org_id')
      sessionStorage.setItem('j1-initialized', 'true')
    }
  })
  const calls = []
  const project = { project_id: 'j1-project', name: 'J1 roof' }
  const workspace = {
    project,
    drawing_versions: [{ version_id: 'j1-version', drawing_id: 'roof', seq: 1 }],
    jobs: [{ job_id: 'j1-job', tool_name: 'Measure roof', status: 'succeeded' }],
    built_tools: [{ tool_id: 'j1-tool', name: 'Roof count' }],
    drawing_artifacts: [],
  }
  let bound = false
  await page.route('**/api/**', async (route) => {
    const req = route.request()
    const path = new URL(req.url()).pathname
    const json = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    if (path === '/api/orgs' && req.method() === 'POST') {
      calls.push(['org', req.postDataJSON()])
      bound = true
      return json({ org: { org_id: 'j1-org', name: 'J1 workspace' } })
    }
    if (path === '/api/projects' && req.method() === 'GET') {
      return bound ? json({ projects: [project] })
        : json({ detail: 'verified subject has no active platform identity binding' }, 403)
    }
    if (path === '/api/projects' && req.method() === 'POST') {
      calls.push(['create', req.postDataJSON()])
      return json({ project })
    }
    if (path === '/api/projects/j1-project') {
      calls.push(['open', path])
      return json(workspace)
    }
    if (path === '/api/site/guest-upload-policy') return json({ enabled: true, accepted: ['.dxf'], max_bytes: 1024 * 1024 })
    if (path === '/api/drawings/upload') {
      calls.push(['upload'])
      return json({ drawing_id: 'j1-upload', status: 'ready', extracted_version: 1 })
    }
    if (path.startsWith('/api/drawings/j1-upload')) return json({ dwg: 'roof.dxf', polylines: [], layers: [] })
    if (path === '/api/projects/j1-project/drawing-versions/import') {
      calls.push(['attach', req.postDataJSON()])
      workspace.drawing_artifacts = [{ drawing_id: 'j1-upload', name: 'roof.dxf', status: 'ready', created_at: '2026-09-17T00:00:00Z' }]
      return json({ drawing_version: { drawing_id: 'j1-upload', version: 1, name: 'roof.dxf' }, replayed: false })
    }
    // Do not send the presentation sentinel to the real local service.
    const headers = { ...req.headers() }
    delete headers.authorization
    return route.continue({ headers })
  })
  await page.goto('/app?surface=browser')
  const board = page.locator('[data-ground="browser"]')
  await expect(board).toBeVisible()
  const createWorkspace = board.getByRole('region', { name: 'Create your workspace' })
  await createWorkspace.getByLabel('Workspace name').fill('J1 workspace')
  await createWorkspace.getByRole('button', { name: 'Create workspace', exact: true }).click()
  const start = board.getByRole('region', { name: 'Workspace projects', exact: true })
  await expect(start.getByRole('button', { name: 'J1 roof', exact: true })).toBeVisible()
  await start.getByLabel('Project name').fill('J1 roof')
  await start.getByRole('button', { name: 'Create project', exact: true }).click()
  await expect(board).toHaveAttribute('data-project-state', 'project')
  expect(calls).toContainEqual(['org', { name: 'J1 workspace' }])
  expect(calls).toContainEqual(['create', { name: 'J1 roof' }])
  // Reload clears the open-project controller state, then use its open handler.
  // The fixture bootstrap is now bound, including on an auth-configured stack.
  await page.reload()
  const openStart = board.getByRole('region', { name: 'Workspace projects', exact: true })
  await openStart.getByRole('button', { name: 'J1 roof', exact: true }).click()
  await expect(board).toHaveAttribute('data-project-state', 'project')
  for (const [action, pane] of [['version', 'versions'], ['job', 'jobs'], ['tool', 'tools']]) {
    await board.locator(`[data-action="${action}"]`).click()
    const panel = board.locator(`[data-pane="${pane}"]`)
    await expect(panel).toBeVisible()
    await expect(panel.locator('.ground-pane-head')).toContainText('J1 roof')
    await panel.getByRole('button', { name: 'Back to board' }).click()
    await expect(board.locator('[data-pane]')).toHaveCount(0)
    await expect(board).toHaveAttribute('data-project-state', 'project')
  }
  await page.locator('[data-tool="files:upload"]').click()
  const material = board.locator('[data-pane="material-intake"]')
  await expect(material).toContainText('Material attaches to J1 roof')
  await expect(material.getByRole('button', { name: 'Upload DWG or DXF' })).toBeEnabled()
  await material.getByLabel('Drawing file').setInputFiles({ name: 'roof.dxf', mimeType: 'application/dxf', buffer: Buffer.from('0\nEOF\n') })
  await expect(material).toContainText('Attached roof.dxf as version 1 to J1 roof')
  expect(calls).toContainEqual(['attach', { source: { drawing_id: 'j1-upload', version: 1 }, name: 'roof.dxf' }])
  expect(calls.findIndex(([kind]) => kind === 'upload')).toBeLessThan(calls.findIndex(([kind]) => kind === 'attach'))
  await board.getByRole('button', { name: 'Back to board' }).click()

  // Same served drawing nodes and profile chrome before/after visiting the
  // newly mounted Browser panes. The unit fixture pins the 85ef8090 slots.
  for (const label of ['CAD', 'Solar CAD']) {
    await page.getByRole('tab', { name: label, exact: true }).click()
    await expectOneCanvasIn(page, '.studio-ground')
    const viewer = page.locator('.studio-ground-viewer')
    await expect(viewer).toBeVisible()
    const node = await viewer.elementHandle()
    await expectSharedChrome(page)
    await expect(page.getByTestId('properties-dock')).toBeVisible()
    await expect(page.locator('[data-pane]')).toHaveCount(0)
    await page.getByRole('tab', { name: 'Browser', exact: true }).click()
    await board.locator('[data-action="version"]').click()
    await expect(board.locator('[data-pane="versions"]')).toBeVisible()
    await page.getByRole('tab', { name: label, exact: true }).click()
    await expect(viewer).toBeVisible()
    expect(await viewer.evaluate((current, before) => current === before, node)).toBe(true)
    await expect(page.locator('[data-pane]')).toHaveCount(0)
    await expectSharedChrome(page)
  }
})

test('J1 row6: served demo material stays disabled and makes no project mutation', async ({ page, request }) => {
  await requireLocalReady(request, test, API_BASE)
  await setRail(page, '1')
  const mutations = []
  page.on('request', (req) => {
    const path = new URL(req.url()).pathname
    if (req.method() !== 'GET' && (path.startsWith('/api/projects') || path === '/api/drawings/upload')) mutations.push(path)
  })
  await page.goto('/app?surface=browser&demo=1')
  const board = page.locator('[data-ground="browser"]')
  await board.locator('[data-action="drawing"]').click()
  const intake = board.locator('[data-pane="material-intake"]')
  await expect(intake).toBeVisible()
  await expect(intake.getByLabel('Drawing file')).toBeDisabled()
  await expect(intake.getByRole('button', { name: 'Upload DWG or DXF' })).toBeDisabled()
  await expect(intake).toContainText('Uploads are unavailable in this demo.')
  expect(mutations).toEqual([])
})

test("E02 row1: a real-size catalog never covers the Start panel's Create project button", async ({ page, request }) => {
  test.setTimeout(120_000)
  await requireLocalReady(request, test, API_BASE)
  await setRail(page, '1')
  await page.setViewportSize({ width: 1920, height: 1080 })
  // The same presentation fixture as J1, never a credential.
  await page.addInitScript(() => {
    localStorage.setItem('leaf.jwt', 'j1-presentation-fixture')
    if (!sessionStorage.getItem('j1-initialized')) {
      localStorage.removeItem('leaf.org_id')
      sessionStorage.setItem('j1-initialized', 'true')
    }
  })
  const project = { project_id: 'j1-project', name: 'J1 roof' }
  // A real-size catalog: 5 families of 8 capabilities (40), the size the
  // local stack serves, with descriptions long enough to wrap.
  const families = Array.from({ length: 5 }, (_, f) => ({
    family_id: `e02-family-${f + 1}`,
    label: `E02 family ${f + 1}`,
    description: `Capabilities of E02 family ${f + 1}.`,
    capabilities: Array.from({ length: 8 }, (_, c) => ({
      name: `e02_f${f + 1}_cap${c + 1}`,
      version: '1.0.0',
      description: `E02 capability ${c + 1} of family ${f + 1} reads the drawing and reports what it found, in a sentence long enough to wrap onto a second line of its tile.`,
      params_schema: { type: 'object', properties: {} },
      capabilities: ['drawing.read'],
      provenance: 'e02',
    })),
  }))
  let bound = false
  await page.route('**/api/**', async (route) => {
    const req = route.request()
    const path = new URL(req.url()).pathname
    const json = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    if (path === '/api/orgs' && req.method() === 'POST') {
      bound = true
      return json({ org: { org_id: 'j1-org', name: 'J1 workspace' } })
    }
    if (path === '/api/projects' && req.method() === 'GET') {
      return bound ? json({ projects: [project] })
        : json({ detail: 'verified subject has no active platform identity binding' }, 403)
    }
    if (path === '/api/projects' && req.method() === 'POST') return json({ project })
    if (path === '/api/capabilities' && req.method() === 'GET') return json({ families })
    // Do not send the presentation sentinel to the real local service.
    const headers = { ...req.headers() }
    delete headers.authorization
    return route.continue({ headers })
  })
  await page.goto('/app?surface=browser')
  const board = page.locator('[data-ground="browser"]')
  await expect(board).toBeVisible()
  const createWorkspace = board.getByRole('region', { name: 'Create your workspace' })
  await createWorkspace.getByLabel('Workspace name').fill('J1 workspace')
  await createWorkspace.getByRole('button', { name: 'Create workspace', exact: true }).click()
  const start = board.getByRole('region', { name: 'Workspace projects', exact: true })
  await expect(start.getByRole('button', { name: 'J1 roof', exact: true })).toBeVisible()

  // The tiles start below the panel: nothing of theirs overlaps it.
  const tiles = board.locator('.ground-tiles')
  await expect(tiles).toBeVisible()
  const region = await start.boundingBox()
  const tilesBox = await tiles.boundingBox()
  expect(tilesBox.y).toBeGreaterThanOrEqual(region.y + region.height - 1)

  // The button's own centre hits the button, not a tile painted over it.
  const create = start.getByRole('button', { name: 'Create project', exact: true })
  await create.scrollIntoViewIfNeeded()
  const box = await create.boundingBox()
  const hitsButton = await create.evaluate((button, point) => {
    const hit = document.elementFromPoint(point.x, point.y)
    return Boolean(hit) && button.contains(hit)
  }, { x: box.x + box.width / 2, y: box.y + box.height / 2 })
  expect(hitsButton).toBe(true)

  await start.getByLabel('Project name').fill('J1 roof')
  await create.click()
  await expect(board).toHaveAttribute('data-project-state', 'project')
})

async function expectSharedChrome(page) {
  await expect(page.locator('.app[data-studio-shell="cockpit"]')).toHaveCount(1)
  await expect(page.getByTestId('cockpit-band')).toHaveCount(1)
  await expect(page.getByRole('toolbar', { name: 'Quick access', exact: true })).toBeVisible()
  await expect(page.getByRole('tablist', { name: 'Ribbon', exact: true })).toBeVisible()
  await expect(page.getByRole('tablist', { name: 'Workspace profile', exact: true })).toBeVisible()
  await expect(page.locator('.bar.bar-command-line')).toHaveCount(1)
  await expect(page.locator('footer.foot-bar')).toBeVisible()
}

async function expectStudioBoardDetails(page, board) {
  // The proof stack runs live; the demo caveat is pinned by the unit rows.
  await expect(page.locator('.start-board-project-caveat')).toHaveCount(0)
  for (const effect of ['Does not change the drawing', 'Changes the drawing']) {
    const row = board.locator('.ground-catalog-tool').filter({ has: page.getByText(effect, { exact: true }) }).first()
    await expect(row).toBeAttached()
    await row.scrollIntoViewIfNeeded()
    const name = row.locator('strong')
    await expect(name).not.toBeEmpty()
    for (const text of [name, row.getByText(effect, { exact: true })]) {
      await expect(text).toBeVisible()
      await expect(text).toBeInViewport({ ratio: 1 })
      expect(await text.evaluate((node) => node.clientWidth > 0 && node.scrollWidth <= node.clientWidth)).toBe(true)
    }
  }
  const planHead = page.getByTestId('properties-dock').locator('.dock-section-head:visible', { hasText: 'Plan' })
  const openPlan = await planHead.count() && await planHead.getAttribute('aria-expanded') === 'false'
  if (openPlan) await planHead.click()
  const summary = page.locator('.studio-profile-info details')
  const openSummary = await summary.count() && !(await summary.evaluate((node) => node.open))
  if (openSummary) await summary.locator('summary').click()
  const plan = page.getByRole('region', { name: 'Entitlements', exact: true })
  await expect(plan.locator('.ent-head')).toContainText(/Plan permissions checked|Plan details unavailable/)
  await expect(plan).not.toContainText('full access')
  await expect(plan).not.toContainText(/drawing\.read|drawing\.write|build lane|converse lane/)
  if (openPlan) await planHead.click()
  if (openSummary) await summary.locator('summary').click()
}

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

test('option A: translucent chrome owns clicks and wheel over the full-bleed drawing', async ({ page, request }) => {
  test.setTimeout(120_000)
  await page.setViewportSize({ width: 1920, height: 940 })
  await requireLocalReady(request, test, API_BASE)
  await setRail(page, '1')
  await page.goto('/app?surface=cad&drawing=cat-panels')
  await expectOneCanvasIn(page, '.studio-ground')
  const canvas = page.locator('.studio-ground .viewer-canvas')
  await expect.poll(() => canvas.evaluate((el) => !!el.__cadviewer?.cameraPose())).toBe(true)
  await expect(canvas).toHaveAttribute('data-safe-rect', /^\d+,\d+,\d+,\d+$/)
  await expect(page.locator('[data-tool="draw:createLine"]')).toBeEnabled({ timeout: 30_000 })
  const readState = () => page.evaluate(() => ({
    pose: document.querySelector('.studio-ground .viewer-canvas').__cadviewer.cameraPose(),
    selection: document.querySelector('.selection-readout')?.textContent,
  }))
  await expect(page.locator('.selection-readout')).toBeVisible()
  await expectSharedChrome(page)
  const before = await readState()
  for (const [selector, horizontal, vertical] of [
    ['#drafting-ribbon', -6, -6],
    ['.properties-dock', 8, -8],
    ['[data-testid="cockpit-view"]', -6, null],
    ['.bar.bar-command-line', 3, null],
    ['footer.foot-bar', null, null],
  ]) {
    const point = await page.locator(selector).evaluate((element, [horizontal, vertical]) => {
      const button = horizontal === null ? element.querySelector('.cockpit-status-toggles button') : null
      if (horizontal === null && !button) throw new Error('footer status button is absent')
      const box = (button || element).getBoundingClientRect()
      const x = horizontal === null ? box.left + box.width / 2 : horizontal < 0 ? box.right + horizontal : box.left + horizontal
      const y = vertical === null ? box.top + box.height / 2 : box.bottom + vertical
      const hit = document.elementFromPoint(x, y)
      return { x, y, owns: element.contains(hit), notDrawing: !document.querySelector('.studio-ground')?.contains(hit) }
    }, [horizontal, vertical])
    expect(point.owns, selector).toBe(true)
    expect(point.notDrawing, selector).toBe(true)
    await page.mouse.click(point.x, point.y)
    await page.mouse.wheel(0, 120)
    // Wheel delivery and any camera update settle across animation frames.
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))
    expect(await readState(), selector).toEqual(before)
  }
  expect(await page.locator('footer.foot-bar').evaluate((element) => {
    const box = element.getBoundingClientRect()
    return !document.querySelector('.studio-ground')?.contains(document.elementFromPoint(box.right - 40, box.top + box.height / 2))
  }), 'footer.foot-bar').toBe(true)
})

test.describe('route matrix, rail ON', () => {
  for (const surface of ['cad', 'solar']) {
    test(`Start preserves the ${surface} profile, document, prompt and mounted nodes`, async ({ page, request }) => {
      test.setTimeout(120_000)
      await requireLocalReady(request, test, API_BASE)
      await setRail(page, '1')
      await page.goto(`/app?surface=${surface}`)
      await expectOneCanvasIn(page, '.studio-ground')
      // C-04B (38e7568e): the solar profile opens on its Solar tab, so the Draw tools mount only after the Draw tab is clicked.
      if (surface === 'solar') await page.getByRole('tab', { name: 'Draw', exact: true }).click()
      await expect(page.locator('[data-tool="draw:createLine"]')).toBeEnabled({ timeout: 30_000 })
      const viewer = page.locator('.studio-ground-viewer')
      const continuity = page.getByTestId('continuity-rail')
      const board = page.locator('[data-ground="browser"]')
      const prompt = page.getByLabel('Command bar', { exact: true })
      const selected = page.locator('[aria-label="Workspace profile"] [aria-selected="true"]')
      const profile = await selected.getAttribute('data-surface')
      const url = page.url()
      const documentName = await page.locator('.viewer-title').textContent()
      const documentId = await page.locator('.workspace-card').getAttribute('data-engine-document')
      expect(documentId).toBeTruthy()
      const viewerNode = await viewer.elementHandle()
      const railNode = await continuity.elementHandle()
      const boardNode = await board.elementHandle()
      const opener = page.locator('.doc-tab-start')
      await expectSharedChrome(page)
      await opener.click()
      await expectStudioBoardDetails(page, board)
      await page.getByRole('button', { name: 'Return to drawing', exact: true }).click()
      await prompt.fill('Keep this project draft')
      await opener.click()
      const heading = page.getByRole('heading', { level: 1, name: 'Project board', exact: true })
      await expect(heading).toBeFocused()
      await expect(page.locator('h1:visible')).toHaveCount(1)
      await expect(selected).toHaveAttribute('data-surface', profile)
      expect(page.url()).toBe(url)
      await expect(page.getByText('Run uses an existing tool. Build creates a new tool. Review the proposed action before it runs.', { exact: false })).toBeInViewport()
      const back = page.getByRole('button', { name: 'Return to drawing', exact: true })
    await expect(back).toBeInViewport()
    // The view cube and the WCS chip are fixed z 3 chrome; with the board open they must not sit on the header.
    await expect(page.locator('.cockpit-cube-wrap')).toBeHidden()
    await expect(page.locator('.cockpit-cube-wcs')).toBeHidden()
      await expect(viewer).toBeAttached()
      await expect(viewer).toBeHidden()
    await back.click()
    await expect(viewer).toBeVisible()
    await expect(page.locator('.cockpit-cube-wrap')).toBeVisible()
      await expect(opener).toBeFocused()
      await expect(prompt).toHaveValue('Keep this project draft')
      expect(await viewer.evaluate((node, previous) => node === previous, viewerNode)).toBe(true)
      expect(await continuity.evaluate((node, previous) => node === previous, railNode)).toBe(true)
      expect(await board.evaluate((node, previous) => node === previous, boardNode)).toBe(true)
      expect(await page.locator('.viewer-title').textContent()).toBe(documentName)
      await expect(page.locator('.workspace-card')).toHaveAttribute('data-engine-document', documentId)
      expect(page.url()).toBe(url)
      await opener.click()
      await expect(heading).toBeFocused()
      await page.keyboard.press('Escape')
      await expect(viewer).toBeVisible()
      await expect(board).toBeHidden()
      await expect(opener).toBeFocused()
      await opener.click()
      const line = page.locator('[data-tool="draw:createLine"]')
      await expect(line).toBeEnabled({ timeout: 30_000 })
      await line.evaluate((node) => node.addEventListener('click', () => {
        const board = document.querySelector('[data-ground="browser"]')
        node.dataset.returnedBeforeAction = String((board.hidden || board.hasAttribute('inert'))
          && !document.querySelector('[data-testid="cockpit-prompt"]'))
      }, { once: true }))
      await line.click()
      await expect(line).toHaveAttribute('data-returned-before-action', 'true')
      await expect(board).toBeHidden()
      await expect(viewer).toBeVisible()
      await expect(page.getByTestId('cockpit-prompt')).toHaveAttribute('data-op', 'createLine')
      await page.getByRole('button', { name: 'Cancel', exact: true }).click()
      await opener.click()
      await page.reload()
      await expect(viewer).toBeVisible()
      await expect(board).toBeHidden()
      expect(page.url()).toBe(url)
    })
  }

  test('/app boots studio mode console: one canvas in the ground, one controller, one command bar', async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    await page.goto('/app')
    await expect(page.locator(STUDIO)).toHaveCount(1)
    await expectOneCanvasIn(page, '.studio-ground')
    await expectSharedChrome(page)
    // No duplicate-instance regressions: one checkout stamp, one WORKSPACE
    // controller stamp (W4c-0 debt: a duplicated WorkspaceControllerProvider
    // would be a second converse session, invisible to the checkout stamp),
    // one Command bar, one main landmark (the console's own).
    expect(await page.locator('[data-checkout-instance]').count()).toBe(1)
    expect(await page.locator('[data-controller-instance]').count()).toBe(1)
    await expect(page.getByLabel('Command bar', { exact: true })).toHaveCount(1)
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
      // Name what was hit. Two reds on slow walks reported only <DIV>, which
      // is not evidence: a dissolving boot overlay and a real click shield
      // look the same as a tag name and nothing alike as a class.
      const describe = (node) => (node ? `${node.tagName}#${node.id || ''}.${String(node.className || '').trim().replace(/\s+/g, '.')}` : 'null')
      return { tag: el?.tagName, what: describe(el), parent: describe(el?.parentElement), inGround: !!el?.closest('.studio-ground') }
    })
    expect(hit.inGround, `elementFromPoint hit ${hit.what} (in ${hit.parent}) outside the ground`).toBe(true)
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

  test('W4g S08: an explicit demo boots without one authenticated request', async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    const apiRequests = []
    page.on('request', (request) => {
      const { pathname } = new URL(request.url())
      if (pathname.startsWith('/api/') && pathname !== '/api/telemetry') {
        apiRequests.push(`${request.method()} ${pathname}`)
      }
    })
    await page.goto('/app?demo=1')
    await expect(page.locator(STUDIO)).toHaveCount(1)
    await expect(page.locator('footer.foot-bar')).toContainText('sample data')
    expect(apiRequests, `unexpected API requests: ${apiRequests.join(', ')}`).toEqual([])
  })

  test('W4g S08: Details carries a copyable diagnostics block with the served build', async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    await page.goto('/app')
    await expect(page.locator(STUDIO)).toHaveCount(1)
    await expect(page.locator('footer.foot-bar')).toContainText(/backend · (local only|cloud live)/, { timeout: 30_000 })
    await page.locator('header.top').getByRole('button', { name: 'Details', exact: true }).click()
    const diagnostics = page.getByTestId('diagnostics-block')
    await expect(diagnostics).toHaveText(/^Leaf Automation diagnostics\n/)
    await expect(diagnostics).toContainText('mode live')
    await expect(diagnostics).toContainText(/served [0-9a-f]{40}/)
    await expect(diagnostics).toContainText('page /app')
    await expect(diagnostics).toContainText('unavailable controls (')
    await expect(page.getByTestId('copy-diagnostics')).toBeVisible()
    await expect(page.getByTestId('copy-diagnostics')).toHaveText('Copy diagnostics')
    await page.keyboard.press('Escape')
    await expect(diagnostics).toHaveCount(0)
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
    await expectSharedChrome(page)
    await expect(board.locator('[data-tile="drawing"]')).toContainText(/polylines/)
    await expect(board.locator('[data-tile="catalog"]')).toContainText(/famil/)
    await expectStudioBoardDetails(page, board)

    await page.getByRole('tab', { name: 'iOS' }).click()
    await expect(device).toBeVisible()
    await expect(board).toBeHidden()
    await expect(viewer).toBeHidden()
    await expect(device.locator('.device-frame')).toHaveCount(1)
    await expect(device.locator('[data-testid="device-state"]')).not.toBeEmpty()
    await expectSharedChrome(page)

    await page.getByRole('tab', { name: 'Solar CAD' }).click()
    await expect(viewer).toBeVisible()
    await expect(board).toBeHidden()
    await expect(device).toBeHidden()
    await expectOneCanvasIn(page, '.studio-ground')

    await expectSharedChrome(page)
    await expect(page.getByRole('tab', { name: 'Solar', exact: true })).toHaveAttribute('aria-selected', 'true')
    const solarRibbon = page.getByTestId('drafting-ribbon')
    await expect(solarRibbon.getByRole('group', { name: 'Stringing', exact: true })).toBeVisible()
    await expect(solarRibbon.getByRole('group', { name: 'Equipment placement', exact: true })).toBeVisible()

    // Deep link straight into a non-drawing surface boots that ground.
    await page.goto('/app?surface=browser')
    await expect(page.locator(STUDIO)).toHaveCount(1)
    await expect(page.locator('.studio-ground [data-ground="browser"]')).toBeVisible()
    await expect(page.locator('.studio-ground .studio-ground-viewer')).toBeHidden()
  })

  test('C-05 row8 demo Ship status rows stay disabled with Setup required', async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    await page.setViewportSize({ width: 1600, height: 1000 })
    await page.goto('/app?surface=ios&dev=1')
    await page.getByLabel('Use mock data (off = live backend)').check()
    await expect(page.getByRole('tab', { name: 'Ship', exact: true })).toHaveAttribute('aria-selected', 'true')
    const ribbon = page.getByTestId('drafting-ribbon')
    for (const [id, reason] of [
      ['ship:revision', 'No approved revision for this project yet'],
      ['ship:readiness', 'Apple readiness is not mounted'],
    ]) {
      const tool = ribbon.locator(`[data-tool="${id}"]`)
      await expect(tool).toBeDisabled()
      await expect(tool).toHaveAttribute('title', reason)
      await expect(tool).toHaveAccessibleName(new RegExp(reason))
    }
    await expect(page.getByRole('tab', { name: 'iOS', exact: true }).locator('small')).toHaveText('Setup required')
    // Contract-served rows are unit-proven only; the demo publishes no contract.
  })

  test('C-04 solar-starter opens locally in a live empty Solar workspace', async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    // On the local APS_LIVE=0 stack write_loop.ensure_demo_drawing bootstraps ANY
    // slug-safe first-seen id with the demo intake (a 200 that seats a drawing), so
    // the only honest 404 the session route gives is an id outside its slug rule:
    // uppercase is refused as `drawing unavailable`. That 404 is the CONFIRMED
    // absence App's drawingLoad needs before the starter may open (CORRECTION 2).
    // No route is mocked or seeded.
    const drawing = 'C04A-EMPTY-SOLAR-STARTER'
    const writes = []
    let sampleFetches = 0
    page.on('request', (req) => {
      const path = new URL(req.url()).pathname
      if (req.method() === 'POST' && path.startsWith('/api/drawings/')) writes.push(path)
      if (path === '/sample.dxf') sampleFetches += 1
    })
    const missing = page.waitForResponse((response) => {
      const url = new URL(response.url())
      return url.pathname === '/api/session' && url.searchParams.get('dwg') === drawing
    })
    await page.goto(`/app?surface=solar&drawing=${drawing}`)
    expect((await missing).status()).toBe(404)
    await expect(page.locator('.workspace-card')).toHaveAttribute('data-engine-document', 'solar-starter.dxf', { timeout: 60_000 })
    await expectOneCanvasIn(page, '.studio-ground')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText('2345')
    await page.getByRole('tab', { name: 'Draw', exact: true }).click()
    await expect(page.locator('[data-tool="draw:createLine"]')).toBeEnabled()
    await page.getByRole('tab', { name: 'Insert', exact: true }).click()
    const save = page.locator('[data-tool="save-version"]')
    await expect(save).toBeDisabled()
    await expect(save).toHaveAttribute('title', /edit something first|download-only here: no project target/)
    // Read the rendered canvas into 2D on a frame: nontransparent colored
    // pixels prove the starter geometry reached WebGL, beyond its store count.
    await expect.poll(() => page.locator('.studio-ground .viewer-canvas canvas').evaluate((canvas) => new Promise((resolve) => {
      requestAnimationFrame(() => {
        const copy = document.createElement('canvas')
        copy.width = canvas.width; copy.height = canvas.height
        const ctx = copy.getContext('2d')
        ctx.drawImage(canvas, 0, 0)
        const pixels = ctx.getImageData(0, 0, copy.width, copy.height).data
        let painted = 0
        for (let i = 0; i < pixels.length; i += 4) {
          if (pixels[i + 3] && Math.max(pixels[i], pixels[i + 1], pixels[i + 2]) > 40) painted += 1
        }
        resolve(painted)
      })
    })), { timeout: 30_000 }).toBeGreaterThan(0)
    expect(sampleFetches).toBe(1)
    expect(writes).toEqual([])
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
    // Attached AND hidden (slice 4b): the rail is owned by SiteRoot's
    // ContinuityStore and adopted into this nav, so it must EXIST here (CSS
    // hides it on the drafting surfaces; nothing unmounts it). A bare
    // toBeHidden() passes on an absent element, which is the regression the
    // hoist could produce.
    const continuity = page.locator('.tc-continuity')
    await expect(continuity).toBeAttached()
    await expect(continuity).toBeHidden()
    // Shared chrome docks to the viewport and keeps the drawing behind it.
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
    if (chrome.navHidden) expect(chrome.navWidth).toBeLessThanOrEqual(1)
    else {
      expect(chrome.navRadius).toBe('0px')
      expect(chrome.navLeft).toBe(0)
    }
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
    // Browser keeps the shared chrome and owns its board heading.
    await page.getByRole('tab', { name: 'Browser' }).click()
    await expect(page.getByRole('tab', { name: 'Browser' })).toBeFocused()
    await expect(page.locator('.app[data-surface="browser"]')).toHaveCount(1)
    await expect(page.locator('h1:visible')).toHaveCount(1)
    await expect(page.getByRole('heading', { level: 1, name: 'Project board', exact: true })).toBeVisible()
    await expect(page.getByRole('heading', { level: 1, name: 'Project board', exact: true })).not.toBeFocused()
    const profileTabs = page.getByRole('tablist', { name: 'Workspace profile' }).getByRole('tab')
    const profileNames = await profileTabs.evaluateAll((tabs) => tabs.map((tab) => tab.getAttribute('aria-label')))
    const nextProfileName = profileNames[(profileNames.indexOf('Browser') + 1) % profileNames.length]
    const nextProfileTab = page.getByRole('tablist', { name: 'Workspace profile' }).getByRole('tab', { name: nextProfileName, exact: true })
    await page.getByRole('tab', { name: 'Browser' }).press('ArrowRight')
    await expect(nextProfileTab).toBeFocused()
    await expect(nextProfileTab).toHaveAttribute('aria-selected', 'true')
    await nextProfileTab.press('ArrowLeft')
    await expect(page.getByRole('tab', { name: 'Browser' })).toBeFocused()
    await expect(page.getByRole('tab', { name: 'Browser' })).toHaveAttribute('aria-selected', 'true')
    await expect(page.locator('.app[data-surface="browser"]')).toHaveCount(1)
    await expect(page.getByRole('heading', { level: 1, name: 'Project board', exact: true })).toBeVisible()
    await expect(page.getByRole('heading', { level: 1, name: 'Project board', exact: true })).not.toBeFocused()
    await expect(page.getByTestId('cockpit-view')).toHaveCount(0)
    await expect(page.getByTestId('cockpit-status')).toHaveCount(0)
  })

  test('bare /try stays the operator stage; /sheets and unknown paths never mount the studio', async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    await page.goto('/try')
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

  test('/try?demo=1 mounts the shared console and its drawing', async ({ page, request }) => {
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    await page.goto('/try?demo=1')
    await expect(page.locator(STUDIO)).toHaveCount(1)
    await expect(page.locator('main.stage-root[data-scene="tool"]')).toHaveCount(0)
    await expectOneCanvasIn(page, '.studio-ground')
    await expectSharedChrome(page)
    await expect(page.locator('[data-controller-instance]')).toHaveCount(1)
    await expect(page.getByTestId('first-run-coach')).toHaveCount(0)
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

    // Browser selects project tools within the same shell and ribbon.
    await page.getByRole('tab', { name: 'Browser' }).click()
    await expectSharedChrome(page)
    await expect(page.getByTestId('drafting-ribbon')).toBeVisible()
    await expect(page.getByRole('tab', { name: 'Project', exact: true })).toHaveAttribute('aria-selected', 'true')
    // productSurfaces.js gives Browser rails.left = 'spine' in the shared cockpit.
    await expect(page.locator('aside.nav[data-spine]')).toHaveCount(1)
    // Expand the shared spine before checking the catalog content it owns.
    await page.getByTestId('cockpit-band').locator('[data-tool="rail-expand"]').click()
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

    // W4g-1c (engine reach on the public demo): the mock console opens its
    // own drawing in the browser engine too, from the static /sample.dxf
    // (the synthesis of the very intake it draws) as version 1, so Draw goes
    // live with no import; Save stays honestly off (mock has no target).
    // Only where the engine is served and the build carries the engine
    // groups (the cad_edit row's own guard).
    await page.getByRole('tab', { name: 'Draw' }).click()
    const engineServedMock = await request.get('/engine/engine.js').catch(() => null)
    const cadEditOnMock = (await ribbon.locator('[data-group="modify"]').count()) > 0
    if (engineServedMock && engineServedMock.status() === 200 && cadEditOnMock) {
      await expect(page.locator('.workspace-card[data-engine-document$="-v1.dxf"]')).toHaveCount(1, { timeout: 60_000 })
      await expect(ribbon.locator('[data-tool="draw:createLine"]')).toBeEnabled()
      await expect(ribbon.locator('[data-group="draw"] .ribbon-note')).toHaveCount(0)
      await page.getByRole('tab', { name: 'Insert' }).click()
      await expect(ribbon.locator('[data-tool="save-version"]')).toBeDisabled()
      test.info().annotations.push({ type: 'engine-reach', description: `public demo: ${await page.locator('.workspace-card').getAttribute('data-engine-document')} opened from /sample.dxf` })
    } else {
      test.info().annotations.push({ type: 'engine-reach', description: 'public demo reach not proven here: engine not served or engine groups absent' })
    }
    await page.getByRole('tab', { name: 'Manage' }).click()

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
    await page.addInitScript(() => {
      const NativeWorker = window.Worker
      window.Worker = class extends NativeWorker {
        constructor(...args) {
          super(...args)
          this.addEventListener('message', ({ data }) => {
            if (data?.type === 'editApplied' && data.ok && Array.isArray(data.entities)) {
              window.__commandPointResult = { id: String(data.createdId), entities: data.entities }
            }
          })
        }
      }
    })
    // W4g-4: the verb rows (COPY, ROTATE, EXPLODE x2 on the real engine) grew this
    // row past three minutes on a loaded host; five is its budget now.
    test.setTimeout(300_000)
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
    // W4g-5c: Clipboard is REAL with the engine on, but it keeps the
    // reference's LAST seat either way: App renders the panel there and
    // the engine portals its tools into it, rather than adding a third
    // engine cluster on the left (which moved the prompt seat 148px).
    const referenceTail = ['annotation', 'layers', 'block', 'properties', 'groups', 'clipboard']
    // W4g-7b-02c: the engine's own Block panel (INSERT BLOCK live) renders
    // the annotation idiom (a child cluster, no portal), so with the flag on
    // it seats right after Annotation rather than after Layers — the same
    // deliberate deviation W4g-5c named for Clipboard, here because "no
    // portal" was the record's own instruction.
    const cadEditTail = ['annotation', 'block', 'layers', 'properties', 'groups', 'clipboard']
    expect(groups).toEqual(cadEditOn ? ['draw', 'modify', ...cadEditTail] : referenceTail)
    await page.getByRole('tab', { name: 'View' }).click()
    // W4g-7a: the View tab carries the Script seat with the engine on (the
    // reference's SCRIPT, run from the browser).
    expect(await groupsOf()).toEqual(cadEditOn ? ['view', 'version', 'layers', 'script'] : ['view', 'version', 'layers'])
    await page.getByRole('tab', { name: 'Manage' }).click()
    groups = await groupsOf()
    expect(groups[0]).toBe('rail')
    expect(groups[groups.length - 1]).toBe('author')
    await page.getByRole('tab', { name: 'Draw' }).click()

    // SEATING (W4e), the reference's bands to the pixel at the 1600x1000
    // viewport: a 28px top band, the 95px ribbon, the 32px document tabs,
    // the full-bleed canvas, the 250px properties pane, the viewport
    // strip at the canvas's top-left and the view cube at its top-right,
    // the 25px command line 35px off the bottom, the 31px status bar, and
    // the ribbon's translucent backing.
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
        viewportW: innerWidth, viewportH: innerHeight,
        ground: r('.studio-ground'), shellW: Math.round(shell.width), shellH: Math.round(shell.height),
        glass: getComputedStyle(document.querySelector('.drafting-ribbon')).backgroundColor,
      }
    })
    expect(seating.header.h).toBe(28)
    expect([seating.band.y, seating.band.h]).toEqual([28, 95])
    expect([seating.tabs.y, seating.tabs.h]).toEqual([123, 32])
    expect([seating.ground.x, seating.ground.y, seating.ground.w, seating.ground.h]).toEqual([0, 0, seating.viewportW, seating.viewportH])
    expect([seating.pane.x, seating.pane.y, seating.pane.w]).toEqual([0, 155, 250])
    expect([seating.strip.x, seating.strip.y, seating.strip.h]).toEqual([250, 155, 26])
    expect(seating.cube.x).toBeGreaterThan(seating.shellW / 2)
    expect([seating.status.h, seating.status.bottom]).toEqual([31, seating.shellH])
    expect([seating.well.h, seating.shellH - seating.well.bottom]).toEqual([25, 35])
    expect(seating.glass).toBe('rgba(30, 34, 39, 0.86)')
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
    await expect(page.getByLabel('Command bar', { exact: true })).toHaveCount(1)
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
        // The one-shell band publishes its height on the shared chrome host.
        published: getComputedStyle(el.closest('.studio-chrome-host')).getPropertyValue('--cockpit-ribbon-h').trim(),
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

    // W4g-1b (engine reach): on a FRESH VISIT the console's own drawing
    // opens in the browser engine (GET .../dxf -> the store's open path), so
    // the canvas shows the engine document of the head and the Draw tools
    // are live with NO import. The ribbon says what it is doing on the way
    // ("opening ... in the browser engine...") and, were the open to fail,
    // why, never a silently greyed group.
    const modify = ribbon.locator('[data-group="modify"]')
    const draw = ribbon.locator('[data-group="draw"]')
    const engineServedEarly = await request.get('/engine/engine.js').catch(() => null)
    if (engineServedEarly && engineServedEarly.status() === 200) {
      // The stamp is `<drawing id>-v<head>.dxf` (the stack's console drawing
      // is `demo`; staging's is `rooftop_demo`), and no import has happened
      // yet, so any head stamp is the opener's.
      await expect(page.locator('.workspace-card[data-engine-document*="-v"]')).toHaveCount(1, { timeout: 60_000 })
      const headDoc = await page.locator('.workspace-card').getAttribute('data-engine-document')
      test.info().annotations.push({ type: 'engine-reach', description: `head opened as ${headDoc}` })
      await expect(draw.locator('.ribbon-note')).toHaveCount(0)
      await expect(draw.locator('[data-tool="draw:createLine"]')).toBeEnabled()
      // Loaded, nothing selected: Modify names the next missing thing.
      await expect(modify.locator('.ribbon-note')).toHaveText('select an entity in the drawing')
      // The fresh-visit edit: a line drawn on the console's own drawing, the
      // engine's entity count up by one, and the edit saved as a NEW VERSION
      // of that drawing (the same write-back every save uses).
      const countBefore = Number(await page.getByTestId('cad-edit-entity-count').textContent())
      expect(countBefore).toBeGreaterThan(0)
      await draw.locator('[data-tool="draw:createLine"]').click()
      await page.getByLabel('ribbon x', { exact: true }).fill('0')
      await page.getByLabel('ribbon y', { exact: true }).fill('0')
      await page.getByLabel('ribbon x2').fill('10')
      await page.getByLabel('ribbon y2').fill('10')
      await page.getByLabel('ribbon y2').press('Enter')
      await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBefore + 1), { timeout: 60_000 })
      await page.keyboard.press('Escape')
      // W4g-2 (one head): with the edit unsaved, every catalog WRITE tool on
      // the ribbon is disabled with the reason (a run would move the server
      // head under the browser copy); read tools stay live. The stack's
      // catalog carries at least one write tool on the Manage families.
      await page.getByRole('tab', { name: 'Manage' }).click()
      const dirtyBlocked = ribbon.locator('.ribbon-tool[aria-label*="the browser engine holds unsaved edits"]')
      await expect(dirtyBlocked.first()).toBeAttached({ timeout: 10_000 })
      test.info().annotations.push({ type: 'one-head', description: `${await dirtyBlocked.count()} write tools held while dirty` })
      // The edit is dirty and the project target exists, so Save is live
      // (the write-back itself is proven by tests/test_save_edited_version.py
      // and the acceptance prover; saving HERE would add a version to the
      // stack's shared demo drawing and move the head under the specs that
      // count versions from a known start). The engine's own Undo returns
      // the copy to clean instead, and the write tools come back with it.
      const saveBtn = ribbon.locator('[data-tool="save-version"]')
      await page.getByRole('tab', { name: 'Insert' }).click()
      await expect(saveBtn).toBeEnabled()
      await ribbon.locator('[data-tool="undo-edit"]').click()
      await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBefore), { timeout: 60_000 })
      await expect(saveBtn).toBeDisabled()
      await page.getByRole('tab', { name: 'Manage' }).click()
      await expect(dirtyBlocked).toHaveCount(0)
      await page.getByRole('tab', { name: 'Draw' }).click()
    } else {
      await expect(modify.locator('.ribbon-note')).toHaveText(/no drawing in the browser engine yet|could not be opened in the browser engine/)
      await expect(draw.locator('.ribbon-note')).toHaveText(/no drawing in the browser engine yet|could not be opened in the browser engine/)
      for (const btn of await draw.locator('.ribbon-tool').all()) await expect(btn).toBeDisabled()
    }
    // W4e: the reference's Draw column (rectangle, ellipse, point) and its
    // other six Modify tools are present and honestly off ("not in the
    // browser engine yet"); the engine's own four and six carry the
    // document reason. 4 + 3 and 6 + 6: the reference's grid.
    // W4g-4: RECTANG joined the Draw group (5 real + 2 off) and COPY, MIRROR,
    // ROTATE, SCALE, EXPLODE joined Modify (11 real + trim/extend off).
    // W4g-5a: OFFSET made it 12 real, so the Modify row is 14.
    // W4g-5b: ARRAY's two forms make it 14 real, so the row is 16.
    // W4g-6: TRIM, EXTEND, FILLET and CHAMFER are real and the two placeholders
    // leave, so the row is 18 and every one of the 18 is real.
    // W4g-4b: ELLIPSE and POINT are real, so all 7 Draw tools are creates and
    // the Properties seat (after Layers and Block, the reference's place)
    // carries the real Match tool in its slot beside its honest ByLayer fields.
    await expect(draw.locator('.ribbon-tool')).toHaveCount(7)
    await expect(draw.locator('[data-tool^="draw:create"]')).toHaveCount(7)
    await expect(page.locator('#cockpit-properties-slot [data-tool="modify:matchprop"]')).toHaveCount(1)
    const modifyTools = modify.locator('.ribbon-tool')
    await expect(modifyTools).toHaveCount(18)
    let modifyReal = 0
    for (const btn of await modifyTools.all()) {
      await expect(btn).toBeDisabled()
      const name = await btn.getAttribute('aria-label')
      if (/\(unavailable: (select an entity in the drawing|no drawing in the browser engine yet|opening .*|the drawing could not be opened.*)\)/.test(name)) modifyReal += 1
      else expect(name).toContain('(unavailable: not in the browser engine yet)')
    }
    expect(modifyReal).toBe(18)

    // View drives the viewer: fit is live with a drawing loaded (View tab).
    await page.getByRole('tab', { name: 'View' }).click()
    await expect(ribbon.locator('[data-tool="fit"]')).toBeEnabled()
    // W4g-7a SCRIPT on the REAL engine: two command lines run one after the
    // other through the same words and prompts the command line uses (+2),
    // and a script whose line the store refuses stops with the line number
    // and the sentence, drawing nothing.
    if (cadEditOn && (await page.getByTestId('cad-edit-entity-count').count())) {
      const countBeforeScript = Number(await page.getByTestId('cad-edit-entity-count').textContent())
      const script = page.getByLabel('ribbon script', { exact: true })
      await script.fill('; W4g-7a\nline 600,600 620,600\ncircle 610,610 5')
      await page.getByTestId('cockpit-script-run').click()
      await expect(page.getByTestId('cockpit-script-status')).toHaveText('Script ran 2 commands.', { timeout: 120_000 })
      await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBeforeScript + 2))
      test.info().annotations.push({ type: 'script', description: `script ran two lines on the engine (${countBeforeScript} -> ${countBeforeScript + 2})` })
      await script.fill('circle 0,0 abc')
      await page.getByTestId('cockpit-script-run').click()
      await expect(page.getByTestId('cockpit-script-status')).toHaveText(/^Script stopped at line 1: Circle refused/, { timeout: 20_000 })
      await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBeforeScript + 2))
    }
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
    // PARITY-4-AUTHORING: publish from Manage, seat on the declared Draw
    // tab, then run on this drawing without replacing the page's session.
    await page.getByRole('tab', { name: 'Manage' }).click()
    const authorBtn = ribbon.locator('[data-tool="author-tool"]')
    if (await authorBtn.isEnabled()) {
      test.setTimeout(600_000)
      const AUTHORED_TOOL_NAME = `parity4_line_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`
      const authorUrl = page.url()
      await page.evaluate((name) => { window.__parity4AuthorSession = name }, AUTHORED_TOOL_NAME)
      // The SCRIPT check above leaves two unsaved primitives. Undo those
      // through the engine so the catalog write gate permits this run.
      if (cadEditOn && (await page.getByTestId('cad-edit-entity-count').count())) {
        const scriptCount = Number(await page.getByTestId('cad-edit-entity-count').textContent())
        await page.getByRole('tab', { name: 'Insert' }).click()
        await ribbon.locator('[data-tool="undo-edit"]').click()
        await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(scriptCount - 1))
        await ribbon.locator('[data-tool="undo-edit"]').click()
        await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(scriptCount - 2))
        await expect(ribbon.locator('[data-tool="save-version"]')).toBeDisabled()
        await page.getByRole('tab', { name: 'Manage' }).click()
      }
      await authorBtn.click()
      await expect(page.locator('aside.nav[data-spine]')).toHaveCount(0)
      await expect(page.locator('.author-section .section-head[aria-expanded="true"]')).toHaveCount(1)
      const author = page.locator('.author-section')
      await author.getByLabel('What should the tool do?').fill(
        `Create a CAD tool named exactly ${AUTHORED_TOOL_NAME}. Declare placement {"tab":"draw","size":"large"} in its tool record. ` +
        'Declare drawing.write capability. On the current open drawing, add exactly one LINE from (600,600) to (620,600), ' +
        'with no required parameters, and save the changed drawing as a new version. Do not create or switch drawings.',
      )
      await author.getByRole('button', { name: 'Generate tool', exact: true }).click()
      await expect(author.locator('.authored-head .tool-name')).toHaveText(AUTHORED_TOOL_NAME, { timeout: 300_000 })
      await author.getByRole('button', { name: 'Request publication', exact: true }).click()
      await expect(author.getByRole('button', { name: 'Run it now', exact: true })).toBeVisible({ timeout: 120_000 })
      await page.getByRole('button', { name: 'Collapse the tool rail to a spine' }).click()
      await page.getByRole('tab', { name: 'Draw', exact: true }).click()
      const authoredTool = ribbon.getByRole('button', { name: AUTHORED_TOOL_NAME, exact: true })
      await expect(authoredTool).toBeVisible({ timeout: 30_000 })
      await expect(authoredTool).toBeEnabled()
      await page.getByRole('button', { name: 'History', exact: true }).click()
      const history = page.getByRole('dialog', { name: 'Version history' })
      await expect(history.locator('.vh-mark', { hasText: 'head' })).toHaveCount(1)
      const oldHead = await history.locator('li').filter({ has: page.locator('.vh-mark') }).getAttribute('data-testid')
      await page.keyboard.press('Escape')
      await expect(history).toHaveCount(0)
      await authoredTool.click()
      await page.getByRole('button', { name: `Run ${AUTHORED_TOOL_NAME}`, exact: true }).click()
      await expect(page.locator('.result-tool')).toContainText(AUTHORED_TOOL_NAME, { timeout: 120_000 })
      await expect(page.locator('.result-block .ok')).toHaveText('Passed', { timeout: 120_000 })
      // The oracle is the version rail: a NEW head attributed to this unique
      // tool. A toast, a successful HTTP response, or a catalog card is not it.
      await page.getByRole('button', { name: 'History', exact: true }).click()
      const authoredHead = history.locator('li').filter({ has: page.locator('.vh-tool', { hasText: AUTHORED_TOOL_NAME }) })
      await expect(authoredHead).toHaveCount(1, { timeout: 120_000 })
      await expect(authoredHead.locator('.vh-tool')).toHaveText(AUTHORED_TOOL_NAME)
      await expect(authoredHead.locator('.vh-mark')).toHaveText('head')
      expect(await authoredHead.getAttribute('data-testid')).not.toBe(oldHead)
      await page.keyboard.press('Escape')
      expect(page.url()).toBe(authorUrl)
      expect(await page.evaluate(() => window.__parity4AuthorSession)).toBe(AUTHORED_TOOL_NAME)
    } else {
      // PARITY-4-UNAVAILABLE: assert the rendered reason, then explicitly
      // limit this branch to source wiring. It is NOT an end-to-end run.
      const reason = await authorBtn.getAttribute('title')
      expect(['your plan does not include authoring tools', 'the authoring stage is off on this deployment']).toContain(reason)
      await expect(authorBtn).toBeDisabled()
      await expect(authorBtn).toHaveAttribute('aria-label', `author-tool (unavailable: ${reason})`)
      // Keep the fallback in this owned spec; app-wiring.test.mjs is outside
      // this executor's file ownership. These checks inspect real producers,
      // not a mocked publish response or an injected catalog tool.
      const appSource = readFileSync(new URL('../../src/App.jsx', import.meta.url), 'utf8')
      const publishPath = appSource.slice(appSource.indexOf('const onPublishAuthor ='), appSource.indexOf('// "Run it now" from the author card'))
      expect(publishPath).toMatch(/if \(res\.published\)\s*\{\s*upsertTool\(tool\)/)
      expect(publishPath.indexOf('loadCatalog()')).toBeGreaterThan(publishPath.indexOf('upsertTool(tool)'))
      const clusterSource = readFileSync(new URL('../../src/lib/ribbonClusters.js', import.meta.url), 'utf8')
      const placementPath = clusterSource.slice(clusterSource.indexOf('export function catalogTabClusters('), clusterSource.indexOf('/** One family cluster'))
      expect(placementPath).toContain('const tab = toolPlacementTab(tool)')
      expect(placementPath).toContain('else buckets.set(tab, [tool])')
      expect(placementPath).toContain('familyCluster(fam, tools, gate, onOpenFamily)')
      expect(placementPath).toContain('else byTab[tab] = [cluster]')
      const recordSource = readFileSync(new URL('../../src/lib/toolRecord.js', import.meta.url), 'utf8')
      expect(recordSource).toContain('tool && tool.placement && tool.placement.tab')
      expect(recordSource).toContain("['draw', 'insert', 'annotate', 'view', 'manage']")
      test.info().annotations.push({ type: 'PARITY-4-UNAVAILABLE', description: `Wiring-level proof only, NOT end to end: ${reason}` })
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
    await expect(modify.locator('.ribbon-note')).toHaveText('select an entity in the drawing')
    await page.getByRole('radio').first().check()
    await expect(modify.locator('.ribbon-note')).toHaveCount(0)
    const del = ribbon.locator('[data-tool="modify:delete"]')
    await expect(del).toBeEnabled()
    await del.click()
    // The engine re-parsed its own written bytes and the pane shows the result.
    await expect(page.getByRole('status').filter({ hasText: /delete applied/ })).toHaveCount(1, { timeout: 60_000 })
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText('1')
    // The deleted entity's selection cleared with it (selection identity).
    await expect(modify.locator('.ribbon-note')).toHaveText('select an entity in the drawing')

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
    // W4f-6: the sentence shows on the prompt as the operand is typed and
    // Run waits, so Enter posts nothing (before, it read from the status
    // after a refused run).
    await page.getByLabel('ribbon r').fill('0')
    await expect(page.getByTestId('cockpit-prompt-note')).toHaveText('Circle refused: radius must be greater than 0.')
    await expect(page.getByTestId('cockpit-prompt-run')).toBeDisabled()
    await page.getByLabel('ribbon r').press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText('3')
    // Esc cancels the command; the prompt leaves with it.
    await page.getByLabel('ribbon r').press('Escape')
    await expect(page.getByTestId('cockpit-prompt')).toHaveCount(0)

    // W4f slice B: a typed command word on the Command bar arms the same
    // prompt and clears the bar; the natural-language router never sees it.
    const bar = page.getByLabel('Command bar', { exact: true })
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
    // W4f-7: object snap is ON from the first session, visibly (the prompt's
    // chip is pressed). The picks below are raw-ground picks measured against
    // the viewer's own unproject, so F3 turns it off for them; the W4f-5 rows
    // turn it back on and prove the snap.
    await expect(page.getByTestId('cockpit-osnap')).toHaveAttribute('aria-pressed', 'true')
    await page.keyboard.press('F3')
    await expect(page.getByTestId('cockpit-osnap')).toHaveAttribute('aria-pressed', 'false')
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
    const groundPick = (fx, fy) => page.evaluate(async ([px, py]) => {
      const ground = document.querySelector('.studio-ground')
      const canvas = ground.querySelector('.viewer-canvas')
      let previous
      let settled = false
      for (let attempt = 0; attempt < 60; attempt++) {
        await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))
        const reading = JSON.stringify([canvas.getAttribute('data-safe-rect'), canvas.__cadviewer.project(0, 0)])
        if (reading === previous) { settled = true; break }
        previous = reading
      }
      if (!settled) throw new Error('geometry did not settle')
      const safe = canvas?.getAttribute('data-safe-rect')?.split(',').map(Number)
      const [left, top, width, height] = safe || []
      const origin = canvas?.getBoundingClientRect()
      const box = safe?.length === 4 && safe.every(Number.isFinite) && origin
        ? { left: origin.left + left, top: origin.top + top, width, height }
        : ground.getBoundingClientRect()
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
    // (W4f-6: the next point is not given yet, so the fields are empty and
    // Run waits quietly with the step's ask, no sentence)
    await expect(page.getByLabel('ribbon x2', { exact: true })).toHaveValue('')
    await expect(page.getByTestId('cockpit-prompt-note')).toHaveCount(0)
    await expect(page.getByTestId('cockpit-prompt-run')).toBeDisabled()
    // W4f-4: F8 turns ORTHO on (the prompt's chip is pressed); the next
    // pick, measured from the chain point, snaps to the axis of the larger
    // move: the pixel below is far to the right of b and a little down, so
    // x2 takes the pick's x and y2 holds b's y. F8 again turns it off.
    await page.keyboard.press('F8')
    await expect(page.getByTestId('cockpit-ortho')).toHaveAttribute('aria-pressed', 'true')
    await expect(page.locator('[data-toggle="ortho"]')).toBeEnabled()
    await expect(page.locator('[data-toggle="ortho"]')).toHaveAttribute('aria-pressed', 'true')
    const o = await groundPick(0.95, 0.42)
    expect(o.onGround, `ortho pick pixel (${o.x},${o.y}) hit ${o.name}, not the drawing`).toBe(true)
    expect(Math.abs(o.wx - b.wx)).toBeGreaterThan(Math.abs(o.wy - b.wy))
    await page.mouse.click(o.x, o.y)
    await expect(page.getByLabel('ribbon x2', { exact: true })).toHaveValue(r3(o.wx))
    await expect(page.getByLabel('ribbon y2', { exact: true })).toHaveValue(r3(b.wy))
    await page.keyboard.press('F8')
    await expect(page.getByTestId('cockpit-ortho')).toHaveAttribute('aria-pressed', 'false')
    await expect(page.locator('[data-toggle="ortho"]')).toBeEnabled()
    await expect(page.locator('[data-toggle="ortho"]')).toHaveAttribute('aria-pressed', 'false')
    // W4f-5: Enter draws that segment (the chain moves on), then F3 turns
    // OSNAP on and a click a few pixels off the imported polyline's corner
    // (50, 5) lands exactly on it. F3 again turns it off.
    // The picker hands the caret to Run in a frame after the pick, so the Enter
    // waits for Run to hold focus, as the first segment does; an Enter pressed
    // before that handoff reaches the body and draws nothing.
    await expect(page.getByTestId('cockpit-prompt-run')).toBeFocused()
    await page.keyboard.press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText('5', { timeout: 60_000 })
    await page.keyboard.press('F3')
    await expect(page.getByTestId('cockpit-osnap')).toHaveAttribute('aria-pressed', 'true')
    await expect(page.locator('[data-toggle="osnap"]')).toBeEnabled()
    await expect(page.locator('[data-toggle="osnap"]')).toHaveAttribute('aria-pressed', 'true')
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
    await expect(page.locator('[data-toggle="osnap"]')).toBeEnabled()
    await expect(page.locator('[data-toggle="osnap"]')).toHaveAttribute('aria-pressed', 'false')
    await page.locator('[data-toggle="ortho"]').click()
    await expect(page.getByTestId('cockpit-ortho')).toHaveAttribute('aria-pressed', 'true')
    await page.locator('[data-toggle="ortho"]').click()
    await expect(page.getByTestId('cockpit-ortho')).toHaveAttribute('aria-pressed', 'false')
    await expect(page.locator('[data-toggle="snap"]')).toBeDisabled()
    await expect(page.locator('[data-toggle="snap"]')).toHaveAttribute('title', /moves the cursor in fixed steps\. Not in the browser viewer yet\./)
    // W4f-6: the prompt validates as you type with the store's own sentence,
    // and since S12 that sentence names only the field at fault (support F2):
    // a word in x2 outlines the field, names the refusal and holds Run; the
    // number back releases it.
    await page.getByLabel('ribbon x2', { exact: true }).fill('abc')
    await expect(page.getByLabel('ribbon x2', { exact: true })).toHaveAttribute('aria-invalid', 'true')
    await expect(page.getByTestId('cockpit-prompt-note')).toHaveText('Line refused: next point x must be a number.')
    await expect(page.getByTestId('cockpit-prompt-run')).toBeDisabled()
    await page.getByLabel('ribbon x2', { exact: true }).fill('50')
    await expect(page.getByTestId('cockpit-prompt-note')).toHaveCount(0)
    await expect(page.getByTestId('cockpit-prompt-run')).toBeEnabled()
    // W4f-8: the point grammar. "@10,0" in the next-point field is relative
    // to the first point (the chain point from the last draw): Run is live,
    // Enter draws, and the chain moves on by exactly 10 in x. A malformed
    // pair is named and its own field outlined. "20<90" is an absolute polar
    // point: the segment ends at (0, 20).
    const chainX = Number(await page.getByLabel('ribbon x', { exact: true }).inputValue())
    const chainY = await page.getByLabel('ribbon y', { exact: true }).inputValue()
    await page.getByLabel('ribbon x2', { exact: true }).fill('@10,0')
    await expect(page.getByTestId('cockpit-prompt-note')).toHaveCount(0)
    await expect(page.getByTestId('cockpit-prompt-run')).toBeEnabled()
    await page.getByLabel('ribbon x2', { exact: true }).press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText('6', { timeout: 60_000 })
    await expect(page.getByLabel('ribbon x', { exact: true })).toHaveValue(r3(chainX + 10))
    await expect(page.getByLabel('ribbon y', { exact: true })).toHaveValue(chainY)
    await page.getByLabel('ribbon x2', { exact: true }).fill('1,2,3')
    await expect(page.getByTestId('cockpit-prompt-note')).toHaveText('LINE refused: "1,2,3" is not a point: use x,y, @dx,dy, dist<angle or @dist<angle.')
    await expect(page.getByLabel('ribbon x2', { exact: true })).toHaveAttribute('aria-invalid', 'true')
    await expect(page.getByTestId('cockpit-prompt-run')).toBeDisabled()
    await page.getByLabel('ribbon x2', { exact: true }).fill('20<90')
    await expect(page.getByTestId('cockpit-prompt-note')).toHaveCount(0)
    await page.getByLabel('ribbon x2', { exact: true }).press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText('7', { timeout: 60_000 })
    await expect(page.getByLabel('ribbon x', { exact: true })).toHaveValue('0')
    await expect(page.getByLabel('ribbon y', { exact: true })).toHaveValue('20')
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

    // W4g-4: the reference's Modify verbs on the REAL engine. COPY of the
    // last drawn line lands a new entity (+1) and selects it; ROTATE about a
    // base point keeps the count; EXPLODE refuses a LINE with the engine's
    // own code, and turns the imported 3-vertex open polyline into its two
    // segments (-1 +2).
    await page.getByRole('tab', { name: 'Draw' }).click()
    await page.getByRole('radio').last().check()
    const countBeforeVerbs = Number(await page.getByTestId('cad-edit-entity-count').textContent())
    await ribbon.locator('[data-tool="modify:copy"]').click()
    await page.getByLabel('ribbon dx', { exact: true }).fill('5')
    await page.getByLabel('ribbon dy', { exact: true }).fill('5')
    await page.getByLabel('ribbon dy', { exact: true }).press('Enter')
    await expect(page.getByRole('status').filter({ hasText: /copy applied: entity \d+ drawn/ })).toHaveCount(1, { timeout: 60_000 })
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBeforeVerbs + 1))
    await ribbon.locator('[data-tool="modify:rotate"]').click()
    await page.getByLabel('ribbon cx', { exact: true }).fill('0')
    await page.getByLabel('ribbon cy', { exact: true }).fill('0')
    await page.getByLabel('ribbon angle', { exact: true }).fill('90')
    await page.getByLabel('ribbon angle', { exact: true }).press('Enter')
    await expect(page.getByRole('status').filter({ hasText: /rotate applied/ })).toHaveCount(1, { timeout: 60_000 })
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBeforeVerbs + 1))
    await ribbon.locator('[data-tool="modify:explode"]').click()
    await expect(page.getByRole('status').filter({ hasText: /Edit refused \(explode\): entity_not_explodable/ })).toHaveCount(1, { timeout: 60_000 })
    // Disarm before touching the import list. A refusal does NOT disarm, and
    // while a command is armed the workbench is deliberately click-through
    // (W4f-2, so a drafter can pick under it), which means a click aimed at
    // this radio lands on the canvas instead: Playwright reports the drawing
    // canvas intercepting the pointer event and retries until the row's whole
    // budget is gone. The product is behaving; the step has to leave picking
    // first.
    await page.keyboard.press('Escape')
    await page.getByRole('radio').first().check()
    await ribbon.locator('[data-tool="modify:explode"]').click()
    await expect(page.getByRole('status').filter({ hasText: /explode applied: entity \d+ drawn/ })).toHaveCount(1, { timeout: 60_000 })
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBeforeVerbs + 2))
    await page.keyboard.press('Escape')

    // W4g-5 OFFSET: a parallel copy of the selection, the distance given and
    // the side named by the point, drawn by the engine's own create (+1); a
    // point ON the entity names no side and is refused with the sentence,
    // drawing nothing.
    const countBeforeOffset = Number(await page.getByTestId('cad-edit-entity-count').textContent())
    await page.getByRole('radio').last().check()
    await ribbon.locator('[data-tool="modify:offset"]').click()
    await page.getByLabel('ribbon distance', { exact: true }).fill('3')
    await page.getByLabel('ribbon x', { exact: true }).fill('40')
    await page.getByLabel('ribbon y', { exact: true }).fill('40')
    await page.getByLabel('ribbon y', { exact: true }).press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBeforeOffset + 1), { timeout: 60_000 })
    test.info().annotations.push({ type: 'offset', description: `offset drew one parallel entity (${countBeforeOffset} -> ${countBeforeOffset + 1})` })
    await page.keyboard.press('Escape')

    // W4g-5b ARRAY: ONE engine operation for the whole grid, so a 2 x 3
    // rectangular array of the selection adds exactly five entities in one
    // round trip and one undo step (never five round trips). A count that
    // would copy nothing is refused on the prompt with the sentence, before
    // any engine call.
    const countBeforeArray = Number(await page.getByTestId('cad-edit-entity-count').textContent())
    await page.getByRole('radio').last().check()
    await ribbon.locator('[data-tool="modify:arrayRect"]').click()
    await page.getByLabel('ribbon rows', { exact: true }).fill('1')
    await page.getByLabel('ribbon columns', { exact: true }).fill('1')
    await expect(page.getByTestId('cockpit-prompt-note'))
      .toHaveText(/1 row by 1 column is the source alone/, { timeout: 20_000 })
    await page.getByLabel('ribbon rows', { exact: true }).fill('2')
    await page.getByLabel('ribbon columns', { exact: true }).fill('3')
    await page.getByLabel('ribbon row spacing', { exact: true }).fill('8')
    await page.getByLabel('ribbon column spacing', { exact: true }).fill('8')
    await page.getByLabel('ribbon column spacing', { exact: true }).press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count'))
      .toHaveText(String(countBeforeArray + 5), { timeout: 60_000 })
    test.info().annotations.push({ type: 'array', description: `2 x 3 array added five entities in one op (${countBeforeArray} -> ${countBeforeArray + 5})` })
    // One operation means ONE undo step: the whole array goes back
    // together. `u` is the ENGINE undo (the band's Undo edit), not the
    // version undo, which is a different tool with a different id.
    await page.keyboard.press('Escape')
    await expect(page.locator('.cockpit-band [data-tool="quick-undo-edit"]')).toBeEnabled()
    await bar.fill('u')
    await bar.press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count'))
      .toHaveText(String(countBeforeArray), { timeout: 60_000 })

    // W4g-5c CUT / COPY / PASTE. A copy takes a RECORD of the selection,
    // so a paste draws the same geometry at the point given (+1) and can
    // be repeated; the clipboard itself never touches the document. A
    // paste with nothing copied is refused by the panel, not the engine.
    const countBeforeClip = Number(await page.getByTestId('cad-edit-entity-count').textContent())
    await page.getByRole('radio').last().check()
    await ribbon.locator('[data-tool="clipboard:copyClip"]').click()
    await expect(page.getByRole('status').filter({ hasText: /is on the clipboard/ }))
      .toHaveCount(1, { timeout: 30_000 })
    // The copy drew nothing: the clipboard is not the document.
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBeforeClip))
    await ribbon.locator('[data-tool="clipboard:pasteClip"]').click()
    await page.getByLabel('ribbon x', { exact: true }).fill('120')
    await page.getByLabel('ribbon y', { exact: true }).fill('60')
    await page.getByLabel('ribbon y', { exact: true }).press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count'))
      .toHaveText(String(countBeforeClip + 1), { timeout: 60_000 })
    test.info().annotations.push({ type: 'clipboard', description: `paste drew one entity (${countBeforeClip} -> ${countBeforeClip + 1})` })
    await page.keyboard.press('Escape')

    // W4g-5d TEXT, single-line, on the REAL engine: the typed word arms the
    // prompt in the Annotation panel's own seat, the four operands and the
    // value place one entity (+1) whose outline box the canvas draws, and a
    // value with a tab in it holds Run with the sentence before any engine
    // call (a DXF group value is one line).
    const countBeforeText = Number(await page.getByTestId('cad-edit-entity-count').textContent())
    await page.keyboard.press('Escape')
    await bar.fill('t')
    await bar.press('Enter')
    await expect(page.getByTestId('cockpit-prompt')).toHaveAttribute('data-op', 'createText', { timeout: 20_000 })
    await expect(ribbon.locator('[data-group="annotation"] [data-tool="draw:createText"]')).toHaveAttribute('aria-expanded', 'true')
    await page.getByLabel('ribbon x', { exact: true }).fill('30')
    await page.getByLabel('ribbon y', { exact: true }).fill('30')
    await page.getByLabel('ribbon height', { exact: true }).fill('4')
    await page.getByLabel('ribbon rotation', { exact: true }).fill('15')
    await page.getByLabel('ribbon text', { exact: true }).fill('tab\there')
    await expect(page.getByTestId('cockpit-prompt-note')).toHaveText(/one line only, with no control characters/, { timeout: 20_000 })
    await page.getByLabel('ribbon text', { exact: true }).fill('Panel A')
    await expect(page.getByTestId('cockpit-prompt-note')).toHaveCount(0)
    await page.getByLabel('ribbon text', { exact: true }).press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count'))
      .toHaveText(String(countBeforeText + 1), { timeout: 60_000 })
    test.info().annotations.push({ type: 'text', description: `TEXT placed one entity (${countBeforeText} -> ${countBeforeText + 1})` })
    await page.keyboard.press('Escape')

    // W4g-6 TRIM and FILLET on the REAL engine. Two crossing lines drawn by
    // typed operands; the second is the selection. TRIM names the first as
    // the cutting edge (its id typed into the same field the canvas click
    // fills) and a point on the part to remove: the count holds and the
    // selection's far end moves to the crossing. FILLET rounds the corner
    // with one arc (+1) in ONE batch, so ONE engine undo takes the whole
    // fillet back: both lines regrow and the arc goes.
    const countBeforeTrim = Number(await page.getByTestId('cad-edit-entity-count').textContent())
    const workbenchStatus = page.locator('.cad-edit-workbench [role="status"]')
    await page.keyboard.press('Escape')
    await bar.fill('l')
    await bar.press('Enter')
    await expect(page.getByTestId('cockpit-prompt')).toHaveAttribute('data-op', 'createLine', { timeout: 20_000 })
    await page.getByLabel('ribbon x', { exact: true }).fill('300')
    await page.getByLabel('ribbon y', { exact: true }).fill('300')
    await page.getByLabel('ribbon x2', { exact: true }).fill('320')
    await page.getByLabel('ribbon y2', { exact: true }).fill('300')
    await page.getByLabel('ribbon y2', { exact: true }).press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBeforeTrim + 1), { timeout: 60_000 })
    await page.keyboard.press('Escape')
    const cuttingEdge = await page.getByRole('radio').last().getAttribute('value')
    expect(cuttingEdge).toBeTruthy()
    await bar.fill('l')
    await bar.press('Enter')
    await expect(page.getByTestId('cockpit-prompt')).toHaveAttribute('data-op', 'createLine', { timeout: 20_000 })
    await page.getByLabel('ribbon x', { exact: true }).fill('310')
    await page.getByLabel('ribbon y', { exact: true }).fill('290')
    await page.getByLabel('ribbon x2', { exact: true }).fill('310')
    await page.getByLabel('ribbon y2', { exact: true }).fill('310')
    await page.getByLabel('ribbon y2', { exact: true }).press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBeforeTrim + 2), { timeout: 60_000 })
    await page.keyboard.press('Escape')
    await page.getByRole('radio').last().check()
    await bar.fill('tr')
    await bar.press('Enter')
    await expect(page.getByTestId('cockpit-prompt')).toHaveAttribute('data-op', 'trim', { timeout: 20_000 })
    // An empty edge holds Run with the ask, like an empty operand.
    // (The prompt's own Run, by class: the page holds other buttons whose
    // accessible name starts with Run, and a held Run is named by its ask.)
    await expect(page.locator('[data-testid="cockpit-prompt"] .cp-run')).toBeDisabled()
    await page.getByLabel('ribbon edge', { exact: true }).fill(cuttingEdge)
    await page.getByLabel('ribbon x', { exact: true }).fill('310')
    await page.getByLabel('ribbon y', { exact: true }).fill('305')
    await page.getByLabel('ribbon y', { exact: true }).press('Enter')
    await expect(workbenchStatus).toContainText('trim applied', { timeout: 60_000 })
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBeforeTrim + 2))
    test.info().annotations.push({ type: 'trim', description: `trim cut the selection at the crossing with ${cuttingEdge} (count held at ${countBeforeTrim + 2})` })
    await page.keyboard.press('Escape')
    await bar.fill('f')
    await bar.press('Enter')
    await expect(page.getByTestId('cockpit-prompt')).toHaveAttribute('data-op', 'fillet', { timeout: 20_000 })
    await page.getByLabel('ribbon radius', { exact: true }).fill('2')
    await page.getByLabel('ribbon edge', { exact: true }).fill(cuttingEdge)
    await page.getByLabel('ribbon edge x', { exact: true }).fill('318')
    await page.getByLabel('ribbon edge y', { exact: true }).fill('300')
    await page.getByLabel('ribbon x', { exact: true }).fill('310')
    await page.getByLabel('ribbon y', { exact: true }).fill('292')
    await page.getByLabel('ribbon y', { exact: true }).press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBeforeTrim + 3), { timeout: 60_000 })
    await expect(workbenchStatus).toContainText('fillet applied: entity')
    test.info().annotations.push({ type: 'fillet', description: `fillet drew one arc in one batch (${countBeforeTrim + 2} -> ${countBeforeTrim + 3})` })
    // One batch is ONE undo step: the arc goes and both lines regrow together.
    await page.keyboard.press('Escape')
    await expect(page.locator('.cockpit-band [data-tool="quick-undo-edit"]')).toBeEnabled()
    await bar.fill('u')
    await bar.press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBeforeTrim + 2), { timeout: 60_000 })

    // W4g-6d FILLET at a polyline's OWN corner on the REAL engine: a RECTANG
    // by typed corners (+1), then FILLET with the rectangle itself as the
    // second object (its id typed into the same edge field a canvas click on
    // it fills), the edge point on its top side and the first point on its
    // right side naming the corner (350, 310). The polyline stays ONE entity
    // (count held) and gains its rounded corner as a bulge; one engine undo
    // takes the corner back.
    await page.keyboard.press('Escape')
    await bar.fill('rec')
    await bar.press('Enter')
    await expect(page.getByTestId('cockpit-prompt')).toHaveAttribute('data-op', 'createRectangle', { timeout: 20_000 })
    await page.getByLabel('ribbon x', { exact: true }).fill('330')
    await page.getByLabel('ribbon y', { exact: true }).fill('290')
    await page.getByLabel('ribbon x2', { exact: true }).fill('350')
    await page.getByLabel('ribbon y2', { exact: true }).fill('310')
    await page.getByLabel('ribbon y2', { exact: true }).press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBeforeTrim + 3), { timeout: 60_000 })
    await page.keyboard.press('Escape')
    const rectangle = await page.getByRole('radio').last().getAttribute('value')
    expect(rectangle).toBeTruthy()
    await page.getByRole('radio').last().check()
    await bar.fill('f')
    await bar.press('Enter')
    await expect(page.getByTestId('cockpit-prompt')).toHaveAttribute('data-op', 'fillet', { timeout: 20_000 })
    await page.getByLabel('ribbon radius', { exact: true }).fill('3')
    await page.getByLabel('ribbon edge', { exact: true }).fill(rectangle)
    await page.getByLabel('ribbon edge x', { exact: true }).fill('340')
    await page.getByLabel('ribbon edge y', { exact: true }).fill('310')
    await page.getByLabel('ribbon x', { exact: true }).fill('350')
    await page.getByLabel('ribbon y', { exact: true }).fill('300')
    await page.getByLabel('ribbon y', { exact: true }).press('Enter')
    await expect(workbenchStatus).toContainText('fillet applied.', { timeout: 60_000 })
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBeforeTrim + 3))
    test.info().annotations.push({ type: 'fillet-polyline', description: `fillet rounded the rectangle's own corner in one step (count held at ${countBeforeTrim + 3})` })
    await page.keyboard.press('Escape')
    await expect(page.locator('.cockpit-band [data-tool="quick-undo-edit"]')).toBeEnabled()
    await bar.fill('u')
    await bar.press('Enter')
    await expect(page.locator('.cockpit-band [data-tool="quick-redo-edit"]')).toBeEnabled({ timeout: 60_000 })
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBeforeTrim + 3))

    // W4g-6e TRIM on a CURVED polyline on the REAL engine: redo the corner
    // fillet (the rectangle is curved again), draw a vertical LINE through
    // the rectangle (+1), then TRIM the rectangle by that line with the pick on
    // its left part. A closed polyline losing a piece opens into ONE polyline
    // whose kept part carries the rounded corner's bulge (count held), and
    // one engine undo takes the trim back; a second undo removes the line.
    await bar.fill('redo')
    await bar.press('Enter')
    await expect(page.locator('.cockpit-band [data-tool="quick-redo-edit"]')).toBeDisabled({ timeout: 60_000 })
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBeforeTrim + 3))
    await bar.fill('l')
    await bar.press('Enter')
    await expect(page.getByTestId('cockpit-prompt')).toHaveAttribute('data-op', 'createLine', { timeout: 20_000 })
    await page.getByLabel('ribbon x', { exact: true }).fill('340')
    await page.getByLabel('ribbon y', { exact: true }).fill('280')
    await page.getByLabel('ribbon x2', { exact: true }).fill('340')
    await page.getByLabel('ribbon y2', { exact: true }).fill('320')
    await page.getByLabel('ribbon y2', { exact: true }).press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBeforeTrim + 4), { timeout: 60_000 })
    await page.keyboard.press('Escape')
    const curvedCutter = await page.getByRole('radio').last().getAttribute('value')
    expect(curvedCutter).toBeTruthy()
    await page.locator(`input[type="radio"][value="${rectangle}"]`).check()
    await bar.fill('tr')
    await bar.press('Enter')
    await expect(page.getByTestId('cockpit-prompt')).toHaveAttribute('data-op', 'trim', { timeout: 20_000 })
    await page.getByLabel('ribbon edge', { exact: true }).fill(curvedCutter)
    await page.getByLabel('ribbon x', { exact: true }).fill('335')
    await page.getByLabel('ribbon y', { exact: true }).fill('300')
    await page.getByLabel('ribbon y', { exact: true }).press('Enter')
    await expect(workbenchStatus).toContainText('trim applied', { timeout: 60_000 })
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBeforeTrim + 4))
    test.info().annotations.push({ type: 'trim-curved', description: `trim opened the rounded rectangle at ${curvedCutter} into one polyline (count held at ${countBeforeTrim + 4})` })
    await page.keyboard.press('Escape')
    // The kept polyline is still CURVED: OFFSET refuses a curved polyline by its sentence (the store's own
    // rule, run on the engine's re-parsed projection), which a flattened polyline would have accepted.
    await page.locator(`input[type="radio"][value="${rectangle}"]`).check()
    await bar.fill('o')
    await bar.press('Enter')
    await expect(page.getByTestId('cockpit-prompt')).toHaveAttribute('data-op', 'offset', { timeout: 20_000 })
    await page.getByLabel('ribbon distance', { exact: true }).fill('3')
    await page.getByLabel('ribbon x', { exact: true }).fill('360')
    await page.getByLabel('ribbon y', { exact: true }).fill('300')
    await page.getByLabel('ribbon y', { exact: true }).press('Enter')
    await expect(workbenchStatus).toContainText('curved segments', { timeout: 60_000 })
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBeforeTrim + 4))
    await page.keyboard.press('Escape')
    await bar.fill('u')
    await bar.press('Enter')
    await expect(page.locator('.cockpit-band [data-tool="quick-redo-edit"]')).toBeEnabled({ timeout: 60_000 })
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBeforeTrim + 4))
    await bar.fill('u')
    await bar.press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBeforeTrim + 3), { timeout: 60_000 })

    // W4g-4b on the REAL engine: a POINT (+1) and an ELLIPSE (+1) by typed
    // operands, then MATCHPROP: a LINE drawn on its own layer is the source,
    // the earlier cutting edge the destination (its id typed into the same
    // edge field a canvas click fills); the workbench's own entity label
    // reads the copied layer back, the count holds, one engine undo puts
    // the old layer back.
    await page.keyboard.press('Escape')
    await bar.fill('po')
    await bar.press('Enter')
    await expect(page.getByTestId('cockpit-prompt')).toHaveAttribute('data-op', 'createPoint', { timeout: 20_000 })
    await page.getByLabel('ribbon x', { exact: true }).fill('360')
    await page.getByLabel('ribbon y', { exact: true }).fill('300')
    await page.getByLabel('ribbon y', { exact: true }).press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBeforeTrim + 4), { timeout: 60_000 })
    await page.keyboard.press('Escape')
    await bar.fill('el')
    await bar.press('Enter')
    await expect(page.getByTestId('cockpit-prompt')).toHaveAttribute('data-op', 'createEllipse', { timeout: 20_000 })
    await page.getByLabel('ribbon x', { exact: true }).fill('380')
    await page.getByLabel('ribbon y', { exact: true }).fill('300')
    await page.getByLabel('ribbon x2', { exact: true }).fill('390')
    await page.getByLabel('ribbon y2', { exact: true }).fill('300')
    await page.getByLabel('ribbon ratio', { exact: true }).fill('0.5')
    await page.getByLabel('ribbon ratio', { exact: true }).press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBeforeTrim + 5), { timeout: 60_000 })
    test.info().annotations.push({ type: 'point-ellipse', description: `POINT and ELLIPSE drew one entity each (${countBeforeTrim + 3} -> ${countBeforeTrim + 5})` })
    await page.keyboard.press('Escape')
    await bar.fill('l')
    await bar.press('Enter')
    await expect(page.getByTestId('cockpit-prompt')).toHaveAttribute('data-op', 'createLine', { timeout: 20_000 })
    await page.getByLabel('ribbon x', { exact: true }).fill('400')
    await page.getByLabel('ribbon y', { exact: true }).fill('290')
    await page.getByLabel('ribbon x2', { exact: true }).fill('400')
    await page.getByLabel('ribbon y2', { exact: true }).fill('310')
    await page.getByLabel('ribbon layer', { exact: true }).fill('W4G4B')
    await page.getByLabel('ribbon layer', { exact: true }).press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBeforeTrim + 6), { timeout: 60_000 })
    await page.keyboard.press('Escape')
    const destinationRow = page.locator('.cad-edit-workbench label', { has: page.locator(`input[type="radio"][value="${cuttingEdge}"]`) })
    await expect(destinationRow).not.toContainText('on layer W4G4B')
    // The source is the line on W4G4B, found by its own label (never by list order);
    // its id is read back after the copy to prove the selection stayed on it.
    const sourceRow = page.locator('.cad-edit-workbench label', { hasText: 'on layer W4G4B' })
    await expect(sourceRow).toHaveCount(1)
    await sourceRow.locator('input[type="radio"]').check()
    const sourceId = await sourceRow.locator('input[type="radio"]').getAttribute('value')
    await bar.fill('ma')
    await bar.press('Enter')
    await expect(page.getByTestId('cockpit-prompt')).toHaveAttribute('data-op', 'matchprop', { timeout: 20_000 })
    await page.getByLabel('ribbon edge', { exact: true }).fill(cuttingEdge)
    await page.getByLabel('ribbon edge', { exact: true }).press('Enter')
    await expect(workbenchStatus).toContainText('matchprop applied', { timeout: 60_000 })
    await expect(destinationRow).toContainText('on layer W4G4B')
    await expect(page.locator(`input[type="radio"][value="${sourceId}"]`)).toBeChecked()
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBeforeTrim + 6))
    test.info().annotations.push({ type: 'matchprop', description: `MATCHPROP copied layer W4G4B onto ${cuttingEdge} in one step (count held at ${countBeforeTrim + 6})` })
    await page.keyboard.press('Escape')
    await bar.fill('u')
    await bar.press('Enter')
    await expect(destinationRow).not.toContainText('on layer W4G4B', { timeout: 60_000 })

    // W4g-7b-03c: the cockpit row. Colour set through the Properties panel's
    // select (no prompt: a select is its own prompt), the dock's Color row
    // reading it back; MATCHPROP copies it onto the circle drawn earlier in
    // this walk; one engine undo takes the match back, the redo depth rises;
    // the three combos sit ON the band (the #1059 lesson), never a separate
    // row under it.
    await page.locator(`input[type="radio"][value="${sourceId}"]`).check()
    const colorSelect = page.locator('#cockpit-properties-slot [data-widget="prop-color"] select')
    await colorSelect.selectOption('red')
    await expect(workbenchStatus).toContainText('setColor applied', { timeout: 60_000 })
    const dockColor = page.getByTestId('dock-properties').locator('dd').first()
    await expect(dockColor).toHaveText('red (1)')
    const circleRow = page.locator('.cad-edit-workbench label', { hasText: 'CIRCLE on layer 0' })
    await expect(circleRow).toHaveCount(1)
    const circleId = await circleRow.locator('input[type="radio"]').getAttribute('value')
    await bar.fill('ma')
    await bar.press('Enter')
    await expect(page.getByTestId('cockpit-prompt')).toHaveAttribute('data-op', 'matchprop', { timeout: 20_000 })
    await page.getByLabel('ribbon edge', { exact: true }).fill(circleId)
    await page.getByLabel('ribbon edge', { exact: true }).press('Enter')
    await expect(workbenchStatus).toContainText('matchprop applied', { timeout: 60_000 })
    // The layer copied too (the batch's first step): find the circle by its
    // id now, never by the label text MATCHPROP just changed. MATCHPROP stays
    // ARMED for the next pick by design (the reference's loop), and an armed
    // command makes the workbench click-through, so disarm before the radio.
    await page.keyboard.press('Escape')
    await expect(page.getByTestId('cockpit-prompt')).toHaveCount(0)
    await page.locator(`input[type="radio"][value="${circleId}"]`).check()
    await expect(dockColor).toHaveText('red (1)')
    test.info().annotations.push({ type: 'properties-panel', description: `setColor through the panel, then MATCHPROP copied it onto ${circleId}` })
    await page.keyboard.press('Escape')
    const redoQuick = page.locator('.cockpit-band [data-tool="quick-redo-edit"]')
    await expect(redoQuick).toBeDisabled()
    await bar.fill('u')
    await bar.press('Enter')
    // Escape (pressed above with nothing armed) clears the console selection, and since #1143 the forward mirror carries that into the engine, so the dock is empty until the entity is re-selected; the store keeps a selection across the undo reload.
    await expect(dockColor).toHaveCount(0, { timeout: 60_000 })
    await expect(redoQuick).toBeEnabled()
    await page.locator(`input[type="radio"][value="${circleId}"]`).check()
    await expect(dockColor).not.toHaveText('red (1)')
    const propertiesCluster = ribbon.locator('[data-group="properties"]')
    const clusterBox = await propertiesCluster.boundingBox()
    for (const id of ['prop-color', 'prop-linetype', 'prop-lineweight']) {
      const box = await page.locator(`#cockpit-properties-slot [data-widget="${id}"]`).boundingBox()
      expect(box.y).toBeGreaterThanOrEqual(clusterBox.y - 1)
      expect(box.y + box.height).toBeLessThanOrEqual(clusterBox.y + clusterBox.height + 1)
    }

    // Named groups keep geometry intact and restore through engine undo.
    const groupCountBefore = Number(await page.getByTestId('cad-edit-entity-count').textContent())
    const groupLines = []
    for (const [index, fraction] of [0.65, 0.75].entries()) {
      const start = await groundPick(fraction, 0.65)
      const end = await groundPick(fraction + 0.04, 0.65)
      groupLines.push({ x: Number(r3((start.wx + end.wx) / 2)), y: Number(r3(start.wy)) })
      await bar.fill('LINE')
      await bar.press('Enter')
      await page.getByLabel('ribbon x', { exact: true }).fill(r3(start.wx))
      await page.getByLabel('ribbon y', { exact: true }).fill(r3(start.wy))
      await page.getByLabel('ribbon x2', { exact: true }).fill(r3(end.wx))
      await page.getByLabel('ribbon y2', { exact: true }).fill(r3(end.wy))
      await page.getByLabel('ribbon layer', { exact: true }).fill(`GROUP_ROW_${index}`)
      await page.getByLabel('ribbon layer', { exact: true }).press('Enter')
      await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(groupCountBefore + index + 1), { timeout: 60_000 })
      await page.keyboard.press('Escape')
    }
    const groupRadios = [0, 1].map((index) => page.locator('.cad-edit-workbench label', { hasText: `on layer GROUP_ROW_${index}` }).locator('input[type="radio"]'))
    const groupIds = await Promise.all(groupRadios.map((radio) => radio.getAttribute('value')))
    await groupRadios[0].check()
    await bar.fill('GROUP')
    await bar.press('Enter')
    await expect(page.getByTestId('cockpit-prompt')).toHaveAttribute('data-op', 'group')
    const groupPick = await page.evaluate(async ({ x, y }) => {
      const canvas = document.querySelector('.studio-ground .viewer-canvas')
      let previous
      for (let attempt = 0; attempt < 60; attempt++) {
        await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))
        const point = canvas.__cadviewer.project(x, y)
        const reading = JSON.stringify([canvas.getAttribute('data-safe-rect'), point])
        if (reading === previous) return point
        previous = reading
      }
      throw new Error('geometry did not settle')
    }, groupLines[1])
    await page.mouse.click(groupPick.x, groupPick.y)
    await expect(page.getByLabel('ribbon members')).toHaveText('2 objects')
    await page.getByTestId('cockpit-prompt-run').click()
    await page.getByLabel('ribbon group name').fill('RACK')
    await page.getByLabel('ribbon group name').press('Enter')
    await expect(page.getByTestId('dock-groups')).toHaveText('RACK', { timeout: 60_000 })
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(groupCountBefore + 2))
    await page.getByLabel('Select group', { exact: true }).selectOption('RACK')
    await expect(page.locator('.studio-ground .viewer-canvas')).toHaveAttribute('data-group-highlight', groupIds.map((id) => BigInt(id).toString(16).toUpperCase()).join(' '))
    await bar.fill('UNGROUP')
    await bar.press('Enter')
    await page.getByLabel('ribbon group name').fill('RACK')
    await page.getByLabel('ribbon group name').press('Enter')
    await expect(page.getByTestId('dock-groups')).toHaveCount(0, { timeout: 60_000 })
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(groupCountBefore + 2))
    await page.keyboard.press('Escape')
    await bar.fill('UNDO')
    await bar.press('Enter')
    await expect(page.getByTestId('dock-groups')).toHaveText('RACK', { timeout: 60_000 })

    // W4g-7b-04c: DIMLINEAR/DIMALIGNED on the real engine. A line by typed
    // operands sets the scene ((0,0)-(3,4)); DIMALIGNED's three typed picks
    // measure it as the chord length (the case table's 5), read back off the
    // dock's own Measurement row (the fresh dimension is the new selection);
    // one engine undo removes it; DIMLINEAR at rotation 0 over the same two
    // points measures the axis projection instead (3, never the cached
    // endpoint distance).
    const dimCountBefore = Number(await page.getByTestId('cad-edit-entity-count').textContent())
    await draw.locator('[data-tool="draw:createLine"]').click()
    await page.getByLabel('ribbon x', { exact: true }).fill('0')
    await page.getByLabel('ribbon y', { exact: true }).fill('0')
    await page.getByLabel('ribbon x2', { exact: true }).fill('3')
    await page.getByLabel('ribbon y2', { exact: true }).fill('4')
    await page.getByLabel('ribbon y2', { exact: true }).press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(dimCountBefore + 1), { timeout: 60_000 })

    await bar.fill('dal')
    await bar.press('Enter')
    await expect(page.getByTestId('cockpit-prompt')).toHaveAttribute('data-op', 'dimAligned', { timeout: 20_000 })
    await page.getByLabel('ribbon x', { exact: true }).fill('0')
    await page.getByLabel('ribbon y', { exact: true }).fill('0')
    await page.getByLabel('ribbon x2', { exact: true }).fill('3')
    await page.getByLabel('ribbon y2', { exact: true }).fill('4')
    await page.getByLabel('ribbon dx', { exact: true }).fill('1.5')
    await page.getByLabel('ribbon dy', { exact: true }).fill('6')
    await page.getByLabel('ribbon dy', { exact: true }).press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(dimCountBefore + 2), { timeout: 60_000 })
    await expect(page.getByTestId('dock-properties')).toContainText('Measurement')
    await expect(page.getByTestId('dock-properties').locator('dd').last()).toHaveText('5')

    await bar.fill('u')
    await bar.press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(dimCountBefore + 1), { timeout: 60_000 })

    await bar.fill('dli')
    await bar.press('Enter')
    await expect(page.getByTestId('cockpit-prompt')).toHaveAttribute('data-op', 'dimLinear', { timeout: 20_000 })
    await page.getByLabel('ribbon x', { exact: true }).fill('0')
    await page.getByLabel('ribbon y', { exact: true }).fill('0')
    await page.getByLabel('ribbon x2', { exact: true }).fill('3')
    await page.getByLabel('ribbon y2', { exact: true }).fill('4')
    await page.getByLabel('ribbon dx', { exact: true }).fill('1.5')
    await page.getByLabel('ribbon dy', { exact: true }).fill('6')
    await page.getByLabel('ribbon rotation', { exact: true }).fill('0')
    await page.getByLabel('ribbon rotation', { exact: true }).press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(dimCountBefore + 2), { timeout: 60_000 })
    await expect(page.getByTestId('dock-properties').locator('dd').last()).toHaveText('3')
    await page.keyboard.press('Escape')

    // MLEADER is typed on the command line and draws its own canvas outline.
    const mleaderCountBefore = Number(await page.getByTestId('cad-edit-entity-count').textContent())
    await bar.fill('MLEADER')
    await bar.press('Enter')
    await expect(page.getByTestId('cockpit-prompt')).toHaveAttribute('data-op', 'createMleader')
    await page.getByLabel('ribbon x', { exact: true }).fill('30')
    await page.getByLabel('ribbon y', { exact: true }).fill('23')
    await page.getByLabel('ribbon x2', { exact: true }).fill('35')
    await page.getByLabel('ribbon y2', { exact: true }).fill('26')
    await page.getByLabel('ribbon text', { exact: true }).fill('Valve')
    await page.getByLabel('ribbon text', { exact: true }).press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(mleaderCountBefore + 1), { timeout: 60_000 })
    await expect(page.getByTestId('cad-edit-entity-list')).toContainText('MLEADER on layer 0 · 2 vertices · read-only')
    await page.keyboard.press('Escape')
    const mleaderCanvas = page.locator('.studio-ground .viewer-canvas canvas')
    await expect(mleaderCanvas).toBeVisible()
    await test.info().attach('MLEADER canvas schematic', { body: await mleaderCanvas.screenshot(), contentType: 'image/png' })
    await bar.fill('u')
    await bar.press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(mleaderCountBefore), { timeout: 60_000 })
    await expect(page.getByTestId('cad-edit-entity-list')).not.toContainText('MLEADER on layer 0 · 2 vertices · read-only')

    // W4g-2 (one head), confirm-time race. LAST in the walk on purpose: a
    // refused run leaves its failed strip on the page and there is no
    // dismiss for it, and that strip sits BETWEEN the command prompt and
    // the command input. Run earlier, it moved the prompt seat 148px and
    // failed the W4e seat check on right behaviour. It had only ever
    // passed because the block is conditional on the catalog tool being
    // seated in time, so it silently skipped some runs.
      // W4g-2 (one head), confirm-time race: listen before arming, arm a
      // catalog WRITE tool while clean, then draw before confirming. The
      // explicit Run click must refuse the now-dirty engine, with no run POST
      // at any point from the arm through the refusal.
      await page.getByRole('tab', { name: 'Manage' }).click()
      const countBeforeConfirm = Number(await page.getByTestId('cad-edit-entity-count').textContent())
      const armedWrite = ribbon.locator('.ribbon-tool[data-tool="delete-marked-panel"]')
      const armedWriteCount = await armedWrite.count()
      test.info().annotations.push({ type: 'one-head', description: armedWriteCount ? 'confirm-time row: delete-marked-panel armed while clean' : 'delete-marked-panel not on this ribbon: confirm-time row skipped' })
      if (armedWriteCount) {
        const runPosts = []
        const onRequest = (request) => { if (request.method() === 'POST' && request.url().includes('/api/run')) runPosts.push(request.url()) }
        page.on('request', onRequest)
        try {
          // At the END of the walk the engine holds every unsaved edit above,
          // so the one-head rule (W4g-2) rightly DISABLES a catalog write tool
          // here, naming the edits. That is the execution-time refusal proving
          // itself on a dirty engine, and it is asserted, never skipped. The
          // confirm-time race below needs a CLEAN engine to stage, so it runs
          // only when the tool is live; both branches are real assertions.
          if (await armedWrite.isDisabled()) {
            expect(await armedWrite.getAttribute('aria-label')).toContain('the browser engine holds unsaved edits')
            test.info().annotations.push({ type: 'one-head', description: 'dirty engine at the end of the walk: delete-marked-panel disabled with the unsaved-edits reason (confirm-time race needs a clean engine, not staged here)' })
            expect(runPosts).toHaveLength(0)
          } else {
            await expect(armedWrite).toBeEnabled()
            await armedWrite.click()
            const confirm = page.getByRole('button', { name: 'Run delete-marked-panel' })
            await expect(confirm).toBeVisible({ timeout: 10_000 })
            await page.getByRole('tab', { name: 'Draw' }).click()
            await draw.locator('[data-tool="draw:createLine"]').click()
            await page.getByLabel('ribbon x', { exact: true }).fill('0')
            await page.getByLabel('ribbon y', { exact: true }).fill('0')
            await page.getByLabel('ribbon x2').fill('20')
            await page.getByLabel('ribbon y2').fill('20')
            await page.getByLabel('ribbon y2').press('Enter')
            await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBeforeConfirm + 1), { timeout: 60_000 })
            await confirm.click()
            await expect(page.locator('.strip-failed').filter({ hasText: 'the browser engine holds unsaved edits' })).toBeVisible({ timeout: 10_000 })
            await page.waitForTimeout(1500)
            expect(runPosts).toHaveLength(0)
            test.info().annotations.push({ type: 'one-head', description: 'confirm-time refusal alert visible; POST /api/run count 0 from arm through refusal' })
            await page.keyboard.press('Escape')
            await page.getByRole('tab', { name: 'Insert' }).click()
            await ribbon.locator('[data-tool="undo-edit"]').click()
            await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBeforeConfirm), { timeout: 60_000 })
          }
        } finally {
          page.off('request', onRequest)
        }
      }

    // The Command bar's points use the engine, including LINE continuation.
    const cockpitCount = page.getByTestId('cockpit-status').locator('.cockpit-count')
    const drawingCountBeforeLine = Number((await cockpitCount.textContent()).match(/^\d+/)[0])
    const dock = page.getByTestId('properties-dock')
    const dockCountBeforeLine = Number((await dock.getByTestId('dock-drawing').locator('dt', { hasText: /^Entities$/ }).locator('+ dd').textContent()).replaceAll(',', ''))
    await page.locator('body').press('Escape')
    const pointRoutes = []
    const onPointRoute = (req) => { if (req.url().includes('/api/nl-prompt')) pointRoutes.push(req) }
    page.on('request', onPointRoute)
    for (const unarmedPoint of ['0,0', '10,5', '@10,0', '10<90']) {
      await bar.fill(unarmedPoint)
      await bar.press('Enter')
      await expect(page.locator('.toast')).toContainText('Start a drawing command before entering a point.')
    }
    expect(pointRoutes).toHaveLength(0)
    await bar.fill('LINE')
    await bar.press('Enter')
    await expect(bar).toHaveAttribute('placeholder', 'LINE  Specify first point:')
    await bar.fill('0,0')
    await bar.press('Enter')
    await expect(bar).toHaveAttribute('placeholder', 'LINE  Specify next point:')
    await expect(page.getByTestId('cockpit-active-ask')).toHaveText('LINE  Specify next point:')
    await bar.fill('@10,0')
    await bar.press('Enter')
    const readPointResult = () => page.evaluate(() => {
      const result = window.__commandPointResult
      return result?.entities.find((entity) => String(entity.id) === result.id)?.vertices?.map((p) => p.slice(0, 2))
    })
    await expect.poll(readPointResult).toEqual([[0, 0], [10, 0]])
    await expect(bar).toHaveAttribute('placeholder', 'LINE  Specify next point:')
    await bar.fill('10<90')
    await bar.press('Enter')
    await expect.poll(readPointResult).toEqual([[10, 0], [10, 10]])
    await expect(cockpitCount).toContainText(new RegExp('(^|[^\\d,])' + (drawingCountBeforeLine + 2).toLocaleString('en-US') + ' entities'))
    const createdLayer = await page.evaluate(() => {
      const result = window.__commandPointResult
      return result.entities.find((entity) => String(entity.id) === result.id).layer
    })
    await expect(dock.locator('.sel-field').filter({ has: page.locator('dt', { hasText: /^Layer$/ }) }).locator('dd')).toHaveText(createdLayer)
    await expect(dock.getByTestId('dock-geometry').locator('dt', { hasText: /^Start$/ }).locator('+ dd')).toHaveText('10.00, 0.00')
    await expect(dock.getByTestId('dock-geometry').locator('dt', { hasText: /^End$/ }).locator('+ dd')).toHaveText('10.00, 10.00')
    await expect(dock.getByTestId('dock-drawing').locator('dt', { hasText: /^Entities$/ }).locator('+ dd')).toHaveText((dockCountBeforeLine + 2).toLocaleString())
    await expect(bar).toHaveAttribute('placeholder', 'LINE  Specify next point:')
    await page.locator('body').press('Escape')
    await bar.fill('LINE')
    await bar.press('Enter')
    await bar.fill('0,0')
    await bar.press('Enter')
    const layerBeforeRetry = await page.getByLabel('ribbon layer', { exact: true }).inputValue()
    await bar.fill('0,0')
    await bar.press('Enter')
    await expect(page.getByTestId('cockpit-prompt-note')).toContainText('refused')
    await expect(bar).toHaveAttribute('placeholder', 'LINE  Specify next point:')
    await bar.fill('10,0')
    await bar.press('Enter')
    await expect.poll(readPointResult).toEqual([[0, 0], [10, 0]])
    await expect(page.getByLabel('ribbon layer', { exact: true })).toHaveValue(layerBeforeRetry)
    for (const order of ['bar/click/bar', 'click/bar/bar']) {
      await page.locator('body').press('Escape')
      await bar.fill('LINE')
      await bar.press('Enter')
      for (const mode of ['cockpit-osnap', 'cockpit-ortho']) {
        if (await page.getByTestId(mode).getAttribute('aria-pressed') === 'true') await page.getByTestId(mode).click()
      }
      // A canvas pick on a pixel measured to be on the drawing; with the whole drawing fitted, world (5,5) and
      // (10,0) project under the prompt strip, so the step asserts the rounded world point the picker writes.
      const c = await groundPick(0.62, 0.55)
      expect(c.onGround, `mixed ${order} pick pixel (${c.x},${c.y}) hit ${c.name}, not the drawing`).toBe(true)
      const picked = [Number(r3(c.wx)), Number(r3(c.wy))]
      if (order === 'bar/click/bar') {
        await bar.fill('5,5')
        await bar.press('Enter')
        await page.mouse.click(c.x, c.y)
        await expect.poll(readPointResult).toEqual([[5, 5], picked])
        await expect(bar).toHaveAttribute('placeholder', 'LINE  Specify next point:')
        await bar.fill('20,0')
        await bar.press('Enter')
        await expect.poll(readPointResult).toEqual([picked, [20, 0]])
      } else {
        await page.mouse.click(c.x, c.y)
        await bar.fill('10,0')
        await bar.press('Enter')
        await expect.poll(readPointResult).toEqual([picked, [10, 0]])
        await expect(bar).toHaveAttribute('placeholder', 'LINE  Specify next point:')
        await bar.fill('20,0')
        await bar.press('Enter')
        await expect.poll(readPointResult).toEqual([[10, 0], [20, 0]])
      }
    }
    expect(pointRoutes).toHaveLength(0)
    // End the LINE chain through the prompt's own Cancel: focus is still in the Command bar here, and Escape
    // pressed there belongs to the bar, so a body Escape would leave LINE armed and the sentence below would
    // land in its next-point field instead of routing.
    await page.getByTestId('cockpit-prompt').getByRole('button', { name: 'Cancel', exact: true }).click()
    await expect(page.getByTestId('cockpit-prompt')).toHaveCount(0)
    page.off('request', onPointRoute)

    // Resolve the same handle again through the workbench, not just the
    // create's automatic selection, without changing either count. Both
    // orders of the mixed loop above end on the segment (10,0) -> (20,0),
    // so the chained handle's dock geometry reads that segment.
    const entitiesBeforeReselect = (await cockpitCount.textContent()).match(/([\d,]+) entities/)[1]
    await dock.getByRole('button', { name: 'Deselect', exact: true }).click()
    const chainedId = await page.evaluate(() => window.__commandPointResult.id)
    await page.getByRole('tab', { name: 'Insert' }).click()
    const importWasOpen = await importBtn.getAttribute('aria-expanded') === 'true'
    if (!importWasOpen) await importBtn.click()
    await page.getByTestId('cad-edit-entity-list').locator(`input[type="radio"][value="${chainedId}"]`).check()
    if (!importWasOpen) await importBtn.click()
    await expect(dock.locator('.sel-field').filter({ has: page.locator('dt', { hasText: /^Layer$/ }) }).locator('dd')).toHaveText(createdLayer)
    await expect(dock.getByTestId('dock-geometry').locator('dt', { hasText: /^Start$/ }).locator('+ dd')).toHaveText('10.00, 0.00')
    await expect(dock.getByTestId('dock-geometry').locator('dt', { hasText: /^End$/ }).locator('+ dd')).toHaveText('20.00, 0.00')
    await expect(cockpitCount).toContainText(new RegExp('(^|[^\\d,])' + entitiesBeforeReselect + ' entities'))

    // A one-unit LINE far from the current drawing is sub-pixel at the
    // viewer's automatic full fit. The dock must reveal that exact result.
    const undoDepthBeforeTiny = Number((await dock.getByTestId('dock-drawing').locator('dt', { hasText: /^Browser edits$/ }).locator('+ dd').textContent()).match(/^[\d,]+/)[0].replaceAll(',', ''))
    // The status count before the tiny LINE: the retry LINE and the mixed loop above have drawn since
    // drawingCountBeforeLine was read, so undo and redo are checked against this count, as the dock's are.
    const entitiesBeforeTiny = Number((await cockpitCount.textContent()).match(/([\d,]+) entities/)[1].replaceAll(',', ''))
    await bar.fill('LINE')
    await bar.press('Enter')
    await bar.fill('1000000,0')
    await bar.press('Enter')
    await bar.fill('1000001,0')
    await bar.press('Enter')
    await expect.poll(readPointResult).toEqual([[1000000, 0], [1000001, 0]])
    const showResult = dock.getByRole('button', { name: 'Show result', exact: true })
    await expect(showResult).toBeVisible()
    await expect(dock.getByTestId('dock-drawing')).toContainText(new RegExp('(^|[^\\d,])' + (undoDepthBeforeTiny + 1).toLocaleString('en-US') + ' to undo'))
    // End the LINE chain through the prompt's own Cancel: focus is still in the Command bar after the typed
    // points, and Escape pressed there belongs to the bar, so a body Escape would leave LINE armed.
    await page.getByTestId('cockpit-prompt').getByRole('button', { name: 'Cancel', exact: true }).click()
    await expect(page.getByTestId('cockpit-prompt')).toHaveCount(0)
    await showResult.click()
    await expect(showResult).toHaveCount(0)
    const visibleResult = await page.evaluate(() => {
      const mount = document.querySelector('.studio-ground .viewer-canvas')
      const canvasRect = mount.querySelector('canvas').getBoundingClientRect()
      const safe = mount.getAttribute('data-safe-rect')?.split(',').map(Number)
      const rect = safe ? {
        left: canvasRect.left + safe[0], top: canvasRect.top + safe[1],
        right: canvasRect.left + safe[0] + safe[2], bottom: canvasRect.top + safe[1] + safe[3],
        width: safe[2], height: safe[3],
      } : canvasRect
      const a = mount.__cadviewer.project(1000000, 0)
      const b = mount.__cadviewer.project(1000001, 0)
      return { inside: [a, b].every((p) => p.x >= rect.left && p.x <= rect.right && p.y >= rect.top && p.y <= rect.bottom), share: Math.max(Math.abs(b.x - a.x) / rect.width, Math.abs(b.y - a.y) / rect.height) }
    })
    expect(visibleResult.inside).toBe(true)
    expect(visibleResult.share).toBeGreaterThan(0.35)
    expect(visibleResult.share).toBeLessThan(0.45)
    await page.getByRole('tab', { name: 'Insert' }).click()
    await ribbon.locator('[data-tool="undo-edit"]').click()
    await expect(cockpitCount).toContainText(new RegExp('(^|[^\\d,])' + entitiesBeforeTiny.toLocaleString('en-US') + ' entities'))
    await expect(dock.getByTestId('dock-drawing')).toContainText(new RegExp('(^|[^\\d,])' + (undoDepthBeforeTiny).toLocaleString('en-US') + ' to undo'))
    await ribbon.locator('[data-tool="redo-edit"]').click()
    await expect(cockpitCount).toContainText(new RegExp('(^|[^\\d,])' + (entitiesBeforeTiny + 1).toLocaleString('en-US') + ' entities'))
    await expect(dock.getByTestId('dock-drawing')).toContainText(new RegExp('(^|[^\\d,])' + (undoDepthBeforeTiny + 1).toLocaleString('en-US') + ' to undo'))

    // A sentence is still a sentence: it routes, it never arms. LAST in the
    // row on purpose: while its route decision is shown the Command bar's
    // Enter belongs to the decision strip, so a word typed after it would be
    // swallowed (the race that failed this row once).
    const sentenceRoute = page.waitForRequest((req) => req.url().includes('/api/nl-prompt') && req.postDataJSON()?.text === '2 rows, 10 panels each')
    await bar.fill('2 rows, 10 panels each')
    await bar.press('Enter')
    await sentenceRoute
    await expect(page.getByTestId('cockpit-prompt')).toHaveCount(0)
  })

  test('W4g bleed-2a: CAD and Solar CAD keep one canvas, one WebGL context and the camera', async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    await page.goto('/app?surface=cad&drawing=cat-panels')
    const viewer = page.locator('.studio-ground .viewer-canvas')
    const canvas = viewer.locator('canvas')
    const safeRectPattern = /^\d+,\d+,\d+,\d+$/
    await expect.poll(() => canvas.count(), { timeout: 30_000 }).toBe(1)
    await expect.poll(() => viewer.getAttribute('data-safe-rect')).toMatch(safeRectPattern)

    const ribbon = page.getByTestId('drafting-ribbon')
    const engine = await request.get('/engine/engine.js').catch(() => null)
    const cadEditOn = (await ribbon.locator('[data-group="modify"]').count()) > 0
    if (engine && engine.status() === 200 && cadEditOn) {
      // Opening the engine head replaces the initial intake's canvas once.
      await expect.poll(() => page.locator('.workspace-card').getAttribute('data-engine-document'), { timeout: 60_000 }).toMatch(/\S+/)
    }
    const settleFrames = () => page.evaluate(() => new Promise((resolve) => {
      requestAnimationFrame(() => requestAnimationFrame(resolve))
    }))
    await page.evaluate(() => new Promise((resolve, reject) => {
      const selector = '.studio-ground .viewer-canvas canvas'
      let current = document.querySelector(selector)
      let stableSince = performance.now()
      let frame
      const timeout = setTimeout(() => {
        cancelAnimationFrame(frame)
        reject(new Error('Canvas did not remain stable for 500 ms within 15 s'))
      }, 15_000)
      const check = (now) => {
        const next = document.querySelector(selector)
        if (next !== current || !next) {
          current = next
          stableSince = now
        }
        if (current && now - stableSince >= 500) {
          clearTimeout(timeout)
          resolve()
          return
        }
        frame = requestAnimationFrame(check)
      }
      frame = requestAnimationFrame(check)
    }))
    await canvas.evaluate((el) => { el.__bleed2a = 1 })
    const readPose = () => viewer.evaluate((el) => el.__cadviewer.cameraPose())
    const cadPose = await readPose()
    await expectSharedChrome(page)
    const expectSameCanvas = async () => {
      await expect.poll(() => canvas.count()).toBe(1)
      await expect.poll(() => canvas.evaluate((el) => el.__bleed2a)).toBe(1)
      await expect.poll(() => canvas.evaluate((el) => {
        const context = el.getContext('webgl2') || el.getContext('webgl')
        return context ? context.isContextLost() : null
      })).toBe(false)
    }

    await page.getByRole('tab', { name: 'Solar CAD', exact: true }).click()
    await expect.poll(() => page.locator('.app[data-surface="solar"]').count()).toBe(1)
    await settleFrames()
    await expectSameCanvas()
    await expect.poll(readPose).toEqual(cadPose)

    await page.getByRole('tab', { name: 'CAD', exact: true }).click()
    await expect.poll(() => page.locator('.app[data-surface="cad"]').count()).toBe(1)
    await settleFrames()
    await expectSameCanvas()
    await expect.poll(readPose).toEqual(cadPose)

    await page.getByRole('tab', { name: 'Browser', exact: true }).click()
    await expect.poll(() => page.locator('.app[data-surface="browser"]').count()).toBe(1)
    await settleFrames()
    await page.getByRole('tab', { name: 'CAD', exact: true }).click()
    await expect.poll(() => page.locator('.app[data-surface="cad"]').count()).toBe(1)
    await settleFrames()
    await expectSameCanvas()
    await expect.poll(() => viewer.getAttribute('data-safe-rect')).toMatch(safeRectPattern)
  })

  test('W4g bleed-2b: profile switches settle, fade inertly and keep one canvas', async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    await page.goto('/app?surface=cad&drawing=cat-panels')
    await expectOneCanvasIn(page, '.studio-ground')
    const ribbon = page.getByTestId('drafting-ribbon')
    const engine = await request.get('/engine/engine.js').catch(() => null)
    const cadEditOn = (await ribbon.locator('[data-group="modify"]').count()) > 0
    if (engine && engine.status() === 200 && cadEditOn) {
      await expect.poll(() => page.locator('.workspace-card').getAttribute('data-engine-document'), { timeout: 60_000 }).toMatch(/\S+/)
    }
    await page.evaluate(() => new Promise((resolve, reject) => {
      const selector = '.studio-ground .viewer-canvas canvas'
      let current = document.querySelector(selector)
      let stableSince = performance.now()
      let frame
      const timeout = setTimeout(() => {
        cancelAnimationFrame(frame)
        reject(new Error('Canvas did not remain stable for 500 ms within 15 s'))
      }, 15_000)
      const check = (now) => {
        const next = document.querySelector(selector)
        if (next !== current || !next) {
          current = next
          stableSince = now
        }
        if (current && now - stableSince >= 500) {
          clearTimeout(timeout)
          resolve()
          return
        }
        frame = requestAnimationFrame(check)
      }
      frame = requestAnimationFrame(check)
    }))
    await expectSharedChrome(page)
    await page.locator('.studio-ground .viewer-canvas canvas').evaluate((canvas) => { canvas.__bleed2a = 1 })
    await page.evaluate(() => {
      const recorder = { leaving: [], phases: [], started: 0, settled: null }
      window.__bleed2b = recorder
      document.addEventListener('click', (event) => {
        if (event.target.closest('[role="tab"][data-surface]')) window.__bleed2b.started = performance.now()
      }, true)
      const record = (records) => {
        for (const mutation of records) {
          const name = mutation.attributeName
          const value = mutation.target.getAttribute(name)
          if (value || mutation.oldValue) recorder.phases.push({ name, value, oldValue: mutation.oldValue })
        }
        for (const ground of document.querySelectorAll('[data-ground-phase="leaving"]')) {
          recorder.leaving.push({
            ariaHidden: ground.getAttribute('aria-hidden') === 'true',
            inert: ground.hasAttribute('inert'),
            painted: !ground.hidden,
          })
        }
        if (!document.querySelector('[data-ground-phase], [data-studio-transition]')) recorder.settled = performance.now()
      }
      const observer = new MutationObserver(record)
      for (const root of [document.querySelector('.studio-ground'), document.querySelector('.app')]) {
        observer.observe(root, { subtree: true, attributes: true, attributeOldValue: true, attributeFilter: ['data-ground-phase', 'data-studio-transition'] })
      }
      window.__bleed2bObserver = observer
    })
    const resetRecorder = () => page.evaluate(() => {
      Object.assign(window.__bleed2b, { leaving: [], phases: [], started: 0, settled: null })
    })
    const expectSettled = async (surface, motion) => {
      await expect(page.locator('.app[data-surface="' + surface + '"]')).toHaveCount(1)
      await expect(page.locator('[data-ground-phase], [data-studio-transition]')).toHaveCount(0, { timeout: 500 })
      const receipt = await page.evaluate(() => window.__bleed2b)
      if (motion) {
        expect(receipt.leaving.length).toBeGreaterThan(0)
        expect(receipt.leaving.every((ground) => ground.ariaHidden && ground.inert && ground.painted)).toBe(true)
        expect(receipt.settled).not.toBeNull()
        expect(receipt.started).toBeGreaterThan(0)
        // Fake-timer unit rows pin exact timings; this sequence settles within a second of the page click on a loaded host.
        expect(receipt.settled - receipt.started).toBeLessThanOrEqual(1000)
      } else {
        expect(receipt.phases).toEqual([])
        expect(receipt.leaving).toEqual([])
      }
    }
    for (const [name, surface] of [['Browser', 'browser'], ['CAD', 'cad']]) {
      await resetRecorder()
      await page.getByRole('tab', { name, exact: true }).click()
      await expectSettled(surface, true)
    }
    await resetRecorder()
    await page.evaluate(() => new Promise((resolve) => {
      document.querySelector('[role="tab"][data-surface="browser"]').click()
      setTimeout(() => {
        document.querySelector('[role="tab"][data-surface="cad"]').click()
        resolve()
      }, 40)
    }))
    await expect(page.locator('.app[data-surface="cad"]')).toHaveCount(1)
    await expect(page.locator('[data-ground-phase], [data-studio-transition]')).toHaveCount(0, { timeout: 500 })
    const reversal = await page.evaluate(() => window.__bleed2b)
    expect(reversal.settled).not.toBeNull()
    expect(reversal.started).toBeGreaterThan(0)
    // Fake-timer unit rows pin exact timings; this sequence settles within a second of the page click on a loaded host.
    expect(reversal.settled - reversal.started).toBeLessThanOrEqual(1000)
    expect(await page.locator('.studio-ground .viewer-canvas canvas').evaluate((canvas) => canvas.__bleed2a)).toBe(1)
    await page.emulateMedia({ reducedMotion: 'reduce' })
    for (const [name, surface] of [['Browser', 'browser'], ['CAD', 'cad']]) {
      await resetRecorder()
      await page.getByRole('tab', { name, exact: true }).click()
      await expectSettled(surface, false)
    }
    expect(await page.locator('.studio-ground .viewer-canvas canvas').evaluate((canvas) => canvas.__bleed2a)).toBe(1)
    await page.evaluate(() => window.__bleed2bObserver.disconnect())
  })

  test('W4g-7b-02c: INSERT of an existing block, on the real engine', async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    await page.goto('/app')
    await expect(page.locator(STUDIO)).toHaveCount(1)
    const ribbon = page.getByTestId('drafting-ribbon')
    await expect(ribbon).toBeVisible()
    const engine = await request.get('/engine/engine.js').catch(() => null)
    const cadEditOn = (await ribbon.locator('[data-group="modify"]').count()) > 0
    if (!engine || engine.status() !== 200 || !cadEditOn) {
      test.info().annotations.push({ type: 'engine', description: 'compiled engine not served, or the flag is off; INSERT half not exercised' })
      return
    }
    await page.getByRole('tab', { name: 'Insert' }).click()
    await ribbon.locator('[data-tool="import-dxf"]').click()
    const fixture = readFileSync(fileURLToPath(new URL('../fixtures/block-fixture.dxf', import.meta.url)))
    await page.getByLabel('DXF file').setInputFiles({ name: 'block-fixture.dxf', mimeType: 'application/dxf', buffer: fixture })
    await expect(page.getByRole('status').filter({ hasText: /Loaded block-fixture\.dxf/ })).toHaveCount(1, { timeout: 60_000 })
    await page.getByRole('tab', { name: 'Draw' }).click()

    // Census: the Block panel reads 1 real (Insert Block) and 1 placeholder
    // (Create Block, still honest).
    const block = ribbon.locator('[data-group="block"]')
    await expect(block.locator('.ribbon-tool')).toHaveCount(2)
    await expect(block.locator('[data-tool="draw:createInsert"]')).toBeEnabled()
    await expect(block.locator('[data-tool="draw:createBlock"]')).toBeEnabled()

    const countBefore = Number(await page.getByTestId('cad-edit-entity-count').textContent())
    const script = page.getByLabel('ribbon script', { exact: true })
    await page.getByRole('tab', { name: 'View' }).click()
    await script.fill('i Fixture 10,20')
    await page.getByTestId('cockpit-script-run').click()
    await expect(page.getByTestId('cockpit-script-status')).toHaveText('Script ran 1 command.', { timeout: 60_000 })
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBefore + 1))
    await expect(page.getByRole('status').filter({ hasText: /createInsert applied/ })).toHaveCount(1)

    // W4g-7b-05c-2: the placed INSERT refuses every geometry verb before the
    // worker sees it (buildEditPayload's own by-kind gate): arming MOVE holds
    // Run with the sentence, no worker message. BLOCK is live since #1140
    // and arms its own prompt after MOVE is dismissed.
    // A create SELECTS what it made (engineSession.js's own rule), so the
    // just-drawn INSERT is already the selection: no radio click needed, and
    // none would work anyway (the workbench list disables a read-only kind's
    // own radio, and INSERT is one).
    const bar = page.getByLabel('Command bar', { exact: true })
    await bar.fill('m')
    await bar.press('Enter')
    await expect(page.getByTestId('cockpit-prompt')).toHaveAttribute('data-op', 'move', { timeout: 20_000 })
    await expect(page.locator('[data-testid="cockpit-prompt"] .cp-run')).toBeDisabled()
    await expect(page.getByTestId('cockpit-prompt-note')).toHaveText('an INSERT is placed, not edited, in this round')
    // Wait for the arming handoff: an Escape still in the Command bar is left to the bar (W4f-2).
    await expect(page.getByLabel('ribbon dx', { exact: true })).toBeFocused()
    await page.keyboard.press('Escape')
    await expect(page.getByTestId('cockpit-prompt')).toHaveCount(0)
    await bar.fill('block')
    await bar.press('Enter')
    // BLOCK is live since #1140; dismiss its prompt before undo.
    await expect(page.getByTestId('cockpit-prompt')).toHaveAttribute('data-op', 'createBlock')
    await expect(page.getByLabel('ribbon members', { exact: true })).toBeFocused()
    await page.keyboard.press('Escape')
    await expect(page.getByTestId('cockpit-prompt')).toHaveCount(0)
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBefore + 1))

    // One engine undo takes it back; the redo depth rises.
    await page.getByRole('tab', { name: 'Insert' }).click()
    await ribbon.locator('[data-tool="undo-edit"]').click()
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(countBefore), { timeout: 60_000 })
    await expect(ribbon.locator('[data-tool="redo-edit"]')).toBeEnabled()
  })

  test('W4g-7c-2d: create a block from committed and unsaved LINE picks', async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    let headDxf = '0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n'
    // After a reload, live boot can fetch the head before the mock switch is checked.
    // Route both heads to this row's bytes so that race cannot open the stack's head.
    await page.route('**/sample.dxf', (route) => route.fulfill({ status: 200, contentType: 'application/dxf', body: headDxf }))
    await page.route('**/api/drawings/*/dxf*', (route) => route.fulfill({ status: 200, contentType: 'application/dxf', body: headDxf }))
    await page.goto('/app?dev=1')
    await page.getByLabel('Use mock data (off = live backend)').check()
    const ribbon = page.getByTestId('drafting-ribbon')
    const engine = await request.get('/engine/engine.js').catch(() => null)
    await page.getByRole('tab', { name: 'Draw' }).click()
    if (!engine || engine.status() !== 200 || !(await ribbon.locator('[data-group="modify"]').count())) {
      test.info().annotations.push({ type: 'engine', description: 'compiled engine not served, or flag off; Create Block not exercised' })
      return
    }
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText('0', { timeout: 60_000 })
    const bar = page.getByLabel('Command bar', { exact: true })
    for (const [index, command] of [
      { word: 'LINE', start: '30,23', end: '35,23' },
      { word: 'LINE', start: '12,23', end: '17,23' },
      { word: 'CIRCLE', start: '11,24', radius: '2' },
    ].entries()) {
      await bar.fill(command.word)
      await bar.press('Enter')
      await page.getByLabel('ribbon x', { exact: true }).fill(command.start)
      const last = page.getByLabel(command.end ? 'ribbon x2' : 'ribbon r', { exact: true })
      await last.fill(command.end || command.radius)
      await last.press('Enter')
      await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(index + 1), { timeout: 60_000 })
      await page.keyboard.press('Escape')
    }
    // Commit the first three entities. Publish the drawn bytes to this test's
    // intercepted mock head, then reopen it; never move the shared demo head.
    headDxf = await page.locator('a[download][href^="blob:"]').evaluate(async (link) => (await fetch(link.href)).text())
    await page.reload()
    await page.getByLabel('Use mock data (off = live backend)').check()
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText('3', { timeout: 60_000 })
    await page.getByRole('tab', { name: 'Draw' }).click()
    await page.locator('body').press('Escape')
    await expect(page.getByTestId('cockpit-prompt')).toHaveCount(0)
    // The fourth entity stays unsaved and becomes an inline block child.
    await bar.fill('LINE')
    await bar.press('Enter')
    await page.getByLabel('ribbon x', { exact: true }).fill('20,30')
    await page.getByLabel('ribbon x2', { exact: true }).fill('25,30')
    await page.getByLabel('ribbon x2', { exact: true }).press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText('4', { timeout: 60_000 })
    await page.keyboard.press('Escape')
    const clickWorld = async (x, y) => {
      const point = await page.evaluate(({ x, y }) => {
        const pt = document.querySelector('.studio-ground .viewer-canvas').__cadviewer.project(x, y)
        return { ...pt, onGround: !!document.elementFromPoint(pt.x, pt.y)?.closest('.studio-ground') }
      }, { x, y })
      expect(point.onGround, `projected point (${x},${y}) must be on the drawing`).toBe(true)
      await page.mouse.click(point.x, point.y)
    }
    await clickWorld(14.5, 23)
    await bar.fill('B')
    await bar.press('Enter')
    await expect(page.getByTestId('cockpit-prompt')).toHaveAttribute('data-op', 'createBlock')
    await expect(page.getByLabel('ribbon members')).toHaveText('1 objects')
    await clickWorld(22.5, 30)
    await expect(page.getByLabel('ribbon members')).toHaveText('2 objects')
    await page.getByLabel('ribbon members').press('Enter')
    await page.getByLabel('ribbon x', { exact: true }).fill('10,20')
    await page.getByLabel('ribbon x', { exact: true }).press('Enter')
    await page.getByLabel('ribbon block name').fill('BLK1')
    await page.getByLabel('ribbon block name').press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText('3', { timeout: 60_000 })
    await expect(page.getByTestId('cad-edit-entity-list')).toContainText('INSERT on layer 0')
    // Select the surviving LINE on the canvas, then reissue B without Escape
    // from a partially answered BLOCK prompt to prove an explicit fresh arm.
    await page.keyboard.press('Escape')
    await clickWorld(32.5, 23)
    await bar.fill('B')
    await bar.press('Enter')
    await page.getByLabel('ribbon members').press('Enter')
    await page.getByLabel('ribbon x', { exact: true }).fill('10,20')
    await page.getByLabel('ribbon x', { exact: true }).press('Enter')
    await page.getByLabel('ribbon block name').fill('STALE')
    await bar.fill('B')
    await bar.press('Enter')
    await expect(page.getByLabel('ribbon members')).toHaveText('1 objects')
    await expect(page.getByLabel('ribbon block name')).toHaveValue('')
    await expect(page.getByLabel('ribbon x', { exact: true })).toHaveValue('')
    await page.keyboard.press('Escape')
    await bar.fill('UNDO')
    await bar.press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText('4', { timeout: 60_000 })
    await bar.fill('REDO')
    await bar.press('Enter')
    await expect(page.getByTestId('cad-edit-entity-count')).toHaveText('3', { timeout: 60_000 })
    await bar.fill('BLOCK')
    await bar.press('Enter')
    await page.getByLabel('ribbon members').press('Enter')
    await page.getByLabel('ribbon x', { exact: true }).fill('10,20')
    await page.getByLabel('ribbon block name').fill('blk1')
    await expect(page.getByTestId('cockpit-prompt-note')).toContainText('already exists')
    await expect(page.getByTestId('cockpit-prompt-run')).toBeDisabled()
  })

  test('C-04C row9 Solar reads Ready only after the held demo DXF is shown', async ({ page, request }) => {
    test.setTimeout(120_000)
    await page.setViewportSize({ width: 1600, height: 1000 })
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    let releaseSample
    let sawSample
    const held = new Promise((resolve) => { releaseSample = resolve })
    const requested = new Promise((resolve) => { sawSample = resolve })
    const holdSample = async (route) => {
      sawSample()
      await held
      await route.continue()
    }
    await page.route('**/sample.dxf', holdSample)
    await page.goto('/app?surface=solar&dev=1')
    await page.getByLabel('Use mock data (off = live backend)').check()
    const solarStatus = page.getByRole('tab', { name: 'Solar CAD', exact: true }).locator('small')
    try {
      await requested
      await expect(page.locator('.workspace-card[data-engine-document]')).toHaveCount(0)
      await expect(solarStatus).toHaveText(/^(Template pending|Beta)$/)
      await expect(solarStatus).toHaveAttribute('data-state', 'beta')
    } finally {
      releaseSample()
    }
    await expect(page.locator('.workspace-card[data-engine-document$="-v1.dxf"]')).toHaveCount(1, { timeout: 60_000 })
    await expect(solarStatus).toHaveText('Ready')
    await expect(solarStatus).toHaveAttribute('data-state', 'available')
    await page.unroute('**/sample.dxf', holdSample)

    // A new demo session must not inherit readiness from the previous parse.
    await page.route('**/sample.dxf', (route) => route.fulfill({
      status: 200, contentType: 'application/dxf', body: 'This is not a DXF document.\n',
    }))
    const malformedSample = page.waitForResponse('**/sample.dxf')
    await page.reload()
    await page.getByLabel('Use mock data (off = live backend)').check()
    await (await malformedSample).finished()
    // Let the engine consume the refused answer before checking readiness.
    // The head opener's failure sentence is a separate follow-up.
    await page.waitForTimeout(1000)
    await expect(page.locator('.workspace-card[data-engine-document]')).toHaveCount(0)
    await expect(solarStatus).toHaveText(/^(Template pending|Beta)$/)
    await expect(solarStatus).toHaveAttribute('data-state', 'beta')
  })

  test('C-04C row10 Solar phone Tools reaches five clusters and preserves input and Escape focus', async ({ page, request }) => {
    test.setTimeout(120_000)
    await page.setViewportSize({ width: 390, height: 844 })
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    await page.goto('/app?surface=solar&dev=1')
    await page.getByLabel('Use mock data (off = live backend)').check()
    await expect(page.locator('.workspace-card[data-engine-document$="-v1.dxf"]')).toHaveCount(1, { timeout: 60_000 })
    const ribbon = page.getByTestId('drafting-ribbon')
    const tools = ribbon.getByRole('button', { name: 'More panels', exact: true })
    await expect(tools).toBeVisible()
    await expect(tools).toHaveAttribute('aria-expanded', 'false')
    expect(await tools.evaluate((el) => getComputedStyle(el, '::before').content)).toContain('Tools')
    await tools.click()
    await expect(tools).toHaveAttribute('aria-expanded', 'true')
    await expect(ribbon.locator('.ribbon-tool:not(:disabled)').first()).toBeFocused()
    await expect(ribbon.locator('.ribbon-cluster')).toHaveCount(5)
    for (const name of ['Panel placement', 'Stringing', 'Equipment placement', 'Measure', 'Select']) {
      const cluster = ribbon.getByRole('group', { name, exact: true })
      await cluster.scrollIntoViewIfNeeded()
      await expect(cluster).toBeVisible()
      const buttons = cluster.locator('.ribbon-tool:not(:disabled)')
      for (const button of await buttons.all()) {
        await button.scrollIntoViewIfNeeded()
        await expect(button).toBeInViewport({ ratio: 1 })
        await button.click({ trial: true })
      }
    }
    // Escape from the disclosure returns to its opener and hides whole clusters.
    await ribbon.locator('.ribbon-tool:not(:disabled)').last().focus()
    await page.keyboard.press('Escape')
    await expect(tools).toHaveAttribute('aria-expanded', 'false')
    await expect(tools).toBeFocused()
    await expect(ribbon.locator('.ribbon-cluster:visible')).toHaveCount(0)
    await tools.click()
    await ribbon.locator('[data-tool="solar-panels:createRectangle"]').click()
    const operand = page.getByLabel('ribbon x', { exact: true })
    await operand.fill('12')
    await expect(operand).toHaveValue('12')
    await expect(operand).toBeInViewport({ ratio: 1 })
    // Focus left the disclosure for the operand; blur closes it as before.
    await expect(tools).toHaveAttribute('aria-expanded', 'false')
    await operand.press('Escape')
    await expect(page.getByTestId('cockpit-prompt')).toHaveCount(0)
    await expect(tools).toBeFocused()
    expect(await page.locator('.studio-shell').evaluate((el) => el.scrollHeight - el.clientHeight)).toBeLessThanOrEqual(1)
  })

  test('C-04B Solar census: five clusters, geometry tools, solved toggle and profile continuity; C-04C row7 reserved-name import is not Ready', async ({ page, request }) => {
    test.setTimeout(120_000)
    await page.setViewportSize({ width: 1600, height: 1000 })
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    await page.goto('/app?surface=solar&dev=1')
    await expect(page.getByRole('tab', { name: 'Solar', exact: true })).toHaveAttribute('aria-selected', 'true')
    await page.getByLabel('Use mock data (off = live backend)').check()
    await expectOneCanvasIn(page, '.studio-ground')
    await expectSharedChrome(page)
    const ribbon = page.getByTestId('drafting-ribbon')
    await expect(ribbon.locator('.ribbon-cluster')).toHaveCount(5)
    expect(await ribbon.locator('.ribbon-cluster').evaluateAll((groups) => groups.map((group) => group.getAttribute('aria-label'))))
      .toEqual(['Panel placement', 'Stringing', 'Equipment placement', 'Measure', 'Select'])
    const toggle = ribbon.locator('[data-tool="solar-strings"]')
    await expect(toggle).toHaveAttribute('aria-pressed', 'true')
    await expect(page.locator('.viewer-canvas[data-string-routes="134"]')).toHaveCount(1, { timeout: 30_000 })
    await toggle.click()
    await expect(toggle).toHaveAttribute('aria-pressed', 'false')
    await expect(page.locator('.viewer-canvas[data-string-routes]')).toHaveCount(0)
    await toggle.click()
    await expect(page.locator('.viewer-canvas[data-string-routes="134"]')).toHaveCount(1)
    // A clean hand import cannot inherit the solve, even with the head's exact name.
    const headDocument = page.locator('.workspace-card[data-engine-document$="-v1.dxf"]')
    await expect(headDocument).toHaveCount(1, { timeout: 60_000 })
    const headDocumentId = await headDocument.getAttribute('data-engine-document')
    const foreignDxf = readFileSync(fileURLToPath(new URL('../fixtures/block-fixture.dxf', import.meta.url)))
    for (const name of ['other.dxf', headDocumentId]) {
      await page.getByRole('tab', { name: 'Insert', exact: true }).click()
      await ribbon.locator('[data-tool="import-dxf"]').click()
      await page.getByLabel('DXF file').setInputFiles({ name, mimeType: 'application/dxf', buffer: foreignDxf })
      await expect(page.locator('.workspace-card[data-engine-document]')).toHaveAttribute('data-engine-document', name, { timeout: 60_000 })
      // C-04C row7: even an import named after the head is not a ready template.
      const solarStatus = page.getByRole('tab', { name: 'Solar CAD', exact: true }).locator('small')
      await expect(solarStatus).toHaveText(/^(Template pending|Beta)$/)
      await expect(solarStatus).toHaveAttribute('data-state', 'beta')
      await page.getByRole('tab', { name: 'Solar', exact: true }).click()
      await expect(toggle).toBeDisabled()
      await expect(toggle).toHaveAttribute('aria-pressed', 'false')
      await expect(toggle).toHaveAttribute('title', 'Solved routes cover the rooftop demo only')
      await expect(page.locator('.viewer-canvas[data-string-routes]')).toHaveCount(0)
      // A fresh visit clears the import and lets the head opener restore the sample.
      await page.reload()
      await expect(page.locator(STUDIO)).toHaveCount(1)
      await page.getByRole('tab', { name: 'Solar CAD', exact: true }).click()
      await expect(page.getByRole('tab', { name: 'Solar', exact: true })).toHaveAttribute('aria-selected', 'true')
      await page.getByLabel('Use mock data (off = live backend)').check()
      await expect(headDocument).toHaveCount(1, { timeout: 60_000 })
      await expect(toggle).toBeEnabled({ timeout: 60_000 })
      await expect(toggle).toHaveAttribute('aria-pressed', 'true')
      await expect(page.locator('.viewer-canvas[data-string-routes="134"]')).toHaveCount(1, { timeout: 30_000 })
    }
    const outline = ribbon.locator('[data-tool="solar-panels:createRectangle"]')
    await expect(outline).toBeEnabled({ timeout: 60_000 })
    const count = page.getByTestId('cad-edit-entity-count')
    const before = Number(await count.textContent())
    await outline.click()
    for (const [field, value] of [['x', '0'], ['y', '0'], ['x2', '2'], ['y2', '1']]) {
      await page.getByLabel(`ribbon ${field}`, { exact: true }).fill(value)
    }
    await page.getByTestId('cockpit-prompt-run').click()
    await expect(count).toHaveText(String(before + 1), { timeout: 60_000 })
    await expect(page.locator('.viewer-canvas[data-string-routes]')).toHaveCount(0)
    await expect(toggle).toBeDisabled()
    await expect(toggle).toHaveAttribute('title', 'Solved routes are available only for the unchanged rooftop demo')
    await page.keyboard.press('Escape')
    for (const op of ['arrayRect', 'move', 'rotate']) {
      const tool = ribbon.locator(`[data-tool="solar-panels:${op}"]`)
      await expect(tool).toBeEnabled()
      await tool.click()
      await expect(page.getByTestId('cockpit-prompt')).toHaveAttribute('data-op', op)
      await page.keyboard.press('Escape')
    }
    const readState = () => page.evaluate(() => ({
      document: document.querySelector('.workspace-card').getAttribute('data-engine-document'),
      count: document.querySelector('[data-testid="cad-edit-entity-count"]').textContent,
      undo: document.querySelector('[data-tool="quick-undo-edit"]').getAttribute('title'),
      pose: document.querySelector('.studio-ground .viewer-canvas').__cadviewer.cameraPose(),
    }))
    const state = await readState()
    for (const chooseView of [false, true]) {
      await page.getByRole('tab', { name: 'CAD', exact: true }).click()
      await expect(page.getByRole('tab', { name: 'Draw', exact: true })).toHaveAttribute('aria-selected', 'true')
      if (chooseView) await page.getByRole('tab', { name: 'View', exact: true }).click()
      await page.getByRole('tab', { name: 'Solar CAD', exact: true }).click()
      await expect(page.getByRole('tab', { name: 'Solar', exact: true })).toHaveAttribute('aria-selected', 'true')
      expect(await readState()).toEqual(state)
    }
    await expectOneCanvasIn(page, '.studio-ground')
    await expectSharedChrome(page)
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
    await expectSharedChrome(page)
    await expect(page.getByRole('tab', { name: 'Solar', exact: true })).toHaveAttribute('aria-selected', 'true')
    await expect(page.getByTestId('drafting-ribbon').getByRole('group', { name: 'Stringing', exact: true })).toBeVisible()
    await expect(page.getByTestId('drafting-ribbon').getByRole('group', { name: 'Equipment placement', exact: true })).toBeVisible()

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
    await expectSharedChrome(page)
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

    // ...and the SAME oracle on Solar CAD (P1 studio-shell pass). This row is
    // an APPEND, not a rewrite: the check above is unchanged and still guards
    // CAD. It is added because solar was the surface the oracle never ran on,
    // and so the surface a page-shaped block survived on - solar declared
    // chrome.productFrame TRUE, which put a 450px opaque white marketing card
    // (measured y=28..478 at 1512x950) over its own ribbon, document band and
    // canvas, pushing the band from y~123 to y=585. The contract now says
    // false; this row is what keeps it that way. An oracle only guards the
    // rows you actually run it on.
    await page.getByRole('tab', { name: 'Solar CAD' }).click()
    await expect(page.locator('.app[data-surface="solar"]')).toHaveCount(1)
    await expect(page.locator('#product-surface-panel')).toHaveCount(0)
    await expect(page.locator('.studio-ground .viewer-canvas canvas')).toHaveCount(1, { timeout: 30_000 })
    const solarSlabs = await page.evaluate(() => [...document.querySelectorAll('.studio-shell *')]
      .filter((el) => {
        const m = getComputedStyle(el).backgroundColor.match(/rgba?\((\d+), (\d+), (\d+)(?:, ([\d.]+))?/)
        if (!m) return false
        if (m[4] !== undefined && Number(m[4]) < 0.2) return false
        const r = el.getBoundingClientRect()
        return Number(m[1]) > 200 && Number(m[2]) > 200 && Number(m[3]) > 200 && r.width * r.height > 30_000
      })
      .map((el) => `${el.tagName}.${el.className}`.slice(0, 60)))
    expect(solarSlabs, 'a light page-shaped block is sitting on the solar drawing').toEqual([])
    // The Solar band owns its empty space over the full-bleed drawing.
    const solarGroundTop = (await page.locator('.studio-shell .studio-ground').boundingBox()).y
    expect(solarGroundTop).toBe(0)
    expect(await page.locator('.viewer-toolbar').evaluate((element) => {
      const box = element.getBoundingClientRect()
      return element.contains(document.elementFromPoint(box.right - 6, box.top + box.height / 2))
    })).toBe(true)
    await page.getByRole('tab', { name: 'CAD', exact: true }).click()
    await expect(page.locator('.app[data-surface="cad"]')).toHaveCount(1)

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
    await expect(page.locator('.app[data-drawer], .studio-drawer-tabs')).toHaveCount(0)
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

  test('<=980px: the CAD shell does not scroll and the ground stays pinned between the fixed chrome', async ({ page, request }) => {
    test.setTimeout(120_000)
    await requireLocalReady(request, test, API_BASE)
    await setRail(page, '1')
    await page.setViewportSize({ width: 900, height: 640 })
    await page.goto('/app')
    await expectOneCanvasIn(page, '.studio-ground')
    // #1228 (W4g-remainder S05) pins the header, ribbon, command line and status bar on the CAD and Solar surfaces
    // at <=980px and insets the drawing ground between them, so the shell itself no longer scrolls
    // (cockpit-viewports.spec.mjs asserts the same at 390x844). The W4c scrolling stack this row used to pin is gone.
    const before = await page.evaluate(() => {
      const shell = document.querySelector('.studio-shell')
      return { scrollHeight: shell.scrollHeight, clientHeight: shell.clientHeight, overflowY: getComputedStyle(shell).overflowY }
    })
    expect(before.overflowY).toBe('hidden')
    expect(before.scrollHeight - before.clientHeight, 'the fixed CAD stack must fit the viewport').toBeLessThanOrEqual(1)
    const ground = await page.evaluate(() => {
      const box = () => {
        const rect = document.querySelector('.studio-ground').getBoundingClientRect()
        return { top: rect.top, bottom: rect.bottom, height: rect.height }
      }
      const shell = document.querySelector('.studio-shell')
      const first = box()
      shell.scrollTop = 400
      return { first, after: box(), scrollTop: shell.scrollTop, viewport: window.innerHeight }
    })
    expect(ground.scrollTop).toBeLessThanOrEqual(1)
    expect(Math.abs(ground.after.top - ground.first.top)).toBeLessThanOrEqual(1)
    expect(ground.first.top).toBeGreaterThanOrEqual(0)
    expect(ground.first.bottom).toBeLessThanOrEqual(ground.viewport)
    expect(ground.first.height, 'the drawing ground keeps a usable height').toBeGreaterThanOrEqual(120)
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
    await expect(page.locator('.app[data-studio-shell], .app[data-drawer], .studio-drawer-tabs')).toHaveCount(0)
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
    await expect(page.getByLabel('Command bar', { exact: true })).toHaveCount(1)
    await expect(page.locator('main')).toHaveCount(1)
    await expect(page).toHaveURL(/\/app$/)

    // No storage residue from the studio session: nothing the old shell did
    // not already own on its own fresh boot may survive the rollback.
    expect(residue(baseline, await storageKeys(page)), 'stale storage survived rollback').toEqual([])
    await expect(page.locator('.app[data-studio-shell], .app[data-drawer], .studio-drawer-tabs')).toHaveCount(0)
  })
})
