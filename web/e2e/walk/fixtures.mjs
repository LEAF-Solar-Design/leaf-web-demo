import { test as base, expect } from '@playwright/test'
import { startStack } from '../../walk/stack.mjs'
import { prepareProductionBundle } from '../../walk/sameOriginProxy.mjs'
import { readFile, writeFile } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import { basename, join } from 'node:path'
import { PRODUCT_SURFACES } from '../../src/site/productSurfaces.js'
import { toolPlacementTab } from '../../src/lib/toolRecord.js'
import { ACTIONS, accessibleName } from '../../src/lib/actionRegistry.js'
import { normalizedControlKey } from './probes.mjs'
import { buildDrawingObjectIndex } from '../../src/lib/drawingObjectIndex.js'
import { collectProbeUxEvidence, packUxEvidence } from './uxEvidence.mjs'
import { seedParams, runBody } from '../solarGraphCommitProof.mjs'

export { expect }
export const LOCAL_IDENTITY = Object.freeze({ tenant: 'demo-tenant', token: 'j1-presentation-fixture' })
export const COACH_STORAGE_KEY = 'leaf.coach.dismissed.v1'

export function injectWalkEntities(source) {
  // Walk selection targets stay outside the panel polygon in the pinned corpus fixture.
  const lines = [190, 470].map((y, index) => `0\nLINE\n5\nA10${index}\n8\nWalk\n10\n111\n20\n${y}\n11\n333\n21\n${y}\n`).join('')
  const dimension = '0\nDIMENSION\n5\nD100\n8\nWalk\n100\nAcDbEntity\n100\nAcDbDimension\n70\n32\n3\nStandard\n10\n333\n20\n160\n30\n0\n11\n222\n21\n160\n31\n0\n100\nAcDbAlignedDimension\n13\n111\n23\n175\n33\n0\n14\n333\n24\n175\n34\n0\n100\nAcDbRotatedDimension\n50\n0\n'
  return source.replace('0\nENDSEC\n0\nEOF', `${lines}${dimension}0\nENDSEC\n0\nEOF`)
}

export async function holdJobRoutes(page, pattern = '**/api/jobs/**') {
  let release
  let passing = false
  let cleanupPromise
  const gate = new Promise((resolve) => { release = resolve })
  const active = new Set()
  const errors = []
  const hold = (route) => {
    const continuation = (async () => {
      if (!passing) await gate
      await route.continue()
    })()
    active.add(continuation)
    continuation.then(() => active.delete(continuation), (error) => {
      errors.push(error)
      active.delete(continuation)
    })
    return continuation
  }
  await page.route(pattern, hold)
  return () => {
    cleanupPromise ||= (async () => {
      passing = true
      release()
      while (active.size) await Promise.allSettled([...active])
      await page.unroute(pattern, hold)
      while (active.size) await Promise.allSettled([...active])
      if (errors.length) throw errors[0]
    })()
    return cleanupPromise
  }
}

export function stackInstanceRef(root, launcherPid, bootTimeMs) {
  return createHash('sha256').update(JSON.stringify([basename(root), launcherPid, bootTimeMs])).digest('hex')
}

export async function openHistory(probe, runtime, assertions = expect) {
  const { page } = runtime
  const expect = assertions
  await setupStep(probe, runtime, { kind: 'ribbon-tab', name: 'View' })
  await page.getByRole('toolbar', { name: 'Drafting tools', exact: true }).getByRole('button', {
    name: accessibleName(ACTIONS.find((action) => action.id === 'history').label), exact: true,
  }).click({ timeout: 15_000 })
  await expect(page.getByRole('dialog', { name: 'Version history', exact: true })).toBeVisible()
  return
}

export async function startPendingRun(probe, runtime, assertions = expect) {
  const { page, evidence } = runtime
  const expect = assertions
  const catalogResponse = await page.request.get('/api/capabilities')
  expect(catalogResponse.ok()).toBe(true)
  const catalog = await catalogResponse.json()
  const tool = catalog.families.flatMap((family) => family.capabilities).find((tool) => tool.name === 'count-by-layer')
  if (!tool) await unsupported(probe, runtime, 'The local catalog has no count-by-layer read tool to establish a pending run')
  const previousTab = runtime.ribbonTab
  const previousPanelName = runtime.catalogPanelName
  try {
    await setupStep(probe, runtime, { kind: 'catalog-tool', name: tool.name })
    runtime.cleanup.push(await holdJobRoutes(page))
    await page.getByRole('toolbar', { name: 'Drafting tools', exact: true }).getByRole('button', { name: tool.name, exact: true }).click({ timeout: 15_000 })
    const submitted = page.waitForResponse((response) => response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/run', { timeout: 15_000 })
    // Observe a rejection even if the click itself fails first.
    submitted.catch(() => {})
    await page.getByRole('button', { name: `Run ${tool.name}`, exact: true }).click({ timeout: 15_000 })
    const response = await submitted
    expect(response.status()).toBe(202)
    const receipt = await response.json()
    expect(receipt.job_id).toBeTruthy()
    await expect(page.locator('.strip-running')).toBeVisible({ timeout: 15_000 })
    evidence.pendingRun = { tool: tool.name, jobId: receipt.job_id, status: response.status(), pendingVisible: true }
    if (previousTab) await setupStep(probe, runtime, { kind: 'ribbon-tab', name: previousTab })
  } finally {
    runtime.catalogPanelName = previousPanelName
  }
  return
}

export async function previewVersion(probe, runtime, assertions = expect, workspace = requireWorkspace) {
  const { page, evidence } = runtime
  const expect = assertions
  const previousTab = runtime.ribbonTab
  const path = `/api/drawings/${runtime.drawingId}`
  const versions = await page.request.get(`${path}/versions`, { timeout: 15_000 })
  expect(versions.ok()).toBe(true)
  const original = await versions.json()
  const preview = Number(original.head)
  expect(Number.isInteger(preview)).toBe(true)
  const restored = await page.request.post(`${path}/versions/${preview}/restore`, { timeout: 15_000 })
  expect(restored.ok()).toBe(true)
  const receipt = await restored.json()
  const head = Number(receipt.new_version.version)
  expect(head).toBeGreaterThan(preview)
  expect(receipt.head).toBe(head)
  const verified = await page.request.get(`${path}/versions`, { timeout: 15_000 })
  expect(verified.ok()).toBe(true)
  const chain = await verified.json()
  expect(chain.head).toBe(head)
  expect(chain.versions.some((row) => Number(row.v) === preview)).toBe(true)
  expect(chain.versions.some((row) => Number(row.v) === head)).toBe(true)
  // Seat the new head in the product before asking it to preview the old one.
  await page.reload({ timeout: 60_000 })
  await workspace(runtime)
  await openHistory(probe, runtime, assertions)
  const history = page.getByRole('dialog', { name: 'Version history', exact: true })
  const version = history.getByRole('button', { name: new RegExp(`^v${preview}\\b`) })
  await expect(version).toBeVisible()
  await version.click({ timeout: 15_000 })
  await expect(page.getByText(new RegExp(`Viewing v${preview}\\b.*read-only preview`))).toBeVisible()
  evidence.versionPreview = { head, preview, restoredFrom: receipt.restored_from }
  runtime.drawingVersion = preview
  await history.getByRole('button', { name: 'Close version history', exact: true }).click({ timeout: 15_000 })
  await expect(history).toBeHidden()
  if (previousTab) await setupStep(probe, runtime, { kind: 'ribbon-tab', name: previousTab })
  return
}

export const test = base.extend({
  firstRun: [false, { option: true }],
  workerFacts: [async ({}, use) => {
    await use({ engineMounted: undefined })
  }, { scope: 'worker' }],
  workerStack: [async ({}, use, workerInfo) => {
    let stack
    let bootError
    let witness
    try {
      // workerIndex also distinguishes replacement workers after a failure.
      try {
        stack = await startStack({ slot: workerInfo.workerIndex, slots: workerInfo.config.workers })
        const bootTimeMs = Date.now()
        const readiness = JSON.parse(await readFile(join(stack.root, 'ready.json'), 'utf8'))
        witness = { ready: true, instance: stackInstanceRef(stack.root, readiness.launcher_pid, bootTimeMs) }
      } catch (error) {
        bootError = error.code === 'QUEUED' ? new Error(`QUEUED: ${error.message}`, { cause: error }) : error
      }
      await use({ stack, bootError, witness })
    } finally {
      if (stack) await stack.stop()
    }
  }, { scope: 'worker', timeout: 300_000 }],
  stack: async ({ workerStack, walkEvidence }, use) => {
    walkEvidence.stack = workerStack.witness || { ready: false }
    if (workerStack.bootError) throw workerStack.bootError
    await use(workerStack.stack)
  },
  baseURL: async ({ stack }, use) => { await use(stack.baseURL) },
  extraHTTPHeaders: async ({}, use) => {
    await use({ 'X-Tenant-Id': LOCAL_IDENTITY.tenant })
  },
  walkEvidence: [async ({ workerFacts }, use, testInfo) => {
    const evidence = {
      schema: 'leaf.walk-evidence.v1', test: testInfo.title, viewport: testInfo.project.name,
      consoleErrors: [], pageErrors: [], failedRequests: [], abortedRequests: [], httpErrors: [],
      responses: [], steps: [], accessibility: null,
      startedAt: new Date().toISOString(),
    }
    // Share observations with existing callers without serializing mutable facts.
    Object.defineProperty(evidence, 'workerFacts', { value: workerFacts })
    try { await use(evidence) } finally {
      evidence.finishedAt = new Date().toISOString()
      await testInfo.attach('walk-evidence', {
        body: Buffer.from(JSON.stringify(evidence, null, 2)), contentType: 'application/json',
      })
    }
  }, { auto: true }],
  page: async ({ page, stack, firstRun, walkEvidence }, use) => {
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

function catalogSha256(catalog) {
  return createHash('sha256').update(JSON.stringify(catalog)).digest('hex')
}

const groupNames = { draw: 'Draw', modify: 'Modify', clipboard: 'Clipboard', properties: 'Properties',
      annotation: 'Annotation', block: 'Block', groups: 'Groups', 'solar-panels': 'Panel placement',
      view: 'View', version: 'Version', author: 'Author', rail: 'Rail' }
const control = (page, recipe) => {
  let scope = recipe.scope ? control(page, recipe.scope) : page
  if (recipe.panelName) scope = scope.getByRole('group', { name: recipe.panelName, exact: true })
  if (recipe.group && recipe.role !== 'combobox') {
    scope = scope.getByRole('group', { name: groupNames[recipe.group], exact: true })
  }
  return scope.getByRole(recipe.role, { name: recipe.name, exact: recipe.exact !== false })
}

export async function discloseControlPanel(page, recipe) {
  const name = recipe.panelName || groupNames[recipe.group]
  if (!name) return
  const toolbar = page.getByRole('toolbar', { name: 'Drafting tools', exact: true })
  const panel = toolbar.getByRole('group', { name, exact: true, includeHidden: true })
  if (await panel.count() === 0 || await panel.isVisible()) return
  const more = toolbar.getByRole('button', { name: 'More panels', exact: true })
  if (await more.isVisible() && await more.getAttribute('aria-expanded') === 'false') {
    await more.click({ timeout: 15_000 })
  }
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
const viewButton = (page, name) => page.getByRole('toolbar', { name: 'View', exact: true })
  .getByRole('button', { name, exact: true })
const layerButtons = (page) => page.getByRole('toolbar', { name: 'Drafting tools', exact: true })
  .getByRole('group', { name: 'Layers', exact: true }).getByRole('button')
const propertiesPane = (page) => page.getByRole('complementary', { name: 'Properties', exact: true })
const propertiesSection = (page, name) => propertiesPane(page).getByRole('button', { name, exact: true })
  .locator('xpath=parent::section')
const legendRow = (page, name) => propertiesPane(page).getByRole('button', {
  name: new RegExp('^' + name + ' [0-9][0-9,]*$'),
})
const ribbonLayer = (page, name) => layerButtons(page).and(page.getByRole('button', { name, exact: true }))
export async function requireNoDrawing(page, assertions = expect) {
  // Solar can render a starter canvas without a seated, versioned drawing.
  const undo = page.getByRole('toolbar', { name: 'Quick access', exact: true }).getByRole('button', {
    name: 'Undo version (unavailable: no versioned drawing)', exact: true,
  })
  await assertions(undo).toBeVisible()
  await assertions(undo).toBeDisabled()
}
async function requireWorkspace(runtime) {
  const { page } = runtime
  await expect(page.getByRole('combobox', { name: 'Command bar', exact: true })).toBeVisible()
  if (runtime.failedDrawing) {
    await expect(page.getByRole('alert').filter({ hasText: /Couldn['’]t load drawing/ })).toBeVisible()
    await requireNoDrawing(page)
  } else await expect(canvas(page)).toBeVisible()
}
async function setSection(page, name, expanded) {
  const section = propertiesSection(page, name)
  const header = section.getByRole('button', { name, exact: true })
  await expect(header).toHaveAttribute('aria-expanded', /^(true|false)$/)
  if ((await header.getAttribute('aria-expanded') === 'true') !== expanded) await header.click()
  await expect(header).toHaveAttribute('aria-expanded', String(expanded))
  if (expanded) await expect(section.locator('.dock-section-body')).toBeVisible()
  else await expect(section.locator('.dock-section-body')).toHaveCount(0)
  return section
}
async function layersShown(page) {
  return propertiesSection(page, 'Drawing').getByTestId('dock-drawing').evaluate((element) => {
    const label = [...element.querySelectorAll('dt')].find((node) => node.textContent === 'Layers shown')
    const value = Number(label?.nextElementSibling?.textContent.replace(/,/g, ''))
    if (!Number.isInteger(value)) throw new Error('Drawing facts need a real Layers shown count')
    return value
  })
}
async function requireLayer(page, name, visible) {
  await expect(ribbonLayer(page, name)).toHaveAttribute('aria-pressed', String(visible))
  const row = legendRow(page, name)
  await expect(row).toHaveCount(1)
  await expect(row).toBeEnabled()
  expect(Number((await row.locator('.legend-count').innerText()).replace(/,/g, ''))).toBeGreaterThan(0)
  await expect.poll(() => row.evaluate((element) => element.classList.contains('off'))).toBe(!visible)
  await expect.poll(() => row.locator('.swatch').evaluate((element) => Number(getComputedStyle(element).opacity))).toBe(visible ? 1 : 0.25)
}
async function setLayer(page, name, visible) {
  const button = ribbonLayer(page, name)
  await expect(button).toHaveAttribute('aria-pressed', /^(true|false)$/)
  if ((await button.getAttribute('aria-pressed') === 'true') !== visible) await button.click()
  await requireLayer(page, name, visible)
}
async function viewState(page) {
  return { viewport: await viewportBounds(page),
    selection: await page.getByTestId('cockpit-status').locator('.cockpit-sel').innerText(),
    layers: await layerButtons(page).evaluateAll((buttons) => buttons.map((button) => ({
      name: button.getAttribute('aria-label') || button.textContent.trim(), pressed: button.getAttribute('aria-pressed'),
    }))) }
}
async function requireGrid(page, on) {
  const grid = page.getByTestId('viewer-grid')
  if (on) {
    await expect(grid).toHaveCount(1)
    await expect.poll(() => grid.evaluate((element) => Number(element.getAttribute('data-grid-step')))).toBeGreaterThan(0)
    await expect.poll(() => grid.evaluate((element) => getComputedStyle(element).backgroundImage)).toContain('linear-gradient')
  } else await expect(grid).toHaveCount(0)
}
async function requireViewport(page, expected) {
  await expect.poll(async () => {
    const current = await viewportBounds(page)
    return Math.max(...['x', 'y', 'width', 'height'].map((key) => Math.abs(current[key] - expected[key])))
  }).toBeLessThanOrEqual(0.2)
}
async function engineCount(page) {
  return Number(await page.getByTestId('cad-edit-entity-count').innerText())
}
export class UnsupportedLocalError extends Error {
  constructor(probe, reason) {
    super(`UNSUPPORTED_LOCAL: ${probe.featureId} [${probe.state}]: ${reason}`)
    this.name = 'UnsupportedLocalError'
    this.reason = reason
  }
}

async function unsupported(probe, runtime, reason) {
  const result = { featureId: probe.featureId, state: probe.state, result: 'unsupported_local',
    declaredCertify: probe.certify, reason, ...runtime.unsupportedAvailability }
  runtime.evidence.result = result
  await runtime.testInfo.attach('walk-result', { body: Buffer.from(JSON.stringify(result)), contentType: 'application/json' })
  throw new UnsupportedLocalError(probe, reason)
}
async function engineReady(probe, runtime) {
  const { page } = runtime
  // A missing build flag is an unavailable local capability, not an engine
  // test that passed because the spec silently returned before its assertions.
  const mounted = !!await page.getByTestId('cad-edit-workbench').count()
  runtime.workerFacts.engineMounted = mounted
  if (!mounted) {
    if (page.request) {
      const response = await page.request.get('/.leaf-walk-build.json')
      if (response.ok()) {
        const marker = await response.json()
        runtime.workerFacts.engineUnavailableReason = marker.engine_unavailable_reason
      }
    }
    const cause = runtime.workerFacts.engineUnavailableReason
    await unsupported(probe, runtime, `The production bundle has no mounted browser editing engine${cause ? ': ' + cause : ''}`)
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
export const FIXTURE_PICK_POINTS = Object.freeze({
  LINE: [[222, 190], [222, 470]], INSERT: [[11.5, 20]], DIMENSION: [[222, 160]],
})

export function exposedCalibrationPoints(element) {
  const box = element.getBoundingClientRect()
  const left = Math.max(0, box.left), top = Math.max(0, box.top)
  const right = Math.min(innerWidth, box.right), bottom = Math.min(innerHeight, box.bottom)
  const points = []
  // Search the visible canvas, including narrow strips left by Solar's panels.
  for (let y = top + 4; y < bottom - 4; y += 8) {
    for (let x = left + 4; x < right - 4; x += 8) {
      if (document.elementFromPoint(x, y) === element) points.push({ x, y })
    }
  }
  const first = points[0]
  const second = points.findLast((point) => first && Math.abs(point.x - first.x) > 20 && Math.abs(point.y - first.y) > 20)
  if (!first || !second) return null
  return [first, second]
}

export function solarCalibrationFailure(probe, recipe, error) {
  return probe.locator?.group === 'solar-panels' && recipe.kind === 'select-entity' && !recipe.viewerOnly
    && error.message === 'The drawing needs two uncovered calibration points'
}

async function selectEntity(probe, runtime, recipe) {
  if (!recipe.viewerOnly) await engineReady(probe, runtime)
  const { page } = runtime
  // Calibrate the flat drawing's projection from its production cursor
  // readout. Selection still uses native mouse input on the real canvas.
  const points = FIXTURE_PICK_POINTS[recipe.type]
  if (!points) await unsupported(probe, runtime, `The private DXF fixture has no ${recipe.type} entity to select`)
  if (recipe.editable === false && recipe.type === 'LINE') {
    await unsupported(probe, runtime, 'The engine exposes the fixture LINE as editable; a read-only LINE needs a provider fixture')
  }
  const samples = await canvas(page).evaluate(exposedCalibrationPoints)
  if (!samples) throw new Error('The drawing needs two uncovered calibration points')
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
  if (recipe.viewerOnly) await expect(page.getByTestId('cockpit-status').locator('.cockpit-sel')).toHaveText(/^sel /)
  else await expect(page.getByTestId('dock-properties')).toBeVisible()
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
const baselineThreePanels = Object.freeze({
  'claude-accounts-panel': { role: 'dialog', name: 'Claude accounts', close: 'Close Claude account panel' },
  'linked-services-panel': { role: 'dialog', name: 'Linked services', close: 'Close linked services panel' },
  'session-provenance': { role: 'dialog', name: 'Session · provenance', close: 'Close details' },
  'version-history': { role: 'dialog', name: 'Version history', close: 'Close version history' },
  'cost-panel': { role: 'region', name: 'What Leaf costs to operate', close: 'Close cost panel' },
})
const namedPanel = (page, spec) => page.getByRole(spec.role, { name: spec.name, exact: true })
async function closeBaselinePanel(page, spec) {
  const panel = namedPanel(page, spec)
  if (await panel.isVisible()) await panel.getByRole('button', { name: spec.close, exact: true }).click()
  await expect(panel).toBeHidden()
}
async function clearComposer(page) {
  const bar = page.getByRole('combobox', { name: 'Command bar', exact: true })
  await bar.fill('')
  await bar.press('Escape')
  await bar.press('Escape')
  await expect(page.getByRole('listbox', { name: 'Scope', exact: true })).toBeHidden()
  await expect(page.getByRole('listbox', { name: 'Tool commands', exact: true })).toBeHidden()
  await expect(page.getByRole('listbox', { name: 'Actions and artifacts', exact: true })).toBeHidden()
  await expect(page.getByRole('listbox', { name: 'Search results', exact: true })).toBeHidden()
  await expect(page.getByRole('button', { name: 'scope ▾', exact: true })).toHaveAttribute('aria-expanded', 'false')
  await expect(bar).toHaveValue('')
  return bar
}
export async function readWalkBuildFlags(probe, runtime) {
  if (!catalogSolarNeedsDrawing(probe)) return
  const facts = runtime.workerFacts
  facts.buildFlagsFetch ||= (async () => {
    const response = await runtime.page.request.get('/.leaf-walk-build.json')
    expect(response.ok(), 'Walk build manifest must be available').toBe(true)
    const marker = await response.json()
    facts.buildFlags = marker.flags || {}
  })()
  await facts.buildFlagsFetch
  await workerCatalog(facts, runtime.page.request)
  runtime.evidence.buildFlags = { ...facts.buildFlags }
}

// A page-specific init script: the ordinary page fixture remains unchanged.
export function seedSignOutIdentity({ identity, coachKey }) {
  if (sessionStorage.getItem('leaf.walk.w1k.identity-seeded') !== '1') {
    localStorage.setItem('leaf.jwt', identity.token)
    localStorage.setItem(coachKey, '1')
    sessionStorage.setItem('leaf.walk.w1k.identity-seeded', '1')
  }
}

async function baselineThreeState(probe, runtime, recipe) {
  const { page } = runtime
  await requireWorkspace(runtime)
  const locator = control(page, recipe.control)
  const target = recipe.target
  const panel = baselineThreePanels[target]
  if (panel) {
    await closeBaselinePanel(page, panel)
    runtime.cleanup.push(() => closeBaselinePanel(page, panel))
  }
  if (recipe.expanded !== undefined) await expect(locator).toHaveAttribute('aria-expanded', String(recipe.expanded))
  if (target === 'project-board') {
    const back = page.getByRole('button', { name: 'Return to drawing', exact: true })
    if (await back.isVisible()) await back.click()
    await expect(page.getByRole('heading', { name: 'Project board', exact: true })).toBeHidden()
    runtime.cleanup.push(async () => {
      if (await back.isVisible()) await back.click()
      await expect(page.getByRole('heading', { name: 'Project board', exact: true })).toBeHidden()
    })
  }
  if (['scope-build-picker', 'scope-picker', 'tool-commands', 'unknown-tool-resolver'].includes(target)) {
    const bar = await clearComposer(page)
    runtime.cleanup.push(async () => { await clearComposer(page) })
    if (target === 'tool-commands') await expect(bar).toHaveAttribute('aria-expanded', 'false')
    if (target === 'unknown-tool-resolver') {
      await expect(page.getByRole('listbox', { name: 'Route resolver', exact: true })).toBeHidden()
      await expect.poll(() => runtime.evidence.responses.some((response) => new URL(response.url).pathname === '/api/capabilities'
        && response.status === 200)).toBe(true)
      const response = await page.request.get('/api/capabilities')
      expect(response.ok()).toBe(true)
      const catalog = await response.json()
      const names = catalog.families.flatMap((family) => family.capabilities.map((tool) => tool.name))
      expect(names.length).toBeGreaterThan(0)
      expect(names).not.toContain('w1k-no-such-tool')
      runtime.evidence.unknownTool = { name: 'w1k-no-such-tool', catalogNames: names }
      await bar.fill('/w1k-no-such-tool')
      await bar.press('Escape')
      await expect(bar).toHaveValue('/w1k-no-such-tool')
      await expect(page.getByRole('listbox', { name: 'Tool commands', exact: true })).toBeHidden()
      await expect(locator).toBeEnabled()
      runtime.localDecisionRequests = []
      const observe = (request) => {
        if (request.method() === 'POST' && /\/api\/(?:.*\/)?(?:route|run)(?:[-/]|$)/.test(new URL(request.url()).pathname)) {
          runtime.localDecisionRequests.push(request.url())
        }
      }
      page.on('request', observe)
      runtime.cleanup.push(async () => {
        page.off('request', observe)
        await page.keyboard.press('Escape')
        await expect(page.getByRole('listbox', { name: 'Route resolver', exact: true })).toBeHidden()
      })
    }
  }
  if (target === 'guided-demo') {
    await expect(page.getByRole('complementary', { name: 'Properties', exact: true })
      .getByRole('definition').filter({ hasText: /^rooftop_demo\.dwg$/ })).toBeHidden()
    await expect(page.getByRole('alert').filter({ hasText: /Couldn['’]t load drawing/ })).toBeVisible()
  }
  if (target === 'session-provenance') {
    await expect(locator).toHaveCount(1)
    await expect(locator).toBeVisible()
    await expect(page.getByRole('dialog', { name: 'Session · provenance', exact: true })).toBeHidden()
  }
  if (target === 'notification-inbox-collapsed') {
    await expect(locator).toBeVisible()
    await expect(locator).toHaveAttribute('aria-expanded', 'true')
    runtime.cleanup.push(async () => {
      const expand = page.getByRole('button', { name: 'Expand the notification inbox', exact: true })
      if (await expand.isVisible()) await expand.click()
      await expect(locator).toHaveAttribute('aria-expanded', 'true')
    })
  }
  if (target === 'edit-lock-held') {
    const release = page.getByRole('button', { name: 'Release', exact: true })
    if (await release.isVisible()) { await expect(release).toBeEnabled(); await release.click() }
    await expect(page.getByText('You hold the edit lock', { exact: true })).toBeHidden()
    await expect(locator).toBeEnabled()
    runtime.cleanup.push(async () => {
      if (await release.isVisible()) { await expect(release).toBeEnabled(); await release.click() }
      await expect(locator).toBeVisible()
    })
  }
  if (target === 'drawing-find-no-match') {
    await expect(locator).toBeVisible()
    const reply = await page.request.get('/api/session?dwg=' + encodeURIComponent(runtime.drawingId))
    expect(reply.ok()).toBe(true)
    const { intake } = await reply.json()
    expect(intake).toBeTruthy()
    const index = buildDrawingObjectIndex({ drawingKey: runtime.drawingId, intake,
      solarGraph: intake.solar_design_graph ? { drawingKey: runtime.drawingId, graph: intake.solar_design_graph } : null })
    expect(index.records.length).toBeGreaterThan(0)
    expect(index.resolve(recipe.control.inputValue).status).toBe('missing')
    await locator.fill('')
    await expect(page.getByRole('status').filter({ hasText: 'No matching object in this drawing.' })).toHaveCount(0)
    await expect(locator).toHaveAttribute('aria-expanded', 'false')
    runtime.cleanup.push(async () => { await locator.fill('') })
  }
  if (target === 'signed-out-session') {
    await expect(locator).toHaveCount(1)
    await expect(locator).toBeVisible()
    await expect(page.getByRole('dialog', { name: 'Session · provenance', exact: true })).toBeHidden()
  }
}

export const HANDLED_SETUP_KINDS = Object.freeze(new Set([
  'fresh-sign-out-page', 'baseline-three-state', 'prepare-engine-transport', 'hold-engine-boot',
  'navigate', 'open-failed-drawing', 'failed-drawing-ribbon-tab', 'open-empty-workspace',
  'open-private-drawing', 'ribbon-tab', 'control-pressed-state', 'fullscreen-state',
  'empty-view-history', 'previous-view-history', 'whole-drawing-view', 'engine-ready',
  'select-entity', 'clear-selection', 'copy-selection', 'drawer-state', 'tool-rail-state',
  'properties-state', 'properties-section-state', 'properties-close-state', 'layer-visible-state',
  'job-monitor-collapsed', 'overview-expanded-state', 'overview-pan-state', 'open-start',
  'open-history', 'slash-menu', 'open-route', 'catalog-tool', 'foreign-checkout', 'private-policy',
  'create-line', 'hold-engine-edit', 'start-pending-run', 'require-authoring-off', 'fresh-history',
  'undo-edit', 'preview-version', 'zoom-before-fit', 'require-engine-state', 'crash-engine-worker',
  'require-surface-context', 'require-local-state',
  'saved-version-history', 'require-versionless-drawing', 'seed-solar-graph',
]))
export const ENGINE_SETUP_KINDS = Object.freeze(new Set([
  'engine-ready', 'select-entity', 'create-line', 'hold-engine-edit',
  'require-engine-state', 'crash-engine-worker',
]))
const localFixtureReason = (recipe) => `The isolated stack has no public fixture recipe for ${recipe.state}; required context ${JSON.stringify(recipe.context)}`

export const TOOL_ARM_EFFECT_KINDS = Object.freeze(new Set(['opens']))
const AVAILABILITY_FIELDS = Object.freeze(['entitled', 'engine_ready', 'input_ready', 'implemented'])

export async function workerCatalog(workerFacts, request, { drawingId, version = 'head' } = {}) {
  const fetch = async () => {
    try {
      delete workerFacts.catalog
      delete workerFacts.catalogError
      const query = drawingId ? `?drawing_id=${encodeURIComponent(drawingId)}&drawing_version=${encodeURIComponent(version)}` : ''
      const response = await request.get(`/api/capabilities${query}`)
      if (!response.ok()) throw new Error(`Catalog request failed: ${response.status()}`)
      const catalog = await response.json()
      if (!Array.isArray(catalog.families)
        || !catalog.families.every((family) => Array.isArray(family.capabilities))) {
        throw new Error('Catalog response has no capability families')
      }
      workerFacts.catalog_sha256 = catalogSha256(catalog)
      workerFacts.catalog = catalog
    } catch (error) { workerFacts.catalogError = error }
    return workerFacts.catalog
  }
  // Scoped readiness can change at the same head after setup or policy edits.
  // Never cache it in the worker or reuse a different drawing's answer.
  if (drawingId) {
    const key = JSON.stringify([drawingId, version])
    workerFacts.catalogInFlight ||= new Map()
    if (!workerFacts.catalogInFlight.has(key)) {
      const pending = fetch().finally(() => workerFacts.catalogInFlight.delete(key))
      workerFacts.catalogInFlight.set(key, pending)
    }
    return workerFacts.catalogInFlight.get(key)
  }
  workerFacts.catalogFetch ||= fetch()
  return workerFacts.catalogFetch
}

export function toolAvailabilityEvidence(probe, workerFacts = {}) {
  if (probe.kind !== 'tool' || !TOOL_ARM_EFFECT_KINDS.has(probe.assertion.kind)
    || probe.assertion.target !== 'catalog-run-decision') return null
  const tool = workerFacts.catalog?.families.flatMap((family) => family.capabilities)
    .find((tool) => tool.name === probe.sourceId)
  if (!tool?.availability || typeof tool.availability !== 'object'
    || Array.isArray(tool.availability)) return null
  const fields = AVAILABILITY_FIELDS.filter((field) => tool.availability[field] !== true)
  if (!fields.length) return null
  return { tool: tool.name, availability_fields_false: fields,
    refusal_codes: Array.isArray(tool.availability.refusal_reasons) ? [...tool.availability.refusal_reasons] : [] }
}

export const UI_UNREACHABLE_STATES = Object.freeze(new Set(['no-versioned-drawing']))
export const VERSIONLESS_DRAWING_REASON = 'The product creates a saved root version for every private drawing and renders the ribbon only with a drawing open, so no-versioned-drawing is unreachable through the UI.'
export const SOLAR_PANEL_CALIBRATION_REASON = 'The private DXF fixture has no two uncovered calibration points for solar panel placement.'
export const CATALOG_SOLAR_FLAG_REASON = 'catalog drawing context requires VITE_SOLAR_SETTINGS_FORM=1; this build has it off'

export function catalogSolarNeedsDrawing(probe) {
  return probe.kind === 'tool' && probe.sourceId?.startsWith('solar-')
    && probe.assertion.kind !== 'disabled_with_reason'
}

export function unsupportedBeforeSetup(probe, workerFacts = {}, checkAvailability = true) {
  if (UI_UNREACHABLE_STATES.has(probe.state)) return VERSIONLESS_DRAWING_REASON
  const tool = workerFacts.catalog?.families.flatMap((family) => family.capabilities)
    .find((tool) => tool.name === probe.sourceId)
  if (catalogSolarNeedsDrawing(probe) && workerFacts.buildFlags
    && workerFacts.buildFlags.VITE_SOLAR_SETTINGS_FORM !== '1'
    && Array.isArray(tool?.availability?.refusal_reasons)
    && tool.availability.refusal_reasons.includes('drawing_context_required')) return CATALOG_SOLAR_FLAG_REASON
  const unavailable = checkAvailability && toolAvailabilityEvidence(probe, workerFacts)
  if (unavailable) {
    const codes = unavailable.refusal_codes.length ? ` (${unavailable.refusal_codes.join(',')})` : ''
    return `The isolated stack cannot run ${unavailable.tool}: ${unavailable.availability_fields_false[0]} is false${codes}`
  }
  for (const recipe of probe.setup.steps) {
    if (!HANDLED_SETUP_KINDS.has(recipe.kind) || recipe.kind === 'require-local-state') {
      return localFixtureReason({ state: probe.state, context: probe.setup.context, ...recipe })
    }
    if (workerFacts.engineMounted === false && ENGINE_SETUP_KINDS.has(recipe.kind)
      && !(recipe.kind === 'select-entity' && recipe.viewerOnly)) {
      const cause = workerFacts.engineUnavailableReason
      return `The production bundle has no mounted browser editing engine${cause ? ': ' + cause : ''}`
    }
  }
  return null
}

export async function setupStep(probe, runtime, recipe, assertions = runtime.recipeAssertions || expect) {
  const { page, stack, evidence } = runtime
  switch (recipe.kind) {
    case 'require-versionless-drawing': {
      assertions(runtime.drawingId).toBeTruthy()
      const response = await page.request.get(`/api/drawings/${runtime.drawingId}/versions`, { timeout: 15_000 })
      assertions(response.ok()).toBe(true)
      const chain = await response.json()
      evidence.versionlessDrawing = { drawingId: runtime.drawingId, chain }
      assertions(chain.versions.length).toBe(0)
      assertions(chain.head).toBe(null)
      await requireWorkspace(runtime)
      const history = ACTIONS.find((action) => action.id === 'history')
      await discloseControlPanel(page, { group: 'version' })
      const button = control(page, { role: 'button', name: accessibleName(history.label,
        history.when({ hasVersions: false })), group: 'version', scope: { role: 'toolbar', name: 'Drafting tools' } })
      await assertions(button).toBeVisible()
      await assertions(button).toBeDisabled()
      evidence.versionlessDrawing.precondition = 'real drawing loaded without saved versions'
      return
    }
    case 'saved-version-history': {
      const path = `/api/drawings/${runtime.drawingId}`
      const response = await page.request.get(`${path}/versions`, { timeout: 15_000 })
      assertions(response.ok()).toBe(true)
      const original = await response.json()
      const restored = await page.request.post(`${path}/versions/${original.head}/restore`, { timeout: 15_000 })
      assertions(restored.ok()).toBe(true)
      const receipt = await restored.json()
      assertions(receipt.head).toBeGreaterThan(original.head)
      if (recipe.redo) {
        const undone = await page.request.post(`${path}/undo`, { timeout: 15_000 })
        assertions(undone.ok()).toBe(true)
      }
      const verified = await page.request.get(`${path}/versions`, { timeout: 15_000 })
      assertions(verified.ok()).toBe(true)
      const chain = await verified.json()
      assertions(chain.versions.length).toBeGreaterThan(1)
      assertions(recipe.redo ? chain.head < chain.latest : chain.head > 1).toBe(true)
      await page.reload({ timeout: 60_000 })
      await requireWorkspace(runtime)
      if (runtime.ribbonTab) await setupStep(probe, runtime, { kind: 'ribbon-tab', name: runtime.ribbonTab })
      evidence.savedVersionHistory = { head: chain.head, latest: chain.latest, versions: chain.versions, redo: recipe.redo }
      return
    }
    case 'fresh-sign-out-page': {
      const ordinaryPage = runtime.page
      const fresh = await ordinaryPage.context().newPage()
      await fresh.addInitScript(seedSignOutIdentity, { identity: LOCAL_IDENTITY, coachKey: COACH_STORAGE_KEY })
      runtime.page = fresh
      runtime.cleanup.push(async () => { await fresh.close(); runtime.page = ordinaryPage })
      return
    }
    case 'baseline-three-state': await baselineThreeState(probe, runtime, recipe); return
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
      runtime.cleanup.push(await holdJobRoutes(page, '**/engine/engine_bg.wasm'))
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
      assertions([400, 404]).toContain(response.status())
      evidence.failedDrawing = { drawingId: 'missing.invalid', status: response.status(), response: await response.json() }
      await assertions(page.getByRole('alert').filter({ hasText: /Couldn['’]t load drawing/ })).toBeVisible()
      await requireNoDrawing(page, assertions)
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
        source = injectWalkEntities(source)
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
      await page.getByRole('tablist', { name: 'Ribbon', exact: true }).getByRole('tab', { name: recipe.name, exact: true }).click({ timeout: 15_000 })
      // Narrow viewports offer the extra panels through a real disclosure.
      { const more = page.getByRole('button', { name: 'More panels', exact: true })
         if (await more.isVisible() && await more.getAttribute('aria-expanded') === 'false') await more.click({ timeout: 15_000 }) }
      runtime.ribbonTab = recipe.name
      return
    case 'control-pressed-state': {
      const button = control(page, recipe.control)
      await expect(button).toHaveAttribute('aria-pressed', /^(true|false)$/)
      if ((await button.getAttribute('aria-pressed') === 'true') !== recipe.pressed) await button.click()
      await expect(button).toHaveAttribute('aria-pressed', String(recipe.pressed))
      await requireGrid(page, recipe.pressed)
      return
    }
    case 'fullscreen-state': {
      const button = control(page, recipe.control)
      runtime.cleanup.push(async () => {
        if (await page.evaluate(() => !!document.fullscreenElement)) {
          await button.click()
          await expect.poll(() => page.evaluate(() => !!document.fullscreenElement)).toBe(false)
        }
      })
      if (await page.evaluate(() => !!document.fullscreenElement) !== recipe.fullscreen) await button.click()
      await expect.poll(() => page.evaluate(() => !!document.fullscreenElement)).toBe(recipe.fullscreen)
      return
    }
    case 'empty-view-history':
      await expect(viewButton(page, 'Back to the previous view')).toHaveAttribute('aria-disabled', 'true')
      runtime.savedView = await viewState(page)
      return
    case 'previous-view-history': {
      await setupStep(probe, runtime, { kind: 'ribbon-tab', name: 'Draw' })
      await selectEntity(probe, runtime, { type: 'LINE', editable: true, viewerOnly: true })
      await expect(page.getByTestId('cockpit-status').locator('.cockpit-sel')).toHaveText(/^sel /)
      await setupStep(probe, runtime, { kind: 'zoom-before-fit', control: {
        role: 'button', name: 'Zoom in', exact: true, scope: { role: 'toolbar', name: 'View', exact: true },
      } })
      runtime.savedView = await viewState(page)
      expect(runtime.savedView.layers.length).toBeGreaterThan(0)
      await viewButton(page, 'Fit drawing to view').click()
      await requireViewport(page, runtime.homeViewport)
      await expect(viewButton(page, 'Back to the previous view')).toBeEnabled()
      await page.keyboard.press('Escape')
      await expect(page.getByTestId('cockpit-status').locator('.cockpit-sel')).toHaveText('no selection')
      const layer = layerButtons(page).filter({ hasText: 'Walk' })
      await expect(layer).toHaveCount(1)
      await expect(layer).toHaveAttribute('aria-pressed', 'true')
      await layer.click()
      await expect(layer).toHaveAttribute('aria-pressed', 'false')
      evidence.previousView = { saved: runtime.savedView, changed: await viewState(page) }
      return
    }
    case 'whole-drawing-view': {
      await page.keyboard.press('Escape')
      const clearFocus = page.getByRole('button', { name: 'Clear focus', exact: true })
      if (await clearFocus.isVisible()) await clearFocus.click()
      await expect(clearFocus).toHaveCount(0)
      await expect(page.getByTestId('cockpit-status').locator('.cockpit-sel')).toHaveText('no selection')
      await setupStep(probe, runtime, { kind: 'zoom-before-fit', control: {
        role: 'button', name: 'Zoom in', exact: true, scope: { role: 'toolbar', name: 'View', exact: true },
      } })
      return
    }
    case 'engine-ready': await engineReady(probe, runtime); return
    case 'select-entity': await selectEntity(probe, runtime, recipe); return
    case 'clear-selection': await page.keyboard.press('Escape'); return
    case 'copy-selection':
      await discloseControlPanel(page, { group: 'clipboard' })
      await page.getByRole('toolbar', { name: 'Drafting tools', exact: true }).getByRole('group', { name: 'Clipboard', exact: true })
        .getByRole('button', { name: ACTIONS.find((action) => action.op === 'copyClip').label, exact: true }).click()
      return
    case 'drawer-state': await setDrawer(page, recipe.name, recipe.open); return
    case 'tool-rail-state': await setToolRail(page, recipe.open, runtime.testInfo.project.name === 'phone'); return
    case 'properties-state': {
      const dock = page.getByRole('complementary', { name: 'Properties', exact: true })
      if (await dock.isVisible() !== recipe.open) {
        await setupStep(probe, runtime, { kind: 'ribbon-tab', name: 'View' })
        await page.getByRole('toolbar', { name: 'Drafting tools', exact: true }).getByRole('button', { name: 'properties', exact: true }).click()
      }
      await expect(dock)[recipe.open ? 'toBeVisible' : 'toBeHidden']()
      return
    }
    case 'properties-section-state': {
      const section = await setSection(page, recipe.name, true)
      const body = section.locator('.dock-section-body')
      if (recipe.name === 'Drawing') await expect(section.getByTestId('dock-drawing')).toBeVisible()
      if (recipe.name === 'Layers' && !runtime.failedDrawing) await expect(section.locator('.legend')).toBeVisible()
      if (recipe.name === 'Selection' && !runtime.failedDrawing) {
        await expect(section.locator('.selection-readout:not(.empty)')).toBeVisible()
        await expect(section.locator('.sel-handle')).not.toHaveText('')
      }
      runtime.sectionContents = await body.innerText()
      await setSection(page, recipe.name, recipe.expanded)
      await requireWorkspace(runtime)
      return
    }
    case 'properties-close-state':
      await expect(propertiesPane(page)).toBeVisible()
      await requireWorkspace(runtime)
      runtime.cleanup.push(() => setupStep(probe, runtime, { kind: 'properties-state', open: true }))
      return
    case 'layer-visible-state': {
      await setupStep(probe, runtime, { kind: 'ribbon-tab', name: 'Draw' })
      await setSection(page, 'Layers', true)
      await setSection(page, 'Drawing', true)
      await page.keyboard.press('Escape')
      await setLayer(page, 'Panels', true)
      await setLayer(page, 'Walk', true)
      runtime.cleanup.push(async () => {
        await setLayer(page, 'Panels', true)
        await setLayer(page, 'Walk', true)
      })
      await setLayer(page, recipe.name, recipe.visible)
      evidence.layerVisibility = { name: recipe.name, initial: recipe.visible }
      return
    }
    case 'job-monitor-collapsed': {
      const collapse = page.getByRole('button', { name: 'Collapse the job monitor to a spine', exact: true })
      if (await collapse.isVisible()) await collapse.click()
      const toolbar = page.getByRole('toolbar', { name: 'Job monitor', exact: true })
      await expect(toolbar).toBeVisible()
      await expect(toolbar.getByRole('button', { name: /^Expand the job monitor \([0-9]+ live\)$/ })).toBeVisible()
      await expect(page.locator('aside.rail .rail-ledger')).toHaveCount(0)
      await requireWorkspace(runtime)
      runtime.cleanup.push(async () => {
        if (await collapse.isVisible()) await collapse.click()
        await expect(toolbar).toBeVisible()
      })
      return
    }
    case 'overview-expanded-state': {
      await viewportBounds(page)
      await expect(page.getByRole('button', { name: 'Collapse drawing overview', exact: true })).toHaveAttribute('aria-expanded', 'true')
      runtime.cleanup.push(async () => {
        const expand = page.getByRole('button', { name: 'Expand drawing overview', exact: true })
        if (await expand.isVisible()) await expand.click()
        await expect(page.getByRole('button', { name: 'Drawing overview', exact: true })).toBeVisible()
      })
      return
    }
    case 'overview-pan-state': {
      await setupStep(probe, runtime, { kind: 'overview-expanded-state' })
      const outline = page.getByRole('button', { name: 'Drawing overview', exact: true }).locator('.cad-overview-outline')
      const map = await outline.evaluate((element) => Object.fromEntries(['x', 'y', 'width', 'height']
        .map((key) => [key, Number(element.getAttribute(key))])))
      let viewport
      for (let attempt = 0; attempt < 20; attempt++) {
        viewport = await viewportBounds(page)
        if (viewport.width < map.width * 0.4 && viewport.height < map.height * 0.4) break
        await viewButton(page, 'Zoom in').click()
      }
      expect(viewport.width).toBeLessThan(map.width * 0.4)
      expect(viewport.height).toBeLessThan(map.height * 0.4)
      // Stay inside the outline so neither recenter nor the rectangle is clipped.
      const center = { x: viewport.x + viewport.width / 2, y: viewport.y + viewport.height / 2 }
      const point = { x: map.x + map.width * (center.x < map.x + map.width / 2 ? 0.65 : 0.35),
        y: map.y + map.height * (center.y < map.y + map.height / 2 ? 0.65 : 0.35) }
      expect(Math.abs(point.x - center.x)).toBeGreaterThan(0.2)
      expect(Math.abs(point.y - center.y)).toBeGreaterThan(0.2)
      runtime.overviewPan = { map, point, before: viewport }
      evidence.overviewPan = runtime.overviewPan
      return
    }
    case 'open-start': await page.getByRole('button', { name: 'Start', exact: true }).click(); return
    case 'open-history': await openHistory(probe, runtime, assertions); return
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
      const facts = runtime.drawingId ? (runtime.catalogFacts ||= {}) : runtime.workerFacts
      const catalog = await workerCatalog(facts, page.request, { drawingId: runtime.drawingId, version: runtime.drawingVersion || 'head' })
      if (!catalog) throw facts.catalogError
      const family = catalog.families.find((family) => family.capabilities.some((tool) => tool.name === recipe.name))
      const tool = family?.capabilities.find((tool) => tool.name === recipe.name)
      evidence.catalog = { requestedTool: recipe.name, record: tool || null,
        catalog_sha256: facts.catalog_sha256, families: catalog.families.length,
        capabilities: catalog.families.reduce((count, family) => count + family.capabilities.length, 0) }
      if (!tool) await unsupported(probe, runtime, `The isolated catalog does not provide ${recipe.name}`)
      runtime.catalogPanelName = family.label
      // Unplaced catalog families live on Manage, not the engine's Draw tab.
      const tab = toolPlacementTab(tool) || 'manage'
      await setupStep(probe, runtime, { kind: 'ribbon-tab', name: tab[0].toUpperCase() + tab.slice(1) })
      if (probe.state === 'ready' && recipe.name.startsWith('solar-') && !toolAvailabilityEvidence(probe, facts)) {
        // The API-side catalog is not proof that the browser seated the same
        // drawing context. Observe its own request before accepting readiness.
        const scopedCatalog = page.waitForResponse((response) => {
          const url = new URL(response.url())
          return response.request().method() === 'GET' && url.pathname === '/api/capabilities'
            && url.searchParams.get('drawing_id') === runtime.drawingId
        }, { timeout: 15_000 })
        scopedCatalog.catch(() => {})
        await page.goto(`/app?drawing=${encodeURIComponent(runtime.drawingId)}&surface=solar`)
        const response = await scopedCatalog
        await assertions(response.ok()).toBe(true)
        evidence.browserCatalog = { url: response.url(), drawingId: runtime.drawingId }
        await setupStep(probe, runtime, { kind: 'ribbon-tab', name: tab[0].toUpperCase() + tab.slice(1) })
        const button = control(page, { role: 'button', name: recipe.name, panelName: family.label,
          scope: { role: 'toolbar', name: 'Drafting tools' } })
        await assertions(button).toBeVisible({ timeout: 15_000 })
        await assertions(button).toBeEnabled({ timeout: 15_000 })
      }
      return
    }
    case 'seed-solar-graph': {
      const path = `/api/drawings/${runtime.drawingId}`
      const versions = await page.request.get(`${path}/versions`)
      expect(versions.ok()).toBe(true)
      const chain = await versions.json()
      const parent = chain.versions.find((row) => Number(row.v) === Number(chain.head))
      const toolsResponse = await page.request.get('/api/tools')
      expect(toolsResponse.ok()).toBe(true)
      const settings = (await toolsResponse.json()).tools.find((tool) => tool.name === 'solar-settings')
      expect(settings).toBeTruthy()
      const checkoutPath = `${path}/checkout`
      const checkout = await page.request.post(checkoutPath, { data: { holder: 'drafter', ttl_s: 300 } })
      expect(checkout.ok()).toBe(true)
      const lease = await checkout.json()
      expect(lease.acquired).toBe(true)
      expect(lease.checkout_capability).toBeTruthy()
      const release = async () => {
        const response = await page.request.delete(checkoutPath, {
          headers: { 'X-Checkout-Capability': lease.checkout_capability },
        })
        expect(response.ok()).toBe(true)
      }
      try {
        const response = await page.request.post('/api/run?wait=1', {
          headers: { 'X-Checkout-Capability': lease.checkout_capability },
          data: runBody({ tool: 'solar-settings', drawingId: runtime.drawingId,
            params: seedParams(parent), catalogDigest: settings.catalog_digest }), timeout: 45_000,
        })
        expect(response.status()).toBe(200)
        const receipt = await response.json()
        expect(receipt.ok).toBe(true)
        runtime.drawingVersion = receipt.result.new_version.version
        evidence.solarSeed = { drawingId: runtime.drawingId, parent: chain.head,
          version: runtime.drawingVersion, graphRev: receipt.result.after_rev }
      } finally { await release() }
      await page.reload({ timeout: 60_000 })
      await requireWorkspace(runtime)
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
    case 'start-pending-run': await startPendingRun(probe, runtime, assertions); return
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
      // App's engine quick controls use these labels, distinct from version Undo/Redo.
      await page.getByRole('toolbar', { name: 'Quick access', exact: true }).getByRole('button', { name: 'Undo edit', exact: true }).click()
      return
    case 'preview-version': await previewVersion(probe, runtime, assertions); return
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
      if (recipe.surface === 'cad' && recipe.context.apsLive === true && health.aps_live !== true && recipe.context.hasDrawing !== false && recipe.context.sessionActive !== false) {
        await unsupported(probe, runtime, 'The surface ready state requires APS_LIVE; the isolated local stack reports execution paused')
      }
      if (recipe.surface === 'solar' && recipe.context.solarReady === true && recipe.context.sessionActive !== false) {
        await engineReady(probe, runtime)
        await expect(page.locator('.workspace-card')).toHaveAttribute('data-engine-document',
          new RegExp(`^${runtime.drawingId}-v[1-9]\\d*\\.dxf$`), { timeout: 60_000 })
      }
      return
    }
    case 'require-local-state':
      // No synthetic React state, mocked response or silent ready-state
      // fallback may stand in for a registry context. Keep this coverage gap
      // red until a public setup/fault fixture for the named state is supplied.
      await unsupported(probe, runtime, localFixtureReason(recipe))
      return
    default: throw new Error(`Unknown walk setup step: ${recipe.kind}`)
  }
}

async function captureBefore(probe, runtime) {
  const { page } = runtime
  // Refusal probes do not activate the action or need its enabled-state baseline.
  if (probe.assertion.kind === 'disabled_with_reason' && probe.kind === 'action') return {}
  const target = probe.assertion.target || ''
  if (/^properties-(drawing|layers|plan|selection)-section$/.test(target)) {
    return { expanded: await control(page, probe.locator).getAttribute('aria-expanded') === 'true' }
  }
  if (/^layer-(panels|walk)-visible$/.test(target)) {
    const name = target === 'layer-panels-visible' ? 'Panels' : 'Walk'
    const before = { layersShown: await layersShown(page),
      visible: await ribbonLayer(page, name).getAttribute('aria-pressed') === 'true',
      legend: await legendRow(page, name).ariaSnapshot() }
    runtime.evidence.layerVisibility.before = before
    return before
  }
  if (target === 'job-monitor') return { url: page.url(),
    workspace: runtime.failedDrawing
      ? await page.getByRole('alert').filter({ hasText: /Couldn['’]t load drawing/ }).innerText()
      : await propertiesPane(page).getByTestId('dock-drawing').innerText(),
    selection: runtime.failedDrawing ? null : await page.getByTestId('cockpit-status').locator('.cockpit-sel').innerText() }
  if (probe.featureId === 'control:view-back' && probe.state === 'empty-history') return viewState(page)
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
  if (probe.assertion.target === 'signed-out-session') {
    await Promise.all([page.waitForEvent('domcontentloaded'), locator.click()])
    return
  }
  if (recipe.trigger === 'type' || recipe.trigger === 'fill-enter') {
    if (recipe.trigger === 'type') await locator.pressSequentially(recipe.inputValue)
    else { await locator.fill(recipe.inputValue); await locator.press('Enter') }
    return
  }
  if (probe.assertion.target === 'viewer-overview-pan') {
    const svg = locator.locator('svg')
    const box = await svg.boundingBox()
    expect(box).toBeTruthy()
    const size = await svg.evaluate((element) => ({ width: element.viewBox.baseVal.width, height: element.viewBox.baseVal.height }))
    const point = runtime.overviewPan.point
    await page.mouse.click(box.x + point.x * box.width / size.width, box.y + point.y * box.height / size.height)
    return
  }
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

export async function assertEffect(probe, runtime, locator, before, assertions = expect) {
  const expect = assertions
  const { page } = runtime
  const effect = probe.assertion
  const target = effect.target || ''
  if (probe.kind === 'control' && baselineThreePanels[target]) {
    const spec = baselineThreePanels[target]
    const panel = namedPanel(page, spec)
    await expect(panel).toBeVisible()
    if (['claude-accounts-panel', 'linked-services-panel', 'version-history', 'cost-panel'].includes(target)) {
      await expect(locator).toHaveAttribute('aria-expanded', 'true')
    }
    await expect(panel.getByRole('button', { name: spec.close, exact: true })).toBeVisible()
    if (target === 'cost-panel') await expect(panel.getByRole('heading', { name: spec.name, exact: true })).toBeVisible()
    return
  }
  if (target === 'scope-build-picker' || target === 'scope-picker') {
    const scope = page.getByRole('listbox', { name: 'Scope', exact: true })
    await expect(scope).toBeVisible()
    if (target === 'scope-build-picker') await expect(scope.getByRole('option', { name: /^build ·/ })).toHaveAttribute('aria-selected', 'true')
    else await expect(locator).toHaveAttribute('aria-expanded', 'true')
    await page.keyboard.press('Escape')
    await expect(scope).toBeHidden()
    return
  }
  if (target === 'guided-demo') {
    const properties = page.getByRole('complementary', { name: 'Properties', exact: true })
    await expect(properties.getByRole('definition').filter({ hasText: /^rooftop_demo\.dwg$/ })).toBeVisible()
    await expect(properties.getByRole('definition').filter({ hasText: /^sample data$/ })).toBeVisible()
    await expect(page.getByRole('alert').filter({ hasText: /Couldn['’]t load drawing/ })).toHaveCount(0)
    return
  }
  if (target === 'project-board') {
    await expect(page.getByRole('heading', { name: 'Project board', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Return to drawing', exact: true })).toBeVisible()
    return
  }
  if (target === 'notification-inbox-collapsed') {
    await expect(page.getByRole('button', { name: 'Expand the notification inbox', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Expand the notification inbox', exact: true })).toHaveAttribute('aria-expanded', 'false')
    await expect(page.getByRole('button', { name: 'Collapse the notification inbox', exact: true })).toHaveCount(0)
    return
  }
  if (target === 'unknown-tool-resolver') {
    const resolver = page.getByRole('listbox', { name: 'Route resolver', exact: true })
    await expect(resolver).toBeVisible()
    await expect(resolver.getByText('“/w1k-no-such-tool” isn’t a tool in this catalog. Pick an alternative:', { exact: true })).toBeVisible()
    expect(runtime.localDecisionRequests).toEqual([])
    return
  }
  if (target === 'signed-out-session') {
    await expect(page.getByRole('button', { name: 'Sign out', exact: true })).toHaveCount(0)
    const details = page.getByRole('button', { name: 'Details', exact: true })
    await expect(details).toHaveCount(1)
    await details.click()
    const panel = page.getByRole('dialog', { name: 'Session · provenance', exact: true })
    await expect(panel).toBeVisible()
    await expect(panel.getByTestId('diagnostics-block')).toHaveText(/(?:^|\n)session signed out(?:\n|$)/)
    await expect(panel.getByRole('button', { name: 'Refresh', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Sign out', exact: true })).toHaveCount(0)
    return
  }
  if (target === 'edit-lock-held') {
    await expect(page.getByText('You hold the edit lock', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Release', exact: true })).toBeEnabled()
    await expect(page.getByRole('button', { name: 'Take edit lock', exact: true })).toHaveCount(0)
    return
  }
  if (target === 'tool-commands') {
    await expect(locator).toHaveAttribute('aria-expanded', 'true')
    await expect(locator).toHaveAttribute('aria-controls', 'slash-menu-listbox')
    await expect(page.getByRole('listbox', { name: 'Tool commands', exact: true })).toBeVisible()
    return
  }
  if (target === 'drawing-find-no-match') {
    await expect(page.getByRole('status').filter({ hasText: /^No matching object in this drawing\.$/ })).toHaveText('No matching object in this drawing.')
    await expect(locator).toHaveAttribute('aria-expanded', 'false')
    return
  }
  if (effect.kind === 'disabled_with_reason') {
    if (probe.kind === 'control') {
      await expect(locator).toBeDisabled()
      await expect(locator).toHaveAccessibleName(probe.locator.name)
      await expect(locator).toHaveAttribute('title', probe.locator.tooltip)
      if (probe.locator.description) {
        await expect(locator).toHaveAttribute('aria-disabled', 'true')
        await expect(locator).toHaveAccessibleDescription(probe.locator.description)
        // Native click input is needed: Playwright deliberately refuses an aria-disabled button.
        const box = await locator.boundingBox()
        expect(box).toBeTruthy()
        await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2)
        await expect(locator).toHaveAttribute('aria-disabled', 'true')
        await requireViewport(page, before.viewport)
        expect(await viewState(page)).toEqual(before)
        await expect(page.getByTestId('cockpit-view-live')).toHaveText('')
      }
      return
    }
    await expect(locator).toBeDisabled()
    if (runtime.failedDrawing) {
      const name = await locator.getAttribute('aria-label')
      runtime.evidence.disabledReason = { expected: probe.locator.disabledVariants.map((variant) => variant.name), observed: name }
      const variant = probe.locator.disabledVariants.find((variant) => variant.name === name)
      await expect(locator).toHaveAccessibleName(new RegExp('^(?:' + probe.locator.disabledVariants
        .map((variant) => variant.name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') + ')$'))
      await expect(locator).toHaveAccessibleName(variant.name)
      await expect(probe.locator.trigger === 'select' ? locator.locator('xpath=ancestor::label[contains(concat(" ", normalize-space(@class), " "), " ribbon-widget ")][1]') : locator)
        .toHaveAttribute('title', new RegExp(variant.reason.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
      runtime.evidence.failedDrawing.renderedReason = variant.reason
      runtime.evidence.failedDrawing.renderedReasonCode = variant.reason_code
      return
    }
    // The accessible name is user-facing evidence, unlike a data-reason-code
    // alone. Exact registry text must also remain on the native tooltip.
    await expect(locator).toHaveAccessibleName(new RegExp(effect.reason.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
    await expect(probe.locator.trigger === 'select' ? locator.locator('xpath=ancestor::label[contains(concat(" ", normalize-space(@class), " "), " ribbon-widget ")][1]') : locator)
      .toHaveAttribute('title', new RegExp(effect.reason.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
    return
  }
  if (target === 'drafting-grid') {
    await expect(locator).toHaveAttribute('aria-pressed', String(effect.value))
    await requireGrid(page, effect.value)
    return
  }
  if (target === 'document-fullscreen') {
    await expect.poll(() => page.evaluate(() => !!document.fullscreenElement)).toBe(effect.value)
    if (effect.value) expect(await page.evaluate(() => document.fullscreenElement === document.documentElement)).toBe(true)
    return
  }
  if (target === 'viewer-previous-view') {
    await requireViewport(page, runtime.savedView.viewport)
    const restored = await viewState(page)
    expect(restored.selection).toBe(runtime.savedView.selection)
    expect(restored.layers).toEqual(runtime.savedView.layers)
    const handle = runtime.savedView.selection.replace(/^sel /, '')
    await expect(page.getByTestId('cockpit-view-live')).toHaveText('Back to your previous view, ' + handle + ' selected')
    runtime.evidence.previousView.restored = restored
    return
  }
  if (target === 'viewer-whole-drawing') {
    await requireViewport(page, runtime.homeViewport)
    await expect(page.getByTestId('cockpit-view-live')).toHaveText('Showing the whole drawing')
    await expect(page.getByTestId('cockpit-status').locator('.cockpit-sel')).toHaveText('no selection')
    await expect(page.getByRole('button', { name: 'Clear focus', exact: true })).toHaveCount(0)
    runtime.evidence.fitViewport.fitted = await viewportBounds(page)
    return
  }
  if (target === 'viewer-overview-pan') {
    await expect.poll(async () => {
      const current = await viewportBounds(page)
      return current.x !== before.viewport.x || current.y !== before.viewport.y
    }, { message: 'overview click must move the visible viewport rectangle' }).toBe(true)
    const after = await viewportBounds(page)
    expect(Math.abs(after.width - before.viewport.width)).toBeLessThanOrEqual(0.2)
    expect(Math.abs(after.height - before.viewport.height)).toBeLessThanOrEqual(0.2)
    await expect(locator.locator('[data-overview-viewport]')).toBeVisible()
    runtime.evidence.overviewPan.after = after
    await requireWorkspace(runtime)
    return
  }
  if (target === 'drawing-overview-expanded') {
    // viewportBounds expands the map; never use it in this collapse oracle.
    await expect(page.getByRole('button', { name: 'Expand drawing overview', exact: true })).toHaveAttribute('aria-expanded', 'false')
    await expect(page.getByRole('button', { name: 'Drawing overview', exact: true })).toHaveCount(0)
    await expect(page.locator('[data-overview-viewport]')).toHaveCount(0)
    await requireWorkspace(runtime)
    return
  }
  if (/^properties-(drawing|layers|plan|selection)-section$/.test(target)) {
    expect(effect.value).toBe(!before.expanded)
    await expect(locator).toHaveAttribute('aria-expanded', String(effect.value))
    const section = propertiesSection(page, probe.locator.name)
    const body = section.locator('.dock-section-body')
    if (effect.value) {
      await expect(body).toBeVisible()
      await expect(body).toHaveText(runtime.sectionContents, { useInnerText: true })
    } else await expect(body).toHaveCount(0)
    const content = target === 'properties-drawing-section' ? section.getByTestId('dock-drawing')
      : target === 'properties-layers-section' && !runtime.failedDrawing ? section.locator('.legend')
        : target === 'properties-selection-section' && !runtime.failedDrawing ? section.locator('.selection-readout:not(.empty)') : null
    if (content) {
      if (effect.value) await expect(content).toBeVisible()
      else await expect(content).toHaveCount(0)
    }
    await requireWorkspace(runtime)
    return
  }
  if (/^layer-(panels|walk)-visible$/.test(target)) {
    const name = target === 'layer-panels-visible' ? 'Panels' : 'Walk'
    expect(effect.value).toBe(!before.visible)
    await requireLayer(page, name, effect.value)
    await expect.poll(() => layersShown(page)).toBe(before.layersShown + (effect.value ? 1 : -1))
    runtime.evidence.layerVisibility.after = { visible: effect.value, layersShown: await layersShown(page),
      legend: await legendRow(page, name).ariaSnapshot() }
    await requireWorkspace(runtime)
    return
  }
  if (target === 'job-monitor') {
    await expect(page.getByRole('toolbar', { name: 'Job monitor', exact: true })).toHaveCount(0)
    await expect(page.getByRole('button', { name: /^Expand the job monitor \([0-9]+ live\)$/ })).toHaveCount(0)
    const rail = page.locator('aside.rail').filter({ has: page.getByRole('button', { name: 'Collapse the job monitor to a spine', exact: true }) })
    await expect(rail.getByRole('heading', { name: /^Job monitor/ })).toBeVisible()
    await expect(rail.locator('.rail-ledger')).toHaveCount(1)
    await expect(rail.locator('.rail-ske, .rail-empty, .rail-ledger > *').first()).toBeVisible()
    await expect(rail.getByRole('button', { name: 'Collapse the job monitor to a spine', exact: true })).toBeVisible()
    expect(page.url()).toBe(before.url)
    if (runtime.failedDrawing) await expect(page.getByRole('alert').filter({ hasText: /Couldn['’]t load drawing/ }))
      .toHaveText(before.workspace, { useInnerText: true })
    else await expect(propertiesPane(page).getByTestId('dock-drawing')).toHaveText(before.workspace, { useInnerText: true })
    if (!runtime.failedDrawing) await expect(page.getByTestId('cockpit-status').locator('.cockpit-sel')).toHaveText(before.selection)
    await requireWorkspace(runtime)
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
    if (effect.value === false) {
      expect(before.visible).toBe(true)
      await expect(propertiesPane(page)).toHaveCount(0)
      await expect(page.getByRole('button', { name: 'Close the properties pane', exact: true })).toHaveCount(0)
      await requireWorkspace(runtime)
      return
    }
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
    else if (rung === 'running') {
      await expect(page.getByText('Stopped following count-by-layer. It keeps running; find it in Jobs.', { exact: true })).toBeVisible()
      await expect(page.locator('.strip-running')).toHaveCount(0)
      runtime.evidence.pendingRun.detached = true
    }
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
    await expect(page.getByRole('group', { name: 'Clipboard', exact: true }).getByRole('button', {
      name: ACTIONS.find((action) => action.op === 'pasteClip').label, exact: true,
    })).toBeEnabled()
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
  const { evidence } = runtime
  let page = runtime.page
  evidence.featureId = probe.featureId
  evidence.state = probe.state
  evidence.fixtureRecipe = probe.setup
  evidence.expectedEffect = probe.assertion
  evidence.assertionId = probe.assertion.assertionId
  runtime.cleanup = []
  runtime.workerFacts ||= evidence.workerFacts || { engineMounted: undefined }
  const setupStartedAt = Date.now()
  let originalError
  try {
    const initialReason = typeof UI_UNREACHABLE_STATES !== 'undefined' && UI_UNREACHABLE_STATES.has(probe.state)
      && typeof unsupportedBeforeSetup === 'function'
      ? unsupportedBeforeSetup(probe, runtime.workerFacts) : null
    if (initialReason) {
      if (typeof toolAvailabilityEvidence === 'function') {
        runtime.unsupportedAvailability = toolAvailabilityEvidence(probe, runtime.workerFacts)
      }
      await unsupported(probe, runtime, initialReason)
    }
    if (typeof readWalkBuildFlags === 'function') await readWalkBuildFlags(probe, runtime)
    // Source-extracted fake runners may omit module dependencies.
    const reason = typeof unsupportedBeforeSetup === 'function'
      ? unsupportedBeforeSetup(probe, { engineMounted: runtime.workerFacts.engineMounted,
        engineUnavailableReason: runtime.workerFacts.engineUnavailableReason,
        buildFlags: runtime.workerFacts.buildFlags, catalog: runtime.workerFacts.catalog }, false) : null
    if (reason) {
      if (typeof toolAvailabilityEvidence === 'function') {
        runtime.unsupportedAvailability = toolAvailabilityEvidence(probe, runtime.workerFacts)
      }
      if (runtime.evidence.buildFlags) runtime.unsupportedAvailability = {
        ...runtime.unsupportedAvailability, flags: runtime.evidence.buildFlags,
      }
      await unsupported(probe, runtime, reason)
    }
    for (const recipe of probe.setup.steps) {
      await (runtime.runStep || test.step)(`Setup: ${recipe.kind}`, async () => {
        try { await setupStep(probe, runtime, recipe) } catch (error) {
          if (typeof solarCalibrationFailure === 'function' && solarCalibrationFailure(probe, recipe, error)) {
            await unsupported(probe, runtime, SOLAR_PANEL_CALIBRATION_REASON)
          }
          throw error
        }
        evidence.steps.push({ phase: 'setup', ...recipe })
      })
    }
    page = runtime.page
    evidence.setupCompleted = { elapsedMs: Date.now() - setupStartedAt }
    if (probe.kind === 'tool' && typeof workerCatalog === 'function') {
      runtime.catalogFacts ||= {}
      await workerCatalog(runtime.catalogFacts, page.request, { drawingId: runtime.drawingId, version: runtime.drawingVersion || 'head' })
      if (runtime.catalogFacts.catalogError) throw runtime.catalogFacts.catalogError
      const availabilityReason = unsupportedBeforeSetup(probe, runtime.catalogFacts)
      if (availabilityReason) {
        runtime.unsupportedAvailability = toolAvailabilityEvidence(probe, runtime.catalogFacts)
        await unsupported(probe, runtime, availabilityReason)
      }
    }
    let targetRecipe = runtime.catalogPanelName ? { ...probe.locator, panelName: runtime.catalogPanelName } : probe.locator
    if (probe.kind === 'action' && probe.assertion.kind === 'disabled_with_reason') {
      // A changed refusal is an oracle verdict; only a missing action is a
      // pre-oracle failure. Match the stable label with any refusal suffix.
      const stableName = probe.locator.availableName || (typeof probe.locator.name === 'string'
        ? probe.locator.name.split(' (unavailable: ')[0] : null)
      if (probe.locator.unavailableName || stableName) targetRecipe = { ...targetRecipe, exact: false,
        name: probe.locator.unavailableName || new RegExp('^' + stableName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '(?: \\(unavailable: .+\\))?$') }
    }
    // Source-extracted fake runners may omit module dependencies.
    if (typeof discloseControlPanel === 'function') await discloseControlPanel(page, targetRecipe)
    const locator = control(page, runtime.testInfo.project.name === 'phone' && probe.locator.phone ? probe.locator.phone : targetRecipe)
    try {
      const observations = await collectProbeUxEvidence(probe, locator, runtime.testInfo.project.name)
      if (observations.length) Object.assign(evidence, packUxEvidence([...(evidence.ux_observations || []), ...observations]))
    } catch { /* Optional UX evidence never changes the functional verdict. */ }
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
    if (probe.kind === 'control') {
      await expect(locator).toHaveCount(1)
      if (probe.locator.normalizedName) {
        const snapshot = await locator.ariaSnapshot()
        const match = snapshot.split('\n')[0].match(/^- button "((?:\\.|[^"\\])*)"/)
        expect(match, 'count control must expose its accessible name').toBeTruthy()
        const liveName = JSON.parse('"' + match[1] + '"')
        expect(normalizedControlKey({ scope: 'document', role: 'button', name: liveName })).toBe(
          normalizedControlKey({ scope: 'document', role: 'button', name: probe.locator.normalizedName }))
        evidence.controlIdentity = { rawName: liveName, normalizedName: probe.locator.normalizedName }
      }
    }
    const before = await captureBefore(probe, runtime)
    if (probe.assertion.kind !== 'disabled_with_reason') {
      await test.step(`Activate ${probe.featureId}`, () => activate(probe, runtime, locator))
    }
    await test.step(probe.assertion.assertionId, () => {
      evidence.oracleReached = probe.assertion.assertionId
      return assertEffect(probe, runtime, locator, before)
    })
    if (runtime.failedDrawing) evidence.failedDrawing.actionAvailability = probe.assertion.kind === 'disabled_with_reason'
      ? 'disabled_with_reason' : 'available'
    evidence.result = { result: 'passed', featureId: probe.featureId, state: probe.state }
  } catch (error) {
    originalError = error
    if (error instanceof UnsupportedLocalError) {
      runtime.testInfo.annotations.push({ type: 'unsupported_local', description: error.reason })
      return { unsupported: true, reason: error.reason }
    }
    evidence.failure = { message: error.message, assertionId: probe.assertion.assertionId }
    throw error
  } finally {
    const cleanupErrors = []
    for (const cleanup of runtime.cleanup.reverse()) {
      try { await cleanup() } catch (error) { cleanupErrors.push(error) }
    }
    evidence.cleanupCompleted = cleanupErrors.length === 0
    if (cleanupErrors.length) {
      evidence.cleanupErrors = cleanupErrors.map((error) => ({ message: error.message }))
      if (!originalError || originalError instanceof UnsupportedLocalError) throw cleanupErrors[0]
    }
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
