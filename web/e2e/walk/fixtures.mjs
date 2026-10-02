import { test as base, expect } from '@playwright/test'
import { startStack } from '../../walk/stack.mjs'
import { prepareProductionBundle } from '../../walk/sameOriginProxy.mjs'
import { readFile, writeFile } from 'node:fs/promises'
import { PRODUCT_SURFACES } from '../../src/site/productSurfaces.js'
import { toolPlacementTab } from '../../src/lib/toolRecord.js'

export { expect }
export const LOCAL_IDENTITY = Object.freeze({ tenant: 'demo-tenant', token: 'j1-presentation-fixture' })
export const COACH_STORAGE_KEY = 'leaf.coach.dismissed.v1'

export const test = base.extend({
  firstRun: [false, { option: true }],
  stack: [async ({}, use, workerInfo) => {
    let stack
    try {
      // workerIndex also distinguishes replacement workers after a failure.
      stack = await startStack({ slot: workerInfo.workerIndex, slots: workerInfo.config.workers })
      await use(stack)
    } catch (error) {
      if (error.code === 'QUEUED') throw new Error(`QUEUED: ${error.message}`, { cause: error })
      throw error
    } finally {
      if (stack) await stack.stop()
    }
  }, { scope: 'worker', timeout: 300_000 }],
  baseURL: async ({ stack }, use) => { await use(stack.baseURL) },
  extraHTTPHeaders: async ({}, use) => {
    await use({ 'X-Tenant-Id': LOCAL_IDENTITY.tenant })
  },
  walkEvidence: [async ({}, use, testInfo) => {
    const evidence = {
      schema: 'leaf.walk-evidence.v1', test: testInfo.title, viewport: testInfo.project.name,
      consoleErrors: [], pageErrors: [], failedRequests: [], abortedRequests: [], httpErrors: [],
      responses: [], steps: [], accessibility: null,
      startedAt: new Date().toISOString(),
    }
    try { await use(evidence) } finally {
      evidence.finishedAt = new Date().toISOString()
      await testInfo.attach('walk-evidence', {
        body: Buffer.from(JSON.stringify(evidence, null, 2)), contentType: 'application/json',
      })
    }
  }, { auto: true }],
  page: async ({ page, stack, firstRun, walkEvidence }, use) => {
    walkEvidence.stack = { baseURL: stack.baseURL, ports: stack.ports, metricsPath: stack.metricsPath }
    const consoleError = (message) => {
      if (message.type() === 'error') walkEvidence.consoleErrors.push({ text: message.text(), location: message.location() })
    }
    const pageError = (error) => { walkEvidence.pageErrors.push({ message: error.message, stack: error.stack }) }
    const requestFailed = (request) => {
      const record = { url: request.url(), method: request.method(), error: request.failure()?.errorText }
      const aborted = ['net::ERR_ABORTED', 'NS_BINDING_ABORTED', 'cancelled'].includes(record.error)
      walkEvidence[aborted ? 'abortedRequests' : 'failedRequests'].push(record)
    }
    const response = (reply) => {
      const record = { url: reply.url(), method: reply.request().method(), status: reply.status() }
      walkEvidence.responses.push(record)
      if (reply.status() >= 400) walkEvidence.httpErrors.push(record)
    }
    page.on('console', consoleError)
    page.on('pageerror', pageError)
    page.on('requestfailed', requestFailed)
    page.on('response', response)
    await page.addInitScript(({ firstRun, identity, coachKey }) => {
      if (firstRun) localStorage.removeItem('leaf.jwt')
      else {
        localStorage.setItem('leaf.jwt', identity.token)
        localStorage.setItem(coachKey, '1')
      }
    }, { firstRun, identity: LOCAL_IDENTITY, coachKey: COACH_STORAGE_KEY })
    try { await use(page) } finally {
      page.off('console', consoleError)
      page.off('pageerror', pageError)
      page.off('requestfailed', requestFailed)
      page.off('response', response)
    }
  },
})

const groupNames = { draw: 'Draw', modify: 'Modify', clipboard: 'Clipboard', properties: 'Properties',
      annotation: 'Annotation', block: 'Block', groups: 'Groups', 'solar-panels': 'Panels',
      view: 'View', version: 'Version', author: 'Author', rail: 'Rail' }
const control = (page, recipe) => {
  let scope = recipe.scope ? control(page, recipe.scope) : page
  if (recipe.group && recipe.role !== 'combobox') {
    scope = scope.getByRole('group', { name: groupNames[recipe.group], exact: true })
  }
  return scope.getByRole(recipe.role, { name: recipe.name, exact: recipe.exact !== false })
}
const panelButton = (page, name) => page.getByRole('group', { name: 'Workspace panels', exact: true })
  .getByRole('button', { name, exact: true })
const canvas = (page) => page.getByRole('region', { name: 'Drawing', exact: true }).locator('canvas:visible')
async function viewportBounds(page) {
  const expand = page.getByRole('button', { name: 'Expand drawing overview', exact: true })
  if (await expand.isVisible()) await expand.click()
  const viewport = page.getByRole('button', { name: 'Drawing overview', exact: true }).locator('[data-overview-viewport]')
  await expect(viewport).toBeVisible()
  return viewport.evaluate((element) => Object.fromEntries(['x', 'y', 'width', 'height'].map((key) => {
    const value = Number(element.getAttribute(key))
    if (!element.hasAttribute(key) || !Number.isFinite(value)) throw new Error(`Invalid overview viewport ${key}`)
    return [key, value]
  })))
}
async function engineCount(page) {
  return Number(await page.getByTestId('cad-edit-entity-count').innerText())
}
async function unsupported(probe, runtime, reason) {
  const result = { featureId: probe.featureId, state: probe.state, result: 'unsupported_local',
    declaredCertify: probe.certify, reason }
  runtime.evidence.result = result
  await runtime.testInfo.attach('walk-result', { body: Buffer.from(JSON.stringify(result)), contentType: 'application/json' })
  throw new Error(`UNSUPPORTED_LOCAL: ${probe.featureId} [${probe.state}]: ${reason}`)
}
async function engineReady(probe, runtime) {
  const { page } = runtime
  // A missing build flag is an unavailable local capability, not an engine
  // test that passed because the spec silently returned before its assertions.
  if (!await page.getByTestId('cad-edit-workbench').count()) {
    await unsupported(probe, runtime, 'The production bundle has no mounted browser editing engine')
  }
  await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(/^[1-9]\d*$/, { timeout: 60_000 })
}
async function createLine(probe, runtime) {
  await engineReady(probe, runtime)
  const { page } = runtime
  const before = await engineCount(page)
  const bar = page.getByRole('combobox', { name: 'Command bar', exact: true })
  await bar.fill('LINE')
  await bar.press('Enter')
  await page.getByLabel('ribbon x', { exact: true }).fill('0,0')
  const end = page.getByLabel('ribbon x2', { exact: true })
  await end.fill('10,0')
  await end.press('Enter')
  await expect.poll(() => engineCount(page)).toBe(before + 1)
  await page.keyboard.press('Escape')
}
async function selectEntity(probe, runtime, recipe) {
  await engineReady(probe, runtime)
  const { page } = runtime
  // Calibrate the flat drawing's projection from its production cursor
  // readout. Selection still uses native mouse input on the real canvas.
  const points = recipe.type === 'LINE' ? [[222, 190], [222, 470]]
    : recipe.type === 'INSERT' ? [[11.5, 20]] : null
  if (!points) await unsupported(probe, runtime, `The private DXF fixture has no ${recipe.type} entity to select`)
  if (recipe.editable === false && recipe.type === 'LINE') {
    await unsupported(probe, runtime, 'The engine exposes the fixture LINE as editable; a read-only LINE needs a provider fixture')
  }
  const samples = await canvas(page).evaluate((element) => {
    const box = element.getBoundingClientRect(), points = []
    for (const fx of [0.3, 0.5, 0.7]) for (const fy of [0.3, 0.5, 0.7]) {
      const point = { x: box.left + box.width * fx, y: box.top + box.height * fy }
      if (document.elementFromPoint(point.x, point.y) === element) points.push(point)
    }
    const first = points[0], second = points.find((point) => first && Math.abs(point.x - first.x) > 20 && Math.abs(point.y - first.y) > 20)
    if (!first || !second) throw new Error('The drawing needs two uncovered calibration points')
    return [first, second]
  })
  const coordinates = page.getByTestId('cockpit-status').locator('.cockpit-coord b')
  const readings = []
  for (const sample of samples) {
    const previous = await coordinates.allTextContents()
    await page.mouse.move(sample.x, sample.y)
    await expect.poll(() => coordinates.allTextContents()).not.toEqual(previous)
    const values = (await coordinates.allTextContents()).map(Number)
    expect(values).toHaveLength(2)
    expect(values.every(Number.isFinite)).toBe(true)
    readings.push(values)
  }
  const scaleX = (samples[1].x - samples[0].x) / (readings[1][0] - readings[0][0])
  const scaleY = (samples[1].y - samples[0].y) / (readings[1][1] - readings[0][1])
  expect(scaleX).toBeGreaterThan(0)
  expect(scaleY).toBeLessThan(0)
  const clickPoint = async ([x, y]) => {
    const point = { x: samples[0].x + (x - readings[0][0]) * scaleX, y: samples[0].y + (y - readings[0][1]) * scaleY }
    const exposed = await canvas(page).evaluate((element, point) => document.elementFromPoint(point.x, point.y) === element, point)
    expect(exposed, 'the selected entity must be visible and uncovered on the drawing canvas').toBe(true)
    await page.mouse.click(point.x, point.y)
  }
  await clickPoint(points[0])
  if (recipe.multiple) {
    expect(points.length).toBeGreaterThan(1)
    await page.keyboard.down('Shift')
    try { await clickPoint(points[1]) } finally { await page.keyboard.up('Shift') }
    await expect(page.getByTestId('dock-selection-count')).toHaveText('2 objects selected')
  }
  await expect(page.getByTestId('dock-properties')).toBeVisible()
}
async function setDrawer(page, name, open) {
  const button = panelButton(page, name)
  await expect(button).toBeVisible()
  if ((await button.getAttribute('aria-expanded') === 'true') !== open) await button.click()
  await expect(button).toHaveAttribute('aria-expanded', String(open))
}
async function setToolRail(page, open, phone) {
  const expand = page.getByRole('button', { name: 'Tool rail', exact: true })
  const collapse = page.getByRole('button', { name: 'Collapse the tool rail to a spine', exact: true })
  if (phone) {
    await expect(expand).toBeVisible()
    if ((await expand.getAttribute('aria-expanded') === 'true') !== open) await expand.click()
    await expect(expand).toHaveAttribute('aria-expanded', String(open))
  } else {
    // A pre-hydration rail can have a visible collapse button before the
    // cockpit has established its spine. Read the rail's settled disclosure.
    await expect(page.getByRole('toolbar', { name: 'Drafting tools', exact: true })).toBeVisible()
    if (open) {
      await expect(expand).toBeVisible()
      await expect(expand).toHaveAttribute('aria-expanded', 'false')
      await expand.click()
    } else if (await collapse.isVisible()) await collapse.click()
  }
  await expect(page.locator('aside.nav'))[open ? 'toBeVisible' : 'toBeHidden']()
  if (!phone) {
    if (open) await expect(collapse).toBeVisible()
    else {
      await expect(expand).toBeVisible()
      await expect(expand).toHaveAttribute('aria-expanded', 'false')
      await expect(page.locator('aside.nav')).toHaveAttribute('aria-hidden', 'true')
    }
  }
}
async function setupStep(probe, runtime, recipe) {
  const { page, stack, evidence } = runtime
  switch (recipe.kind) {
    case 'prepare-engine-transport':
      await page.addInitScript(() => {
        const NativeWorker = globalThis.Worker
        const transport = { hold: false, pending: [] }
        globalThis.__walkEngineTransport = transport
        globalThis.Worker = class extends NativeWorker {
          constructor(url, options) {
            super(url, options)
            this.walkEngine = String(url).includes('worker-browser')
            this.walkListeners = new Map()
          }
          addEventListener(type, listener, options) {
            if (type !== 'message' || !this.walkEngine) return super.addEventListener(type, listener, options)
            let wrapped = this.walkListeners.get(listener)
            if (!wrapped) {
              wrapped = (event) => {
                const deliver = () => typeof listener === 'function' ? listener.call(this, event) : listener.handleEvent(event)
                if (transport.hold) transport.pending.push(deliver)
                else deliver()
              }
              this.walkListeners.set(listener, wrapped)
            }
            return super.addEventListener(type, wrapped, options)
          }
          removeEventListener(type, listener, options) {
            return super.removeEventListener(type, this.walkListeners.get(listener) || listener, options)
          }
        }
      })
      runtime.cleanup.push(async () => {
        await page.evaluate(() => {
          const transport = globalThis.__walkEngineTransport
          if (!transport) return
          transport.hold = false
          for (const deliver of transport.pending.splice(0)) deliver()
        })
      })
      return
    case 'hold-engine-boot': {
      let release
      const gate = new Promise((resolve) => { release = resolve })
      const pattern = '**/engine/engine_bg.wasm'
      const hold = async (route) => { await gate; await route.continue() }
      await page.route(pattern, hold)
      runtime.cleanup.push(async () => { release(); await page.unroute(pattern, hold) })
      return
    }
    case 'navigate': await page.goto(recipe.url); return
    case 'open-failed-drawing': {
      const sessionReply = page.waitForResponse((response) => {
        const url = new URL(response.url())
        return url.pathname === '/api/session' && url.searchParams.get('dwg') === 'missing.invalid'
      })
      await page.goto(recipe.url)
      const response = await sessionReply
      expect([400, 404]).toContain(response.status())
      evidence.failedDrawing = { drawingId: 'missing.invalid', status: response.status(), response: await response.json() }
      await expect(page.getByRole('alert').filter({ hasText: /Couldn['’]t load drawing/ })).toBeVisible()
      await expect(page.locator('.viewer-canvas')).toHaveCount(0)
      await expect(canvas(page)).toHaveCount(0)
      runtime.failedDrawing = true
      return
    }
    case 'failed-drawing-ribbon-tab': {
      expect(runtime.failedDrawing).toBe(true)
      const tabs = page.getByRole('tablist', { name: 'Ribbon', exact: true })
      const toolbar = page.getByRole('toolbar', { name: 'Drafting tools', exact: true })
      if (await tabs.count() === 0 && await toolbar.count() === 0) {
        runtime.ribbonAbsent = true
        return
      }
      await setupStep(probe, runtime, { kind: 'ribbon-tab', name: recipe.name })
      await expect(toolbar).toBeVisible()
      return
    }
    case 'open-empty-workspace':
      if (recipe.signedOut) await page.addInitScript(() => localStorage.removeItem('leaf.jwt'))
      await page.goto(`/try?surface=${recipe.surface}`)
      return
    case 'open-private-drawing': {
      const selected = probe.setup.steps.find((step) => step.kind === 'select-entity')
      let source = await readFile(new URL(selected?.type === 'INSERT' ? '../fixtures/block-fixture.dxf'
        : '../fixtures/distinctive-panel.dxf', import.meta.url), 'utf8')
      if (selected?.type !== 'INSERT') {
        // Two LINEs outside the real panel polygon give single and multiple
        // selection deterministic targets without API or UI state fabrication.
        const lines = [190, 470].map((y, index) => `0\nLINE\n5\nA10${index}\n8\nWalk\n10\n111\n20\n${y}\n11\n333\n21\n${y}\n`).join('')
        source = source.replace('0\nENDSEC\n0\nEOF', `${lines}0\nENDSEC\n0\nEOF`)
      }
      const uploaded = await page.request.post('/api/drawings/upload', {
        multipart: { file: { name: 'walk.dxf', mimeType: 'application/dxf', buffer: Buffer.from(source) } },
      })
      expect(uploaded.status()).toBe(202)
      const receipt = await uploaded.json()
      expect(receipt.drawing_id).toBeTruthy()
      runtime.drawingId = receipt.drawing_id
      await expect.poll(async () => {
        const response = await page.request.get(`/api/drawings/${receipt.drawing_id}/upload-status`)
        expect(response.ok()).toBe(true)
        const status = await response.json()
        expect(status.status, JSON.stringify(status)).not.toBe('failed')
        return status.status
      }, { timeout: 60_000 }).toBe('ready')
      await page.goto(`/app?drawing=${receipt.drawing_id}&surface=${recipe.surface}`)
      if (recipe.surface === 'cad' || recipe.surface === 'solar') {
        await expect.poll(async () => {
          const box = await canvas(page).boundingBox().catch(() => null)
          return !!box && box.width > 0 && box.height > 0
        }, { timeout: 60_000 }).toBe(true)
      }
      evidence.drawingId = runtime.drawingId
      return
    }
    case 'ribbon-tab':
      await page.getByRole('tablist', { name: 'Ribbon', exact: true }).getByRole('tab', { name: recipe.name, exact: true }).click()
      // Narrow viewports offer the extra panels through a real disclosure.
      { const more = page.getByRole('button', { name: 'More panels', exact: true })
        if (await more.isVisible() && await more.getAttribute('aria-expanded') === 'false') await more.click() }
      runtime.ribbonTab = recipe.name
      return
    case 'engine-ready': await engineReady(probe, runtime); return
    case 'select-entity': await selectEntity(probe, runtime, recipe); return
    case 'clear-selection': await page.keyboard.press('Escape'); return
    case 'copy-selection':
      await page.getByRole('toolbar', { name: 'Drafting tools', exact: true }).getByRole('group', { name: 'Clipboard', exact: true })
        .getByRole('button', { name: 'Copy', exact: true }).click()
      return
    case 'drawer-state': await setDrawer(page, recipe.name, recipe.open); return
    case 'tool-rail-state': await setToolRail(page, recipe.open, runtime.testInfo.project.name === 'phone'); return
    case 'properties-state': {
      const dock = page.getByRole('complementary', { name: 'Properties', exact: true })
      if (await dock.isVisible() !== recipe.open) {
        await page.getByRole('toolbar', { name: 'Drafting tools', exact: true }).getByRole('button', { name: 'Properties', exact: true }).click()
      }
      await expect(dock)[recipe.open ? 'toBeVisible' : 'toBeHidden']()
      return
    }
    case 'open-start': await page.getByRole('button', { name: 'Start', exact: true }).click(); return
    case 'open-history':
      await page.getByRole('tablist', { name: 'Ribbon', exact: true }).getByRole('tab', { name: 'View', exact: true }).click()
      await page.getByRole('toolbar', { name: 'Drafting tools', exact: true }).getByRole('button', { name: 'History', exact: true }).click()
      await expect(page.getByRole('dialog', { name: 'Version history', exact: true })).toBeVisible()
      return
    case 'slash-menu': {
      const bar = page.getByRole('combobox', { name: 'Command bar', exact: true })
      await bar.fill(`/${recipe.command}`)
      await expect(page.getByRole('listbox', { name: 'Tool commands', exact: true })).toBeVisible()
      return
    }
    case 'open-route':
      await page.getByRole('combobox', { name: 'Command bar', exact: true }).fill(recipe.tool)
      await page.getByRole('combobox', { name: 'Command bar', exact: true }).press('Enter')
      await expect(page.getByRole('button', { name: `Run ${recipe.tool}`, exact: true })).toBeVisible()
      return
    case 'catalog-tool': {
      const response = await page.request.get('/api/capabilities')
      expect(response.ok()).toBe(true)
      const catalog = await response.json()
      const tool = catalog.families.flatMap((family) => family.capabilities).find((tool) => tool.name === recipe.name)
      evidence.catalog = { requestedTool: recipe.name, response: catalog }
      if (!tool) await unsupported(probe, runtime, `The isolated catalog does not provide ${recipe.name}`)
      // Unplaced catalog families live on Manage, not the engine's Draw tab.
      const tab = toolPlacementTab(tool) || 'manage'
      await setupStep(probe, runtime, { kind: 'ribbon-tab', name: tab[0].toUpperCase() + tab.slice(1) })
      return
    }
    case 'foreign-checkout': {
      const response = await page.request.post(`/api/drawings/${runtime.drawingId}/checkout`, { data: { holder: 'walk-other-editor', ttl_s: 300 } })
      expect(response.status()).toBe(200)
      expect((await response.json()).acquired).toBe(true)
      await page.reload()
      await expect(page.getByText('Editing locked by walk-other-editor', { exact: false })).toBeVisible()
      if (runtime.ribbonTab) await setupStep(probe, runtime, { kind: 'ribbon-tab', name: runtime.ribbonTab })
      return
    }
    case 'private-policy': {
      const path = stack.env.LEAF_ENTITLEMENTS_FILE
      expect(path, 'the policy fixture must stay in this worker private stack root').toContain(stack.root)
      const original = await readFile(path, 'utf8')
      const policy = JSON.parse(original)
      policy.demo[recipe.capability] = recipe.allowed
      runtime.cleanup.push(() => writeFile(path, original))
      await writeFile(path, JSON.stringify(policy))
      const response = await page.request.get('/api/entitlements')
      expect(response.ok()).toBe(true)
      expect((await response.json()).entitlements[recipe.capability]).toBe(recipe.allowed)
      await page.reload()
      if (runtime.ribbonTab) await setupStep(probe, runtime, { kind: 'ribbon-tab', name: runtime.ribbonTab })
      return
    }
    case 'create-line': await createLine(probe, runtime); return
    case 'hold-engine-edit': {
      await engineReady(probe, runtime)
      await page.evaluate(() => { globalThis.__walkEngineTransport.hold = true })
      const bar = page.getByRole('combobox', { name: 'Command bar', exact: true })
      await bar.fill('LINE'); await bar.press('Enter')
      await page.getByLabel('ribbon x', { exact: true }).fill('0,0')
      const end = page.getByLabel('ribbon x2', { exact: true })
      await end.fill('10,0'); await end.press('Enter')
      // A real edit has been sent. Only its genuine worker replies are held.
      await expect.poll(() => page.evaluate(() => globalThis.__walkEngineTransport.pending.length)).toBeGreaterThan(0)
      return
    }
    case 'start-pending-run': {
      const catalogResponse = await page.request.get('/api/capabilities')
      expect(catalogResponse.ok()).toBe(true)
      const catalog = await catalogResponse.json()
      const tool = catalog.families.flatMap((family) => family.capabilities).find((tool) => tool.name === 'count-by-layer')
      if (!tool) await unsupported(probe, runtime, 'The local catalog has no count-by-layer read tool to establish a pending run')
      const previousTab = runtime.ribbonTab
      await setupStep(probe, runtime, { kind: 'catalog-tool', name: tool.name })
      let release
      const gate = new Promise((resolve) => { release = resolve })
      const pattern = '**/api/jobs/**'
      const hold = async (route) => { await gate; await route.continue() }
      await page.route(pattern, hold)
      runtime.cleanup.push(async () => { release(); await page.unroute(pattern, hold) })
      await page.getByRole('toolbar', { name: 'Drafting tools', exact: true }).getByRole('button', { name: tool.name, exact: true }).click()
      const submitted = page.waitForResponse((response) => response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/run')
      await page.getByRole('button', { name: `Run ${tool.name}`, exact: true }).click()
      expect((await submitted).status()).toBe(202)
      if (previousTab) await setupStep(probe, runtime, { kind: 'ribbon-tab', name: previousTab })
      return
    }
    case 'require-authoring-off': {
      const response = await page.request.get('/api/entitlements')
      expect(response.ok()).toBe(true)
      const entitlement = await response.json()
      evidence.entitlements = entitlement
      if (entitlement.availability?.author_stage !== false) {
        await unsupported(probe, runtime, 'The local provider reports author_stage enabled; startStack has no per-worker author-stage override')
      }
      return
    }
    case 'fresh-history':
      // The upload is new for this test. Neither an earlier test's edit nor a
      // reused demo head is allowed to establish the no-undo/no-redo state.
      expect(await engineCount(page)).toBeGreaterThan(0)
      return
    case 'undo-edit':
      await page.getByRole('toolbar', { name: 'Quick access', exact: true }).getByRole('button', { name: 'Undo', exact: true }).click()
      return
    case 'preview-version': {
      await setupStep(probe, runtime, { kind: 'open-history' })
      const history = page.getByRole('dialog', { name: 'Version history', exact: true })
      const version = history.getByRole('button', { name: /^v\d+\b/ }).first()
      await expect(version).toBeVisible()
      await version.click()
      await expect(page.getByText(/Viewing v\d+/).first()).toBeVisible()
      await history.getByRole('button', { name: 'Close version history', exact: true }).click()
      return
    }
    case 'zoom-before-fit': {
      runtime.homeViewport = await viewportBounds(page)
      const zoom = control(page, recipe.control)
      // The overview clips at drawing extents. Zoom until the camera
      // rectangle changes, so a clipped first step cannot be false evidence.
      for (let attempt = 0; attempt < 12; attempt++) {
        await zoom.click()
        try {
          await expect.poll(async () => {
            const current = await viewportBounds(page)
            return Math.max(...['x', 'y', 'width', 'height'].map((key) => Math.abs(current[key] - runtime.homeViewport[key])))
          }, { timeout: 1000 }).toBeGreaterThan(0.2)
          break
        } catch (error) { if (attempt === 11) throw error }
      }
      evidence.fitViewport = { home: runtime.homeViewport, zoomed: await viewportBounds(page) }
      return
    }
    case 'require-engine-state':
      if (!await page.getByTestId('cad-edit-workbench').count()) await unsupported(probe, runtime, 'No browser editing engine is mounted to test its boot state')
      if (await page.getByTestId('cad-edit-entity-count').count()) await expect(page.getByTestId('cad-edit-entity-count')).toHaveText('0')
      return
    case 'crash-engine-worker': {
      const worker = page.workers().find((worker) => /worker-browser/.test(worker.url()))
      if (!worker) await unsupported(probe, runtime, 'No browser engine worker is mounted to exercise its crash recovery')
      // Real worker failure, never a manufactured disabled attribute.
      await worker.evaluate(() => { setTimeout(() => { throw new Error('walk fixture: engine crash') }, 0) })
      return
    }
    case 'require-surface-context': {
      const response = await page.request.get('/api/health')
      expect(response.ok()).toBe(true)
      const health = await response.json()
      evidence.health = health
      if (recipe.context.apsLive === true && health.aps_live !== true && recipe.context.hasDrawing !== false && recipe.context.sessionActive !== false) {
        await unsupported(probe, runtime, 'The surface ready state requires APS_LIVE; the isolated local stack reports execution paused')
      }
      return
    }
    case 'require-local-state':
      // No synthetic React state, mocked response or silent ready-state
      // fallback may stand in for a registry context. Keep this coverage gap
      // red until a public setup/fault fixture for the named state is supplied.
      await unsupported(probe, runtime, `The isolated stack has no public fixture recipe for ${recipe.state}; required context ${JSON.stringify(recipe.context)}`)
      return
    default: throw new Error(`Unknown walk setup step: ${recipe.kind}`)
  }
}

async function captureBefore(probe, runtime) {
  const { page } = runtime
  const target = probe.assertion.target || ''
  if (target.startsWith('viewer-')) return { viewport: await viewportBounds(page) }
  if (target === 'properties-pane') return { visible: await page.getByRole('complementary', { name: 'Properties', exact: true }).isVisible() }
  if (target === 'version-history') return { visible: await page.getByRole('dialog', { name: 'Version history', exact: true }).isVisible() }
  if (target.startsWith('engine:') || target === 'browser-clipboard-cut') return { count: await engineCount(page) }
  if (target.startsWith('drawing-version-')) {
    const response = await page.request.get(`/api/drawings/${runtime.drawingId}/versions`)
    expect(response.ok()).toBe(true)
    return { versions: await response.json(), count: await engineCount(page).catch(() => null) }
  }
  return {}
}

async function activate(probe, runtime, locator) {
  const { page } = runtime
  const recipe = probe.locator
  if (recipe.trigger === 'keyboard') {
    if (recipe.key === 'Mod+K') await page.keyboard.press('ControlOrMeta+k')
    else {
      // Focus a real non-text control without clicking outside a drawer.
      // That click could close it before the keyboard action under test.
      await page.getByRole('tablist', { name: 'Workspace profile', exact: true }).getByRole('tab').first().focus()
      await page.keyboard.press(recipe.key)
    }
  } else if (recipe.trigger === 'navigate') await page.goto(recipe.url)
  else if (recipe.trigger === 'select') {
    const choices = await locator.locator('option').evaluateAll((options) => options.map((option) => option.value))
    const current = await locator.inputValue()
    const choice = choices.find((value) => value !== current && value !== 'index...')
    expect(choice, 'a property change must offer another real value').toBeDefined()
    await locator.selectOption(choice)
  } else await locator.click()
}

async function assertEffect(probe, runtime, locator, before) {
  const { page } = runtime
  const effect = probe.assertion
  const target = effect.target || ''
  if (effect.kind === 'disabled_with_reason') {
    await expect(locator).toBeDisabled()
    if (runtime.failedDrawing) {
      const name = await locator.getAttribute('aria-label')
      const variant = probe.locator.disabledVariants.find((variant) => variant.name === name)
      expect(variant, 'Rendered disabled control must expose a registry reason').toBeTruthy()
      await expect(locator).toHaveAccessibleName(variant.name)
      await expect(locator).toHaveAttribute('title', new RegExp(variant.reason.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
      runtime.evidence.failedDrawing.renderedReason = variant.reason
      runtime.evidence.failedDrawing.renderedReasonCode = variant.reason_code
      return
    }
    // The accessible name is user-facing evidence, unlike a data-reason-code
    // alone. Exact registry text must also remain on the native tooltip.
    await expect(locator).toHaveAccessibleName(new RegExp(effect.reason.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
    await expect(locator).toHaveAttribute('title', new RegExp(effect.reason.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
    return
  }
  if (effect.kind === 'navigates') { await expect(page).toHaveURL(target); return }
  if (target === 'command-bar-focus') { await expect(locator).toBeFocused(); return }
  if (target === 'cockpit-prompt') {
    await expect(page.getByTestId('cockpit-prompt')).toBeVisible()
    await expect(page.getByTestId('cockpit-prompt')).toHaveAccessibleName(`${effect.verb} command`)
    return
  }
  if (target === 'command-menu') { await expect(page.getByRole('listbox', { name: 'Tool commands', exact: true })).toBeVisible(); return }
  if (target === 'mounted-mcp-servers') { await expect(page.getByRole('status', { name: 'Mounted MCP servers', exact: true })).toBeVisible(); return }
  if (target === 'keyboard-shortcut-sheet') { await expect(page.getByRole('dialog', { name: 'Keyboard shortcuts', exact: true })).toBeVisible(); return }
  if (target === 'pending-property-change') { await expect(page.getByTestId('property-apply-strip')).toBeVisible(); return }
  if (target === 'catalog-run-decision') {
    await expect(page.getByRole('button', { name: `Run ${effect.tool}`, exact: true })).toBeVisible()
    expect(runtime.evidence.responses.filter((response) => response.method === 'POST' && new URL(response.url).pathname === '/api/run')).toHaveLength(0)
    return
  }
  if (target === 'author-panel') { await expect(page.getByRole('textbox', { name: /describe|build|tool/i }).first()).toBeVisible(); return }
  if (target === 'properties-pane') {
    await expect(page.getByRole('complementary', { name: 'Properties', exact: true }))[before.visible ? 'toBeHidden' : 'toBeVisible']()
    return
  }
  if (target === 'version-history') {
    await expect(page.getByRole('dialog', { name: 'Version history', exact: true }))[before.visible ? 'toBeHidden' : 'toBeVisible']()
    return
  }
  if (target.startsWith('drawer:')) {
    if (target === 'drawer:nav') {
      const open = effect.value !== 'none'
      await expect(page.locator('aside.nav'))[open ? 'toBeVisible' : 'toBeHidden']()
      if (runtime.testInfo.project.name === 'phone') {
        await expect(page.getByRole('button', { name: 'Tool rail', exact: true })).toHaveAttribute('aria-expanded', String(open))
      } else if (open) await expect(page.getByRole('button', { name: 'Collapse the tool rail to a spine', exact: true })).toBeVisible()
      else {
        await expect(page.getByRole('button', { name: 'Tool rail', exact: true })).toHaveAttribute('aria-expanded', 'false')
        await expect(page.locator('aside.nav')).toHaveAttribute('aria-hidden', 'true')
      }
      return
    }
    const name = { nav: 'Catalog', jobs: 'Jobs', result: 'Result', plan: 'Plan' }[target.slice(7)]
    if (target === 'drawer:none' || effect.value === 'none') {
      for (const label of ['Catalog', 'Jobs', 'Result', 'Plan']) await expect(panelButton(page, label)).toHaveAttribute('aria-expanded', 'false')
    } else await expect(panelButton(page, name)).toHaveAttribute('aria-expanded', 'true')
    return
  }
  if (target.startsWith('escape:')) {
    const rung = target.slice(7)
    if (rung === 'drawer') {
      for (const label of ['Catalog', 'Jobs', 'Result', 'Plan']) await expect(panelButton(page, label)).toHaveAttribute('aria-expanded', 'false')
    } else if (rung === 'history') await expect(page.getByRole('dialog', { name: 'Version history', exact: true })).toBeHidden()
    else if (rung === 'start') await expect(page.getByRole('region', { name: 'Project workspace', exact: true })).toBeHidden()
    else if (rung === 'route') await expect(page.getByRole('button', { name: /^Run / })).toHaveCount(0)
    else if (rung === 'selection') await expect(page.getByTestId('dock-properties')).toHaveCount(0)
    else throw new Error(`No observable Escape oracle for ${rung}`)
    return
  }
  if (target.startsWith('viewer-')) {
    if (target === 'viewer-home') {
      // CadOverview rounds SVG coordinates to tenths of an overview unit.
      await expect.poll(async () => {
        const current = await viewportBounds(page)
        return Math.max(...['x', 'y', 'width', 'height'].map((key) => Math.abs(current[key] - runtime.homeViewport[key])))
      }).toBeLessThanOrEqual(0.2)
      runtime.evidence.fitViewport.fitted = await viewportBounds(page)
    }
    else {
      const direction = target === 'viewer-zoom-in' ? 'toBeLessThan' : 'toBeGreaterThan'
      await expect.poll(async () => {
        const viewport = await viewportBounds(page)
        return viewport.width * viewport.height
      })[direction](before.viewport.width * before.viewport.height)
    }
    return
  }
  if (target.startsWith('ribbon:')) {
    await expect(locator).toHaveAttribute('aria-selected', 'true')
    await expect(page.getByRole('toolbar', { name: 'Drafting tools', exact: true })).toBeVisible()
    return
  }
  if (target.startsWith('surface:')) {
    const surface = PRODUCT_SURFACES.find((surface) => surface.id === probe.sourceId)
    if (surface.contract.chrome.tab) {
      await expect(locator).toHaveAttribute('aria-selected', 'true')
      await expect(locator).toContainText(effect.label)
      await expect(locator.locator('[data-state]')).toHaveAttribute('data-state', effect.state)
    }
    if (effect.ground === 'drawing' && effect.state !== 'setup' && effect.state !== 'sign-in') await expect(canvas(page)).toBeVisible()
    else if (effect.ground === 'board') await expect(page.getByRole('region', { name: 'Project workspace', exact: true })).toBeVisible()
    else if (effect.ground === 'sheet') { await expect(page).toHaveURL(/\/sheets$/); await expect(page.getByRole('main')).toBeVisible() }
    return
  }
  if (target === 'browser-clipboard') {
    await expect(page.getByRole('group', { name: 'Clipboard', exact: true }).getByRole('button', { name: 'Paste', exact: true })).toBeEnabled()
    return
  }
  if (target.startsWith('drawing-version-')) {
    if (before.count !== null) {
      await expect.poll(() => engineCount(page)).not.toBe(before.count)
    } else {
      await expect.poll(async () => {
        const response = await page.request.get(`/api/drawings/${runtime.drawingId}/versions`)
        expect(response.ok()).toBe(true)
        return (await response.json()).head
      }).not.toBe(before.versions.head)
    }
    return
  }
  if (target.startsWith('engine:') || target === 'browser-clipboard-cut') {
    // Immediate engine operations must commit geometry, not merely leave
    // their button enabled or open an unrelated panel.
    await expect.poll(() => engineCount(page)).not.toBe(before.count)
    return
  }
  throw new Error(`No effect oracle for ${probe.featureId}/${probe.state}: ${effect.kind} ${target}`)
}

export async function runProbe(probe, runtime) {
  const { page, evidence } = runtime
  evidence.featureId = probe.featureId
  evidence.state = probe.state
  evidence.fixtureRecipe = probe.setup
  evidence.expectedEffect = probe.assertion
  evidence.assertionId = probe.assertion.assertionId
  runtime.cleanup = []
  try {
    for (const recipe of probe.setup.steps) {
      await test.step(`Setup: ${recipe.kind}`, async () => {
        await setupStep(probe, runtime, recipe)
        evidence.steps.push({ phase: 'setup', ...recipe })
      })
    }
    const locator = control(page, runtime.testInfo.project.name === 'phone' && probe.locator.phone ? probe.locator.phone : probe.locator)
    const failedDrawingGroup = runtime.failedDrawing && probe.locator.group
      ? page.getByRole('toolbar', { name: 'Drafting tools', exact: true })
        .getByRole('group', { name: groupNames[probe.locator.group], exact: true, includeHidden: true }) : null
    if (runtime.failedDrawing && (runtime.ribbonAbsent || (failedDrawingGroup && await failedDrawingGroup.count() === 0))) {
      await test.step(probe.assertion.assertionId, async () => {
        expect(probe.assertion.kind).toBe('disabled_with_reason')
        if (runtime.ribbonAbsent) {
          await expect(page.getByRole('tablist', { name: 'Ribbon', exact: true })).toHaveCount(0)
          await expect(page.getByRole('toolbar', { name: 'Drafting tools', exact: true })).toHaveCount(0)
        } else await expect(failedDrawingGroup).toHaveCount(0)
        // Check the action globally as well: an absent scope alone would
        // make every scoped locator empty, even if the action moved elsewhere.
        await expect(page.getByRole(probe.locator.role, { name: probe.locator.unavailableName, includeHidden: true })).toHaveCount(0)
      })
      evidence.failedDrawing.actionAvailability = 'unavailable_not_rendered'
      evidence.result = { result: 'passed', featureId: probe.featureId, state: probe.state }
      return
    }
    await expect(locator).toBeVisible()
    const before = await captureBefore(probe, runtime)
    if (probe.assertion.kind !== 'disabled_with_reason') {
      await test.step(`Activate ${probe.featureId}`, () => activate(probe, runtime, locator))
    }
    await test.step(probe.assertion.assertionId, () => assertEffect(probe, runtime, locator, before))
    if (runtime.failedDrawing) evidence.failedDrawing.actionAvailability = 'disabled_with_reason'
    evidence.result = { result: 'passed', featureId: probe.featureId, state: probe.state }
  } catch (error) {
    evidence.failure = { message: error.message, assertionId: probe.assertion.assertionId }
    throw error
  } finally {
    for (const cleanup of runtime.cleanup.reverse()) await cleanup()
  }
}

// This reporter is colocated to keep all runner support inside the owned set.
// Expected failures carrying walk-certification are exclusions, never passes.
export class WalkIntegrityReporter {
  onBegin(config, suite) {
    this.selected = suite.allTests().length
    this.skipped = suite.allTests().some((test) => test.expectedStatus === 'skipped')
    this.queued = false
    this.invalidWorkers = config.workers > config.metadata.walk.admittedSlots
    if (this.invalidWorkers) throw new Error('Walk workers exceed the admitted slot count')
  }
  onTestEnd(test, result) {
    if (result.status === 'skipped' || test.expectedStatus === 'skipped') this.skipped = true
    if (result.errors.some((error) => /QUEUED:/.test(error.message || ''))) this.queued = true
  }
  onError(error) { if (/QUEUED:/.test(error.message || '')) this.queued = true }
  onEnd(result) {
    if (!this.selected || this.skipped || this.invalidWorkers || this.queued) {
      process.stderr.write(this.queued ? 'QUEUED: Local stack admission changed before launch\n'
        : `WALK_INTEGRITY: selected=${this.selected || 0}, skipped=${!!this.skipped}\n`)
      return { status: 'failed' }
    }
    return { status: result.status }
  }
  onExit() { if (this.queued) process.exitCode = 75 }
}

// Playwright constructs a reporter with `new`, but calls a setup hook as a
// function. Sharing this entry point avoids an extra unowned support file.
// Build once in the parent: otherwise two cold workers can race over dist.
export default function walkSupport(config) {
  if (new.target) return new WalkIntegrityReporter()
  if (config.workers > config.metadata.walk.admittedSlots) throw new Error('Walk workers exceed the admitted slot count')
  if (config.projects.some((project) => project.retries !== 0)) throw new Error('Walk retries must remain zero')
  return prepareProductionBundle().then(() => undefined)
}
