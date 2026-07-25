import { expect, test } from '@playwright/test'
import { mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { REQUEST, catProofResponse, makeCatProofState } from './catProofFixture.mjs'
import { writeProofReceipt } from './proofReceipt.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
const PROOF_DIR = join(HERE, '..', '..', 'artifacts', 'cat-operator-proof')
const UNIFIED_PROOF_DIR = join(HERE, '..', '..', 'artifacts', 'unified-surface-proof', 'wave0-route')

test('standards surface keeps the complete cat operator flow in one scene', async ({ page }) => {
  test.setTimeout(360_000)
  const walkStartedAt = Date.now()
  const mark = (label) => console.log(`[standards-walk] ${label}: ${Date.now() - walkStartedAt}ms`)
  mkdirSync(PROOF_DIR, { recursive: true })
  const proofState = makeCatProofState()
  const apiEndpoints = []
  let catalogRunHeaders = null
  let authorStageBody = null
  let authorRegisterBody = null
  let grantLinkBody = null

  await page.addInitScript(() => localStorage.setItem('leaf.org_id', 'cat-proof-org'))

  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    apiEndpoints.push(`${request.method()} ${url.pathname}`)
    const body = request.postData() ? request.postDataJSON() : {}
    if (url.pathname === '/api/run' && body.tool === 'count-panels') catalogRunHeaders = await request.allHeaders()
    if (url.pathname === '/api/author/stage') authorStageBody = body
    if (url.pathname === '/api/author/register') authorRegisterBody = body
    if (url.pathname === '/api/tenant/claude-grant' && request.method() === 'POST') grantLinkBody = body
    const result = catProofResponse({ method: request.method(), path: url.pathname, body, query: Object.fromEntries(url.searchParams) }, proofState)
    await route.fulfill({
      status: result.status,
      contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
    })
  })

  await page.goto('/try')
  await expect(page).toHaveURL(/\/try$/)
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })
  await page.waitForTimeout(2_600)

  await page.getByRole('tab', { name: 'View' }).click()
  const viewerCanvas = page.locator('.viewer-canvas canvas')
  await expect(viewerCanvas).toHaveCount(1)
  const viewerBox = await page.locator('.stage-viewer').boundingBox()
  const visibleDrawing = await page.screenshot({ clip: viewerBox })
  await page.getByRole('button', { name: /PANELS/ }).click()
  const hiddenDrawing = await page.screenshot({ clip: viewerBox })
  expect(hiddenDrawing.equals(visibleDrawing)).toBe(false)
  await page.getByRole('button', { name: /PANELS/ }).click()
  await expect(page.getByRole('button', { name: /PANELS/ })).not.toHaveClass(/off/)
  await page.waitForTimeout(100)
  await page.locator('.viewer-canvas').evaluate((mount) => {
    const point = mount.__cadviewer.project(32.5, 26.5)
    const canvas = mount.querySelector('canvas')
    canvas.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, button: 0, clientX: point.x, clientY: point.y }))
    canvas.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, button: 0, clientX: point.x, clientY: point.y }))
  })
  await expect(page.getByText('P1696')).toBeVisible()
  await expect(page.getByText('Polyline')).toBeVisible()
  await page.getByRole('button', { name: 'Deselect' }).click()
  await expect(page.getByText('Click an entity to select it')).toBeVisible()
  await page.getByRole('tab', { name: 'Execution' }).click()
  await page.getByRole('button', { name: 'Take edit lock' }).click()
  await expect(page.getByText('You hold the edit lock')).toBeVisible()
  await page.getByRole('button', { name: 'Release' }).click()
  await expect(page.getByRole('button', { name: 'Take edit lock' })).toBeVisible()
  mark('viewer and checkout')

  await page.getByRole('button', { name: /Project cat-panels/ }).click()
  await page.getByRole('menuitem', { name: /Cat Roof/ }).click()
  await expect(page.getByText('Cat Roof').first()).toBeVisible()
  await expect(page.getByText('1 drawing version')).toBeVisible()
  await expect(page.getByText('0 built tools')).toBeVisible()

  await page.getByRole('tab', { name: /Catalog/ }).click()
  const countTool = page.getByRole('button', { name: /count-panels/i })
  await expect(countTool).toBeVisible()
  await countTool.click()
  await page.getByRole('button', { name: 'Review & run' }).click()
  await expect(page.getByRole('button', { name: 'Run count-panels' })).toBeVisible()
  await page.getByRole('button', { name: 'Run count-panels' }).click()
  await expect(page.getByText('count-panels').first()).toBeVisible({ timeout: 12_000 })
  await page.getByRole('tab', { name: 'Execution' }).click()
  await expect(page.getByTestId('catalog-run-result')).toContainText('count-panels completed')
  expect(catalogRunHeaders?.['x-org-id']).toBe('cat-proof-org')
  expect(catalogRunHeaders?.['x-project-id']).toBe('cat-project')
  await page.screenshot({ path: join(PROOF_DIR, 'standards-00-catalog-run.png'), fullPage: true })
  await page.getByRole('tab', { name: 'Project' }).click()
  const workspaceSummary = page.locator('.workspace-summary')
  await expect(workspaceSummary).toContainText('1')
  await expect(workspaceSummary).toContainText('job')
  await expect(workspaceSummary).toContainText('count-panels')
  await expect(workspaceSummary).toContainText('succeeded')
  mark('catalog and project')

  await page.getByRole('tab', { name: 'Author' }).click()
  await page.getByLabel('What should the tool do?').fill('count panels within 24in of the roof edge')
  await page.getByRole('button', { name: 'Generate tool' }).click()
  await expect(page.getByText('count-panels-near-edge')).toBeVisible()
  await expect(page.getByText(/Staged and awaiting approval/)).toBeVisible()
  mark('author staged')
  expect(authorStageBody).toMatchObject({ description: 'count panels within 24in of the roof edge', mode: 'build' })
  expect(authorStageBody?.idempotency_key).toBeTruthy()

  await page.getByRole('button', { name: 'Publish tool' }).click()
  await expect(page.getByText(/independent reviewer has not approved/)).toBeVisible()
  mark('author review gate')
  expect(authorRegisterBody).toBeNull()
  proofState.independentApproved = true
  await page.getByRole('button', { name: 'Publish tool' }).click()
  await expect(page.getByRole('button', { name: 'Run it now' })).toBeVisible()
  await expect(page.getByText('Tool published, count-panels-near-edge')).toBeVisible()
  expect(authorRegisterBody).toMatchObject({
    change_set_id: '11111111-1111-4111-8111-111111111111',
    confirmation_id: 'publish-confirmation-0001',
    staged_commit: 'c'.repeat(40),
    catalog_digest: 'd'.repeat(64),
    workspace_contract_digest: 'e'.repeat(64),
  })
  expect(authorRegisterBody?.idempotency_key).toBeTruthy()
  expect(authorRegisterBody.idempotency_key).not.toBe(authorStageBody.idempotency_key)
  expect(Object.keys(authorRegisterBody).sort()).toEqual([
    'catalog_digest', 'change_set_id', 'confirmation_id', 'idempotency_key',
    'platform_release', 'staged_commit', 'workspace_contract_digest',
  ].sort())

  await page.getByRole('button', { name: 'Run it now' }).click()
  await expect(page.getByRole('button', { name: 'Run count-panels-near-edge' })).toBeVisible()
  expect(proofState.authorJob).toBe(false)
  await page.getByRole('button', { name: 'Run count-panels-near-edge' }).click()
  await page.getByRole('tab', { name: 'Execution' }).click()
  await expect(page.getByTestId('catalog-run-result')).toContainText('count-panels-near-edge completed')
  await page.screenshot({ path: join(PROOF_DIR, 'standards-00b-author-publish-run.png'), fullPage: true })
  mark('author published and run')
  await page.getByRole('tab', { name: 'Operator' }).click()

  const command = page.getByRole('textbox', { name: 'Command bar' })
  await expect(command).toHaveValue(REQUEST)
  await page.getByRole('button', { name: 'Run', exact: true }).click()

  const surface = page.getByTestId('operator-surface')
  await expect(surface).toContainText('Rearrange the existing panels')
  await expect(surface.getByRole('button', { name: 'Approve' })).toBeVisible({ timeout: 12_000 })
  await page.screenshot({ path: join(PROOF_DIR, 'standards-01-approval.png'), fullPage: true })
  await expect(page).toHaveURL(/\/try$/)

  await surface.getByRole('button', { name: 'Approve' }).click()
  await expect(page.getByTestId('operator-phase')).toContainText('Cat version ready', { timeout: 15_000 })
  await expect(page.getByTestId('version-head')).toContainText('Version 2')
  await expect(page.getByText('Panels preserved')).toBeVisible()
  await expect(page.getByText('3,328')).toBeVisible()
  await page.screenshot({ path: join(PROOF_DIR, 'standards-02-cat-complete.png'), fullPage: true })
  mark('cat complete')
  await expect(page).toHaveURL(/\/try$/)

  await page.getByRole('tab', { name: /Versions/ }).click()
  const versionHistory = page.getByRole('region', { name: 'Version history' })
  await expect(versionHistory.getByRole('button', { name: /v2/ })).toBeVisible()
  await expect(versionHistory.getByRole('button', { name: /v1/ })).toBeVisible()
  await versionHistory.getByRole('button', { name: /v1/ }).click()
  await expect(page.getByText('Viewing v1 read-only')).toBeVisible()
  await expect(page.getByTestId('version-head')).toContainText('Version 2')
  await page.getByRole('button', { name: 'Back to head' }).click()

  await page.getByRole('tab', { name: 'Trust' }).click()
  await expect(page.getByText('Backend').locator('..')).toContainText('healthy')
  await expect(page.locator('.tc-trust-row').filter({ hasText: 'Claude account' })).toContainText('linked · oauth')
  await expect(page.getByText('Runs today').locator('..')).toContainText('1')
  await expect(page.getByText('Spend remaining').locator('..')).toContainText('$10.00')
  await expect(page.getByText('Entitlements')).toBeVisible()
  await page.getByRole('button', { name: /Claude account linked/ }).click()
  await page.getByRole('button', { name: 'Unlink', exact: true }).click()
  await page.getByRole('button', { name: 'Unlink', exact: true }).click()
  await expect(page.getByRole('button', { name: /Claude account not linked/ })).toBeVisible()
  const grantToken = page.getByLabel('Claude token')
  await grantToken.fill('sk-ant-oat01-fixture-only')
  await page.getByRole('button', { name: 'Link Claude account' }).click()
  await expect(page.getByRole('button', { name: /Claude account linked/ })).toBeVisible()
  await expect(grantToken).toHaveCount(0)
  expect(grantLinkBody).toMatchObject({ kind: 'oauth' })
  expect(grantLinkBody?.token).toBe('sk-ant-oat01-fixture-only')
  await page.screenshot({ path: join(PROOF_DIR, 'standards-02b-versions-trust.png'), fullPage: true })
  mark('versions and trust')

  await page.getByRole('tab', { name: 'Execution' }).click()

  await page.getByRole('button', { name: 'Undo' }).click()
  await expect(page.getByTestId('operator-phase')).toContainText('Original restored')
  await expect(page.getByTestId('version-head')).toContainText('Version 1')
  await page.screenshot({ path: join(PROOF_DIR, 'standards-03-undo.png'), fullPage: true })

  await page.getByRole('button', { name: 'Redo' }).click()
  await expect(page.getByTestId('operator-phase')).toContainText('Cat version ready')
  await expect(page.getByTestId('version-head')).toContainText('Version 2')
  await page.screenshot({ path: join(PROOF_DIR, 'standards-04-redo.png'), fullPage: true })
  await expect(page).toHaveURL(/\/try$/)

  writeProofReceipt(join(UNIFIED_PROOF_DIR, 'receipt.json'), {
    capability_ids: ['ID-02', 'ID-03', 'CA-01', 'CA-02', 'CV-01', 'CV-02', 'RN-01', 'AU-01', 'JB-01', 'JB-02', 'VW-01', 'VW-02', 'VR-01', 'VR-02', 'VR-03', 'EN-01', 'HL-01', 'AC-01', 'DS-01'],
    evidence_tier: 'contract',
    route: '/try',
    runtime: 'Vite with deterministic API transport',
    api_endpoints: apiEndpoints,
    assertions: [
      'the request, approval, job, drawing result, version, undo, and redo stay on /try',
      'the production viewer layer control changes the canvas and entity picking drives the shared selection readout',
      'checkout take and release refresh the authoritative version manifest before write controls update',
      'the registered catalog stages, confirms, and completes count-panels through the shared job controller',
      'the project resolver hydrates Cat Roof and binds its canonical drawing version into the run intent',
      'authoring stages a non-runnable tool, preserves it through pending independent review, publishes the exact receipt, refreshes the catalog, and routes its first run through confirmation',
      'the operator scene remains mounted through the complete flow',
      'version history previews version 1 without moving the version 2 head',
      'trust shows backend health, linked account kind, usage cap, and entitlements',
      'the Claude grant surface unlinks with confirmation, links a write-only subscription token, and clears the field',
      'undo restores version 1 and redo restores version 2',
    ],
    artifacts: [
      '../../cat-operator-proof/standards-00-catalog-run.png',
      '../../cat-operator-proof/standards-00b-author-publish-run.png',
      '../../cat-operator-proof/standards-01-approval.png',
      '../../cat-operator-proof/standards-02-cat-complete.png',
      '../../cat-operator-proof/standards-02b-versions-trust.png',
      '../../cat-operator-proof/standards-03-undo.png',
      '../../cat-operator-proof/standards-04-redo.png',
      '../../cat-operator-proof/test-results/cat-standards-surface-stan-9d9b1--operator-flow-in-one-scene/video.webm',
    ],
    result: { verdict: 'pass', final_route: '/try', final_version: 2 },
    limitations: [
      'This proves registered catalog and cat operator contracts with deterministic data.',
      'It does not prove a real local backend, Claude, or APS.',
    ],
  })
})
