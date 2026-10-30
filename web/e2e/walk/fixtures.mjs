import { test as base, expect, request as playwrightRequest } from '@playwright/test'
import { startStack } from '../../walk/stack.mjs'
import { prepareProductionBundle } from '../../walk/sameOriginProxy.mjs'
import { SERVER_ERROR_CAP, extractErrorId, serverDiagnostic } from '../../walk/serverDiagnostics.mjs'
import { readFile, writeFile } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import { basename, join } from 'node:path'
import { PRODUCT_SURFACES } from '../../src/site/productSurfaces.js'
import { toolPlacementTab } from '../../src/lib/toolRecord.js'
import { familiesForSurface } from '../../src/lib/surfaceRails.js'
import { buildViewSearch } from '../../src/lib/urlState.js'
import { ACTIONS, accessibleName, reasonCode } from '../../src/lib/actionRegistry.js'
import { PROMPTS } from '../../src/cadedit/promptKeys.js'
import { normalizedControlKey, solarChainSteps, projectSharedShellProbe } from './probes.mjs'
import { solarToolReadyEffect } from '../../walk/featureMap.mjs'
import { buildDrawingObjectIndex } from '../../src/lib/drawingObjectIndex.js'
import { collectProbeUxEvidence, packUxEvidence } from './uxEvidence.mjs'
import { activateWithMobileTouchMeasurements } from './mobileTouchMeasurements.mjs'
import { createAccessibilityMeasurementCapture } from './accessibilityMeasurements.mjs'
import { createEndUserMeasurementCapture } from './endUserMeasurements.mjs'
import { collectSupportMeasurements } from './supportMeasurements.mjs'
import { createOpsSreMeasurementCapture } from './opsSreMeasurements.mjs'
import { createRedHatMeasurementCapture } from './redHatMeasurements.mjs'
import { createPerformanceMeasurementCapture } from './performanceMeasurements.mjs'
import { createQcDevMeasurementCapture } from './qcDevMeasurements.mjs'
import { collectUiUxMeasurements } from './uiUxMeasurements.mjs'
import { collectUnfamiliarMeasurements } from './unfamiliarMeasurements.mjs'
import { composePersonaMeasurementBundle } from './personaMeasurementBundle.mjs'
import { seedParams, runBody, expectHeadAdvanced } from '../solarGraphCommitProof.mjs'
import { actionResponse, isUuid, projectBinding, observeProjectTraffic, readContext, runProjectTool } from '../local/solarProjectWalk.mjs'

export { expect }

export const STUDIO_RAIL_FEATURES = Object.freeze(new Set([
  'control:linked-services', 'control:job-monitor-expand', 'control:notification-collapse', 'control:objects',
]))

const studioRailButton = page => page.getByRole('navigation', { name: 'Workspace controls', exact: true })
  .getByRole('button', { name: 'Studio', exact: true })

export async function revealStudioRail(page) {
  const button = studioRailButton(page)
  if (!await button.isVisible() || await button.getAttribute('aria-expanded') === 'true') return false
  await button.click()
  await expect(button).toHaveAttribute('aria-expanded', 'true')
  await expect(page.locator('#project-workspace-activity')).toBeVisible()
  return true
}

export async function prepareProjectSharedShell(probe, runtime, assertions = expect) {
  if (probe.profile !== 'project' || probe.state !== 'ready'
    || !['tab:project:activity', 'tab:project:project', 'tab:project:tools'].includes(probe.featureId)) return probe
  const { page } = runtime
  const surface = await page.locator('.app').first().getAttribute('data-surface')
  if (surface !== 'browser') return probe
  const resolved = projectSharedShellProbe(probe, { surface, studioPresent: await studioRailButton(page).isVisible() })
  if (resolved === probe) return probe
  const tools = probe.featureId === 'tab:project:tools'
  const pane = tools ? page.locator('#environment-project-panel .ground-pane') : null
  if (tools && await pane.count() !== 0) {
    throw new Error('Open tools precondition: Project must start on the board with no pane open')
  }
  const navigation = page.getByRole('navigation', { name: 'Workspace controls', exact: true })
  const regions = ['Studio', 'Project'].map(name => ({ name,
    region: page.getByRole('region', { name: `${name} sidebar`, exact: true }),
    button: navigation.getByRole('button', { name, exact: true }),
  }))
  const original = []
  for (const panel of regions) if (await panel.region.isVisible()) original.push(panel)
  const close = async panel => {
    await panel.region.getByRole('button', { name: `Close ${panel.name}`, exact: true }).click()
    await assertions(panel.region).toBeHidden()
  }
  runtime.cleanup.push(async () => {
    if (tools && await pane.count() !== 0) {
      await page.getByRole('region', { name: 'Project sidebar', exact: true })
        .getByRole('button', { name: 'Back to board', exact: true }).click()
      await assertions(page.locator('#environment-project-panel .ground-tile').first()).toBeVisible()
    }
    for (const panel of regions) if (await panel.region.isVisible()) await close(panel)
    for (const panel of original) {
      await panel.button.click()
      await assertions(panel.region).toBeVisible()
    }
  })
  if (tools) {
    const project = regions.find(panel => panel.name === 'Project')
    if (!await project.region.isVisible()) await project.button.click()
    await assertions(project.region).toBeVisible()
    await assertions(page.locator('#environment-project-panel .ground-tile').first()).toBeVisible()
    return resolved
  }
  for (const panel of original) await close(panel)
  for (const panel of regions) if (await panel.region.isVisible()) await close(panel)
  await assertions(page.getByRole('region', { name: resolved.assertion.region, exact: true })).toBeHidden()
  return probe.featureId === 'tab:project:project'
    ? { ...resolved, assertion: { ...resolved.assertion,
      content: '#environment-project-panel :is(.ground-pane, .ground-tile, .project-material-intake):visible' } }
    : resolved
}

export function redactSolarDiagnostic(value, { maxLength = 300 } = {}) {
  if (!['string', 'number', 'boolean'].includes(typeof value)) return '[unavailable]'
  return String(value).replace(/eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*/g, '[redacted]')
    .replace(/lco1\.[A-Za-z0-9_.-]+/g, '[redacted]').trim().slice(0, maxLength)
}

export async function walkClick(locator, label, { timeout = 15_000 } = {}) {
  try {
    await locator.click({ timeout })
  } catch (error) {
    let probe = '[unavailable]'
    try {
      probe = JSON.stringify(await locator.evaluate(node => {
        const box = node.getBoundingClientRect()
        const doc = node.ownerDocument
        const view = doc.defaultView
        const hit = doc.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2)
        const style = view.getComputedStyle(node)
        return {
          box: Object.fromEntries(['x', 'y', 'width', 'height'].map(key => [key, Math.round(box[key])])),
          viewport: { width: view.innerWidth, height: view.innerHeight },
          hit: hit ? `${hit.tagName.toLowerCase()}${hit.id ? `#${hit.id}` : ''}${[...hit.classList].slice(0, 3).map(name => `.${name}`).join('')}` : null,
          hitIsTarget: hit !== null && (hit === node || node.contains(hit)),
          position: { position: style.position, top: style.top, left: style.left, zIndex: style.zIndex },
        }
      }, undefined, { timeout: 2_000 }))
    } catch { /* Preserve the click failure when the target cannot be inspected. */ }
    const reason = String(error?.message ?? error).split(/\r?\n/)[0].slice(0, 300)
    throw new Error(redactSolarDiagnostic(`${label}: ${reason}\nClick target: ${probe}`, { maxLength: Infinity }), { cause: error })
  }
}

export async function walkFill(locator, value, label, { timeout = 15_000 } = {}) {
  try {
    await locator.fill(value, { timeout })
  } catch (error) {
    const reason = String(error?.message ?? error).split(/\r?\n/)[0]
    throw new Error(redactSolarDiagnostic(`${label}: ${reason}`, { maxLength: Infinity }), { cause: error })
  }
}

export async function walkSelect(locator, value, label, { timeout = 15_000 } = {}) {
  try {
    await locator.selectOption(value, { timeout })
  } catch (error) {
    let options = '[unavailable]'
    try {
      options = JSON.stringify(await locator.evaluate(node => Array.from(node.options, option => option.value),
        undefined, { timeout: 2_000 }))
    } catch { /* Preserve the select failure when its options cannot be inspected. */ }
    const reason = String(error?.message ?? error).split(/\r?\n/)[0]
    throw new Error(redactSolarDiagnostic(`${label}: ${reason}\nSelect options: ${options}`, { maxLength: Infinity }), { cause: error })
  }
}

export async function walkEnabled(locator, label, { timeout = 15_000 } = {}) {
  try {
    return await locator.isEnabled({ timeout })
  } catch (error) {
    const reason = String(error?.message ?? error).split(/\r?\n/)[0]
    throw new Error(redactSolarDiagnostic(`${label}: ${reason}`, { maxLength: Infinity }), { cause: error })
  }
}

export async function projectSolarRunDiagnostic(page, settings, start) {
  const diagnosticText = async read => {
    try { return redactSolarDiagnostic(await read()) } catch { return '[unavailable]' }
  }
  const text = locator => locator.textContent({ timeout: 2_000 })
  const checkout = () => page.getByRole('group', { name: 'Project checkout', exact: true })
  const fields = {
    'solar-settings-reason': await diagnosticText(() => text(settings.getByTestId('solar-settings-reason'))),
    'solar-settings-run': await diagnosticText(() => text(settings.getByTestId('solar-settings-run'))),
    'data-mode': await diagnosticText(() => settings.getAttribute('data-mode', { timeout: 2_000 })),
    'data-submit-seq': await diagnosticText(() => settings.getAttribute('data-submit-seq', { timeout: 2_000 })),
    'data-submit-exit': await diagnosticText(() => settings.getAttribute('data-submit-exit', { timeout: 2_000 })),
    startEnabled: await diagnosticText(() => start.isEnabled({ timeout: 2_000 })),
    checkoutStatus: await diagnosticText(() => text(checkout().getByRole('status'))),
    releaseEnabled: await diagnosticText(() => checkout().getByRole('button', { name: 'Release', exact: true }).isEnabled({ timeout: 2_000 })),
  }
  let alerts
  try {
    const locators = settings.getByRole('alert')
    alerts = []
    for (let index = 0, count = await locators.count(); index < count; index++) {
      alerts.push(await diagnosticText(() => text(locators.nth(index))))
    }
  } catch { alerts = ['[unavailable]'] }
  return `Solar settings before cleanup: ${JSON.stringify({ ...fields, alerts })}`
}
export const ENGINE_WASM_ROUTE = /\/engine\/engine_bg\.wasm(?:\?[^#]*)?$/
export const LOCAL_IDENTITY = Object.freeze({ tenant: 'demo-tenant', token: 'j1-presentation-fixture' })
export const COACH_STORAGE_KEY = 'leaf.coach.dismissed.v1'

export async function setupRequestHeaders(runtime, path) {
  const headers = { 'X-Tenant-Id': LOCAL_IDENTITY.tenant }
  const facts = runtime.workerFacts || runtime.evidence?.workerFacts
  if (facts?.authLive !== true) return headers
  const { page, stack } = runtime
  const pageURL = new URL(page.url())
  const requestURL = new URL(path, stack.baseURL)
  if (requestURL.origin !== pageURL.origin) return headers
  const token = await page.evaluate(() => localStorage.getItem('leaf.jwt'))
  if (!token?.trim()) {
    const error = new Error(`setup_auth_token_missing: ${requestURL.pathname} requires leaf.jwt on the live-auth page`)
    error.name = 'WalkSetupAuthError'
    throw error
  }
  headers.Authorization = `Bearer ${token}`
  return headers
}

export function injectWalkEntities(source) {
  // Walk selection targets stay outside the panel polygon in the pinned corpus fixture.
  const lines = [190, 470].map((y, index) => `0\nLINE\n5\nA10${index}\n8\nWalk\n10\n111\n20\n${y}\n11\n333\n21\n${y}\n`).join('')
  const dimension = '0\nDIMENSION\n5\nD100\n8\nWalk\n100\nAcDbEntity\n100\nAcDbDimension\n70\n32\n3\nStandard\n10\n333\n20\n160\n30\n0\n11\n222\n21\n160\n31\n0\n100\nAcDbAlignedDimension\n13\n111\n23\n175\n33\n0\n14\n333\n24\n175\n34\n0\n100\nAcDbRotatedDimension\n50\n0\n'
  const polyline = '0\nLWPOLYLINE\n5\nA200\n8\nWalk\n100\nAcDbEntity\n100\nAcDbPolyline\n90\n3\n70\n0\n10\n111\n20\n200\n10\n222\n20\n200\n10\n222\n20\n210\n'
  return source.replace('0\nENDSEC\n0\nEOF', `${lines}${dimension}${polyline}0\nENDSEC\n0\nEOF`)
}

export async function holdJobRoutes(page, pattern = '**/api/jobs/**', onHold, { holdResponse = false } = {}) {
  let release
  let passing = false
  let cleanupPromise
  const gate = new Promise((resolve) => { release = resolve })
  const active = new Set()
  const errors = []
  const hold = (route) => {
    const continuation = (async () => {
      const response = holdResponse ? await route.fetch({ timeout: 15_000 }) : null
      if (!passing) {
        onHold?.(route)
        await gate
      }
      // Forward the upstream response unchanged; no fixture response body is manufactured.
      if (response) {
        try { await route.fulfill({ response }) } finally { await response.dispose() }
      } else await route.continue()
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
  const catalogResponse = await page.request.get('/api/capabilities', { timeout: 15_000 })
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

function requestTiming(request, observedStart) {
  const timing = request.timing()
  const startTime = timing.startTime > 0 ? timing.startTime : observedStart
  const durationMs = Math.max(0, Math.round(timing.responseEnd >= 0 ? timing.responseEnd : Date.now() - startTime))
  return { startTime, durationMs }
}

export function observeProbeRequests(page, evidence, startedAt = Date.now()) {
  // Context events also cover recipes that replace the ordinary page.
  const source = typeof page.context === 'function' ? page.context() : page
  const starts = new WeakMap()
  const pending = []
  evidence.requestTimings = []
  evidence.failedRequests = []
  evidence.abortedRequests = []
  const request = (request) => { starts.set(request, Date.now()) }
  const complete = (request, failed) => {
    try {
      const observedStart = starts.get(request)
      if (observedStart === undefined || evidence.requestTimings.length >= 50) return
      starts.delete(request)
      const { startTime, durationMs } = requestTiming(request, observedStart)
      if (!failed && durationMs <= 2000) return
      const record = { method: request.method(), path: new URL(request.url()).pathname,
        startOffsetMs: Math.max(0, Math.round(startTime - startedAt)), durationMs }
      if (failed) {
        record.failure = request.failure()?.errorText || 'request failed'
        const aborted = /abort|cancelled|canceled/i.test(record.failure)
        evidence[aborted ? 'abortedRequests' : 'failedRequests'].push(record)
      }
      evidence.requestTimings.push(record)
      if (!failed) pending.push(Promise.resolve().then(() => request.response()).then((response) => {
        record.status = response?.status() ?? null
      }).catch(() => {}))
    } catch { /* Optional request evidence never changes the probe verdict. */ }
  }
  const finished = (request) => complete(request, false)
  const failed = (request) => complete(request, true)
  source.on('request', request)
  source.on('requestfinished', finished)
  source.on('requestfailed', failed)
  return async () => {
    source.off('request', request)
    source.off('requestfinished', finished)
    source.off('requestfailed', failed)
    await Promise.allSettled(pending)
  }
}

export const test = base.extend({
  firstRun: [false, { option: true }],
  walkPostgres: [false, { option: true, scope: 'worker' }],
  workerFacts: [async ({}, use) => {
    await use({ engineMounted: undefined })
  }, { scope: 'worker' }],
  workerStack: [async ({ walkPostgres, workerFacts }, use, workerInfo) => {
    let stack
    let bootError
    let witness
    try {
      // workerIndex also distinguishes replacement workers after a failure.
      try {
        workerFacts.walkPostgres = walkPostgres
        const options = { slot: workerInfo.workerIndex, slots: workerInfo.config.workers }
        try {
          stack = await startStack({ ...options, ...(walkPostgres ? { postgres: 'ephemeral' } : {}) })
        } catch (error) {
          if (!walkPostgres || !['postgres_binaries_missing', 'SOLVER_MISSING'].includes(error.code)) throw error
          // Keep the ordinary stack available so runProbe can attach a normal
          // UNAVAILABLE result for project states without crashing the worker.
          workerFacts.postgresUnavailableReason = error.code === 'SOLVER_MISSING' ? 'autofill_solver_missing' : error.code
          workerFacts.postgresDiagnostic = error.message
          stack = await startStack(options)
        }
        // Use the launched stack's environment, not the host's auth setting.
        // Match server/deps.py's auth_live() spellings.
        workerFacts.authLive = ['1', 'true', 'yes', 'on'].includes((stack.env?.LEAF_AUTH_LIVE || '0').trim().toLowerCase())
        const bootTimeMs = Date.now()
        const readiness = JSON.parse(await readFile(join(stack.root, 'ready.json'), 'utf8'))
        witness = { ready: true, instance: stackInstanceRef(stack.root, readiness.launcher_pid, bootTimeMs),
          ...(stack.canonicalWorkerTools?.length ? { canonicalWorkerTools: stack.canonicalWorkerTools } : {}),
          ...stack.postgres, ...(workerFacts.postgresUnavailableReason ? {
            postgres: 'unavailable', reason: workerFacts.postgresUnavailableReason, diagnostic: workerFacts.postgresDiagnostic,
          } : {}) }
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
      serverErrors: [], serverErrorsDropped: 0,
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
    const pendingDiagnostics = []
    const consoleError = (message) => {
      if (message.type() === 'error') walkEvidence.consoleErrors.push({ text: message.text(), location: message.location() })
    }
    const pageError = (error) => { walkEvidence.pageErrors.push({ message: error.message, stack: error.stack }) }
    const requestFailed = (request) => {
      if (walkEvidence.requestTimings) return // The probe observer owns bounded request evidence while probes run.
      const record = { url: request.url(), method: request.method(), error: request.failure()?.errorText }
      const aborted = ['net::ERR_ABORTED', 'NS_BINDING_ABORTED', 'cancelled'].includes(record.error)
      walkEvidence[aborted ? 'abortedRequests' : 'failedRequests'].push(record)
    }
    const response = (reply) => {
      const record = { url: reply.url(), method: reply.request().method(), status: reply.status() }
      walkEvidence.responses.push(record)
      if (reply.status() >= 400) walkEvidence.httpErrors.push(record)
      if (reply.status() >= 500) {
        if (walkEvidence.serverErrors.length >= SERVER_ERROR_CAP) {
          walkEvidence.serverErrorsDropped += 1
          return
        }
        const diagnosticRecord = { ...record, errorId: null, matched: false, serverOutput: '' }
        walkEvidence.serverErrors.push(diagnosticRecord)
        pendingDiagnostics.push((async () => {
          let timer
          const body = await Promise.race([
            Promise.resolve().then(() => reply.text()),
            new Promise((resolve) => { timer = setTimeout(() => resolve(''), 2000) }),
          ]).catch(() => '').finally(() => clearTimeout(timer))
          diagnosticRecord.errorId = extractErrorId(body)
          const diagnostic = serverDiagnostic(stack?.output?.(), diagnosticRecord.errorId)
          diagnosticRecord.matched = diagnostic.matched
          diagnosticRecord.serverOutput = diagnostic.text
        })())
      }
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
      await Promise.allSettled(pendingDiagnostics)
      for (const record of walkEvidence.serverErrors) {
        const diagnostic = serverDiagnostic(stack?.output?.(), record.errorId)
        if (diagnostic.matched || !record.matched) {
          record.matched = diagnostic.matched
          record.serverOutput = diagnostic.text
        }
      }
    }
  },
})

function catalogSha256(catalog) {
  return createHash('sha256').update(JSON.stringify(catalog)).digest('hex')
}

export function protectedStateDigest(snapshot) {
  return createHash('sha256').update(JSON.stringify(snapshot)).digest('hex')
}

const groupNames = { draw: 'Draw', modify: 'Modify', clipboard: 'Clipboard', properties: 'Properties',
      annotation: 'Annotation', block: 'Block', groups: 'Groups', 'solar-panels': 'Panel placement',
      view: 'View', version: 'Version', author: 'Author', rail: 'Rail' }
const control = (page, recipe) => {
  if (typeof recipe.css === 'string' && recipe.css.length > 0) return (recipe.scope ? control(page, recipe.scope) : page).locator(recipe.css)
  let scope = recipe.scope ? control(page, recipe.scope) : page
  if (recipe.panelName) scope = scope.getByRole('group', { name: recipe.panelName, exact: true })
  if (recipe.group && recipe.role !== 'combobox') {
    scope = scope.getByRole('group', { name: groupNames[recipe.group], exact: true })
  }
  const target = scope.getByRole(recipe.role, { name: recipe.name, exact: recipe.exact !== false })
  return recipe.selector ? target.locator(recipe.selector) : target
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

export function studioJobMonitorContent(page) {
  return page.locator('aside.rail').filter({ has: page.getByRole('heading', { name: /^Job monitor/ }) })
    .locator('.rail-ske, .rail-empty, .rail-ledger > *').first()
}

// Values are from same-tool server inputs, before schema defaults/enums/minima.
// Labels and select option text follow SchemaForm's accessible names.
export const SOLAR_REQUIRED_FORM_INPUTS = Object.freeze(Object.fromEntries(Object.entries({
  'solar-nec-ac-voltage-drop': {
    current_a: { label: 'Current a', value: 100 },
    one_way_length_ft: { label: 'One way length ft', value: 100 },
    r_ohm_per_1000ft: { label: 'R ohm per 1000ft', value: 0.1 },
    x_ohm_per_1000ft: { label: 'X ohm per 1000ft', value: 0 },
    power_factor: { label: 'Power factor', value: 1 },
    phase: { label: 'Phase', value: 1, select: '1' },
    source_voltage: { label: 'Source voltage', value: 480 },
  },
  'solar-nec-ampacity-correction': {
    base_ampacity: { label: 'Base ampacity', value: 100 },
    temp_factor: { label: 'Temp factor', value: 0.91 },
    conduit_factor: { label: 'Conduit factor', value: 0.8 },
  },
  'solar-nec-conduit-fill': {
    conductor_gauge: { label: 'Conductor gauge', value: '12 AWG', select: '12 AWG' },
    current_carrying_count: { label: 'Current carrying count', value: 3 },
    conduit_type: { label: 'Conduit type', value: 'EMT', select: 'EMT' },
    conductor_insulation: { label: 'Conductor insulation', value: 'THWN2', select: 'THWN2' },
  },
  'solar-nec-feeder-ocpd': {
    continuous_current_a: { label: 'Continuous current a', value: 100 },
    is_continuous: { label: 'Is continuous', value: true, select: 'True' },
    egc_material: { label: 'Egc material', value: 'Copper', select: 'Copper' },
  },
  'solar-trench-manual': {
    operation: { label: 'Operation', value: 'add-trench', select: 'add-trench' },
    expected_rev: { label: 'Expected rev', value: 0 },
    start_x: { label: 'Start x', value: -80 },
    start_y: { label: 'Start y', value: 0 },
    end_x: { label: 'End x', value: 320 },
    end_y: { label: 'End y', value: 0 },
  },
}).map(([tool, fields]) => [tool, Object.freeze(Object.fromEntries(Object.entries(fields).map(([key, field]) =>
  [key, Object.freeze({ ...field, source: tool === 'solar-trench-manual'
    ? `scenario/test input: server/tests/test_solar_tool_solar_trench_manual.py:${key === 'operation' ? 140 : 40}`
    : 'scenario/test input: server/tests/test_sip_r6_read.py:28-33' })])))])))

export const PANEL_PERFORMANCE_BUDGET_SOURCE = 'WL28-PANEL-PERFORMANCE-BUDGETS.md'
export const PANEL_PERFORMANCE_VIEWPORTS = Object.freeze({
  desktop: Object.freeze({ width: 1440, height: 1000 }),
  phone: Object.freeze({ width: 390, height: 844 }),
})
// Product acceptance targets, not historical baselines. These probes currently
// run on desktop only; retain the declared phone target without widening scope.
export const PANEL_PERFORMANCE_BUDGETS = Object.freeze(Object.fromEntries([
  'action:bar-shortcuts', 'control:claude-accounts', 'control:linked-services', 'control:version-history',
].map(featureId => [featureId, Object.freeze({
  source: PANEL_PERFORMANCE_BUDGET_SOURCE, state: 'ready', viewports: Object.freeze(['desktop']),
  maxDurationMs: Object.freeze({ desktop: 2000, phone: 3000 }), maxRequiredSteps: 1,
})])))

export async function preparePanelPerformanceCapture({ page, probe, viewport, locator, evidence,
  createCapture = createPerformanceMeasurementCapture, assertions = expect }) {
  const budget = PANEL_PERFORMANCE_BUDGETS[probe.featureId]
  if (!budget || probe.state !== budget.state || !budget.viewports.includes(viewport)) return null
  const diagnostic = () => {
    evidence.measurementDiagnostics ||= []
    evidence.measurementDiagnostics.push({ persona: 'performance', message: 'Optional persona observation failed' })
  }
  try {
    const url = page.url()
    if (!/^\/app(?:\/|$)/.test(new URL(url).pathname)
      || !await page.evaluate(() => document.readyState === 'complete') || page.url() !== url) return null
    for (const role of ['dialog', 'alertdialog', 'menu', 'listbox']) {
      const overlays = page.getByRole(role)
      for (let index = 0; index < await overlays.count(); index++) {
        if (await overlays.nth(index).isVisible()) return null
      }
    }
    let launcher = locator
    if (probe.featureId === 'action:bar-shortcuts') {
      const project = page.getByRole('navigation', { name: 'Workspace controls', exact: true })
        .getByRole('button', { name: 'Project', exact: true })
      launcher = await project.isVisible() ? project
        : page.getByRole('tablist', { name: 'Workspace profile', exact: true }).getByRole('tab').first()
    }
    if (!await launcher.isVisible() || !await launcher.isEnabled()) return null
    const suffix = `${probe.featureId}[${probe.state}]@${viewport}`
    let capture = createCapture({ reference: `performance:${suffix}`, baseline: {
      reference: `${budget.source}:${probe.featureId}@${viewport}`,
      maxDurationMs: budget.maxDurationMs[viewport], maxRequiredSteps: budget.maxRequiredSteps,
    } })
    let started = false
    const observe = callback => {
      if (!capture) return
      try { callback(capture) } catch { capture = null; diagnostic() }
    }
    return {
      // The real dispatch stays outside the optional observer's catch.
      async dispatch(action) {
        observe(current => {
          if (!started) { current.start(); started = true }
          current.requiredStep()
        })
        return action()
      },
      async finish() {
        if (!capture || !started) return
        try {
          const spec = ACCESSIBILITY_DIALOG_SCOPE[probe.featureId]
          const panel = page.getByRole('dialog', { name: spec.name, exact: true })
          const closeName = probe.featureId === 'action:bar-shortcuts' ? 'Close'
            : probe.featureId === 'control:version-history' ? 'Close version history' : spec.initialFocus.name
          const close = panel.getByRole('button', { name: closeName, exact: true })
          await assertions(panel).toBeVisible()
          await assertions(close).toBeVisible()
          await assertions(close).toBeEnabled()
          const focus = spec.initialFocus.role ? panel.getByRole(spec.initialFocus.role,
            { name: spec.initialFocus.name, exact: true }) : launcher
          await assertions(focus).toBeFocused()
          capture.observeWait({ observed: false, progress: '', reason: '' })
          capture.finish()
          const result = capture.result()
          if (result) addPersonaFragment(evidence, { persona: 'performance', measurements: result,
            bundleReference: `walk:${suffix}` })
          else diagnostic()
        } catch { diagnostic() }
        capture = null
      },
    }
  } catch { diagnostic(); return null }
}

export function studioDrawerLocator(page, drawer) {
  if (!['jobs', 'nav', 'result'].includes(drawer)) throw new Error(`Unknown Studio drawer: ${drawer}`)
  if (drawer === 'result') return page.locator('.result-block')
  return drawer === 'jobs' ? studioJobMonitorContent(page) : page.locator('aside.nav')
}

export async function assertStudioDrawer(page, drawer, open, assertions = expect) {
  const content = studioDrawerLocator(page, drawer)
  const sidebar = page.getByRole('region', { name: 'Studio sidebar', exact: true })
  if (open) {
    await assertions(studioRailButton(page)).toHaveAttribute('aria-expanded', 'true')
    await assertions(sidebar).toBeVisible()
    if (drawer === 'jobs') await assertions(page.getByRole('heading', { name: /Job monitor/ })).toBeVisible()
    await assertions(content).toBeVisible()
  } else {
    await assertions(sidebar).toBeHidden()
    if (drawer === 'nav') await assertions(content).toBeHidden()
  }
}

export async function setStudioDrawer(page, drawer, open, assertions = expect) {
  if (open) await revealStudioRail(page)
  else if (await page.getByRole('region', { name: 'Studio sidebar', exact: true }).isVisible()) {
    await page.getByRole('button', { name: 'Close Studio', exact: true }).click()
  }
  await assertStudioDrawer(page, drawer, open, assertions)
}

export async function studioDrawerProbeLocator(page, probe, describeControl) {
  const drawer = probe.locator.studioDrawer
  if (!drawer || !await studioRailButton(page).isVisible()) return null
  const closing = probe.state === 'open' || probe.state === 'drawer-open'
  describeControl?.({ role: 'button', name: closing ? 'Close Studio' : 'Studio' })
  return closing ? page.getByRole('button', { name: 'Close Studio', exact: true }) : studioRailButton(page)
}

export async function railExpandLauncher(probe, runtime, locator, describeControl) {
  if (probe.featureId !== 'action:rail-expand' || probe.state !== 'ready'
    || runtime.testInfo.project.name === 'phone' || await locator.isVisible()) return null
  const launchers = runtime.page.getByRole('button', { name: /^Open .+ in the tool rail \([0-9]+ tools\)$/ })
  for (let index = 0; index < await launchers.count(); index++) {
    const launcher = launchers.nth(index)
    if (!await launcher.isVisible()) continue
    const name = await launcher.getAttribute('aria-label') || await launcher.getAttribute('title')
    const family = name?.match(/^Open (.+) in the tool rail \([0-9]+ tools\)$/)?.[1]
    if (!family) throw new Error('Tool rail launcher must identify its family')
    runtime.railExpandFamily = family
    describeControl?.({ role: 'button', name })
    return launcher
  }
  return null
}

export async function assertRailExpandFamily(page, family, assertions = expect) {
  await assertStudioDrawer(page, 'nav', true, assertions)
  const name = new RegExp('^' + family.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*·')
  const section = page.locator('aside.nav .section').filter({ has: page.getByRole('button', { name }) })
  await assertions(section.locator('.section-body')).toBeVisible()
}

export async function prepareChooserStart(runtime, assertions = expect) {
  const { page, evidence } = runtime
  evidence.chooserTask = {
    startingChooser: 'What would you like to work on?',
    targetLabel: 'Open project or material',
    nextStep: 'Project sidebar visible; Close Project focused; Project expanded',
  }
  const heading = page.getByRole('heading', { name: evidence.chooserTask.startingChooser, exact: true })
  if (!await heading.isVisible()) {
    await page.getByRole('navigation', { name: 'Workspace controls', exact: true })
      .getByRole('button', { name: 'Choose', exact: true }).click()
  }
  await assertions(heading).toBeVisible()
  await assertions(page.getByRole('region', { name: 'Project sidebar', exact: true })).toBeHidden()
}

export async function assertChooserOpen(runtime, assertions = expect) {
  const { page } = runtime
  await assertions(page.getByRole('region', { name: 'Project sidebar', exact: true })).toBeVisible()
  await assertions(page.getByRole('button', { name: 'Close Project', exact: true })).toBeFocused()
  await assertions(page.getByRole('navigation', { name: 'Workspace controls', exact: true })
    .getByRole('button', { name: 'Project', exact: true })).toHaveAttribute('aria-expanded', 'true')
}

export async function runChooserOpen(probe, runtime, assertions = expect) {
  const { page, evidence } = runtime
  const primaryLocator = page.getByRole('button', { name: 'Open project or material', exact: true })
  const navigationLocator = page.getByRole('navigation', { name: 'Workspace controls', exact: true })
  let nextStepFound = false, failure
  try {
    await assertions(primaryLocator).toBeVisible()
    await assertions(primaryLocator).toHaveCount(1)
    await primaryLocator.click()
    evidence.oracleReached = probe.assertion.assertionId
    await assertEffect(probe, runtime, primaryLocator, {}, assertions)
    nextStepFound = true
  } catch (error) { failure = error }
  try {
    await navigationLocator.getByRole('button', { name: 'Choose', exact: true }).click()
    await assertions(page.getByRole('heading', { name: 'What would you like to work on?', exact: true })).toBeVisible()
  } catch (error) { failure ||= error }
  try {
    const measurements = await collectUnfamiliarMeasurements({ page, primaryLocator, navigationLocator,
      nextStepFound, reference: 'unfamiliar:control:chooser-open[ready]@desktop' })
    if (measurements) addPersonaFragment(evidence, { persona: 'unfamiliar', measurements,
      bundleReference: 'walk:control:chooser-open[ready]@desktop' })
  } catch { /* Optional evidence never changes the functional verdict. */ }
  if (failure) throw failure
}

export function addPersonaFragment(evidence, { persona, measurements, bundleReference }) {
  try {
    const fragments = (evidence.personaFragments || []).filter(fragment => fragment.persona !== persona)
    fragments.push({ persona, measurements })
    let bundle = composePersonaMeasurementBundle({ reference: bundleReference, fragments })
    if (!bundle) {
      evidence.measurementDiagnostics ||= []
      evidence.measurementDiagnostics.push({ message: 'Optional persona measurement fragment rejected' })
      bundle = composePersonaMeasurementBundle({ reference: bundleReference, fragments: evidence.personaFragments || [] })
    } else evidence.personaFragments = fragments
    evidence.measurements = bundle?.measurements ?? null
    evidence.measurementSources = bundle?.sources ?? []
    evidence.measurementConflicts = bundle?.conflicts ?? []
  } catch { /* Optional evidence never changes the functional verdict. */ }
}

// Optional observers consume only facts supplied at the existing assertion sites.
export function createWalkPersonaObservers({ probe, viewport, evidence,
  endUser = createEndUserMeasurementCapture, support = collectSupportMeasurements,
  opsSre = createOpsSreMeasurementCapture, redHat = createRedHatMeasurementCapture,
  qcDev = createQcDevMeasurementCapture, uiUx = collectUiUxMeasurements }) {
  const captures = new Map(), observed = new Set()
  const suffix = `${probe.featureId}[${probe.state}]@${viewport}`
  const retry = probe.featureId === 'action:bar-retry' && probe.state === 'retry-history'
  const diagnostic = (persona) => {
    captures.delete(persona)
    observed.delete(persona)
    evidence.measurementDiagnostics ||= []
    evidence.measurementDiagnostics.push({ persona, message: 'Optional persona observation failed' })
  }
  const create = (persona, factory, options = {}) => {
    try {
      const capture = factory({ reference: `${persona}:${suffix}`, ...options })
      if (capture) captures.set(persona, capture)
    } catch { diagnostic(persona) }
  }
  if (viewport === 'desktop' && probe.featureId === 'control:version-history' && probe.state === 'ready') {
    create('end-user', endUser, { expectedEffect: 'version-history-open' })
    try { captures.get('end-user')?.start() } catch { diagnostic('end-user') }
    create('qc-dev', qcDev, { action: 'control:version-history', expectedResult: 'version-history-open',
      expectedOrder: ['control:version-history', 'version-history-open'] })
    captures.set('ui-ux-dev', uiUx)
  }
  if (retry && viewport === 'desktop') {
    create('qc-dev', qcDev, { action: 'action:bar-retry', expectedResult: 'Versions restored',
      expectedOrder: ['action:bar-retry', 'history-recovery-request', 'Versions restored'] })
  }
  if (retry && ['desktop', 'phone'].includes(viewport)) {
    create('ops-sre', opsSre)
    captures.set('support', support)
  }
  if (probe.featureId && probe.state && probe.assertion?.target === 'engine-refusal'
    && ['desktop', 'phone'].includes(viewport)) create('red-hat', redHat)
  return {
    async observe(persona, event, value) {
      const capture = captures.get(persona)
      if (!capture) return
      try {
        if (persona === 'ui-ux-dev') {
          if (event !== 'completion') return
          const measurements = await capture({ ...value, reference: `ui-ux-dev:${suffix}` })
          if (measurements) {
            captures.set(persona, { ...measurements, coverage: { kind: 'automated-proxy',
              predicates: ['ui-ux-dev.completion-proxy'] } })
            observed.add(persona)
          }
        } else if (persona === 'support') {
          if (event !== 'alert' || value.errorObserved !== true) return
          const { page, alert } = value
          const measurements = await capture({ page, errorObserved: true, reference: `support:${suffix}`,
            errorLocator: alert.locator('.pane-fail-title'), explanationLocator: alert.locator('.pane-fail-reason'),
            recoveryLocator: alert.getByRole('button', { name: 'Retry', exact: true }) })
          if (measurements) { captures.set(persona, measurements); observed.add(persona) }
        } else {
          if (persona === 'red-hat' && ['before', 'after'].includes(event)) value = protectedStateDigest(value)
          await capture[event](value)
          if ((persona === 'end-user' && event === 'observeEffect')
            || (persona === 'qc-dev' && event === 'start')
            || (persona === 'ops-sre' && event === 'observeFailure' && value === true)
            || (persona === 'red-hat' && event === 'observeRefusal' && value.observed === true)) observed.add(persona)
        }
      } catch { diagnostic(persona) }
    },
    async finish() {
      for (const persona of observed) {
        try {
          const capture = captures.get(persona)
          const measurements = ['support', 'ui-ux-dev'].includes(persona) ? capture
            : persona === 'red-hat' ? await capture.result() : await capture.finish()
          if (measurements) addPersonaFragment(evidence, { persona, measurements, bundleReference: `walk:${suffix}` })
        } catch { diagnostic(persona) }
      }
      observed.clear()
    },
  }
}

export async function prepareQcDevHistory(probe, runtime, locator, before, assertions = expect) {
  if (probe.featureId !== 'control:version-history' || probe.state !== 'ready'
    || runtime.testInfo?.project?.name !== 'desktop') return
  try {
    // The private fixture must already be loaded; this observer never opens it.
    if (!runtime.drawingId || runtime.evidence.drawingId !== runtime.drawingId || runtime.failedDrawing
      || before.visible !== false) return
    await assertions(canvas(runtime.page)).toBeVisible()
    await assertions(runtime.page.getByRole('dialog', { name: 'Version history', exact: true })).toBeHidden()
    await assertions(locator).toHaveAttribute('aria-expanded', 'false')
    await runtime.walkPersonaObservers?.observe('qc-dev', 'start')
  } catch { /* A failed precondition cannot produce a QC PASS. */ }
}

export function mergeAccessibilityMeasurements(existing, result) {
  const merge = (previous, observed) => {
    const merged = { ...previous }
    for (const [key, value] of Object.entries(observed || {})) {
      if (value === undefined) continue
      merged[key] = value && typeof value === 'object' && !Array.isArray(value)
        ? merge(previous?.[key], value) : value
    }
    return merged
  }
  return merge(existing, Object.fromEntries(['reference', 'overlay', 'focus']
    .filter(key => result?.[key] !== undefined).map(key => [key, result[key]])))
}

export const ACCESSIBILITY_DIALOG_SCOPE = Object.freeze({
  'control:linked-services': Object.freeze({ states: Object.freeze(['ready', 'failed-load']), name: 'Linked services',
    initialFocus: Object.freeze({ role: 'button', name: 'Close linked services panel', key: 'close-linked-services' }),
    expectedModal: false, expectedEscapeClosed: true }),
  'control:claude-accounts': Object.freeze({ states: Object.freeze(['ready', 'failed-load']), name: 'Claude accounts',
    initialFocus: Object.freeze({ role: 'button', name: 'Close Claude account panel', key: 'close-claude-accounts' }),
    expectedModal: false, expectedEscapeClosed: true }),
  'control:session-details': Object.freeze({ states: Object.freeze(['ready', 'failed-load']), name: 'Session details' }),
  'control:version-history': Object.freeze({ states: Object.freeze(['ready']), name: 'Version history',
    initialFocus: Object.freeze({ key: 'control:version-history' }), expectedModal: false, expectedEscapeClosed: true }),
  'action:bar-shortcuts': Object.freeze({ states: Object.freeze(['ready']), name: 'Keyboard shortcuts',
    initialFocus: Object.freeze({ role: 'switch', name: 'Single-key shortcuts', key: 'single-key-shortcuts' }),
    expectedModal: false, expectedEscapeClosed: true }),
})

export async function accessibilityDialogTargets({ page, probe, viewport, locator }) {
  const spec = ACCESSIBILITY_DIALOG_SCOPE[probe.featureId]
  if (viewport !== 'desktop' || !spec?.states.includes(probe.state)) return null
  let opener = locator
  let key = probe.featureId
  if (probe.featureId === 'action:bar-shortcuts') {
    const project = page.getByRole('navigation', { name: 'Workspace controls', exact: true })
      .getByRole('button', { name: 'Project', exact: true })
    const projectVisible = await project.isVisible()
    opener = projectVisible ? project : page.getByRole('tablist', { name: 'Workspace profile', exact: true })
      .getByRole('tab').first()
    key = projectVisible ? 'workspace-project' : 'workspace-profile-tab'
  }
  const target = { key, locator: opener }
  const openTarget = spec.initialFocus?.role
    ? { key: spec.initialFocus.key,
      locator: page.getByRole(spec.initialFocus.role, { name: spec.initialFocus.name, exact: true }) } : target
  return { openTarget, returnTarget: { ...target },
    dialog: page.getByRole('dialog', { name: spec.name, exact: true }) }
}

function accessibilityDiagnostic(evidence, phase) {
  evidence.accessibilityDiagnostics ||= []
  evidence.accessibilityDiagnostics.push({ phase, message: 'Optional accessibility observation failed' })
}

export async function prepareAccessibilityCapture({ page, probe, viewport, locator, evidence, cleanup,
  createCapture = createAccessibilityMeasurementCapture }) {
  try {
    const targets = await accessibilityDialogTargets({ page, probe, viewport, locator })
    if (!targets) return null
    const capture = createCapture({ page, ...targets,
      reference: `accessibility:${probe.featureId}[${probe.state}]@${viewport}` })
    cleanup.push(async () => {
      try { await capture.dispose() } catch { accessibilityDiagnostic(evidence, 'dispose') }
    })
    return { ...capture, expectedModal: ACCESSIBILITY_DIALOG_SCOPE[probe.featureId].expectedModal }
  } catch { accessibilityDiagnostic(evidence, 'prepare'); return null }
}

export async function followAccessibilityCapture({ capture, page, evidence,
  bundleReference = `walk:${evidence.featureId}[${evidence.state}]@${evidence.viewport}` }) {
  if (!capture) return
  let phase = 'opened'
  try {
    await capture.opened()
    phase = 'tab'
    await page.keyboard.press('Tab')
    await capture.afterTab()
    phase = 'escape'
    await page.keyboard.press('Escape')
    await capture.afterEscape()
  } catch { accessibilityDiagnostic(evidence, phase) }
  try {
    const result = capture.result()
    const measurements = Object.fromEntries(['reference', 'overlay', 'focus']
      .filter(key => result?.[key] !== undefined).map(key => [key, result[key]]))
    if (typeof capture.expectedModal === 'boolean') {
      measurements.overlay = { ...measurements.overlay, expectedModal: capture.expectedModal }
    }
    addPersonaFragment(evidence, { persona: 'accessibility', measurements, bundleReference })
    evidence.accessibilityDiagnostics ||= []
    if (result.diagnostics) evidence.accessibilityDiagnostics.push({ phase: 'observations', ...result.diagnostics })
  } catch { accessibilityDiagnostic(evidence, 'result') }
}

async function disclosePhonePlan(page) {
  const tab = page.getByRole('tablist', { name: 'Ribbon', exact: true })
    .getByRole('tab', { name: 'View', exact: true })
  if (await tab.getAttribute('aria-selected') !== 'true') await tab.click()
  await discloseControlPanel(page, { group: 'view' })
}

export async function readPhonePanel(page, panel) {
  if (!['Jobs', 'Plan'].includes(panel)) throw new Error(`Unknown phone panel: ${panel}`)
  if (!await studioRailButton(page).isVisible()) return await panelButton(page, panel).getAttribute('aria-expanded') === 'true'
  if (panel === 'Plan') return await page.getByRole('region', { name: 'Entitlements', exact: true }).isVisible()
  return await page.getByRole('region', { name: 'Studio sidebar', exact: true }).isVisible()
    && await page.getByRole('heading', { name: /Job monitor/ }).isVisible()
    && await studioJobMonitorContent(page).isVisible()
}

export async function assertPhonePanel(page, panel, open, assertions = expect) {
  if (!['Jobs', 'Plan'].includes(panel)) throw new Error(`Unknown phone panel: ${panel}`)
  const studio = studioRailButton(page)
  if (!await studio.isVisible()) {
    await assertions(panelButton(page, panel)).toHaveAttribute('aria-expanded', String(open))
    return
  }
  if (panel === 'Plan') {
    await assertions(page.locator('.app')).toHaveAttribute('data-drawer', open ? 'plan' : 'none')
    await assertions(page.getByRole('region', { name: 'Entitlements', exact: true }))[open ? 'toBeVisible' : 'toBeHidden']()
    return
  }
  await assertions(studio).toHaveAttribute('aria-expanded', String(open))
  await assertions(page.getByRole('region', { name: 'Studio sidebar', exact: true }))[open ? 'toBeVisible' : 'toBeHidden']()
  if (open) {
    await assertions(page.getByRole('heading', { name: /Job monitor/ })).toBeVisible()
    await assertions(studioJobMonitorContent(page)).toBeVisible()
  }
}

export async function setPhonePanel(page, panel, open, assertions = expect) {
  if (!['Jobs', 'Plan'].includes(panel)) throw new Error(`Unknown phone panel: ${panel}`)
  const studio = studioRailButton(page)
  if (!await studio.isVisible()) {
    const button = panelButton(page, panel)
    await assertions(button).toBeVisible()
    if ((await button.getAttribute('aria-expanded') === 'true') !== open) await button.click()
  } else if (panel === 'Jobs') {
    await setStudioDrawer(page, 'jobs', open, assertions)
  } else if ((await page.locator('.app').getAttribute('data-drawer') === 'plan') !== open) {
    await disclosePhonePlan(page)
    await page.getByRole('toolbar', { name: 'Drafting tools', exact: true })
      .getByRole('button', { name: 'properties', exact: true }).click()
  }
  await assertPhonePanel(page, panel, open, assertions)
}

async function phoneProbeLocator(page, probe) {
  const recipe = probe.locator.phone
  if (!recipe?.phonePanel) return null
  if (!await studioRailButton(page).isVisible()) return recipe.legacy || probe.locator
  if (recipe.phonePanel === 'Plan') await disclosePhonePlan(page)
  return recipe
}

async function assertPhoneDrawersClosed(page, assertions) {
  if (await studioRailButton(page).isVisible()) {
    await assertPhonePanel(page, 'Jobs', false, assertions)
    await assertPhonePanel(page, 'Plan', false, assertions)
  } else {
    for (const label of ['Catalog', 'Jobs', 'Result', 'Plan']) await assertions(panelButton(page, label)).toHaveAttribute('aria-expanded', 'false')
  }
}

export function absenceNamePattern(locator) {
  if (locator?.unavailableName !== undefined) {
    if (locator.unavailableName instanceof RegExp
      || typeof locator.unavailableName === 'string' && locator.unavailableName.trim()) return locator.unavailableName
    throw new Error('Absence oracle requires a stable action name')
  }
  const name = locator?.availableName || locator?.name
  const label = typeof name === 'string' ? name.split(' (unavailable: ')[0] : ''
  if (!label.trim()) throw new Error('Absence oracle requires a stable action name')
  return new RegExp('^' + label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '(?: \\(unavailable: .+\\))?$')
}

export async function resolveSurfaceControl(page, probe) {
  const profile = page.getByRole('navigation', { name: 'Workspace controls', exact: true })
    .getByRole('combobox', { name: 'Workspace profile', exact: true })
  const modern = await profile.isVisible()
  const legacy = modern
    ? page.getByRole('tablist', { name: 'Workspace profile', exact: true, includeHidden: true })
      .getByRole('tab', { name: probe.locator.name, exact: probe.locator.exact !== false, includeHidden: true })
    : control(page, probe.locator)
  return {
    locator: modern ? profile : legacy, legacy, modern,
    activate: async () => {
      if (modern) await profile.selectOption(probe.sourceId)
      else await legacy.click()
    },
    assertSelected: async (assertions = expect) => {
      if (modern) {
        await assertions(profile).toHaveValue(probe.sourceId)
        await assertions(page.locator('section.workspace-environment')).toHaveAttribute('data-profile', probe.sourceId)
      } else await assertions(legacy).toHaveAttribute('aria-selected', 'true')
    },
  }
}

export async function assertSurfaceState(locator, state, assertions = expect) {
  await assertions(locator.locator('[data-state]')).toHaveAttribute('data-state', state)
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
export async function establishZoomBaseline(page, bounds = viewportBounds) {
  await bounds(page)
  const outline = page.getByRole('button', { name: 'Drawing overview', exact: true }).locator('.cad-overview-outline')
  const map = await outline.evaluate((element) => ({ width: Number(element.getAttribute('width')), height: Number(element.getAttribute('height')) }))
  for (let attempt = 0; attempt < 30; attempt++) {
    const viewport = await bounds(page)
    if (viewport.width > 0 && viewport.height > 0 && viewport.width < map.width * 0.4 && viewport.height < map.height * 0.4) {
      return { map, viewport, unsaturated: true }
    }
    await viewButton(page, 'Zoom in').click()
  }
  throw new Error('Zoom-out setup could not establish an unsaturated overview viewport')
}
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
    name: 'Undo version (unavailable: No saved versions yet)', exact: true,
  })
  await assertions(undo).toBeVisible()
  await assertions(undo).toBeDisabled()
}
async function requireWorkspace(runtime) {
  const { page } = runtime
  await enterWorkspaceEnvironment(page)
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
      const response = await page.request.get('/.leaf-walk-build.json', { timeout: 15_000 })
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
  LINE: [[222, 190], [222, 470]], INSERT: [[11.5, 20]], DIMENSION: [[222, 160]], LWPOLYLINE: [[160, 200]],
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

async function selectEntity(probe, runtime, recipe, panAttempt = 0) {
  if (!recipe.viewerOnly) await engineReady(probe, runtime)
  const { page } = runtime
  // Calibrate the flat drawing's projection from its production cursor
  // readout. Selection still uses native mouse input on the real canvas.
  const points = FIXTURE_PICK_POINTS[recipe.type]
  if (!points) await unsupported(probe, runtime, `The private DXF fixture has no ${recipe.type} entity to select`)
  if (recipe.editable === false && recipe.type === 'LINE') {
    await unsupported(probe, runtime, 'The engine exposes the fixture LINE as editable; a read-only LINE needs a provider fixture')
  }
  const more = page.getByRole('button', { name: 'More panels', exact: true })
  if (await more.isVisible() && await more.getAttribute('aria-expanded') === 'true') {
    await more.click({ timeout: 15_000 })
    await expect(more).toHaveAttribute('aria-expanded', 'false')
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
  const project = ([x, y]) => ({ x: samples[0].x + (x - readings[0][0]) * scaleX, y: samples[0].y + (y - readings[0][1]) * scaleY })
  let picks = points.map(project)
  if (recipe.multiple && recipe.type === 'LINE') {
    // Search each fixture segment, not just its midpoint: Solar's fixed flow
    // seat can hide the midpoint while leaving an end of the line exposed.
    picks = await canvas(page).evaluate((element, candidates) => candidates.map((segment) =>
      segment.find((point) => document.elementFromPoint(point.x, point.y) === element)),
    points.map(([, y]) => Array.from({ length: 55 }, (_, i) => project([222 + Math.ceil(i / 2) * 4 * (i % 2 ? -1 : 1), y]))))
    if (picks.some((point) => !point)) {
      expect(panAttempt, 'both fixture lines must become exposed through normal viewport controls').toBeLessThan(4)
      const expand = page.getByRole('button', { name: 'Expand drawing overview', exact: true })
      if (await expand.isVisible()) await expand.click()
      const overview = page.getByRole('button', { name: 'Drawing overview', exact: true })
      const before = await viewportBounds(page)
      await overview.press(panAttempt < 2 ? 'ArrowUp' : 'ArrowLeft')
      await expect.poll(() => viewportBounds(page)).not.toEqual(before)
      return selectEntity(probe, runtime, recipe, panAttempt + 1)
    }
  }
  const clickPoint = async (point) => {
    const exposed = await canvas(page).evaluate((element, point) => document.elementFromPoint(point.x, point.y) === element, point)
    expect(exposed, 'the selected entity must be visible and uncovered on the drawing canvas').toBe(true)
    await page.mouse.click(point.x, point.y)
  }
  await clickPoint(picks[0])
  if (recipe.multiple) {
    expect(points.length).toBeGreaterThan(1)
    await page.keyboard.down('Shift')
    try { await clickPoint(picks[1]) } finally { await page.keyboard.up('Shift') }
    await expect(page.getByTestId('dock-selection-count')).toHaveText('2 objects selected')
    await expect.poll(() => page.getByTestId('cad-edit-entity-list').locator('li[data-in-selection="true"] input')
      .evaluateAll((inputs) => inputs.map((input) => input.value).sort())).toEqual([0xA100, 0xA101].map(String).sort())
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
export async function setJobRail(page, open, phone = false, assertions = expect, detectStudio = true) {
  if (detectStudio && await studioRailButton(page).isVisible()) { await setStudioDrawer(page, 'jobs', open, assertions); return }
  if (phone) { await setPhonePanel(page, 'Jobs', open, assertions); return }
  const expand = page.getByRole('button', { name: /^Expand the job monitor \([0-9]+ live\)$/ })
  const collapse = page.getByRole('button', { name: 'Collapse the job monitor to a spine', exact: true })
  if (open ? await expand.isVisible() : await collapse.isVisible()) await (open ? expand : collapse).click()
  await assertions(open ? collapse : expand).toBeVisible()
  await assertions(page.locator('aside.rail .rail-ledger')).toHaveCount(open ? 1 : 0)
}
export async function setToolRail(page, open, phone, assertions = expect, detectStudio = true) {
  if (detectStudio && await studioRailButton(page).isVisible()) { await setStudioDrawer(page, 'nav', open, assertions); return }
  const expect = assertions
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
  'session-provenance': { role: 'dialog', name: 'Session details', close: 'Close details' },
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
    const response = await runtime.page.request.get('/.leaf-walk-build.json', { timeout: 15_000 })
    expect(response.ok(), 'Walk build manifest must be available').toBe(true)
    const marker = await response.json()
    facts.buildFlags = marker.flags || {}
  })()
  await facts.buildFlagsFetch
  await workerCatalog(facts, runtime.page.request)
  runtime.evidence.buildFlags = { ...facts.buildFlags }
}

// A passive tap: native worker input and delivery are forwarded unchanged.
// Full reparsed entity records avoid accepting a count-only geometry oracle.
export function installGeometryObserver() {
  const NativeWorker = globalThis.Worker
  const observations = { geometry: null, dispatches: [] }
  globalThis.__walkGeometry = observations
  globalThis.Worker = class extends NativeWorker {
    constructor(url, options) {
      super(url, options)
      this.walkGeometryEngine = String(url).includes('worker-browser')
      if (this.walkGeometryEngine) super.addEventListener('message', ({ data }) => {
        if (['documentLoaded', 'editApplied'].includes(data?.type) && Array.isArray(data.entities)) {
          observations.geometry = structuredClone({ documentId: data.documentId, entities: data.entities,
            blocks: data.blocks, groups: data.groups, linetypes: data.linetypes, dimstyles: data.dimstyles })
        }
      })
    }
    postMessage(message, ...rest) {
      if (this.walkGeometryEngine) observations.dispatches.push({ type: message?.type, op: message?.op })
      return super.postMessage(message, ...rest)
    }
  }
}

export async function observedGeometry(page) {
  const geometry = await page.evaluate(() => globalThis.__walkGeometry?.geometry)
  expect(geometry?.entities?.length, 'a real engine parse must precede geometry assertions').toBeGreaterThan(0)
  return geometry
}

const pasteButton = (page) => page.getByRole('group', { name: 'Clipboard', exact: true, includeHidden: true })
  .getByRole('button', { name: /^paste(?: \(unavailable: .+\))?$/, includeHidden: true })

export async function captureEngineRefusal(runtime) {
  const { page } = runtime
  const geometry = await observedGeometry(page)
  const selection = await page.getByTestId('dock-properties').innerText()
  const history = await page.getByRole('toolbar', { name: 'Quick access', exact: true }).getByRole('button', {
    name: /^(?:Undo|Redo) edit(?: \(unavailable: .+\))?$/,
  }).evaluateAll((buttons) => buttons.map((button) => ({ name: button.getAttribute('aria-label'),
    title: button.getAttribute('title'), disabled: button.disabled })))
  const clipboard = await pasteButton(page).evaluate((button) => ({ name: button.getAttribute('aria-label'),
    title: button.getAttribute('title'), disabled: button.disabled }))
  // Fresh isolated uploads start with an empty clipboard. Its disabled Paste
  // refusal is the observable, exact null-record signal before and after.
  expect(clipboard.disabled, 'refusal baseline needs an empty clipboard').toBe(true)
  expect(clipboard.name).toContain('nothing on the clipboard yet')
  const response = await page.request.get(`/api/drawings/${runtime.drawingId}/versions`)
  expect(response.ok()).toBe(true)
  return { geometry, selection, history, clipboard, versions: await response.json(),
    dispatches: await page.evaluate(() => globalThis.__walkGeometry.dispatches) }
}

export async function assertEngineRefusal(probe, runtime, before, assertions = expect) {
  const status = runtime.page.getByTestId('cad-edit-workbench').getByRole('status')
  runtime.evidence.engineRefusal = { expected: probe.assertion.refusal, observed: await status.innerText(), before }
  await assertions(status).toHaveText(probe.assertion.refusal)
  const after = await captureEngineRefusal(runtime)
  runtime.evidence.engineRefusal = { expected: probe.assertion.refusal, observed: await status.innerText(), before, after }
  if (probe.featureId && probe.state && probe.assertion.target === 'engine-refusal') {
    await runtime.walkPersonaObservers?.observe('red-hat', 'observeRefusal', {
      observed: true, visibleText: runtime.evidence.engineRefusal.observed,
    })
    await runtime.walkPersonaObservers?.observe('red-hat', 'after', after)
  }
  assertions(after).toEqual(before)
}

export function barNoRung(probe) {
  return probe.kind === 'action' && probe.locator?.trigger === 'keyboard'
    && probe.assertion.kind === 'disabled_with_reason' && probe.state === 'ready'
    && ['action:bar-escape', 'action:bar-retry'].includes(probe.featureId)
}

async function barState(page) {
  return page.evaluate(() => {
    const visible = (selector) => [...document.querySelectorAll(selector)].filter((element) => element.getClientRects().length)
      .map((element) => element.textContent.trim())
    return { url: location.href, selection: visible('.cockpit-sel'), drawing: visible('.cad-edit-workbench-doc'),
      surfaces: visible('.strip-running, .strip-failed, .cockpit-prompt, [role="dialog"], [role="listbox"]'),
      viewport: [...document.querySelectorAll('[data-overview-viewport]')].map((element) => ['x', 'y', 'width', 'height'].map((key) => element.getAttribute(key))),
      tabs: [...document.querySelectorAll('[role="tab"]')].map((element) => [element.textContent.trim(), element.getAttribute('aria-selected')]),
      disclosure: [...document.querySelectorAll('[aria-expanded]')].map((element) => [element.getAttribute('aria-label'), element.getAttribute('aria-expanded')]) }
  })
}

export async function captureBarNoRung(runtime) {
  runtime.noRungDispatches = []
  const observe = (request) => {
    const path = new URL(request.url()).pathname
    if (/\/api\/(?:run|route|capabilities|catalog|tools|session)(?:\/|$)/.test(path)
      || (request.method() !== 'GET' && path.startsWith('/api/'))) runtime.noRungDispatches.push({ method: request.method(), path })
  }
  runtime.page.on('request', observe)
  runtime.cleanup.push(async () => runtime.page.off('request', observe))
  return { state: await barState(runtime.page), value: await runtime.page.getByRole('combobox', { name: 'Command bar', exact: true }).inputValue(),
    engineDispatches: await runtime.page.evaluate(() => globalThis.__walkGeometry?.dispatches || []) }
}

export async function assertBarNoRung(probe, runtime, locator, before, assertions = expect) {
  await assertions(locator).toBeEnabled()
  await activate(probe, runtime, locator)
  await runtime.page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))
  const after = await barState(runtime.page)
  runtime.evidence.noRung = { expected: before.state, observed: after, dispatches: runtime.noRungDispatches }
  assertions(after).toEqual(before.state)
  assertions(runtime.noRungDispatches).toEqual([])
  assertions(await runtime.page.evaluate(() => globalThis.__walkGeometry?.dispatches || [])).toEqual(before.engineDispatches)
  await assertions(locator).toHaveValue(before.value)
  await locator.fill('walk text entry remains usable')
  await assertions(locator).toHaveValue('walk text entry remains usable')
  await locator.fill(before.value)
}

export async function assertCopiedGeometry(probe, runtime, before, assertions = expect) {
  assertions(await observedGeometry(runtime.page)).toEqual(before.geometry)
  const source = before.geometry.entities.find((entity) => String(entity.id) === String(before.sourceId))
  assertions(!!source, 'the copied source must be observed before activation').toBe(true)
  const anchor = source.vertices[0]
  const expected = { type: source.type, layer: source.layer,
    vertices: source.vertices.map(([x, y]) => [400 + x - anchor[0], 100 + y - anchor[1]]) }
  const { page } = runtime
  await pasteButton(page).click()
  await assertions(page.getByTestId('cockpit-prompt')).toHaveAccessibleName('PASTE command')
  await page.getByLabel('ribbon x', { exact: true }).fill('400,100')
  await page.getByTestId('cockpit-prompt-run').click()
  await assertions.poll(() => engineCount(page)).toBe(before.count + 1)
  const after = await observedGeometry(page)
  const added = after.entities.filter((entity) => !before.geometry.entities.some((old) => String(old.id) === String(entity.id)))
  assertions(added.map((entity) => ({ type: entity.type, layer: entity.layer, vertices: entity.vertices.map((point) => point.slice(0, 2)) }))).toEqual([
    expected,
  ])
  assertions(after.entities.filter((entity) => before.geometry.entities.some((old) => String(old.id) === String(entity.id)))).toEqual(before.geometry.entities)
  runtime.evidence.clipboardPaste = { base: [400, 100], source, expected, added }
}

export function expectedVersionHead(probe, versions) {
  const head = Number(versions.head)
  const candidates = versions.versions.map((row) => Number(row.v)).filter((v) => probe.sourceId === 'undo' ? v < head : v > head)
  if (!candidates.length) throw new Error(`No ${probe.sourceId} head exists in the captured version chain`)
  return probe.sourceId === 'undo' ? Math.max(...candidates) : Math.min(...candidates)
}

export async function assertVersionTransition(probe, runtime, before, assertions = expect) {
  const expected = expectedVersionHead(probe, before.versions)
  let observed
  await assertions.poll(async () => {
    const response = await runtime.page.request.get(`/api/drawings/${runtime.drawingId}/versions`)
    assertions(response.ok()).toBe(true)
    observed = await response.json()
    return Number(observed.head)
  }).toBe(expected)
  await assertions(runtime.page.locator('.cad-edit-workbench-doc')).toContainText(`${runtime.drawingId}-v${expected}.dxf`)
  await assertions.poll(async () => (await observedGeometry(runtime.page)).documentId).toBe(`${runtime.drawingId}-v${expected}.dxf`)
  const after = await observedGeometry(runtime.page)
  assertions(after.entities).toEqual(before.geometry.entities)
  assertions(await engineCount(runtime.page)).toBe(before.count)
  runtime.evidence.versionTransition = { expected, observed: observed.head, documentId: after.documentId }
}

export function solarBrowserCatalogRequired(name) {
  // These catalog records need the settings-form drawing-context fetch.
  // Solve proposal is available without that flag and arms a run decision.
  return ['solar-settings', 'solar-string-data', 'solar-autofill'].includes(name)
}

export async function readEngineModes(page) {
  // Ask the real bridge for its provider values; never publish synthetic modes.
  return page.evaluate(() => new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      window.removeEventListener('cockpit:modes', receive)
      reject(new Error('The engine mode bridge did not answer within 5 seconds'))
    }, 5_000)
    function receive({ detail }) {
      if (detail?.live !== true || typeof detail.ortho !== 'boolean' || typeof detail.osnap !== 'boolean') return
      clearTimeout(timer)
      window.removeEventListener('cockpit:modes', receive)
      resolve({ live: true, ortho: detail.ortho, osnap: detail.osnap })
    }
    window.addEventListener('cockpit:modes', receive)
    window.dispatchEvent(new CustomEvent('cockpit:modes-request'))
  }))
}

export async function assertEngineMode(probe, runtime, locator, assertions = expect) {
  const { page } = runtime
  const mode = probe.assertion.target.split(':')[1]
  const value = probe.assertion.value
  const other = mode === 'ortho' ? 'osnap' : 'ortho'
  await assertions(locator).toHaveAttribute('aria-pressed', String(value))
  const expected = { ...runtime.initialEngineModes, [mode]: value }
  await assertions.poll(() => readEngineModes(page)).toEqual(expected)
  assertions(expected[other]).toBe(runtime.initialEngineModes[other])
  if (!runtime.failedDrawing) {
    // Ribbon panels remount while the provider keeps the chosen drafting mode.
    const tabs = page.getByRole('tablist', { name: 'Ribbon', exact: true })
    await tabs.getByRole('tab', { name: 'View', exact: true }).click({ timeout: 15_000 })
    await tabs.getByRole('tab', { name: 'Draw', exact: true }).click({ timeout: 15_000 })
  }
  const persisted = await readEngineModes(page)
  runtime.evidence.engineMode = { mode, expected, observed: persisted }
  assertions(persisted).toEqual(expected)
  await assertions(locator).toHaveAttribute('aria-pressed', String(value))
}

// A page-specific init script: the ordinary page fixture remains unchanged.
export function solarDocumentProbe(probe, document) {
  if (probe.kind !== 'action' || probe.state !== 'no-drawing' || probe.locator.group !== 'solar-panels') return probe
  if (!document.documentId) return probe
  if (document.documentId !== 'solar-starter.dxf' || !(document.count > 0) || document.selection !== 'no selection') {
    throw new Error('Solar no-saved-drawing recipe needs a parsed starter with no selection')
  }
  const action = ACTIONS.find((action) => action.id === probe.sourceId)
  const reason = action.when({ session: { engineParsed: true, selected: null, selectedIds: [], busy: false } })
  const name = accessibleName(action.label, reason)
  return { ...probe, locator: { ...probe.locator, name,
    disabledVariants: reason ? [{ name, reason, reason_code: reasonCode(reason) }] : [] },
    assertion: reason ? { ...probe.assertion, reason, reason_code: reasonCode(reason) }
      : { kind: 'opens', target: 'cockpit-prompt', operation: action.op, group: action.group,
        verb: PROMPTS[action.op].verb, assertionId: probe.assertion.assertionId } }
}

export async function requireSolarDocument(probe, runtime, assertions = expect) {
  const { page, evidence } = runtime
  await engineReady(probe, runtime)
  const card = page.locator('.workspace-card')
  await assertions(card).toHaveAttribute('data-engine-document', 'solar-starter.dxf', { timeout: 60_000 })
  await page.keyboard.press('Escape')
  const selection = page.getByTestId('cockpit-status').locator('.cockpit-sel')
  await assertions(selection).toHaveText('no selection')
  const outline = page.getByRole('group', { name: 'Panel placement', exact: true })
    .getByRole('button', { name: 'Panel outline', exact: true })
  evidence.solarDocument = { savedDrawing: false, documentId: await card.getAttribute('data-engine-document'),
    origin: 'starter', count: await engineCount(page), selection: await selection.innerText(),
    outline: { visible: await outline.isVisible(), enabled: await outline.isEnabled() } }
  if (probe.sourceId === 'solar-panels:createRectangle') {
    try { await assertions(outline).toBeDisabled({ timeout: 500 }) }
    catch (error) {
      if (!error.matcherResult) throw error
      evidence.solarDocument.originalAssertion = { message: error.message,
        expected: error.matcherResult.expected ?? 'disabled: no engine document',
        observed: error.matcherResult.actual ?? 'enabled: starter document loaded' }
    }
  }
  runtime.solarDocument = evidence.solarDocument
}

export async function authorAvailability(probe, runtime, assertions = expect) {
  if (probe.featureId !== 'action:author-tool' || probe.state !== 'ready') return
  const response = await runtime.page.request.get('/api/entitlements', { timeout: 15_000 })
  assertions(response.ok()).toBe(true)
  const policy = await response.json()
  runtime.evidence.authorAvailability = { status: response.status(), policy,
    configuration: { LEAF_CUSTOMIZATION_R5_MODE: runtime.stack.env.LEAF_CUSTOMIZATION_R5_MODE } }
  if (policy.availability?.author_stage === false) {
    await unsupported(probe, runtime, 'Author ready requires author_stage; /api/entitlements reports false with LEAF_CUSTOMIZATION_R5_MODE=' + runtime.stack.env.LEAF_CUSTOMIZATION_R5_MODE)
  }
}

export async function disclosureEvidence(page, group) {
  const toolbar = page.getByRole('toolbar', { name: 'Drafting tools', exact: true })
  const panel = toolbar.getByRole('group', { name: group, exact: true, includeHidden: true })
  const more = toolbar.getByRole('button', { name: 'More panels', exact: true })
  return { group, rendered: await panel.count(), visible: await panel.isVisible(),
    disclosure: await more.count() ? await more.getAttribute('aria-expanded') : null }
}

export async function phoneRailAvailability(probe, runtime) {
  if (probe.featureId !== 'action:rail-expand' || runtime.testInfo.project.name !== 'phone') return
  runtime.evidence.phoneRail = await disclosureEvidence(runtime.page, 'Rail')
  if (runtime.evidence.phoneRail.rendered === 0) {
    await unsupported(probe, runtime, 'Phone Manage does not render the Rail ribbon group after More panels; App navSpine requires wideViewport. B2 must reconcile the map row.')
  }
}

export async function completeSolarReadiness(probe, runtime, locator, assertions = expect, workspace = engineReady) {
  if (!runtime.releaseSolarProjection) return
  await runtime.releaseSolarProjection()
  await workspace(probe, runtime)
  await assertions(runtime.page.locator('.workspace-card')).toHaveAttribute('data-engine-document',
    new RegExp(`^${runtime.drawingId}-v[1-9]\\d*\\.dxf$`), { timeout: 60_000 })
  await assertSurfaceState(locator, 'available', assertions)
  await assertions(locator).toContainText('Ready')
  runtime.evidence.solarReadiness.released = 'Ready'
}

// Faults change transport only. Successful replies always come from the private stack.
export async function faultRouteOnce(page, pattern, onFault) {
  let fired = false
  let cleanupPromise
  const active = new Set()
  const errors = []
  const handler = (route) => {
    const fail = !fired
    fired = true
    const pending = (async () => {
      if (fail) {
        onFault?.(route.request())
        await route.abort('failed')
      } else await route.continue()
    })()
    active.add(pending)
    pending.then(() => active.delete(pending), (error) => { errors.push(error); active.delete(pending) })
    return pending
  }
  await page.route(pattern, handler)
  return () => {
    cleanupPromise ||= (async () => {
      fired = true
      await page.unroute(pattern, handler)
      while (active.size) await Promise.allSettled([...active])
      if (errors.length) throw errors[0]
    })()
    return cleanupPromise
  }
}

export function drawingRequest(path, method = 'GET') {
  return (response) => new URL(response.url()).pathname === path && response.request().method() === method
}

export async function setupVersionFault(probe, runtime, recipe, assertions = expect, setup = setupStep, history = openHistory) {
  const { page, evidence } = runtime
  await setup(probe, runtime, { kind: 'saved-version-history', redo: recipe.kind === 'hold-version-change' && recipe.redo }, assertions)
  const path = `/api/drawings/${runtime.drawingId}`
  const versions = evidence.savedVersionHistory
  const fault = evidence.versionFault = { kind: recipe.kind, requests: [], head: versions.head }
  if (recipe.kind === 'hold-version-change') {
    const operation = recipe.redo ? 'redo' : 'undo'
    fault.expected = expectedVersionHead({ sourceId: operation }, versions)
    runtime.releaseVersionFault = await holdJobRoutes(page, `**${path}/${operation}`, (route) => fault.requests.push(route.request().url()), { holdResponse: true })
    runtime.cleanup.push(runtime.releaseVersionFault)
    const button = page.getByRole('toolbar', { name: 'Quick access', exact: true })
      .getByRole('button', { name: `${recipe.redo ? 'Redo' : 'Undo'} version`, exact: true })
    await assertions(button).toBeEnabled()
    await button.click({ timeout: 15_000 })
    await assertions.poll(() => fault.requests.length).toBe(1)
    return
  }
  await history(probe, runtime, assertions)
  const version = Math.min(...versions.versions.map((row) => Number(row.v)))
  const panel = page.getByRole('dialog', { name: 'Version history', exact: true })
  const row = panel.getByTestId(`vh-row-v${version}`)
  await row.getByRole('button', { name: 'Restore', exact: true }).click({ timeout: 15_000 })
  runtime.releaseVersionFault = await faultRouteOnce(page, `**${path}/intake?version=head`, (request) => fault.requests.push(request.url()))
  runtime.cleanup.push(runtime.releaseVersionFault)
  const committed = page.waitForResponse(drawingRequest(`${path}/versions/${version}/restore`, 'POST'), { timeout: 15_000 })
  committed.catch(() => {})
  await row.getByRole('button', { name: `Restore v${version}`, exact: true }).click({ timeout: 15_000 })
  const response = await committed
  assertions(response.ok()).toBe(true)
  fault.receipt = await response.json()
  fault.expected = Number(fault.receipt.head)
  assertions(fault.expected).toBeGreaterThan(Number(versions.head))
  await assertions.poll(() => fault.requests.length).toBe(1)
  const lock = page.getByTestId('unreadable-head-lock')
  await assertions(lock).toBeVisible()
  await assertions(lock).toHaveAttribute('data-head', String(fault.expected))
  await assertions(lock).toHaveAttribute('role', 'alert')
  await panel.getByRole('button', { name: 'Close version history', exact: true }).click({ timeout: 15_000 })
  await assertions(panel).toBeHidden()
  if (runtime.ribbonTab) await setup(probe, runtime, { kind: 'ribbon-tab', name: runtime.ribbonTab }, assertions)
}

export async function finishVersionFault(runtime, assertions = expect) {
  const { page, evidence } = runtime
  const fault = evidence.versionFault
  const path = `/api/drawings/${runtime.drawingId}`
  const restore = fault.kind === 'fault-restored-head'
  const responsePromise = page.waitForResponse(drawingRequest(restore ? `${path}/intake` : new URL(fault.requests[0]).pathname,
    restore ? 'GET' : 'POST'), { timeout: 15_000 })
  responsePromise.catch(() => {})
  await runtime.releaseVersionFault()
  if (restore) await page.getByTestId('unreadable-head-lock').getByRole('button', { name: 'Retry loading', exact: true }).click({ timeout: 15_000 })
  const response = await responsePromise
  assertions(response.ok()).toBe(true)
  const seated = await response.json()
  assertions(Number(seated.head)).toBe(fault.expected)
  assertions(Number(seated.version)).toBe(fault.expected)
  assertions(seated.intake).toBeTruthy()
  await assertions(page.getByTestId('unreadable-head-lock')).toHaveCount(0)
  if (!restore) await assertions(page.locator('.toast[role="status"]')).toContainText(
    `${new URL(fault.requests[0]).pathname.endsWith('/undo') ? 'Reverted to' : 'Advanced to'} version ${fault.expected}`)
  else await assertions(page.getByRole('toolbar', { name: 'Quick access', exact: true })
    .getByRole('button', { name: 'Undo version', exact: true })).toBeEnabled()
  if (await page.getByTestId('cad-edit-workbench').count()) {
    await assertions(page.locator('.workspace-card')).toHaveAttribute('data-engine-document', `${runtime.drawingId}-v${fault.expected}.dxf`, { timeout: 60_000 })
  }
  fault.recovered = { head: seated.head, version: seated.version }
}

export async function setupHistoryFault(probe, runtime, assertions = expect, history = openHistory) {
  const { page, evidence } = runtime
  const path = `/api/drawings/${runtime.drawingId}/versions`
  const fault = evidence.historyFault = { failed: [], recovery: [] }
  runtime.releaseHistoryFault = await faultRouteOnce(page, `**${path}?*`, (request) => fault.failed.push(request.url()))
  runtime.cleanup.push(runtime.releaseHistoryFault)
  await history(probe, runtime, assertions)
  await assertions(page.getByRole('dialog', { name: 'Version history', exact: true }).getByRole('alert')).toBeVisible()
  if (probe.featureId === 'action:bar-retry' && probe.state === 'retry-history') {
    const alert = page.getByRole('dialog', { name: 'Version history', exact: true }).getByRole('alert')
    await runtime.walkPersonaObservers?.observe('ops-sre', 'observeFailure', true)
    // No wait was observed by this setup. Elapsed request time is not UI evidence.
    await runtime.walkPersonaObservers?.observe('ops-sre', 'observeWait', { observed: false, progress: '', reason: '' })
    await runtime.walkPersonaObservers?.observe('support', 'alert', { page, alert, errorObserved: true })
  }
  assertions(fault.failed.length).toBe(1)
  await runtime.releaseHistoryFault()
  const observe = (request) => {
    if (request.method() === 'GET' && new URL(request.url()).pathname === path) {
      fault.recovery.push(request.url())
      void runtime.walkPersonaObservers?.observe('qc-dev', 'observeEvent', 'history-recovery-request')
    }
  }
  page.on('request', observe)
  runtime.cleanup.push(async () => page.off('request', observe))
  await runtime.walkPersonaObservers?.observe('qc-dev', 'start')
}

export async function assertHistoryRecovery(runtime, assertions = expect) {
  const panel = runtime.page.getByRole('dialog', { name: 'Version history', exact: true })
  await assertions(panel.getByRole('alert')).toHaveCount(0)
  await assertions(panel.getByRole('button', { name: /^v\d+\b/ }).first()).toBeVisible()
  assertions(runtime.evidence.historyFault.recovery.length).toBe(1)
}

export async function setupRealProject(runtime, assertions = expect) {
  const { page, evidence } = runtime
  const orgResponse = await page.request.post('/api/orgs', {
    headers: await setupRequestHeaders(runtime, '/api/orgs'),
    data: { name: `Walk Escape ${Date.now()}` }, timeout: 15_000,
  })
  if (!runtime.projectSolar) assertions(orgResponse.ok()).toBe(true)
  const { org } = runtime.projectSolar
    ? await projectSolarResponse(orgResponse, 'open-real-project create org', [200, 201], runtime) : await orgResponse.json()
  assertions(org.org_id).toBeTruthy()
  const previousOrg = await page.evaluate((orgId) => {
    const previous = localStorage.getItem('leaf.org_id')
    localStorage.setItem('leaf.org_id', orgId)
    return previous
  }, org.org_id)
  runtime.cleanup.push(async () => {
    const close = page.getByRole('button', { name: 'Close workspace', exact: true })
    if (await close.isVisible()) await close.click({ timeout: 15_000 })
    await page.evaluate((previous) => {
      if (previous == null) localStorage.removeItem('leaf.org_id')
      else localStorage.setItem('leaf.org_id', previous)
    }, previousOrg)
  })
  await page.reload({ timeout: 60_000 })
  await page.getByRole('button', { name: 'Start', exact: true }).click({ timeout: 15_000 })
  const board = page.getByRole('region', { name: 'Project workspace', exact: true })
  await assertions(board).toBeVisible()
  // WorkspaceProjectSlot supplies the drawing's name; the controller creates
  // and opens the project directly, without a name field or native dialog.
  const create = board.getByRole('button', { name: 'Create project from this drawing', exact: true })
  await assertions(create).toBeEnabled()
  const created = page.waitForResponse(drawingRequest('/api/projects', 'POST'), { timeout: 15_000 })
  created.catch(() => {})
  await create.click({ timeout: 15_000 })
  const response = await created
  if (!runtime.projectSolar) assertions(response.ok()).toBe(true)
  assertions(response.request().headers()['x-org-id']).toBe(org.org_id)
  const { project } = runtime.projectSolar
    ? await projectSolarResponse(response, 'open-real-project create project', [200, 201], runtime) : await response.json()
  assertions(project.project_id).toBeTruthy()
  assertions(project.org_id).toBe(org.org_id)
  const name = project.name
  assertions(name).toBeTruthy()
  evidence.escapeProject = { orgId: org.org_id, projectId: project.project_id, name }
  await assertions(page.locator('.workspace-summary')).toContainText(name)
  await assertions(board).toHaveAttribute('data-project-state', 'project')
  await assertions(board.getByText('Versions live with a workspace project', { exact: true })).toHaveCount(0)
  await assertions(board.getByText('Runs appear here with a project open', { exact: true })).toHaveCount(0)
  const back = page.getByRole('button', { name: 'Return to drawing', exact: true })
  if (await back.isVisible()) await back.click({ timeout: 15_000 })
  await assertions(board).toBeHidden()
  await assertions(page.getByRole('dialog', { name: 'Version history', exact: true })).toBeHidden()
  await assertions(page.locator('.strip-failed, .strip-running')).toHaveCount(0)
  await assertions(page.getByRole('button', { name: /^Run / })).toHaveCount(0)
  await assertions(page.getByTestId('cockpit-status').locator('.cockpit-sel')).toHaveText('no selection')
}

export async function projectSolarResponse(response, label, statuses, runtime) {
  let body
  try { body = await response.json() } catch {
    throw new Error(`${label}: HTTP ${response.status()} response JSON unreadable`)
  }
  // APIRequestContext calls do not emit page response events. Record these
  // public setup receipts explicitly, including malformed successful bodies.
  if (runtime) {
    const proof = runtime.evidence.projectSolar ||= { steps: [] }
    const record = { step: label, status: response.status(), body }
    proof.lastCanonicalResponse = record
    if (!statuses.includes(response.status())) proof.serverFailure = record
  }
  if (!statuses.includes(response.status())) throw new Error(`${label}: HTTP ${response.status()} ${JSON.stringify(body)}`)
  return body
}

export async function projectSolarStep(runtime, label, action) {
  const proof = runtime.evidence.projectSolar ||= { steps: [] }
  const record = { step: label }
  proof.steps.push(record)
  try { await action(); record.complete = true } catch (error) {
    await runtime.projectSolar?.drainDiagnostics?.()
    record.error = `${label}: ${error.message}; server response ${JSON.stringify(proof.serverFailure ?? proof.lastCanonicalResponse ?? proof.lastResponse ?? null)}`
    throw new Error(record.error, { cause: error })
  }
}

export async function assertProjectSolarDrawing(runtime, assertions = expect) {
  const { page, projectSolar: { binding } } = runtime
  const toggle = page.locator('[data-testid="solar-flow-seat"] button.solar-flow-seat-toggle')
  if (await toggle.count() && await toggle.getAttribute('aria-expanded') === 'false') {
    await walkClick(toggle, 'Expand Solar flow seat')
    await assertions(toggle).toHaveAttribute('aria-expanded', 'true')
  }
  await assertions(page.locator('.project-solar-workspace')).toBeVisible()
  await assertions(page.getByTestId('solar-flow-rail')).toBeVisible()
  await assertions(page.getByTestId('solar-flow-seat')).toBeVisible()
  await assertions(page.locator('.viewer-title')).toContainText(binding.input_version_id, { timeout: 30_000 })
  const drawing = page.locator('.studio-ground .viewer-canvas canvas')
  await assertions(drawing).toHaveCount(1)
  await assertions(drawing).toBeVisible()
  assertions(await drawing.evaluate((node) => node.width > 0 && node.height > 0)).toBe(true)
}

export function observeProjectSolarResponses(runtime) {
  const { page, stack, evidence } = runtime
  const solar = runtime.projectSolar ||= {}
  const proof = evidence.projectSolar ||= { steps: [] }
  solar.apiBase = stack.baseURL
  const pending = new Set()
  solar.drainDiagnostics = () => Promise.all([...pending])
  const observe = (response) => {
    const path = new URL(response.url()).pathname
    if (!path.startsWith('/api/')) return
    const record = { path, status: response.status() }
    proof.lastResponse = record
    const job = /^\/api\/jobs\/[0-9a-f-]{36}$/.test(path)
    const context = path.endsWith('/context')
    const imported = path.endsWith('/drawing-versions/import')
    // Include HTTP-200 job failures and stale context responses in diagnostics.
    // Keep only public job/context fields and never successful checkout bodies.
    if (response.status() >= 400 || path === '/api/run' || job || context || imported
      || path === '/api/orgs' || path === '/api/projects' || path === '/api/drawings/upload'
      || path.endsWith('/upload-status')) {
      const work = response.json().then((body) => {
        record.body = response.status() >= 400 ? body
          : job ? { job_id: body?.job_id, status: body?.status, error: body?.error,
            input_version_id: body?.input_version_id, output_version_id: body?.result?.output_version_id }
          : context ? { context: body?.context, graphRev: body?.intake?.solar_design_graph?.rev,
              graphSha256: body?.intake?.solar_design_graph_sha256 } : body
        proof.lastCanonicalResponse = record
        if (response.status() >= 400 || job && body?.status === 'failed'
          || path.endsWith('/upload-status') && body?.status === 'failed') proof.serverFailure = record
      }, () => {})
      pending.add(work)
      work.finally(() => pending.delete(work))
    }
  }
  page.on('response', observe)
  runtime.cleanup.push(async () => { page.off('response', observe); await Promise.all([...pending]) })
}

export async function openProjectSolar(runtime, assertions = expect) {
  const { page, evidence, projectSolar: solar } = runtime
  const proof = evidence.projectSolar
  solar.traffic = observeProjectTraffic(page, solar.apiBase)
  runtime.cleanup.push(async () => { try { await solar.traffic.drain() } finally { solar.traffic.dispose() } })
  await projectSolarStep(runtime, 'canonical material import', async () => {
    await walkClick(page.getByRole('tab', { name: 'Browser', exact: true }), 'Open Browser tab')
    await walkClick(page.getByRole('button', { name: 'Upload drawing', exact: true }), 'Upload drawing')
    const material = page.locator('[data-pane="material-intake"]')
    await assertions(material.getByRole('button', { name: 'Upload DWG or DXF', exact: true })).toBeEnabled()
    const path = `/api/projects/${evidence.escapeProject.projectId}/drawing-versions/import`
    // Import can finish before the upload response is inspected. Install both
    // listeners before selecting the real material, as the local walk does.
    const imported = page.waitForResponse(drawingRequest(path, 'POST'), { timeout: 120_000 })
    const settled = imported.then((response) => ({ response }), (error) => ({ error }))
    const source = await readFile(new URL('../fixtures/distinctive-panel.dxf', import.meta.url))
    const uploaded = await actionResponse(page, solar.apiBase, 'POST', '/api/drawings/upload',
      () => material.getByLabel('Drawing file', { exact: true }).setInputFiles({
        name: 'walk-project-solar.dxf', mimeType: 'application/dxf', buffer: source,
      }), 120_000)
    const upload = await projectSolarResponse(uploaded, 'project upload', [202])
    assertions(isUuid(upload.drawing_id)).toBe(true)
    const result = await settled
    if (result.error) throw result.error
    const body = await projectSolarResponse(result.response, 'canonical import', [201])
    const version = body.drawing_version
    solar.binding = projectBinding({ organization_id: evidence.escapeProject.orgId,
      project_id: evidence.escapeProject.projectId, drawing_id: version?.drawing_id, input_version_id: version?.version_id })
    assertions(version.org_id).toBe(solar.binding.organization_id)
    assertions(version.project_id).toBe(solar.binding.project_id)
    assertions(version.drawing_id).toBe(upload.drawing_id)
    assertions(body.replayed).toBe(false)
    const headers = result.response.request().headers()
    assertions(headers['x-org-id']).toBe(solar.binding.organization_id)
    assertions(/^Bearer \S+$/i.test(headers.authorization || '')).toBe(true)
    assertions(typeof headers['idempotency-key']).toBe('string')
    await solar.traffic.drain()
    const ready = solar.traffic.readyUploads.findLast((row) => row.drawing_id === upload.drawing_id)
    assertions(ready).toBeTruthy()
    const input = result.response.request().postDataJSON()
    assertions(input.source).toEqual({ drawing_id: upload.drawing_id, version: ready.version })
    await assertions(material).toContainText('Attached', { timeout: 30_000 })
    proof.binding = { kind: 'project', ...solar.binding }
    proof.importedVersionId = solar.binding.input_version_id
    runtime.drawingId = solar.binding.drawing_id
  })
  await projectSolarStep(runtime, 'select canonical version and open Solar CAD', async () => {
    await selectProjectSolarVersion(runtime, assertions)
    solar.context = await readContext(page, solar.apiBase, solar.binding, { graphRequired: false })
    assertions(solar.context.graph).toBe(null)
    proof.open = { head: solar.context.head, binding: { ...solar.binding }, workspaceVisible: true, projectRailVisible: true }
  })
}

const readLocatorAttribute = async (locator, name) => await locator.getAttribute?.(name) ?? null

export async function closeProjectPanelIfOpen(page) {
  const controls = page.getByRole('navigation', { name: 'Workspace controls', exact: true })
  if (!await controls.count?.()) return
  const project = controls.getByRole('button', { name: 'Project', exact: true })
  if (!await project.count() || await readLocatorAttribute(project, 'aria-expanded') !== 'true') return
  await walkClick(project, 'Close Project panel')
  await expect(project).toHaveAttribute('aria-expanded', 'false', { timeout: 15_000 })
}

export async function enterWorkspaceIfChooser(page, beforeClick = () => {}) {
  const environment = page.locator('section.workspace-environment')
  if (!await environment.count() || await readLocatorAttribute(environment, 'data-entered') === 'true') return false
  const resume = environment.getByRole('button', { name: 'Continue workspace', exact: true })
  if (await resume.isVisible()) {
    beforeClick()
    await walkClick(resume, 'Continue workspace')
  } else {
    const open = environment.getByRole('button', { name: 'Open project or material', exact: true })
    if (!await open.isVisible()) return false
    beforeClick()
    await walkClick(open, 'Enter workspace')
    await closeProjectPanelIfOpen(page)
  }
  return true
}

export async function enterWorkspaceEnvironment(page, assertions = expect) {
  const environment = page.locator('section.workspace-environment')
  let attached = false, clicks = 0, entryLimitError
  try {
    await assertions.poll(async () => {
      attached = !!await environment.count() || attached
      try {
        await enterWorkspaceIfChooser(page, () => {
          if (clicks >= 3) {
            entryLimitError = new Error('Workspace data-entered still requires entry after 3 clicks')
            throw entryLimitError
          }
          clicks++
        })
      } catch (error) {
        // Finish the poll before throwing: expect.poll retries predicate errors.
        if (error === entryLimitError) return true
        throw error
      }
      return !!await environment.count() && await readLocatorAttribute(environment, 'data-entered') === 'true'
    }, { timeout: 20_000 }).toBe(true)
  } catch (error) {
    if (attached) throw error
  }
  if (entryLimitError) throw entryLimitError
}

export async function reloadAndReenter(page, options) {
  await page.reload(options)
  await enterWorkspaceEnvironment(page)
}

export async function openSolarCadProfile(page) {
  const controls = page.getByRole('navigation', { name: 'Workspace controls', exact: true })
  if (await controls.count?.()) {
    const profile = controls.getByRole('combobox', { name: 'Workspace profile', exact: true })
    if (await profile.count()) {
      await profile.selectOption({ label: 'Solar CAD' })
      const environment = page.locator('section.workspace-environment')
      await expect.poll(() => readLocatorAttribute(environment, 'data-profile'), { timeout: 15_000 }).toBe('solar')
      await enterWorkspaceEnvironment(page)
      return
    }
  }
  await walkClick(page.getByRole('tab', { name: 'Solar CAD', exact: true }), 'Open Solar CAD tab')
}

export async function selectProjectSolarVersion(runtime, assertions = expect) {
  const { page, projectSolar: solar } = runtime
  // web/src/site/BoardTiles.jsx:41,59-61 renders this count only from workspace versions;
  // web/src/controllers/workspace/createWorkspaceController.js:139-149 clears workspace on open,
  // then publishes only the requested project's current response. The Versions pane mounts
  // only after selection (web/src/workspace/ProjectWorkspacePanels.jsx:189,204).
  await assertions(page.getByRole('region', { name: 'Project workspace', exact: true })
    .locator('[data-tile="versions"] > strong'),
  `project ${solar.binding.project_id ?? solar.binding.input_version_id} did not open in the UI within 30 s`)
    .toHaveCount(1, { timeout: 30_000 })
  await walkClick(page.getByRole('button', { name: 'Open versions', exact: true }), 'Open versions')
  const version = page.locator('[data-pane="versions"]').getByRole('button', {
    name: new RegExp(`^v\\d+ .*${solar.binding.input_version_id}$`),
  })
  await assertions(version, `version ${solar.binding.input_version_id} is not listed in the Versions pane within 30 s`)
    .toBeVisible({ timeout: 30_000 })
  await version.click({ timeout: 30_000 })
  await closeProjectPanelIfOpen(page)
  await openSolarCadProfile(page)
  await assertProjectSolarDrawing(runtime, assertions)
}

export async function openProjectSolarTool(runtime, tool, assertions = expect, { closeEditor = true, allowDisabled = false } = {}) {
  const { page } = runtime
  const editor = page.locator('.project-solar-workspace-editor')
  if (closeEditor && await editor.isVisible()) {
    await walkClick(editor.getByRole('button', { name: /^(Close|Cancel)$/ }), 'Close Solar editor')
    await assertions(editor).toHaveCount(0)
  }
  await closeProjectPanelIfOpen(page)
  await walkClick(page.getByRole('tab', { name: 'Solar', exact: true }), 'Open Solar tab')
  const ribbon = page.getByTestId('drafting-ribbon').locator(`button[data-tool="${tool}"]`)
  const rail = page.locator(`#solar-step-${tool}`)
  const more = page.getByRole('button', { name: 'More panels', exact: true })
  await assertions.poll(async () => await ribbon.isVisible() || await more.isVisible() || await rail.isVisible(),
    { timeout: 30_000, message: `project Solar control ${tool} must render` }).toBe(true)
  if (!await ribbon.isVisible() && await more.isVisible()) await walkClick(more, 'Open More panels')
  const control = await ribbon.isVisible() ? ribbon : rail
  if (allowDisabled && !await control.isEnabled()) return false
  await assertions(control).toBeEnabled()
  await walkClick(control, `Open Solar tool ${tool}`)
  return true
}

export async function releaseProjectSolarCheckout(runtime, { request = playwrightRequest, timeoutMs = 10_000 } = {}) {
  const lease = runtime.projectSolarCheckout
  if (!lease || lease.released) return
  const deadline = Date.now() + timeoutMs
  const bounded = async (action, budget = deadline - Date.now()) => {
    let timer
    try {
      if (budget <= 0) throw new Error('release timeout')
      return await Promise.race([
        Promise.resolve().then(action),
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('release timeout')), budget) }),
      ])
    } finally { clearTimeout(timer) }
  }
  let context
  let uiReleaseFailed = false
  try {
    if (runtime.page && !runtime.page.isClosed?.()) {
      try {
        // Reserve half the remaining time for a page-independent fallback.
        await bounded(() => runtime.projectSolarReleaseUI
          ? runtime.projectSolarReleaseUI()
          : releaseUI(), Math.max(1, Math.floor((deadline - Date.now()) / 2)))
        lease.released = true
        return
      } catch {
        // A closed, crashed or unresponsive page must not strand the lease.
        uiReleaseFailed = true
      }
    }
    await bounded(async () => {
      context = await request.newContext()
      if (Date.now() >= deadline) {
        void context.dispose().catch(() => {})
        throw new Error('release timeout')
      }
      const headers = { Authorization: `Bearer ${runtime.projectSolarToken || ''}` }
      if (lease.capability) headers['X-Checkout-Capability'] = lease.capability
      const response = await context.delete(`${lease.apiBase}${lease.path}`, {
        headers, timeout: Math.max(1, deadline - Date.now()),
      })
      if ([404, 409].includes(response.status())) return
      if (response.status() !== 200 || (await response.json()).released !== true) throw new Error('release rejected')
    })
    lease.released = true
  } catch {
    // Transport errors can contain request headers. Persist and return only a
    // fixed diagnostic, never the transport message, bearer or capability.
    const error = new Error('Project Solar checkout release failed or timed out')
    runtime.evidence ||= {}
    runtime.evidence.projectSolar ||= {}
    runtime.evidence.projectSolar.releaseError = error.message
    return error
  } finally {
    if (uiReleaseFailed) {
      runtime.evidence ||= {}
      runtime.evidence.projectSolar ||= {}
      runtime.evidence.projectSolar.releaseUiError = 'Project checkout UI release failed'
      lease.uiReleaseFailed = true
    }
    if (context) {
      try {
        // Start disposal even when the overall deadline has elapsed, aborting
        // a DELETE whose transport did not honour its own timeout.
        const disposal = Promise.resolve(context.dispose()).catch(() => {})
        await bounded(() => disposal)
      } catch { /* Release verdict is already recorded. */ }
    }
  }

  async function releaseUI() {
    const response = await actionResponse(runtime.page, lease.apiBase, 'DELETE', lease.path,
      () => walkClick(runtime.page.getByRole('group', { name: 'Project checkout', exact: true })
        .getByRole('button', { name: 'Release', exact: true }), 'Release Project checkout'))
    const body = await projectSolarResponse(response, 'release Project checkout', [200])
    expect(body.released).toBe(true)
    expect(body.checkout).toBe(null)
  }
}

export async function takeProjectSolarCheckout(runtime, assertions = expect, action = actionResponse) {
  const { page, projectSolar: solar, evidence: { projectSolar: proof } } = runtime
  const checkout = () => page.getByRole('group', { name: 'Project checkout', exact: true })
  const path = `/api/projects/${solar.binding.project_id}/drawings/${solar.binding.drawing_id}/checkout`
  await projectSolarStep(runtime, 'take Project checkout', async () => {
    const response = await action(page, solar.apiBase, 'POST', path,
      () => walkClick(checkout().getByRole('button', { name: 'Take', exact: true }), 'Take Project checkout'))
    const lease = await projectSolarResponse(response, 'Project checkout', [200])
    // Record acquisition before validating the binding or lease body.
    if (lease.acquired === true) {
      runtime.projectSolarCheckout = { apiBase: solar.apiBase, path, capability: lease.checkout_capability, released: false }
      runtime.projectSolarReleaseUI = async () => {
        const released = await action(page, solar.apiBase, 'DELETE', path,
          () => walkClick(checkout().getByRole('button', { name: 'Release', exact: true }), 'Release Project checkout'))
        const body = await projectSolarResponse(released, 'release Project checkout', [200])
        assertions(body.released).toBe(true)
        assertions(body.checkout).toBe(null)
      }
      runtime.cleanup.push(async () => {
        const error = await releaseProjectSolarCheckout(runtime)
        if (error) throw error
        if (runtime.projectSolarCheckout.uiReleaseFailed) throw new Error('Project checkout UI release failed')
      })
    }
    assertions(lease.acquired).toBe(true)
    for (const key of ['organization_id', 'project_id', 'drawing_id']) assertions(lease[key]).toBe(solar.binding[key])
    assertions(lease.checkout.fence).toMatch(/^[0-9]+$/)
    proof.checkoutFence = lease.checkout.fence
    await assertions(checkout().getByRole('button', { name: 'Release', exact: true })).toBeEnabled()
  })
}

export async function publishProjectSolar(runtime, assertions = expect, {
  action = actionResponse, run = runProjectTool, allowExistingGraph = false,
} = {}) {
  const { page, projectSolar: solar, evidence: { projectSolar: proof } } = runtime
  await takeProjectSolarCheckout(runtime, assertions, action)
  await projectSolarStep(runtime, 'publish solar-settings canonical job', async () => {
    await openProjectSolarTool(runtime, 'solar-settings', assertions)
    const settings = page.getByTestId('solar-settings-form')
    await assertions(settings).toHaveAttribute('data-mode', allowExistingGraph ? /^(initialize|edit)$/ : 'initialize')
    const initializing = await settings.getAttribute('data-mode') === 'initialize'
    if (initializing) await settings.getByRole('combobox', { name: 'Drawing units', exact: true }).selectOption('m')
    // A scratch fixture can already contain a graph from an earlier run.
    const sequence = allowExistingGraph && solar.context.graph?.settings?.panels_in_sequence === 1 ? 2 : 1
    await settings.getByLabel('Panels in sequence', { exact: true }).fill(String(sequence))
    const buttonName = initializing ? 'Start Solar design' : 'Apply settings'
    const start = settings.getByRole('button', { name: buttonName, exact: true })
    try {
      await assertions(start).toBeEnabled({ timeout: 30_000 })
    } catch (error) {
      const diagnosticText = async testId => {
        try {
          const locator = settings.getByTestId(testId)
          return await locator.count() ? redactSolarDiagnostic(await locator.textContent({ timeout: 2_000 }) ?? '') : ''
        } catch { return '[unavailable]' }
      }
      const reason = await diagnosticText('solar-settings-reason')
      const runStatus = await diagnosticText('solar-settings-run')
      throw new Error(`${buttonName} did not become enabled within 30 s; solar-settings-reason: ${reason}; solar-settings-run: ${runStatus}`, { cause: error })
    }
    let result
    try {
      result = await run({ page, expect: assertions, apiBase: solar.apiBase, traffic: solar.traffic,
        binding: solar.binding, before: solar.context, tool: 'solar-settings', action: () => walkClick(start, buttonName), checkParams: (params) => {
          if (initializing) assertions(params.initialize?.units?.drawing_units).toBe('m')
          assertions(params.changes?.panels_in_sequence).toBe(sequence)
        } })
    } catch (error) {
      const diagnostic = await projectSolarRunDiagnostic(page, settings, start)
      if (error instanceof Error) {
        error.message += `\n${diagnostic}`
        throw error
      }
      throw new Error(`${redactSolarDiagnostic(error)}\n${diagnostic}`, { cause: error })
    }
    solar.binding = result.binding
    solar.context = result.context
    proof.publication = result.step
    proof.jobStatus = 'complete'
    proof.binding = { kind: 'project', ...solar.binding }
    proof.publishedHead = result.context.head
    await assertProjectSolarDrawing(runtime, assertions)
    proof.drawingFollowedVersionId = solar.binding.input_version_id
  })
}

export async function switchProjectSolarForms(runtime, assertions = expect) {
  const { page, projectSolar: solar, evidence: { projectSolar: proof } } = runtime
  const runCount = solar.traffic.runCount
  await projectSolarStep(runtime, 'switch Solve to Correct strings and back', async () => {
    await openProjectSolarTool(runtime, 'solar-solve-proposal', assertions)
    const solve = page.getByRole('form', { name: 'Solve', exact: true })
    await solve.getByLabel('Grant ref', { exact: true }).fill('walk-unsent-grant')
    await solve.getByLabel('Request', { exact: true }).fill('{"grid":{}}')
    await solve.getByLabel('Frame', { exact: true }).fill('walk-unsent-frame')
    // SchemaForm encodes enum option values as indices; use the schema labels.
    await solve.getByRole('combobox', { name: 'Phase', exact: true }).selectOption({ label: 'background' })
    await openProjectSolarTool(runtime, 'solar-correct-string', assertions, { closeEditor: false })
    const correction = page.getByRole('form', { name: 'Correct strings', exact: true })
    for (const name of ['Grant ref', 'Request', 'Frame', 'Phase']) await assertions(page.getByLabel(name, { exact: true })).toHaveCount(0)
    await assertions(correction.getByLabel('Memberships', { exact: true })).toHaveValue('')
    await correction.getByLabel('Memberships', { exact: true }).fill('[{"string_ref":"unsent","ordered_panel_refs":["unsent-panel"]}]')
    await openProjectSolarTool(runtime, 'solar-solve-proposal', assertions, { closeEditor: false })
    await assertions(page.getByLabel('Memberships', { exact: true })).toHaveCount(0)
    for (const name of ['Grant ref', 'Request', 'Frame']) await assertions(solve.getByLabel(name, { exact: true })).toHaveValue('')
    await assertions(solve.getByLabel('Phase', { exact: true }).locator('option:checked')).toHaveText('initial')
    await assertions(solve.getByRole('button', { name: 'Run solve', exact: true })).toBeDisabled()
    assertions(solar.traffic.runCount).toBe(runCount)
    proof.formSwitch = { solveFieldsReset: true, membershipsReset: true, runSolveDisabled: true, submittedRuns: 0 }
  })
}

export const CENSUS_DISCLOSURES = Object.freeze({
  'object-snap-menu-expanded': { body: '.cockpit-status-toggles .object-snap-trigger', attribute: 'aria-expanded',
    menu: '.cockpit-status-toggles .object-snap-menu' },
  'dxf-import-expanded': { body: '#cockpit-import-pane', attribute: 'data-import-open' },
  'ribbon-overflow-expanded': { body: '#drafting-ribbon', attribute: 'data-overflow-open' },
  'drawing-objects-expanded': { body: 'details.drawing-objects-panel', native: true },
})

export async function censusDisclosureState(page, target, assertions = expect) {
  const spec = CENSUS_DISCLOSURES[target]
  assertions(spec).toBeTruthy()
  const body = page.locator(spec.body)
  await assertions(body).toHaveCount(1)
  return spec.native ? body.evaluate((element) => element.open)
    : await body.getAttribute(spec.attribute) === 'true'
}

export async function setupCensusDisclosure(runtime, recipe, assertions = expect) {
  const { page } = runtime
  if (recipe.target === 'ribbon-overflow-expanded') {
    const original = page.viewportSize()
    runtime.cleanup.push(async () => { if (original) await page.setViewportSize(original) })
    await page.setViewportSize({ width: 900, height: original?.height || 900 })
  }
  const button = control(page, recipe.control)
  await assertions(button).toBeVisible()
  const set = async (expanded) => {
    if (await censusDisclosureState(page, recipe.target, assertions) !== expanded) await button.click({ timeout: 15_000 })
    if (!CENSUS_DISCLOSURES[recipe.target].native) await assertions(button).toHaveAttribute('aria-expanded', String(expanded))
    await assertions.poll(() => censusDisclosureState(page, recipe.target, assertions)).toBe(expanded)
    if (CENSUS_DISCLOSURES[recipe.target].menu) {
      const menu = page.locator(CENSUS_DISCLOSURES[recipe.target].menu)
      await assertions(menu).toHaveCount(expanded ? 1 : 0)
      if (expanded) await assertions(menu).toBeVisible()
    }
  }
  runtime.cleanup.push(async () => { await set(false); runtime.evidence.censusDisclosure.cleaned = true })
  runtime.evidence.censusDisclosure = { target: recipe.target, initial: recipe.expanded }
  await set(recipe.expanded)
}

export async function setupCensusDownloadOnly(runtime, assertions = expect) {
  const ordinary = runtime.page
  const storage = await ordinary.context().storageState()
  const items = storage.origins.flatMap((origin) => origin.localStorage)
  const identity = { token: items.find((item) => item.name === 'leaf.jwt')?.value ?? null,
    coach: items.find((item) => item.name === COACH_STORAGE_KEY)?.value ?? null }
  const fresh = await ordinary.context().newPage()
  runtime.page = fresh
  runtime.cleanup.push(async () => {
    await fresh.evaluate(({ token, coach, coachKey }) => {
      if (token === null) localStorage.removeItem('leaf.jwt')
      else localStorage.setItem('leaf.jwt', token)
      if (coach === null) localStorage.removeItem(coachKey)
      else localStorage.setItem(coachKey, coach)
    }, { ...identity, coachKey: COACH_STORAGE_KEY })
    await fresh.close()
    runtime.page = ordinary
    runtime.evidence.censusDownloadOnly.cleaned = true
  })
  runtime.evidence.censusDownloadOnly = { route: '/app?demo=1&surface=cad' }
  await fresh.addInitScript((coachKey) => {
    localStorage.removeItem('leaf.jwt')
    localStorage.setItem(coachKey, '1')
  }, COACH_STORAGE_KEY)
  await fresh.goto('/app?demo=1&surface=cad')
  await assertions(fresh.getByRole('complementary', { name: 'Properties', exact: true })
    .getByRole('definition').filter({ hasText: /^rooftop_demo\.dwg$/ })).toBeVisible()
}

export async function releaseCensusEngineTransport(page) {
  await page.evaluate(() => {
    const transport = globalThis.__walkEngineTransport
    if (!transport) throw new Error('Census running recipe requires the prepared engine transport')
    transport.hold = false
    for (const deliver of transport.pending.splice(0)) deliver()
  })
}

export async function setupCensusScript(runtime, recipe, assertions = expect) {
  const { page } = runtime
  const input = page.getByRole('textbox', { name: 'ribbon script', exact: true })
  const status = page.getByTestId('cockpit-script-status')
  await assertions(input).toBeVisible()
  await input.fill(recipe.text)
  runtime.evidence.censusScript = { text: recipe.text, running: recipe.running }
  runtime.cleanup.push(async () => {
    if (recipe.running) {
      await releaseCensusEngineTransport(page)
      await assertions(status).toHaveAttribute('data-phase', 'done')
      await assertions(status).toHaveText('Script ran 1 command.')
    }
    await assertions(input).toBeEnabled()
    await input.fill('')
    await assertions(input).toHaveValue('')
    runtime.evidence.censusScript.cleaned = true
  })
  if (recipe.running) {
    await page.evaluate(() => { globalThis.__walkEngineTransport.hold = true })
    await page.getByRole('button', { name: 'Run script', exact: true }).click({ timeout: 15_000 })
    await assertions.poll(() => page.evaluate(() => globalThis.__walkEngineTransport.pending.length)).toBeGreaterThan(0)
    await assertions(status).toHaveAttribute('data-phase', 'running')
    await assertions(input).toBeDisabled()
  }
}

export async function captureCensusEffect(probe, runtime, assertions = expect) {
  const { page } = runtime
  const target = probe.assertion.target
  // In particular, Redo setup must finish its real Undo before counting.
  await assertions(control(page, probe.locator)).toBeEnabled()
  const before = { count: await engineCount(page) }
  if (target === 'engine-save-version') {
    const response = await page.request.get(`/api/drawings/${runtime.drawingId}/versions`, { timeout: 15_000 })
    assertions(response.ok()).toBe(true)
    before.versions = await response.json()
    runtime.censusSaveResponse = page.waitForResponse((reply) => reply.request().method() === 'POST'
      && new URL(reply.url()).pathname.startsWith(`/api/drawings/${runtime.drawingId}/versions/`), { timeout: 60_000 })
    runtime.censusSaveResponse.catch(() => {})
  }
  return before
}

export async function activateCensusFileChooser(runtime, locator) {
  const chooser = runtime.page.waitForEvent('filechooser', { timeout: 15_000 })
  chooser.catch(() => {})
  await locator.click({ timeout: 15_000 })
  runtime.censusFileChooser = await chooser
  runtime.evidence.censusFilePicker = { opened: true }
  runtime.cleanup.push(async () => {
    delete runtime.censusFileChooser
    runtime.evidence.censusFilePicker.cleaned = true
  })
}

export function observeDxfImportState(probe, runtime) {
  if (!['control:open-dxf', 'control:open-dxf-browser'].includes(probe.featureId)) return
  const { page } = runtime
  let fileChooserOpened = null
  const opened = () => { fileChooserOpened = true }
  try {
    page.on('filechooser', opened)
    fileChooserOpened = false
  } catch { /* A missing event API must not affect the probe. */ }
  runtime.cleanup.push(async () => {
    try { page.off('filechooser', opened) } catch { /* Optional diagnostic cleanup. */ }
    delete runtime.captureDxfImportState
  })
  runtime.captureDxfImportState = async () => {
    const read = async (callback) => { try { return await callback() } catch { return null } }
    try {
      const pane = page.locator('#cockpit-import-pane')
      const input = page.getByLabel('DXF file', { exact: true })
      const paneCount = await read(() => pane.count())
      const count = await read(() => input.count())
      const enabled = count > 0 ? await read(() => input.first().isEnabled({ timeout: 1000 })) : null
      runtime.evidence.dxfImportState = {
        importOpen: paneCount > 0 ? await read(async () => await pane.getAttribute('data-import-open') === 'true') : null,
        paneVisible: paneCount > 0 ? await read(() => pane.isVisible()) : null,
        // CadEditSurface's file input is disabled={busy}; there is no separate busy attribute.
        busy: enabled === null ? null : !enabled,
        picking: paneCount > 0 ? await read(async () => await pane.getAttribute('data-cockpit-picking') === '1') : null,
        input: { count, visible: count > 0 ? await read(() => input.first().isVisible()) : null, enabled },
        fileChooserOpened,
      }
    } catch { /* Diagnostics never replace the original expectation failure. */ }
  }
}

export async function assertCensusEffect(probe, runtime, locator, before, assertions = expect) {
  const { page } = runtime
  const target = probe.assertion.target
  if (CENSUS_DISCLOSURES[target]) {
    if (!CENSUS_DISCLOSURES[target].native) await assertions(locator).toHaveAttribute('aria-expanded', String(probe.assertion.value))
    await assertions.poll(() => censusDisclosureState(page, target, assertions)).toBe(probe.assertion.value)
    if (CENSUS_DISCLOSURES[target].menu) {
      const menu = page.locator(CENSUS_DISCLOSURES[target].menu)
      await assertions(menu).toHaveCount(probe.assertion.value ? 1 : 0)
      if (probe.assertion.value) await assertions(menu).toBeVisible()
    }
    if (target === 'dxf-import-expanded') {
      try {
        await assertions(page.getByLabel('DXF file', { exact: true }))
          [probe.assertion.value ? 'toBeVisible' : 'toBeHidden']()
      } catch (error) {
        try { await runtime.captureDxfImportState?.() } catch { /* Preserve the oracle failure. */ }
        throw error
      }
    }
    if (target === 'drawing-objects-expanded') await assertions(page.locator('.drawing-objects-content'))
      .toHaveCount(probe.assertion.value ? 1 : 0)
    return
  }
  if (target === 'ribbon-script-text') { await assertions(locator).toHaveValue(probe.locator.inputValue); return }
  if (target === 'script-file-picker') {
    assertions(runtime.censusFileChooser).toBeTruthy()
    assertions(await runtime.censusFileChooser.element().getAttribute('aria-label')).toBe('Script file')
    assertions(await runtime.censusFileChooser.element().getAttribute('accept')).toBe('.scr,.txt')
    assertions(runtime.censusFileChooser.isMultiple()).toBe(false)
    return
  }
  if (target === 'script-run') {
    await assertions(page.getByTestId('cockpit-script-status')).toHaveAttribute('data-phase', 'done')
    await assertions(page.getByTestId('cockpit-script-status')).toHaveText('Script ran 1 command.')
  }
  if (target === 'script-run' || target === 'engine-undo-edit' || target === 'engine-redo-edit') {
    await assertions.poll(() => engineCount(page)).toBe(before.count + (target === 'engine-undo-edit' ? -1 : 1))
    await assertions(page.getByRole('toolbar', { name: 'Quick access', exact: true })
      .getByRole('button', { name: target === 'engine-undo-edit' ? 'Redo edit' : 'Undo edit', exact: true })).toBeEnabled()
    runtime.evidence.censusEdit = { before: before.count, after: await engineCount(page), target }
    return
  }
  if (target === 'engine-save-version') {
    const response = await runtime.censusSaveResponse
    assertions(response.ok()).toBe(true)
    const receipt = await response.json()
    assertions(receipt.new_version?.version).toBeGreaterThan(before.versions.head)
    let chain
    await assertions.poll(async () => {
      const reply = await page.request.get(`/api/drawings/${runtime.drawingId}/versions`, { timeout: 15_000 })
      assertions(reply.ok()).toBe(true)
      chain = await reply.json()
      return chain.head
    }).toBe(receipt.new_version.version)
    assertions(chain.versions.length).toBe(before.versions.versions.length + 1)
    await assertions(page.getByTestId('cad-edit-workbench').getByRole('status', { includeHidden: true })).toContainText(`Saved as version ${chain.head}`)
    await assertions.poll(() => engineCount(page)).toBe(before.count)
    runtime.evidence.censusSave = { receipt, before: before.versions, after: chain }
    return
  }
  throw new Error(`Unknown census effect: ${target}`)
}

export function observeSolarEditorRequests(runtime) {
  const runRequests = []
  const observe = (request) => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/run') runRequests.push(request.url())
  }
  runtime.page.on('request', observe)
  runtime.cleanup.push(() => runtime.page.off('request', observe))
  return { runRequests }
}

export async function assertSolarStepEditor(probe, runtime, locator, before, assertions = expect) {
  const { page } = runtime
  const tool = probe.assertion.tool
  const label = runtime.evidence.catalog?.record?.label || tool
  const region = page.getByRole('region', { name: `${label} parameters`, exact: true })
  await assertions(region).toBeVisible()
  // Supply only the proof's form inputs through the same controls an operator uses.
  for (const name of ['Expected rev', 'Graph revision']) {
    const field = region.getByLabel(name, { exact: true })
    if (await field.count() && await field.inputValue() === '') await field.fill('0')
  }
  const unit = region.getByLabel('Distance unit', { exact: true })
  if (await unit.count()) await unit.selectOption({ label: 'Meters' })
  for (const fieldInput of Object.values(SOLAR_REQUIRED_FORM_INPUTS[tool] || {})) {
    const field = region.getByLabel(fieldInput.label, { exact: true })
    if (await field.count() && await field.inputValue() === '') {
      if (fieldInput.select !== undefined) await field.selectOption({ label: fieldInput.select })
      else await field.fill(String(fieldInput.value))
    }
  }
  if (tool === 'solar-design-presets') {
    for (const name of ['Subcommand', 'Action']) {
      const field = region.getByLabel(name, { exact: true })
      if (await field.count()) await field.selectOption({ label: 'Create' })
    }
    for (const name of ['Preset name', 'Name']) {
      const field = region.getByLabel(name, { exact: true })
      if (await field.count()) await field.fill('Walk proof preset')
    }
  }
  const review = region.getByRole('button', { name: 'Review & run', exact: true })
  try {
    await assertions(review).toBeEnabled()
  } catch (error) {
    if (SOLAR_REQUIRED_FORM_INPUTS[tool]) {
      let blockedReason = { source: 'none', text: '' }
      try {
        const text = (await review.getAttribute('title') || '').trim()
        if (text) blockedReason = { source: 'button-title', text }
      } catch {}
      if (!blockedReason.text) {
        const visibleReasons = []
        // GenericSolarToolForm exposes invalid required fields as SchemaForm alerts.
        try {
          const alerts = region.getByRole('alert')
          for (let index = 0; index < await alerts.count(); index++) {
            try {
              const alert = alerts.nth(index)
              if (await alert.isVisible()) {
                const text = (await alert.innerText()).trim()
                if (text) visibleReasons.push(text)
              }
            } catch {}
          }
        } catch {}
        if (visibleReasons.length) blockedReason = { source: 'field-alerts', text: visibleReasons.join('; ') }
      }
      if (!blockedReason.text) {
        try {
          const reason = region.getByTestId('project-solar-equipment-reason')
          if (await reason.isVisible()) {
            const text = (await reason.innerText()).trim()
            if (text) blockedReason = { source: 'equipment-reason', text }
          }
        } catch {}
      }
      runtime.evidence.solarRunBlockedReason = blockedReason
    }
    throw error
  }
  await review.click()
  const decision = page.getByRole('button', { name: `Run ${tool}`, exact: true })
  await assertions(decision).toBeVisible()
  // Move Escape outside the editor, whose own Escape closes the editor first.
  await decision.focus()
  await page.keyboard.press('Escape')
  await assertions(decision).toBeHidden()
  // SolarToolForm closes on review; SolarStepEditor retains its inputs.
  if (!await region.isVisible()) {
    await discloseControlPanel(page, probe.locator)
    await locator.click()
  }
  await assertions(region).toBeVisible()
  await region.getByRole('button', { name: 'Cancel', exact: true }).click()
  await assertions(region).toBeHidden()
  if (await locator.isVisible()) await assertions(locator).toBeFocused()
  else await assertions(page.locator('button[aria-controls="drafting-ribbon-panels"]')).toBeFocused()
  assertions(before.runRequests).toEqual([])
  runtime.evidence.solarStepEditor = { tool, reviewed: true, cancelled: true, runRequests: [...before.runRequests] }
}

export async function recoverFailedCatalog(page, runtime, assertions = expect) {
  if (runtime.catalogRecoveryAttempted) return
  const status = page.getByRole('toolbar', { name: 'Drafting tools', exact: true })
    .getByRole('status').filter({ hasText: /^Couldn't load tools:/ })
  if (!await status.isVisible()) return
  runtime.catalogRecoveryAttempted = true
  const reason = await status.innerText()
  const response = page.waitForResponse((reply) => reply.request().method() === 'GET'
    && new URL(reply.url()).pathname === '/api/capabilities' && reply.ok(), { timeout: 15_000 })
  response.catch(() => {})
  await status.getByRole('button', { name: 'Retry', exact: true }).click({ timeout: 15_000 })
  runtime.evidence.catalogRecovery = { reason, retried: true }
  await response
  await assertions(status).toBeHidden({ timeout: 15_000 })
}

export async function captureEngineRepeat(probe, runtime, assertions = expect) {
  const { page } = runtime
  if (probe.state === 'engine-crashed') {
    await assertions(page.getByRole('toolbar', { name: 'Quick access', exact: true }).getByRole('button', {
      name: /^Undo edit \(unavailable: engine stopped: open a drawing again\)$/, exact: true,
    })).toBeVisible()
  }
  // A held edit can leave its operand prompt mounted. Escape dismisses it
  // without releasing the worker reply or changing the engine-busy state.
  await page.keyboard.press('Escape')
  const bar = page.getByRole('combobox', { name: 'Command bar', exact: true })
  await bar.fill('')
  await assertions(page.getByTestId('cockpit-prompt')).toBeHidden()
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))
  const dispatches = await page.evaluate(() => structuredClone(globalThis.__walkGeometry.dispatches))
  runtime.evidence.repeatBaseline = { dispatches }
  return { dispatches }
}

export async function activateEngineRepeat(runtime, locator, assertions = expect) {
  // Surface landmarks are not necessarily tab stops. Temporarily making the
  // real Drawing region programmatically focusable avoids an activation
  // element, an editor, and a canvas click that would change selection.
  const tabindex = await locator.getAttribute('tabindex')
  await locator.evaluate((element) => { element.setAttribute('tabindex', '-1'); element.focus() })
  runtime.cleanup.push(async () => locator.evaluate((element, original) => {
    if (original === null) element.removeAttribute('tabindex')
    else element.setAttribute('tabindex', original)
  }, tabindex))
  await assertions(locator).toBeFocused()
  await runtime.page.keyboard.press('Enter')
}

export async function assertEngineRepeat(probe, runtime, before, assertions = expect) {
  const { page } = runtime
  const prompt = page.getByTestId('cockpit-prompt')
  if (probe.state === 'ready') {
    await assertions(prompt).toBeVisible()
    await assertions(prompt).toHaveAccessibleName(`${probe.assertion.verb} command`)
  } else {
    await assertions(prompt).toBeHidden()
    // CommandLineArmer.jsx onKey calls current.refuse(action.when(ctx)).
    // EngineSessionProvider.jsx exposes that exact sentence as session status.
    // engineShortcutDecision (actionRegistry.js) returns null for absent
    // canvas/unparsed/crashed engines, so those paths announce no refusal.
    if (['no-drawing', 'engine-not-parsed', 'engine-crashed'].includes(probe.state)) {
      runtime.evidence.repeatIgnored = true
    } else {
      const status = page.getByTestId('cad-edit-workbench').getByRole('status', { includeHidden: true })
      await assertions(status).toHaveText(probe.assertion.reason)
      runtime.evidence.repeatRefusal = { text: await status.innerText(), visible: await status.isVisible() }
    }
  }
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))
  await assertions(prompt)[probe.state === 'ready' ? 'toBeVisible' : 'toBeHidden']()
  const dispatches = await page.evaluate(() => structuredClone(globalThis.__walkGeometry.dispatches))
  assertions(dispatches).toEqual(before.dispatches)
  runtime.evidence.repeatDispatches = { before: before.dispatches, after: dispatches }
}

export function solarAvailabilityProbe(probe, catalog) {
  if (probe.kind !== 'tool' || probe.state !== 'ready' || !probe.sourceId.startsWith('solar-')) return probe
  if (probe.assertion.target === 'catalog-run-decision') return probe
  const family = catalog?.families.find((family) => family.capabilities.some((tool) => tool.name === probe.sourceId))
  const tool = family?.capabilities.find((tool) => tool.name === probe.sourceId)
  if (!tool) return probe
  const effect = solarToolReadyEffect(tool, family.family_id, catalog.families)
  if (effect.target === 'catalog-run-decision') return probe
  return { ...probe, assertion: { ...effect,
    assertionId: `${probe.featureId}/${probe.state}/${effect.kind}` },
  locator: { ...probe.locator, name: accessibleName(tool.name,
    effect.kind === 'disabled_with_reason' ? effect.reason : '') } }
}

export async function captureMultipleSelection(probe, runtime, assertions = expect) {
  const { page } = runtime
  await assertions(page.getByTestId('dock-selection-count')).toHaveText('2 objects selected')
  const before = { selectionCount: 2 }
  if (probe.assertion.target === 'engine:delete') {
    before.count = await engineCount(page)
    before.geometry = await observedGeometry(page)
    before.selectedIds = [0xA100, 0xA101].map(String)
    assertions(before.geometry.entities.filter((entity) => before.selectedIds.includes(String(entity.id))).length).toBe(2)
  }
  runtime.evidence.multipleSelection = { selectionCount: before.selectionCount }
  return before
}

export async function assertMultipleSelectionDelete(runtime, before, assertions = expect) {
  const { page } = runtime
  assertions(before.selectionCount).toBe(2)
  await assertions.poll(() => engineCount(page)).toBe(before.count - 2)
  const after = await observedGeometry(page)
  assertions(after.entities.some((entity) => before.selectedIds.includes(String(entity.id)))).toBe(false)
  assertions(after.entities.map(({ index, ...entity }) => entity)).toEqual(before.geometry.entities
    .filter((entity) => !before.selectedIds.includes(String(entity.id))).map(({ index, ...entity }) => entity))
  await page.getByRole('toolbar', { name: 'Quick access', exact: true })
    .getByRole('button', { name: ACTIONS.find((action) => action.id === 'engine:undo').label, exact: true }).click()
  await assertions.poll(() => engineCount(page)).toBe(before.count)
  assertions(await observedGeometry(page)).toEqual(before.geometry)
  runtime.evidence.multipleSelection = { selectionCount: before.selectionCount, before: before.count,
    after: after.entities.length, restored: await engineCount(page), undoSteps: 1 }
}

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
    const restore = async () => {
      if (await page.locator('section.workspace-environment').count()) await enterWorkspaceEnvironment(page)
      else {
        if (await back.isVisible()) await back.click()
        await expect(page.getByRole('heading', { name: 'Project board', exact: true })).toBeHidden()
      }
    }
    await restore()
    runtime.cleanup.push(restore)
  }
  if (['scope-build-picker', 'scope-picker', 'tool-commands', 'unknown-tool-resolver'].includes(target)) {
    const bar = await clearComposer(page)
    runtime.cleanup.push(async () => { await clearComposer(page) })
    if (target === 'tool-commands') await expect(bar).toHaveAttribute('aria-expanded', 'false')
    if (target === 'unknown-tool-resolver') {
      await expect(page.getByRole('listbox', { name: 'Route resolver', exact: true })).toBeHidden()
      await expect.poll(() => runtime.evidence.responses.some((response) => new URL(response.url).pathname === '/api/capabilities'
        && response.status === 200)).toBe(true)
      const response = await page.request.get('/api/capabilities', { timeout: 15_000 })
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
    await expect(page.getByRole('dialog', { name: 'Session details', exact: true })).toBeHidden()
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
    const reply = await page.request.get('/api/session?dwg=' + encodeURIComponent(runtime.drawingId), { timeout: 15_000 })
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
    await expect(page.getByRole('dialog', { name: 'Session details', exact: true })).toBeHidden()
  }
}

export const HANDLED_SETUP_KINDS = Object.freeze(new Set([
  'census-disclosure', 'census-script', 'census-download-only',
  'fresh-sign-out-page', 'baseline-three-state', 'prepare-engine-transport', 'hold-engine-boot',
  'navigate', 'chooser-start', 'open-failed-drawing', 'failed-drawing-ribbon-tab', 'open-empty-workspace',
  'open-private-drawing', 'ribbon-tab', 'control-pressed-state', 'engine-mode-state', 'fullscreen-state',
  'empty-view-history', 'previous-view-history', 'whole-drawing-view', 'engine-ready',
  'select-entity', 'vertex-index', 'clear-selection', 'copy-selection', 'drawer-state', 'tool-rail-state', 'job-rail-state', 'zoom-inside-extents',
  'properties-state', 'properties-section-state', 'properties-close-state', 'layer-visible-state',
  'job-monitor-collapsed', 'overview-expanded-state', 'overview-pan-state', 'open-start',
  'open-escape-owner', 'find-submit-state', 'open-real-project',
  'open-project-solar', 'publish-project-solar', 'switch-project-solar-forms',
  'open-history', 'slash-menu', 'open-route', 'catalog-tool', 'foreign-checkout', 'private-policy',
  'create-line', 'hold-engine-edit', 'start-pending-run', 'require-authoring-off', 'fresh-history',
  'undo-edit', 'preview-version', 'zoom-before-fit', 'require-engine-state', 'crash-engine-worker',
  'require-surface-context', 'require-local-state',
  'hold-version-change', 'fault-restored-head', 'fault-history',
  'saved-version-history', 'require-versionless-drawing', 'seed-solar-graph', 'seed-solar-chain',
  'require-solar-document', 'hold-solar-projection',
]))
export const ENGINE_SETUP_KINDS = Object.freeze(new Set([
  'engine-ready', 'select-entity', 'vertex-index', 'create-line', 'hold-engine-edit',
  'require-engine-state', 'crash-engine-worker',
  'require-solar-document',
]))
const localFixtureReason = (recipe) => recipe.reason || `The isolated stack has no public fixture recipe for ${recipe.state}; required context ${JSON.stringify(recipe.context)}`

export const TOOL_ARM_EFFECT_KINDS = Object.freeze(new Set(['opens']))
const AVAILABILITY_FIELDS = Object.freeze(['entitled', 'engine_ready', 'input_ready', 'implemented'])

export async function workerCatalog(workerFacts, request, { drawingId, version = 'head' } = {}) {
  const fetch = async () => {
    try {
      delete workerFacts.catalog
      delete workerFacts.catalogError
      const query = drawingId ? `?drawing_id=${encodeURIComponent(drawingId)}&drawing_version=${encodeURIComponent(version)}` : ''
      const response = await request.get(`/api/capabilities${query}`, { timeout: 15_000 })
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
export const PROJECT_SOLAR_AUTH_REASON = 'authenticated_subject_required: canonical project Solar needs an authenticated subject (server/platform_link.py:159-161, server/deps.py:1300-1302); walked signed in on staging (F1)'

export function catalogSolarNeedsDrawing(probe) {
  return probe.kind === 'tool' && probe.sourceId?.startsWith('solar-')
    && probe.assertion.kind !== 'disabled_with_reason'
}
export function requireSeededSolarReady(probe, catalog) {
  const seed = probe.setup?.steps?.find((recipe) => recipe.kind === 'seed-solar-chain')
  if (!seed || probe.state !== 'ready') return
  const availability = catalog?.families.flatMap((family) => family.capabilities)
    .find((tool) => tool.name === probe.sourceId)?.availability
  if (availability?.input_ready === false) {
    throw new Error(`seed-solar-chain ${seed.stage} did not make ${probe.sourceId} input ready: ${JSON.stringify(availability)}`)
  }
}
export function unsupportedBeforeSetup(probe, workerFacts = {}, checkAvailability = true) {
  // Canonical Solar needs a verified subject even when PostgreSQL is ready.
  // Keep the authenticated recipes intact for staging (F1).
  if (['open', 'published', 'form-switch'].includes(probe.setup?.context?.projectSolar)
    && workerFacts.authLive !== true) return PROJECT_SOLAR_AUTH_REASON
  if (probe.setup?.context?.openProjectId) {
    if (workerFacts.postgresUnavailableReason) return workerFacts.postgresUnavailableReason
    if (workerFacts.walkPostgres !== true) return 'walk_postgres_required'
  }
  if (UI_UNREACHABLE_STATES.has(probe.state)) return VERSIONLESS_DRAWING_REASON
  const offlineSolar = probe.setup.steps.find((recipe) => recipe.kind === 'require-local-state'
    && /^(needs_live_sizing|cloud_service_offline|ground_physical_state_required):/.test(recipe.reason || ''))
  if (offlineSolar) return offlineSolar.reason
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

// API-only so the planner's isolated-stack proof can exercise exactly the walk's seed path.
// A single lease protects the entire prefix. Every successful write is checked against
// the persisted version chain, and failures preserve the stage, server body and timing.
// Legacy request exemplars (server/tests/): seedParams uses test_w1_seed_product_path.py;
// test_w1_rooftop_acceptance.py:325-336 supplies project completion and panel import,
// and :449 supplies the revision-only homeruns request. Equipment is gated before submission.
export async function seedSolarChain(runtime, recipe) {
  const steps = solarChainSteps(recipe.stage)
  if (recipe.electricalZone) steps.push({ stage: 'zone', tool: 'solar-electrical-zones', operation: 0 })
  const { page: { request }, drawingId, evidence } = runtime
  const path = `/api/drawings/${drawingId}`
  const proof = evidence.solarChain = { drawingId, requestedStage: recipe.stage, stages: [] }
  let currentStage, stageProof, lease, chain, rev = 0
  const checked = async (response, label) => {
    const body = await response.json()
    if (!response.ok() || body.ok === false) throw new Error(`${label}: HTTP ${response.status()} ${JSON.stringify(body)}`)
    return body
  }
  const readGraph = async () => {
    const session = await checked(await request.get(`/api/session?dwg=${encodeURIComponent(drawingId)}`, { timeout: 15_000 }), 'session')
    const graph = session.intake?.solar_design_graph
    if (!graph || graph.rev !== rev) throw new Error(`session graph revision is not ${rev}`)
    return graph
  }
  const finishStage = async () => {
    const graph = await readGraph()
    if (!graph.panels?.length) throw new Error('physical DXF produced no panels')
    if (['sized', 'strung'].includes(currentStage)
      && (graph.settings?.global_string_sizing_confirmed !== true
        || graph.settings?.extra?.string_sizing?.mode !== 'manual-global')) {
      throw new Error('confirmed manual sizing was not persisted')
    }
    if (currentStage === 'strung' && !graph.strings?.length) throw new Error('local string was not persisted')
    if (currentStage === 'zone' && !graph.electrical_zones?.length) throw new Error('local electrical zone was not persisted')
    stageProof.graphRev = graph.rev
    stageProof.elapsedMs = Date.now() - stageProof.startedAt
    proof.completedStage = currentStage
  }
  try {
    chain = await checked(await request.get(`${path}/versions`, { timeout: 15_000 }), 'versions')
    const tools = (await checked(await request.get('/api/tools', { timeout: 15_000 }), 'tools')).tools
    lease = await checked(await request.post(`${path}/checkout`, { data: { holder: 'drafter', ttl_s: 300 }, timeout: 15_000 }), 'checkout')
    if (!lease.acquired || !lease.checkout_capability) throw new Error('checkout was not acquired')
    for (const operation of steps) {
      if (operation.stage !== currentStage) {
        if (stageProof) await finishStage()
        currentStage = operation.stage
        stageProof = { stage: currentStage, startedAt: Date.now(), operations: [] }
        proof.stages.push(stageProof)
      }
      const { tool } = operation
      let params = { expected_rev: rev }
      if (tool === 'solar-settings' && operation.operation === 0) {
        params = seedParams(chain.versions.find((row) => Number(row.v) === Number(chain.head)))
      } else if (tool === 'solar-settings') {
        // test_w1_rooftop_acceptance.py:331-335: project completion is its own commit.
        params.project_changes = { name: 'Walk rooftop', zip_code: '44224', latitude: null, longitude: null }
      } else if (tool === 'solar-size-strings') {
        // test_solar_tool_solar_size_strings.py:400-419: manual_params/test_W21C2_manual_commit.
        params = { ...params, mode: 'manual-global', confirm: true }
      } else if (tool === 'solar-string-add') {
        // test_solar_ground_equipment.py:493-512: test_ground_equipment_rail_end_to_end
        // submits this operation/revision/ordered-ref shape through the legacy commit rail.
        const graph = await readGraph()
        const refs = graph.panels.slice(0, graph.settings.panels_in_sequence).map((panel) => panel.id)
        if (!refs.length) throw new Error('solar-string-add: physical DXF produced no panels')
        params = { ...params, operation: 'add-string', ordered_panel_refs: refs }
      } else if (tool === 'solar-electrical-zones') {
        // test_solar_w2_registration.py:207-217: test_zone_add_publishes_a_version.
        params = { ...params, operation: 'add', name: 'Walk zone', color_index: 1 }
      } else if (tool === 'solar-assign-equipment') {
        // Never manufacture frames or module power to get past this gate. The legacy
        // chain requires panelled frames even for manually added, otherwise valid strings.
        const catalog = await checked(await request.get(`/api/capabilities?drawing_id=${encodeURIComponent(drawingId)}&drawing_version=${runtime.drawingVersion}`, { timeout: 15_000 }), 'equipment readiness')
        const availability = catalog.families.flatMap((family) => family.capabilities)
          .find((record) => record.name === tool)?.availability
        proof.equipmentAvailability = availability
        if (availability?.input_ready !== false) {
          throw new Error(`equipment readiness did not confirm the missing frame prerequisite: ${JSON.stringify(availability)}`)
        }
        throw new Error(`needs_live_sizing: server/product_capability_availability.py:432-452; ${JSON.stringify(availability)}; local equipment requires grouped frames with module power`)
      }
      const catalogDigest = tools.find((record) => record.name === tool)?.catalog_digest
      const receipt = await checked(await request.post('/api/run?wait=1', {
        headers: { 'X-Checkout-Capability': lease.checkout_capability },
        data: runBody({ tool, drawingId, params, catalogDigest }), timeout: 45_000,
      }), tool)
      if (receipt.result?.after_rev !== rev + 1 || !receipt.result?.new_version?.version) {
        throw new Error(`${tool}: missing committed revision/version: ${JSON.stringify(receipt)}`)
      }
      const after = await checked(await request.get(`${path}/versions`, { timeout: 15_000 }), 'committed versions')
      const head = expectHeadAdvanced(chain, after)
      if (Number(head.v) !== Number(receipt.result.new_version.version)) throw new Error(`${tool}: receipt version differs from persisted head`)
      rev = receipt.result.after_rev
      chain = after
      runtime.drawingVersion = head.v
      stageProof.operations.push({ tool, graphRev: rev, version: head.v })
    }
    await finishStage()
    proof.version = runtime.drawingVersion
  } catch (error) {
    proof.error = `seed-solar-chain ${currentStage || recipe.stage}: ${error.message}`
    throw new Error(proof.error, { cause: error })
  } finally {
    if (stageProof) stageProof.elapsedMs = Date.now() - stageProof.startedAt
    if (lease?.acquired && lease.checkout_capability) {
      try {
        await checked(await request.delete(`${path}/checkout`, {
          headers: { 'X-Checkout-Capability': lease.checkout_capability }, timeout: 15_000,
        }), 'release checkout')
      } catch (error) {
        proof.releaseError = error.message
        if (!proof.error) throw error
      }
    }
  }
}

export async function prepareStudioRail(probe, runtime, recipe, { enter = requireWorkspace, reveal = revealStudioRail } = {}) {
  const { page } = runtime
  if (STUDIO_RAIL_FEATURES.has(probe.featureId)
    && (['baseline-three-state', 'job-monitor-collapsed'].includes(recipe.kind)
      || (probe.featureId === 'control:objects' && recipe.kind === 'census-disclosure'))) {
    await enter(runtime)
    if (await reveal(page)) runtime.cleanup.push(async () => {
      const button = studioRailButton(page)
      if (await button.getAttribute('aria-expanded') === 'true') await button.click()
    })
  }
}

export async function setupStep(probe, runtime, recipe, assertions = runtime.recipeAssertions || expect) {
  const { page, stack, evidence } = runtime
  if (probe.setup?.context?.projectSolar && !runtime.projectSolar) observeProjectSolarResponses(runtime)
  await prepareStudioRail(probe, runtime, recipe)
  switch (recipe.kind) {
    case 'chooser-start': await prepareChooserStart(runtime, assertions); return
    case 'open-project-solar': await openProjectSolar(runtime, assertions); return
    case 'publish-project-solar': await publishProjectSolar(runtime, assertions); return
    case 'switch-project-solar-forms': await switchProjectSolarForms(runtime, assertions); return
    case 'census-disclosure': await setupCensusDisclosure(runtime, recipe, assertions); return
    case 'census-script': await setupCensusScript(runtime, recipe, assertions); return
    case 'census-download-only': await setupCensusDownloadOnly(runtime, assertions); return
    case 'hold-version-change':
    case 'fault-restored-head': await setupVersionFault(probe, runtime, recipe, assertions); return
    case 'fault-history': await setupHistoryFault(probe, runtime, assertions); return
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
      await reloadAndReenter(page, { timeout: 60_000 })
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
      runtime.cleanup.push(await holdJobRoutes(page, ENGINE_WASM_ROUTE))
      return
    }
    case 'hold-solar-projection': {
      evidence.solarProjectionRequests = []
      runtime.releaseSolarProjection = await holdJobRoutes(page, '**/api/drawings/*/dxf?*', (route) => {
        evidence.solarProjectionRequests.push(route.request().url())
      })
      runtime.cleanup.push(runtime.releaseSolarProjection)
      return
    }
    case 'require-solar-document': await requireSolarDocument(probe, runtime, assertions); return
    case 'navigate': await page.goto(recipe.url); return
    case 'open-failed-drawing': {
      const starts = new WeakMap()
      const matches = (request) => {
        const url = new URL(request.url())
        return url.pathname === '/api/session' && url.searchParams.get('dwg') === 'missing.invalid'
      }
      let timer, onResponse, onFailed
      const onRequest = (request) => { if (matches(request)) starts.set(request, Date.now()) }
      const sessionReply = new Promise((resolve, reject) => {
        onResponse = (response) => { if (matches(response)) resolve(response) }
        onFailed = (request) => {
          if (!matches(request)) return
          const { durationMs } = requestTiming(request, starts.get(request) ?? Date.now())
          reject(new Error(`${recipe.kind}: ${request.method()} /api/session failed after ${durationMs} ms: ${request.failure()?.errorText || 'request failed'}`))
        }
        timer = setTimeout(() => reject(new Error(`${recipe.kind}: /api/session response timed out after 15000 ms`)), 15_000)
        page.on('request', onRequest)
        page.on('response', onResponse)
        page.on('requestfailed', onFailed)
      })
      // Observe failure immediately, including while navigation is still pending.
      let response
      try {
        const navigation = page.goto(recipe.url)
        response = await Promise.race([sessionReply, navigation.then(() => sessionReply)])
        await navigation
      } finally {
        clearTimeout(timer)
        page.off('request', onRequest)
        page.off('response', onResponse)
        page.off('requestfailed', onFailed)
      }
      await enterWorkspaceEnvironment(page)
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
      await enterWorkspaceEnvironment(page)
      return
    case 'open-private-drawing': {
      const selected = probe.setup.steps.find((step) => step.kind === 'select-entity')
      let source = await readFile(new URL(selected?.type === 'INSERT' ? '../fixtures/block-fixture.dxf'
        : '../fixtures/distinctive-panel.dxf', import.meta.url), 'utf8')
      if (selected?.type !== 'INSERT') {
        source = injectWalkEntities(source)
      }
      // Setup uploads precede the drawing navigation. Live auth needs the
      // page's own origin loaded before its localStorage can supply the JWT.
      if ((runtime.workerFacts || evidence.workerFacts)?.authLive === true && page.url() === 'about:blank') {
        await page.goto(`/try?surface=${recipe.surface}`, { timeout: 60_000 })
      }
      // Explicit bounds: these setup reads must not inherit the 15 s actionTimeout on a loaded host.
      const uploaded = await page.request.post('/api/drawings/upload', {
        headers: await setupRequestHeaders(runtime, '/api/drawings/upload'),
        multipart: { file: { name: 'walk.dxf', mimeType: 'application/dxf', buffer: Buffer.from(source) } },
        timeout: 60_000,
      })
      if (!probe.setup.context.projectSolar) expect(uploaded.status()).toBe(202)
      const receipt = probe.setup.context.projectSolar
        ? await projectSolarResponse(uploaded, 'open-private-drawing upload', [202], runtime) : await uploaded.json()
      expect(receipt.drawing_id).toBeTruthy()
      runtime.drawingId = receipt.drawing_id
      await expect.poll(async () => {
        const path = `/api/drawings/${receipt.drawing_id}/upload-status`
        const response = await page.request.get(path, {
          headers: await setupRequestHeaders(runtime, path), timeout: 30_000,
        })
        // A transient non-OK read is still pending; only a 'failed' status fails the setup.
        if (!response.ok()) {
          if (probe.setup.context.projectSolar) await projectSolarResponse(response, 'open-private-drawing extraction', [200], runtime)
          return `http-${response.status()}`
        }
        const status = await response.json()
        expect(status.status, JSON.stringify(status)).not.toBe('failed')
        return status.status
      }, { timeout: 60_000 }).toBe('ready')
      await page.goto(`/app?drawing=${receipt.drawing_id}&surface=${recipe.surface}`)
      await enterWorkspaceEnvironment(page, assertions)
      if (recipe.surface === 'cad' || recipe.surface === 'solar') {
        await assertions.poll(async () => {
          await enterWorkspaceIfChooser(page)
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
    case 'engine-mode-state': {
      const button = control(page, recipe.control)
      await assertions(button).toBeEnabled()
      await assertions(button).toHaveAttribute('aria-pressed', /^(true|false)$/)
      if ((await button.getAttribute('aria-pressed') === 'true') !== recipe.pressed) await button.click({ timeout: 15_000 })
      await assertions(button).toHaveAttribute('aria-pressed', String(recipe.pressed))
      await assertions.poll(async () => (await readEngineModes(page))[recipe.mode]).toBe(recipe.pressed)
      runtime.initialEngineModes = await readEngineModes(page)
      return
    }
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
    case 'vertex-index':
      await page.getByTestId('cad-edit-workbench').getByRole('textbox', { name: 'vertex index', exact: true }).fill(recipe.value)
      await assertions(page.getByTestId('cad-edit-workbench').getByRole('button', { name: 'Delete vertex', exact: true })).toBeEnabled()
      return
    case 'select-entity': await selectEntity(probe, runtime, recipe); return
    case 'clear-selection': await page.keyboard.press('Escape'); return
    case 'copy-selection':
      await discloseControlPanel(page, { group: 'clipboard' })
      await page.getByRole('toolbar', { name: 'Drafting tools', exact: true }).getByRole('group', { name: 'Clipboard', exact: true })
        .getByRole('button', { name: ACTIONS.find((action) => action.op === 'copyClip').label, exact: true }).click()
      return
    case 'drawer-state':
      if (runtime.testInfo?.project.name === 'phone' && probe.featureId === 'drawer:result'
        && recipe.name === 'Result' && await studioRailButton(page).isVisible()) {
        await setStudioDrawer(page, 'result', recipe.open, assertions)
      } else if (probe.featureId === 'drawer:jobs' && recipe.name === 'Jobs' && await studioRailButton(page).isVisible()) {
        await setStudioDrawer(page, 'jobs', recipe.open, assertions)
      } else if (runtime.testInfo?.project.name === 'phone' && (probe.featureId === 'drawer:none'
        || probe.featureId === 'action:bar-escape' && probe.state === 'drawer-open') && await studioRailButton(page).isVisible()) {
        await setPhonePanel(page, 'Plan', false, assertions)
        await setPhonePanel(page, 'Jobs', recipe.open, assertions)
      } else if (runtime.testInfo?.project.name === 'phone' && ['Jobs', 'Plan'].includes(recipe.name)) {
        await setPhonePanel(page, recipe.name, recipe.open, assertions)
      } else await setDrawer(page, recipe.name, recipe.open)
      return
    case 'job-rail-state': await setJobRail(page, recipe.open, runtime.testInfo.project.name === 'phone', assertions,
      probe.featureId === 'drawer:jobs'); return
    case 'tool-rail-state': await setToolRail(page, recipe.open, runtime.testInfo.project.name === 'phone', assertions,
      probe.featureId === 'drawer:nav'); return
    case 'properties-state': {
      if (runtime.testInfo?.project.name === 'phone') {
        await setPhonePanel(page, 'Plan', recipe.open, assertions)
        evidence.phoneProperties = { host: 'Plan', open: recipe.open }
        return
      }
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
    case 'open-escape-owner': {
      const bar = await clearComposer(page)
      await bar.fill('/')
      await expect(page.getByRole('listbox', { name: 'Tool commands', exact: true })).toBeVisible()
      await expect(bar).toHaveAttribute('aria-expanded', 'true')
      await expect(bar).toBeFocused()
      evidence.escapeOwner = { menu: 'Tool commands', focus: 'Command bar' }
      return
    }
    case 'find-submit-state': {
      const field = page.getByRole('combobox', { name: 'Find in drawing', exact: true })
      const reply = await page.request.get('/api/session?dwg=' + encodeURIComponent(runtime.drawingId), { timeout: 15_000 })
      expect(reply.ok()).toBe(true)
      const { intake } = await reply.json()
      expect(intake).toBeTruthy()
      const index = buildDrawingObjectIndex({ drawingKey: runtime.drawingId, intake,
        solarGraph: intake.solar_design_graph ? { drawingKey: runtime.drawingId, graph: intake.solar_design_graph } : null })
      expect(index.records.length).toBeGreaterThan(0)
      expect(index.resolve('w1k-absent-object-7f942').status).toBe('missing')
      await field.fill('')
      await expect(page.getByRole('status').filter({ hasText: 'No matching object in this drawing.' })).toHaveCount(0)
      await field.fill('w1k-absent-object-7f942')
      await expect(field).toHaveAttribute('aria-expanded', 'false')
      await expect(page.getByRole('status').filter({ hasText: 'No matching object in this drawing.' })).toHaveCount(0)
      runtime.cleanup.push(async () => { await field.fill('') })
      return
    }
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
      requireSeededSolarReady(probe, catalog)
      runtime.catalogPanelName = family.label
      // Solar seats unplaced families outside its Manage fold on the Solar tab.
      const placementTab = toolPlacementTab(tool)
      const tab = !placementTab && typeof family.family_id === 'string'
        && !familiesForSurface(catalog.families, 'solar').some((row) => row.family_id === family.family_id)
        && await page.locator('.app').first().getAttribute('data-surface') === 'solar'
        ? 'Solar' : placementTab || 'manage'
      await setupStep(probe, runtime, { kind: 'ribbon-tab', name: tab[0].toUpperCase() + tab.slice(1) })
      if (typeof recoverFailedCatalog === 'function') await recoverFailedCatalog(page, runtime, assertions)
      if (probe.state === 'ready' && solarBrowserCatalogRequired(recipe.name) && !toolAvailabilityEvidence(probe, facts)) {
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
    case 'seed-solar-chain': {
      await seedSolarChain(runtime, recipe)
      await reloadAndReenter(page, { timeout: 60_000 })
      await requireWorkspace(runtime)
      return
    }
    case 'seed-solar-graph': {
      const path = `/api/drawings/${runtime.drawingId}`
      const versions = await page.request.get(`${path}/versions`, { timeout: 15_000 })
      expect(versions.ok()).toBe(true)
      const chain = await versions.json()
      const parent = chain.versions.find((row) => Number(row.v) === Number(chain.head))
      const toolsResponse = await page.request.get('/api/tools', { timeout: 15_000 })
      expect(toolsResponse.ok()).toBe(true)
      const settings = (await toolsResponse.json()).tools.find((tool) => tool.name === 'solar-settings')
      expect(settings).toBeTruthy()
      const checkoutPath = `${path}/checkout`
      const checkout = await page.request.post(checkoutPath, { data: { holder: 'drafter', ttl_s: 300 }, timeout: 15_000 })
      expect(checkout.ok()).toBe(true)
      const lease = await checkout.json()
      expect(lease.acquired).toBe(true)
      expect(lease.checkout_capability).toBeTruthy()
      const release = async () => {
        const response = await page.request.delete(checkoutPath, {
          timeout: 15_000,
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
      await reloadAndReenter(page, { timeout: 60_000 })
      await requireWorkspace(runtime)
      return
    }
    case 'foreign-checkout': {
      const response = await page.request.post(`/api/drawings/${runtime.drawingId}/checkout`, { data: { holder: 'walk-other-editor', ttl_s: 300 }, timeout: 15_000 })
      expect(response.status()).toBe(200)
      expect((await response.json()).acquired).toBe(true)
      await reloadAndReenter(page)
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
      const response = await page.request.get('/api/entitlements', { timeout: 15_000 })
      expect(response.ok()).toBe(true)
      const policyResponse = await response.json()
      expect(policyResponse.entitlements[recipe.capability]).toBe(recipe.allowed)
      if (probe.featureId === 'action:author-tool') evidence.authorPolicy = { status: response.status(), policyResponse,
        beforeReload: await disclosureEvidence(page, 'Author') }
      await reloadAndReenter(page, { timeout: 60_000 })
      if (probe.featureId === 'action:author-tool') {
        await requireWorkspace(runtime)
        evidence.authorPolicy.afterReload = await disclosureEvidence(page, 'Author')
      }
      if (runtime.ribbonTab) await setupStep(probe, runtime, { kind: 'ribbon-tab', name: runtime.ribbonTab })
      if (probe.featureId === 'action:author-tool') {
        const author = page.getByRole('toolbar', { name: 'Drafting tools', exact: true })
          .getByRole('group', { name: 'Author', exact: true, includeHidden: true })
        await assertions(author).toHaveCount(1, { timeout: 15_000 })
        await discloseControlPanel(page, { group: 'author' })
        await assertions(author).toBeVisible()
        evidence.authorPolicy.afterDisclosure = await disclosureEvidence(page, 'Author')
      }
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
      const response = await page.request.get('/api/entitlements', { timeout: 15_000 })
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
    case 'zoom-inside-extents': {
      evidence.zoomBaseline = await establishZoomBaseline(page)
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
      const response = await page.request.get('/api/health', { timeout: 15_000 })
      expect(response.ok()).toBe(true)
      const health = await response.json()
      evidence.health = health
      if (runtime.releaseSolarProjection) {
        await assertions.poll(() => evidence.solarProjectionRequests.length).toBeGreaterThan(0)
        const solar = page.getByRole('tablist', { name: 'Workspace profile', exact: true, includeHidden: true })
          .getByRole('tab', { name: 'Solar CAD', exact: false, includeHidden: true })
        await assertSurfaceState(solar, 'beta', assertions)
        await assertions(solar).toContainText('Beta')
        await assertions(page.locator('.workspace-card')).not.toHaveAttribute('data-engine-document', /.+/)
        evidence.solarReadiness = { transport: 'drawing DXF projection held', pending: 'Beta' }
      }
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
    case 'open-real-project':
      if (probe.setup.context.projectSolar) {
        await projectSolarStep(runtime, 'open-real-project', () => setupRealProject(runtime, assertions))
      } else await setupRealProject(runtime, assertions)
      return
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
  if (probe.locator?.keyboardAction === 'engine:repeat' && typeof captureEngineRepeat === 'function') {
    return captureEngineRepeat(probe, runtime)
  }
  if (probe.assertion.target === 'solar-step-editor') return observeSolarEditorRequests(runtime)
  if (['engine-save-version', 'engine-undo-edit', 'engine-redo-edit', 'script-run'].includes(probe.assertion.target)) return captureCensusEffect(probe, runtime)
  if (typeof barNoRung === 'function' && barNoRung(probe)) {
    if (await page.getByTestId('cad-edit-workbench').count()) await engineReady(probe, runtime)
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))
    return captureBarNoRung(runtime)
  }
  // Refusal probes do not activate the action or need its enabled-state baseline.
  if (probe.assertion.kind === 'disabled_with_reason' && probe.kind === 'action') return {}
  const target = probe.assertion.target || ''
  if (probe.state === 'multiple-selected' && ['engine:delete', 'cockpit-prompt'].includes(target)
    && typeof captureMultipleSelection === 'function') return captureMultipleSelection(probe, runtime)
  if (target === 'engine-refusal') return captureEngineRefusal(runtime)
  if (target === 'browser-clipboard' || target === 'engine:explode') return { count: await engineCount(page), geometry: await observedGeometry(page),
    ...(target === 'browser-clipboard' ? { sourceId: await page.getByTestId('cad-edit-entity-list').locator('input:checked').inputValue() } : {}) }
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
  if (target === 'properties-pane') return { visible: runtime.testInfo.project.name === 'phone'
    ? await readPhonePanel(page, 'Plan')
    : await page.getByRole('complementary', { name: 'Properties', exact: true }).isVisible() }
  if (target === 'escape:drawer' && runtime.testInfo.project.name === 'phone') return {
    command: await page.getByRole('combobox', { name: 'Command bar', exact: true }).inputValue(),
  }
  if (target === 'version-history') return { visible: await page.getByRole('dialog', { name: 'Version history', exact: true }).isVisible() }
  if (target.startsWith('engine:') || target === 'browser-clipboard-cut') return { count: await engineCount(page) }
  if (target.startsWith('drawing-version-')) {
    const response = await page.request.get(`/api/drawings/${runtime.drawingId}/versions`)
    expect(response.ok()).toBe(true)
    await engineReady(probe, runtime)
    const versions = await response.json()
    await expect(page.locator('.cad-edit-workbench-doc')).toContainText(`${runtime.drawingId}-v${versions.head}.dxf`)
    await expect.poll(async () => (await observedGeometry(page)).documentId).toBe(`${runtime.drawingId}-v${versions.head}.dxf`)
    return { versions, count: await engineCount(page), geometry: await observedGeometry(page) }
  }
  return {}
}

async function activate(probe, runtime, locator) {
  const { page } = runtime
  const recipe = probe.locator
  if (probe.kind === 'surface' && recipe.role === 'tab') {
    await (await resolveSurfaceControl(page, probe)).activate()
    return
  }
  if (recipe?.keyboardAction === 'engine:repeat' && typeof activateEngineRepeat === 'function') {
    await activateEngineRepeat(runtime, locator)
    return
  }
  if (probe.assertion.target === 'script-file-picker') {
    await activateCensusFileChooser(runtime, locator)
    return
  }
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
    else if (recipe.key === 'Escape' && probe.assertion.target === 'escape:owner') {
      // Exercise the slash menu while its prepared input still owns focus.
      const bar = page.getByRole('combobox', { name: 'Command bar', exact: true })
      await expect(bar).toBeFocused()
      await bar.press('Escape')
    }
    else if (probe.featureId === 'action:bar-escape' && probe.state === 'drawer-open'
      && runtime.testInfo.project.name === 'phone') {
      await page.getByRole('combobox', { name: 'Command bar', exact: true }).press('Escape')
    }
    else {
      // Focus a real non-text control without clicking outside a drawer.
      // That click could close it before the keyboard action under test.
      const project = page.getByRole('navigation', { name: 'Workspace controls', exact: true }).getByRole('button', { name: 'Project', exact: true })
      if (await project.isVisible()) await project.focus()
      else await page.getByRole('tablist', { name: 'Workspace profile', exact: true }).getByRole('tab').first().focus()
      const press = async () => {
        if (probe.featureId === 'action:bar-retry' && probe.state === 'retry-history') {
          await runtime.walkPersonaObservers?.observe('qc-dev', 'observeEvent', 'action:bar-retry')
        }
        return page.keyboard.press(recipe.key)
      }
      if (runtime.panelPerformanceCapture) await runtime.panelPerformanceCapture.dispatch(press)
      else await press()
    }
  } else if (recipe.trigger === 'navigate') await page.goto(recipe.url)
  else if (recipe.trigger === 'select') {
    const choices = await locator.locator('option').evaluateAll((options) => options.map((option) => option.value))
    const current = await locator.inputValue()
    const choice = choices.find((value) => value !== current && value !== 'index...')
    expect(choice, 'a property change must offer another real value').toBeDefined()
    await locator.selectOption(choice)
  } else {
    const click = async () => {
      if (probe.featureId === 'control:version-history' && probe.state === 'ready') {
        await runtime.walkPersonaObservers?.observe('qc-dev', 'observeEvent', 'control:version-history')
      }
      return locator.click()
    }
    if (runtime.panelPerformanceCapture) await runtime.panelPerformanceCapture.dispatch(click)
    else await click()
  }
}

export async function assertEffect(probe, runtime, locator, before, assertions = expect) {
  const expect = assertions
  const { page } = runtime
  const effect = probe.assertion
  const target = effect.target || ''
  if (target === 'project-shared-tools') {
    const region = page.getByRole('region', { name: effect.region, exact: true })
    await expect(region).toBeVisible()
    await expect(region).toHaveAttribute('data-pane', 'tools')
    await expect(page.locator(effect.content).first()).toBeVisible()
    return
  }
  if (target === 'project-shared-sidebar') {
    await expect(page.getByRole('region', { name: effect.region, exact: true })).toBeVisible()
    const content = page.locator(effect.content)
    await expect(effect.region === 'Project sidebar' ? content.first() : content).toBeVisible()
    return
  }
  if (target === 'chooser-project-sidebar') {
    await assertChooserOpen(runtime, assertions)
    return
  }
  if (probe.featureId === 'action:rail-expand' && probe.state === 'ready'
    && runtime.testInfo.project.name !== 'phone' && runtime.railExpandFamily) {
    await assertRailExpandFamily(page, runtime.railExpandFamily, assertions)
    return
  }
  if (probe.locator?.keyboardAction === 'engine:repeat' && typeof assertEngineRepeat === 'function') {
    await assertEngineRepeat(probe, runtime, before, assertions)
    return
  }
  if (target === 'solar-step-editor') {
    await assertSolarStepEditor(probe, runtime, locator, before, assertions)
    return
  }
  if (typeof CENSUS_DISCLOSURES !== 'undefined' && CENSUS_DISCLOSURES[target] || ['engine-save-version', 'engine-undo-edit', 'engine-redo-edit',
    'ribbon-script-text', 'script-file-picker', 'script-run'].includes(target)) {
    await assertCensusEffect(probe, runtime, locator, before, assertions)
    return
  }
  if (typeof barNoRung === 'function' && barNoRung(probe)) { await assertBarNoRung(probe, runtime, locator, before, assertions); return }
  if (target === 'engine-refusal') { await assertEngineRefusal(probe, runtime, before, assertions); return }
  if (probe.kind === 'control' && baselineThreePanels[target]) {
    const spec = baselineThreePanels[target]
    const panel = namedPanel(page, spec)
    await expect(panel).toBeVisible()
    if (['claude-accounts-panel', 'linked-services-panel', 'version-history', 'cost-panel'].includes(target)) {
      await expect(locator).toHaveAttribute('aria-expanded', 'true')
    }
    await expect(panel.getByRole('button', { name: spec.close, exact: true })).toBeVisible()
    if (target === 'cost-panel') await expect(panel.getByRole('heading', { name: spec.name, exact: true })).toBeVisible()
    if (probe.featureId === 'control:version-history' && probe.state === 'ready'
      && runtime.testInfo.project.name === 'desktop' && !before.visible) {
      await runtime.walkPersonaObservers?.observe('end-user', 'observeEffect', 'version-history-open')
      await runtime.walkPersonaObservers?.observe('qc-dev', 'observeEvent', 'version-history-open')
      await runtime.walkPersonaObservers?.observe('qc-dev', 'observeResult', 'version-history-open')
      await runtime.walkPersonaObservers?.observe('ui-ux-dev', 'completion', { page,
        controls: [{ id: 'history-trigger', locator },
          { id: 'history-close', locator: panel.getByRole('button', { name: spec.close, exact: true }) }],
        completionLocator: panel, completionObserved: true })
    }
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
    const environment = page.locator('section.workspace-environment')
    if (await environment.count()) {
      await assertions(page.getByRole('heading', { name: 'What would you like to work on?', exact: true })).toBeVisible()
      await assertions(environment).toHaveAttribute('data-entered', 'false')
    } else {
      await assertions(page.getByRole('heading', { name: 'Project board', exact: true })).toBeVisible()
      await assertions(page.getByRole('button', { name: 'Return to drawing', exact: true })).toBeVisible()
    }
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
    await enterWorkspaceEnvironment(page)
    const details = page.getByRole('button', { name: 'Details', exact: true })
    await expect(details).toHaveCount(1)
    await details.click()
    const panel = page.getByRole('dialog', { name: 'Session details', exact: true })
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
  if (target === 'drawing-find-no-match' || target === 'drawing-find-submit-no-match') {
    await expect(page.getByRole('status').filter({ hasText: /^No matching object in this drawing\.$/ })).toHaveText('No matching object in this drawing.')
    await expect(page.getByRole('combobox', { name: 'Find in drawing', exact: true })).toHaveAttribute('aria-expanded', 'false')
    return
  }
  if (effect.kind === 'disabled_with_reason') {
    if (probe.kind === 'control') {
      await expect(locator).toBeDisabled()
      await expect(locator).toHaveAccessibleName(probe.locator.name)
      if (probe.locator.tooltip !== undefined) await expect(locator).toHaveAttribute('title', probe.locator.tooltip)
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
    if (runtime.evidence?.versionFault) await finishVersionFault(runtime, assertions)
    return
  }
  if (/^engine-mode:(ortho|osnap)$/.test(target)) {
    await assertEngineMode(probe, runtime, locator, assertions)
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
    const beforeUrl = new URL(before.url)
    const afterUrl = new URL(page.url())
    expect(afterUrl.origin).toBe(beforeUrl.origin)
    expect(afterUrl.pathname).toBe(beforeUrl.pathname)
    expect(afterUrl.hash).toBe(beforeUrl.hash)
    // App.jsx setJobRailExpanded seats drawer=jobs via useViewParamSeat in both ready and failed-load states.
    expect(afterUrl.search).toBe(buildViewSearch(beforeUrl.search, { drawer: 'jobs' }))
    runtime.evidence.jobMonitorUrl = { before: before.url, after: page.url() }
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
    if (probe.state === 'multiple-selected') {
      expect(before.selectionCount).toBe(2)
      await expect(page.getByTestId('dock-selection-count')).toHaveText('2 objects selected')
      runtime.evidence.multipleSelection = { selectionCount: before.selectionCount, verb: effect.verb, promptArmed: true }
    }
    return
  }
  if (target === 'command-menu') { await expect(page.getByRole('listbox', { name: 'Tool commands', exact: true })).toBeVisible(); return }
  if (target === 'mounted-mcp-servers') { await expect(page.getByRole('status', { name: 'Mounted MCP servers', exact: true })).toBeVisible(); return }
  if (target === 'keyboard-shortcut-sheet') { await expect(page.getByRole('dialog', { name: 'Keyboard shortcuts', exact: true })).toBeVisible(); return }
  if (target === 'pending-property-change') { await expect(page.getByTestId('property-apply-strip')).toBeVisible(); return }
  if (target === 'catalog-run-decision') {
    if (probe.sourceId === 'solar-solve-proposal') {
      const record = runtime.evidence.catalog?.record
      expect(record?.name).toBe(probe.sourceId)
      expect(Object.fromEntries(AVAILABILITY_FIELDS.map((field) => [field, record?.availability?.[field]]))).toEqual({
        entitled: true, engine_ready: true, input_ready: true, implemented: true,
      })
    }
    await expect(page.getByRole('button', { name: `Run ${effect.tool}`, exact: true })).toBeVisible()
    if (probe.sourceId === 'solar-solve-proposal') {
      expect(runtime.catalogRunRequests || []).toEqual([])
      expect(runtime.evidence.responses.filter((response) => response.method === 'POST' && new URL(response.url).pathname === '/api/run').length).toBe(0)
    }
    return
  }
  if (target === 'author-panel') { await expect(page.getByRole('textbox', { name: /describe|build|tool/i }).first()).toBeVisible(); return }
  if (target === 'properties-pane') {
    if (runtime.testInfo?.project.name === 'phone') {
      if (effect.value === false) expect(before.visible).toBe(true)
      await assertPhonePanel(page, 'Plan', !before.visible, assertions)
      runtime.evidence.phoneProperties = { host: 'Plan', before: before.visible, after: !before.visible }
      return
    }
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
    if (runtime.testInfo.project.name === 'phone' && probe.featureId === 'drawer:result'
      && target === 'drawer:result' && await studioRailButton(page).isVisible()) {
      await assertStudioDrawer(page, 'result', effect.value !== 'none', assertions)
      return
    }
    if (['drawer:jobs', 'drawer:nav'].includes(probe.featureId)
      && ['drawer:jobs', 'drawer:nav'].includes(target) && await studioRailButton(page).isVisible()) {
      await assertStudioDrawer(page, target.slice(7), effect.value !== 'none', assertions)
      return
    }
    if (runtime.testInfo.project.name === 'phone' && ['drawer:plan', 'drawer:jobs'].includes(target)) {
      await assertPhonePanel(page, target === 'drawer:plan' ? 'Plan' : 'Jobs', effect.value !== 'none', assertions)
      return
    }
    if (runtime.testInfo.project.name === 'phone' && target === 'drawer:none') {
      await assertPhoneDrawersClosed(page, assertions)
      return
    }
    if (target === 'drawer:jobs' && runtime.testInfo.project.name !== 'phone') {
      const open = effect.value !== 'none'
      await expect(page.getByRole('button', { name: open ? 'Collapse the job monitor to a spine' : /^Expand the job monitor \([0-9]+ live\)$/ })).toBeVisible()
      await expect(page.locator('aside.rail .rail-ledger')).toHaveCount(open ? 1 : 0)
      return
    }
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
    if (rung === 'owner') {
      await expect(page.getByRole('listbox', { name: 'Tool commands', exact: true })).toBeHidden()
      const bar = page.getByRole('combobox', { name: 'Command bar', exact: true })
      await expect(bar).toHaveAttribute('aria-expanded', 'false')
      await expect(bar).toBeFocused()
      runtime.evidence.escapeOwner.dismissed = true
    } else if (rung === 'drawer') {
      if (runtime.testInfo.project.name === 'phone') {
        await assertPhoneDrawersClosed(page, assertions)
        await expect(page.getByRole('combobox', { name: 'Command bar', exact: true })).toHaveValue(before.command)
      } else for (const label of ['Catalog', 'Jobs', 'Result', 'Plan']) await expect(panelButton(page, label)).toHaveAttribute('aria-expanded', 'false')
    } else if (rung === 'history') await expect(page.getByRole('dialog', { name: 'Version history', exact: true })).toBeHidden()
    else if (rung === 'start') await expect(page.getByRole('region', { name: 'Project workspace', exact: true })).toBeHidden()
    else if (rung === 'route') await expect(page.getByRole('button', { name: /^Run / })).toHaveCount(0)
    else if (rung === 'selection') await expect(page.getByTestId('dock-properties')).toHaveCount(0)
    else if (rung === 'running') {
      await expect(page.locator('.toast[role="status"]').getByText('Stopped following count-by-layer. It keeps running; find it in Jobs.', { exact: true })).toBeVisible()
      await expect(page.locator('.strip-running')).toHaveCount(0)
      const reply = await page.request.get('/api/jobs')
      expect(reply.ok()).toBe(true)
      const body = await reply.json()
      const jobs = Array.isArray(body) ? body : body.jobs
      expect(jobs.some((job) => job.job_id === runtime.evidence.pendingRun.jobId)).toBe(true)
      await setJobRail(page, true, runtime.testInfo.project.name === 'phone', assertions, false)
      await expect(page.locator('aside.rail').getByText('count-by-layer', { exact: true }).first()).toBeVisible()
      runtime.evidence.pendingRun.detached = true
    }
    else if (rung === 'project') {
      await expect(page.locator('.workspace-summary')).toHaveCount(0)
      await expect(page.getByRole('button', { name: 'Close workspace', exact: true })).toHaveCount(0)
      expect(runtime.evidence.escapeProject.projectId).toBeTruthy()
      runtime.evidence.escapeProject.closed = true
    }
    else throw new Error(`No observable Escape oracle for ${rung}`)
    return
  }
  if (target === 'retry:history') {
    await assertHistoryRecovery(runtime, assertions)
    if (probe.featureId === 'action:bar-retry' && probe.state === 'retry-history') {
      await runtime.walkPersonaObservers?.observe('qc-dev', 'observeEvent', 'Versions restored')
      await runtime.walkPersonaObservers?.observe('qc-dev', 'observeResult', 'Versions restored')
      await runtime.walkPersonaObservers?.observe('ops-sre', 'observeRecovery', {
        action: 'Retry', outcome: 'Versions restored', outcomeVisible: true,
      })
    }
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
  if (target === 'project-solar-workspace') {
    const proof = runtime.evidence.projectSolar
    await assertProjectSolarDrawing(runtime, assertions)
    expect(proof.open.workspaceVisible).toBe(true)
    expect(proof.open.projectRailVisible).toBe(true)
    expect(proof.importedVersionId).toBe(proof.open.head)
    if (['published', 'form-switch'].includes(effect.phase)) {
      expect(isUuid(proof.publication.job_id)).toBe(true)
      expect(proof.publication.run_status).toBe(202)
      expect(proof.jobStatus).toBe('complete')
      expect(proof.publication.output_version_id).not.toBe(proof.importedVersionId)
      expect(proof.publishedHead).toBe(proof.publication.output_version_id)
      expect(proof.drawingFollowedVersionId).toBe(proof.publishedHead)
    }
    if (effect.phase === 'form-switch') expect(proof.formSwitch).toEqual({
      solveFieldsReset: true, membershipsReset: true, runSolveDisabled: true, submittedRuns: 0,
    })
    return
  }
  if (target.startsWith('surface:')) {
    if (probe.state === 'signed-out') {
      await expect(page.getByRole('heading', { name: 'You are not signed in', exact: true })).toBeVisible()
      runtime.evidence.surfaceHost = { host: '/try', contract: 'signed-out' }
      return
    }
    if (probe.state === 'no-drawing') {
      await expect(page.getByText('No drawing yet. Upload a DWG or DXF to begin.', { exact: true })).toBeVisible()
      runtime.evidence.surfaceHost = { host: '/try', contract: 'empty' }
      return
    }
    const surface = PRODUCT_SURFACES.find((surface) => surface.id === probe.sourceId)
    let statusLocator = locator
    if (surface.contract.chrome.tab) {
      const resolved = await resolveSurfaceControl(page, probe)
      await resolved.assertSelected(assertions)
      statusLocator = resolved.legacy
      await expect(statusLocator).toContainText(effect.label)
      await assertSurfaceState(statusLocator, effect.state, assertions)
    }
    if (effect.ground === 'drawing' && effect.state !== 'setup' && effect.state !== 'sign-in') await expect(canvas(page)).toBeVisible()
    else if (effect.ground === 'board') await expect(page.getByRole('region', { name: 'Project workspace', exact: true })).toBeVisible()
    else if (effect.ground === 'sheet') { await expect(page).toHaveURL(/\/sheets$/); await expect(page.getByRole('main')).toBeVisible() }
    await completeSolarReadiness(probe, runtime, statusLocator, assertions)
    return
  }
  if (target === 'browser-clipboard') {
    await discloseControlPanel(page, { group: 'clipboard' })
    await expect(page.getByRole('group', { name: 'Clipboard', exact: true }).getByRole('button', {
      name: ACTIONS.find((action) => action.op === 'pasteClip').label, exact: true,
    })).toBeEnabled()
    await assertCopiedGeometry(probe, runtime, before, assertions)
    return
  }
  if (target.startsWith('drawing-version-')) {
    await assertVersionTransition(probe, runtime, before, assertions)
    return
  }
  if (target === 'engine:explode') {
    await expect.poll(() => engineCount(page)).toBe(before.count + 1)
    const after = await observedGeometry(page)
    const source = before.geometry.entities.find((entity) => String(entity.id) === String(0xA200))
    expect(source?.type).toBe('LWPOLYLINE')
    expect(after.entities.some((entity) => String(entity.id) === String(source.id))).toBe(false)
    const preserved = before.geometry.entities.filter((entity) => entity !== source)
    expect(after.entities.filter((entity) => preserved.some((old) => String(old.id) === String(entity.id)))
      .map(({ index, ...entity }) => entity)).toEqual(preserved.map(({ index, ...entity }) => entity))
    const segments = after.entities.filter((entity) => !before.geometry.entities.some((old) => String(old.id) === String(entity.id)))
    expect(segments.map((entity) => ({ type: entity.type, layer: entity.layer, vertices: entity.vertices.map((point) => point.slice(0, 2)) }))).toEqual([
      { type: 'LINE', layer: 'Walk', vertices: [[111, 200], [222, 200]] },
      { type: 'LINE', layer: 'Walk', vertices: [[222, 200], [222, 210]] },
    ])
    return
  }
  if (target === 'engine:delete' && probe.state === 'multiple-selected'
    && typeof assertMultipleSelectionDelete === 'function') {
    await assertMultipleSelectionDelete(runtime, before, assertions)
    return
  }
  if (target.startsWith('engine:') || target === 'browser-clipboard-cut') {
    // Immediate engine operations must commit geometry, not merely leave
    // their button enabled or open an unrelated panel.
    if (target === 'engine:undo' || target === 'engine:redo') {
      await expect.poll(() => engineCount(page)).toBe(before.count + (target === 'engine:undo' ? -1 : 1))
    } else await expect.poll(() => engineCount(page)).not.toBe(before.count)
    return
  }
  throw new Error(`No effect oracle for ${probe.featureId}/${probe.state}: ${effect.kind} ${target}`)
}

export async function runProbeOracle(probe, runtime, locator, before, assertions = expect,
  oracle = () => assertEffect(probe, runtime, locator, before, assertions)) {
  try {
    await oracle()
  } catch (error) {
    if (probe.assertion.target === 'engine-refusal' && typeof runtime.walkPersonaObservers?.finish === 'function') {
      try {
        await runtime.walkPersonaObservers.finish()
      } catch {
        runtime.evidence.measurementDiagnostics ||= []
        runtime.evidence.measurementDiagnostics.push({ persona: 'red-hat', message: 'Optional persona observation failed' })
      }
    }
    throw error
  }
  if (typeof runtime.panelPerformanceCapture?.finish === 'function') await runtime.panelPerformanceCapture.finish()
  if (typeof runtime.walkPersonaObservers?.finish === 'function') await runtime.walkPersonaObservers.finish()
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
    runtime.walkPersonaObservers = typeof createWalkPersonaObservers === 'function'
      ? createWalkPersonaObservers({ probe, viewport: runtime.testInfo?.project?.name, evidence }) : null
    if (typeof observeProbeRequests === 'function' && typeof page.on === 'function') {
      runtime.cleanup.push(observeProbeRequests(page, evidence, setupStartedAt))
    }
    const initialReason = (probe.setup?.context?.openProjectId
      || typeof UI_UNREACHABLE_STATES !== 'undefined' && UI_UNREACHABLE_STATES.has(probe.state))
      && typeof unsupportedBeforeSetup === 'function'
      ? unsupportedBeforeSetup(probe, runtime.workerFacts) : null
    if (initialReason) {
      if (typeof toolAvailabilityEvidence === 'function') {
        runtime.unsupportedAvailability = toolAvailabilityEvidence(probe, runtime.workerFacts)
      }
      await unsupported(probe, runtime, initialReason)
    }
    if (typeof readWalkBuildFlags === 'function') await readWalkBuildFlags(probe, runtime)
    if (typeof authorAvailability === 'function') await authorAvailability(probe, runtime)
    // Source-extracted fake runners may omit module dependencies.
    const reason = typeof unsupportedBeforeSetup === 'function'
      ? unsupportedBeforeSetup(probe, { ...runtime.workerFacts, engineMounted: runtime.workerFacts.engineMounted,
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
    if (typeof installGeometryObserver === 'function' && (probe.locator?.keyboardAction === 'engine:repeat'
      || barNoRung(probe) || ['engine-refusal', 'engine:explode', 'browser-clipboard'].includes(probe.assertion.target)
      || (probe.state === 'multiple-selected' && ['engine:delete', 'cockpit-prompt'].includes(probe.assertion.target))
      || probe.assertion.target?.startsWith('drawing-version-'))) await page.addInitScript(installGeometryObserver)
    for (const recipe of probe.setup.steps) {
      await (runtime.runStep || test.step)(`Setup: ${recipe.kind}`, async () => {
        try {
          if (probe.setup.context?.projectSolar && typeof projectSolarStep === 'function') {
            await projectSolarStep(runtime, recipe.kind, () => setupStep(probe, runtime, recipe))
          } else await setupStep(probe, runtime, recipe)
        } catch (error) {
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
    if (typeof prepareProjectSharedShell === 'function') {
      probe = await prepareProjectSharedShell(probe, runtime)
      if (['project-shared-sidebar', 'project-shared-tools'].includes(probe.assertion.target)) evidence.effectiveEffect = probe.assertion
    }
    if (probe.featureId === 'control:chooser-open' && typeof runChooserOpen === 'function') {
      await runChooserOpen(probe, runtime, runtime.recipeAssertions || expect)
      evidence.result = { result: 'passed', featureId: probe.featureId, state: probe.state }
      return
    }
    if (runtime.solarDocument && typeof solarDocumentProbe === 'function') {
      probe = solarDocumentProbe(probe, runtime.solarDocument)
      evidence.effectiveEffect = probe.assertion
      evidence.solarDocument.oracle = probe.assertion.kind
    }
    if (probe.kind === 'tool' && typeof workerCatalog === 'function') {
      runtime.catalogFacts ||= {}
      await workerCatalog(runtime.catalogFacts, page.request, { drawingId: runtime.drawingId, version: runtime.drawingVersion || 'head' })
      if (runtime.catalogFacts.catalogError) throw runtime.catalogFacts.catalogError
      if (typeof requireSeededSolarReady === 'function') requireSeededSolarReady(probe, runtime.catalogFacts.catalog)
      if (typeof solarAvailabilityProbe === 'function') {
        probe = solarAvailabilityProbe(probe, runtime.catalogFacts.catalog)
        evidence.effectiveEffect = probe.assertion
      }
      const availabilityReason = unsupportedBeforeSetup(probe, runtime.catalogFacts)
      if (availabilityReason) {
        runtime.unsupportedAvailability = toolAvailabilityEvidence(probe, runtime.catalogFacts)
        await unsupported(probe, runtime, availabilityReason)
      }
    }
    let targetRecipe = runtime.catalogPanelName ? { ...probe.locator, panelName: runtime.catalogPanelName } : probe.locator
    if (probe.kind === 'action' && probe.assertion.kind === 'disabled_with_reason' && probe.locator?.keyboardAction !== 'engine:repeat') {
      // A changed refusal is an oracle verdict; only a missing action is a
      // pre-oracle failure. Match the stable label with any refusal suffix.
      const stableName = probe.locator.availableName || (typeof probe.locator.name === 'string'
        ? probe.locator.name.split(' (unavailable: ')[0] : null)
      if (probe.locator.unavailableName || stableName) targetRecipe = { ...targetRecipe, exact: false,
        name: probe.locator.unavailableName || new RegExp('^' + stableName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '(?: \\(unavailable: .+\\))?$') }
    }
    // Source-extracted fake runners may omit module dependencies.
    if (typeof discloseControlPanel === 'function') await discloseControlPanel(page, targetRecipe)
    if (typeof phoneRailAvailability === 'function') await phoneRailAvailability(probe, runtime)
    const phoneRecipe = runtime.testInfo.project.name === 'phone' && typeof phoneProbeLocator === 'function'
      ? await phoneProbeLocator(page, probe) : null
    let locator = probe.kind === 'surface' && probe.locator.role === 'tab' && typeof resolveSurfaceControl === 'function'
      ? (await resolveSurfaceControl(page, probe)).locator
      : control(page, phoneRecipe || (runtime.testInfo.project.name === 'phone' && probe.locator.phone ? probe.locator.phone : targetRecipe))
    let locatorDescription
    if (typeof studioDrawerProbeLocator === 'function'
      && (['drawer:jobs', 'drawer:nav'].includes(probe.featureId)
        || probe.featureId === 'drawer:result' && runtime.testInfo.project.name === 'phone'
        || probe.featureId === 'drawer:none' && probe.state === 'drawer-open' && runtime.testInfo.project.name === 'phone')) {
      locator = await studioDrawerProbeLocator(page, probe, description => { locatorDescription = description }) || locator
    }
    if (typeof railExpandLauncher === 'function' && probe.featureId === 'action:rail-expand'
      && probe.state === 'ready' && runtime.testInfo.project.name !== 'phone') {
      locator = await railExpandLauncher(probe, runtime, locator, description => { locatorDescription = description }) || locator
    }
    try {
      const observations = await collectProbeUxEvidence(probe, locator, runtime.testInfo.project.name)
      if (observations.length) Object.assign(evidence, packUxEvidence([...(evidence.ux_observations || []), ...observations]))
    } catch { /* Optional UX evidence never changes the functional verdict. */ }
    const failedDrawingGroup = runtime.failedDrawing && probe.locator.group
      ? page.getByRole('toolbar', { name: 'Drafting tools', exact: true })
        .getByRole('group', { name: groupNames[probe.locator.group], exact: true, includeHidden: true }) : null
    const failedDrawingQuickAbsent = runtime.failedDrawing && probe.locator.scope?.name === 'Quick access'
      && await locator.count() === 0
    if (runtime.failedDrawing && (runtime.ribbonAbsent || failedDrawingQuickAbsent || (failedDrawingGroup && await failedDrawingGroup.count() === 0))) {
      await test.step(probe.assertion.assertionId, async () => {
        expect(probe.assertion.kind).toBe('disabled_with_reason')
        if (runtime.ribbonAbsent) {
          await expect(page.getByRole('tablist', { name: 'Ribbon', exact: true })).toHaveCount(0)
          await expect(page.getByRole('toolbar', { name: 'Drafting tools', exact: true })).toHaveCount(0)
        } else if (failedDrawingQuickAbsent) await expect(locator).toHaveCount(0)
        else await expect(failedDrawingGroup).toHaveCount(0)
        // Check the action globally as well: an absent scope alone would
        // make every scoped locator empty, even if the action moved elsewhere.
        await expect(page.getByRole(probe.locator.role, { name: typeof absenceNamePattern === 'function' ? absenceNamePattern(probe.locator) : probe.locator.unavailableName, includeHidden: true })).toHaveCount(0)
      })
      evidence.failedDrawing.actionAvailability = 'unavailable_not_rendered'
      evidence.result = { result: 'passed', featureId: probe.featureId, state: probe.state }
      return
    }
    if (probe.kind === 'tool' && typeof recoverFailedCatalog === 'function') await recoverFailedCatalog(page, runtime)
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
    if (typeof prepareQcDevHistory === 'function') await prepareQcDevHistory(probe, runtime, locator, before)
    if (probe.featureId && probe.state && probe.assertion.target === 'engine-refusal'
      && typeof runtime.walkPersonaObservers?.observe === 'function') {
      await runtime.walkPersonaObservers.observe('red-hat', 'before', before)
    }
    const accessibilityCapture = typeof prepareAccessibilityCapture === 'function'
      ? await prepareAccessibilityCapture({ page, probe, viewport: runtime.testInfo.project.name,
        locator, evidence, cleanup: runtime.cleanup }) : null
    runtime.panelPerformanceCapture = typeof preparePanelPerformanceCapture === 'function'
      ? await preparePanelPerformanceCapture({ page, probe, viewport: runtime.testInfo.project.name,
        locator, evidence }) : null
    if (probe.kind === 'tool' && probe.sourceId === 'solar-solve-proposal' && probe.state === 'ready'
      && probe.assertion.target === 'catalog-run-decision') {
      runtime.catalogRunRequests = []
      const observe = (request) => {
        if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/run') runtime.catalogRunRequests.push(request.url())
      }
      page.on('request', observe)
      runtime.cleanup.push(async () => page.off('request', observe))
    }
    if (typeof observeDxfImportState === 'function') observeDxfImportState(probe, runtime)
    if (probe.assertion.kind !== 'disabled_with_reason' || probe.locator?.keyboardAction === 'engine:repeat') {
      await test.step(`Activate ${probe.featureId}`, () => typeof activateWithMobileTouchMeasurements === 'function'
        ? activateWithMobileTouchMeasurements({ page, locator, probe, viewport: runtime.testInfo.project.name, locatorDescription,
          addFragment: (persona, measurements) => {
            if (typeof addPersonaFragment === 'function') addPersonaFragment(evidence, { persona, measurements,
              bundleReference: `walk:${probe.featureId}[${probe.state}]@${runtime.testInfo.project.name}` })
          },
          evidence, activate: () => activate(probe, runtime, locator) })
        : activate(probe, runtime, locator))
    }
    const runOracle = () => test.step(probe.assertion.assertionId, () => {
      evidence.oracleReached = probe.assertion.assertionId
      return assertEffect(probe, runtime, locator, before)
    })
    if (typeof runProbeOracle === 'function') {
      await runProbeOracle(probe, runtime, locator, before, undefined, runOracle)
    } else {
      await runOracle()
      if (typeof runtime.panelPerformanceCapture?.finish === 'function') await runtime.panelPerformanceCapture.finish()
      if (typeof runtime.walkPersonaObservers?.finish === 'function') await runtime.walkPersonaObservers.finish()
    }
    if (typeof followAccessibilityCapture === 'function') {
      await followAccessibilityCapture({ capture: accessibilityCapture, page, evidence,
        bundleReference: `walk:${probe.featureId}[${probe.state}]@${runtime.testInfo.project.name}` })
    }
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
