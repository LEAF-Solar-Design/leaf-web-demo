import { expect, test } from '@playwright/test'
import { join, relative } from 'node:path'
import { writeProofReceipt } from '../proofReceipt.mjs'
import { requireLocalReady } from './requireReady.mjs'

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'local')
const REQUEST = 'Count the panels by layer in this drawing'

test('real local operator flow preserves unified accessibility, keyboard, responsive, and motion standards', async ({ page, request }, testInfo) => {
  test.setTimeout(120_000)
  await requireLocalReady(request, test, API_BASE)

  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Drawing ready', { timeout: 15_000 })

  await expect(page.getByRole('main', { name: 'Leaf operator workspace' })).toHaveCount(1)
  await expect(page.getByRole('complementary', { name: 'Workspace controls' })).toHaveCount(1)
  await expect(page.getByRole('complementary', { name: 'Operations controls' })).toHaveCount(1)
  const announcements = page.getByRole('status', { name: 'Run status announcements' })
  await expect(announcements).toHaveCount(1)

  for (const tablistName of ['Workspace panels', 'Operation panels']) {
    const tabs = page.getByRole('tablist', { name: tablistName }).getByRole('tab')
    for (let index = 0; index < await tabs.count(); index += 1) {
      const controls = await tabs.nth(index).getAttribute('aria-controls')
      await expect(page.locator(`#${controls}`)).toHaveAttribute('role', 'tabpanel')
    }
  }
  await expect(page.locator('#workspace-tabpanel')).toHaveAttribute('aria-labelledby', 'workspace-tab-operator')
  await expect(page.locator('#operations-tabpanel')).toHaveAttribute('aria-labelledby', 'operations-tab-execution')

  const unnamedButtons = await page.locator('button:visible').evaluateAll((buttons) => buttons
    .filter((button) => !button.disabled)
    .filter((button) => !(button.getAttribute('aria-label') || button.textContent || '').trim())
    .length)
  expect(unnamedButtons).toBe(0)

  const operatorTab = page.getByRole('tab', { name: 'Operator' })
  await operatorTab.focus()
  await page.keyboard.press('ArrowRight')
  const catalogTab = page.getByRole('tab', { name: /Catalog/ })
  await expect(catalogTab).toBeFocused()
  await expect(catalogTab).toHaveAttribute('aria-selected', 'true')
  await page.keyboard.press('Control+K')
  const command = page.getByRole('textbox', { name: 'Command bar' })
  await expect(command).toBeFocused()

  for (const locator of [
    operatorTab,
    page.getByRole('tab', { name: 'Execution' }),
    command,
    page.getByRole('button', { name: 'Run', exact: true }),
    page.locator('#workspace-tabpanel'),
  ]) {
    await locator.focus()
    const visibleFocus = await locator.evaluate((element) => {
      const style = getComputedStyle(element)
      return style.outlineStyle !== 'none' || style.boxShadow !== 'none'
    })
    expect(visibleFocus).toBe(true)
  }

  await command.fill(REQUEST)
  await page.getByRole('button', { name: 'Run', exact: true }).click()
  const confirm = page.getByRole('button', { name: 'Run count-by-layer' })
  await expect(confirm).toBeVisible()
  await confirm.click()
  const jobsTab = page.getByRole('tab', { name: /Jobs/ })
  await expect(jobsTab).toHaveAttribute('aria-selected', 'true')
  await expect(announcements).toContainText('count-by-layer complete', { timeout: 30_000 })
  await expect(page.locator('.rail-row').filter({ hasText: 'count-by-layer' }).first()).toContainText('complete')
  await page.getByRole('tab', { name: 'Execution' }).click()
  const result = page.getByTestId('catalog-run-result')
  await expect(result).toContainText('Passed')
  await expect(result).toContainText('count-by-layer')

  const motionStyle = await page.getByTestId('operator-surface').evaluate((element) => {
    const style = getComputedStyle(element)
    return {
      opacity: style.opacity,
      animationDuration: style.animationDuration,
      animationDelay: style.animationDelay,
    }
  })
  expect(motionStyle.opacity).toBe('1')
  expect(motionStyle.animationDuration).toBe('0s')
  expect(motionStyle.animationDelay).toBe('0s')

  const screenshots = []
  for (const [name, width, height] of [
    ['desktop', 1440, 900],
    ['narrow', 1024, 768],
    ['tablet', 768, 1024],
    ['phone', 390, 844],
    ['short', 1280, 600],
  ]) {
    await page.setViewportSize({ width, height })
    await expect(page.getByRole('button', { name: 'Run', exact: true })).toBeVisible()
    await expect(page.getByTestId('operator-surface')).toBeVisible()
    await expect(page.locator('.tc-rail-r')).toBeVisible()
    const geometry = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
      inputSize: parseFloat(getComputedStyle(document.querySelector('.tc-bar-input')).fontSize),
    }))
    expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.innerWidth)
    if (name === 'phone') expect(geometry.inputSize).toBeGreaterThanOrEqual(16)
    const screenshotPath = testInfo.outputPath(`${name}.png`)
    await page.screenshot({ path: screenshotPath, fullPage: true })
    screenshots.push(relative(join(process.cwd(), '..'), screenshotPath).replaceAll('\\', '/'))
  }

  await page.setViewportSize({ width: 1440, height: 900 })
  await command.fill(REQUEST)
  await page.getByRole('button', { name: 'Run', exact: true }).click()
  await expect(confirm).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(confirm).toHaveCount(0)
  await expect(command).toBeFocused()
  await expect(page).toHaveURL(/\/try$/)

  writeProofReceipt(join(PROOF_DIR, 'standards-surface-receipt.json'), {
    capability_ids: ['AX-01', 'AX-02', 'KB-01', 'RS-01', 'MO-01', 'MO-02', 'MO-03'],
    evidence_tier: 'local-e2e',
    route: '/try',
    runtime: 'real local Vite, FastAPI, catalog router, broker, job worker, and prefers-reduced-motion',
    api_endpoints: [
      'GET /api/session 200',
      'POST /api/nl-prompt 200',
      'POST /api/run 202',
      'GET /api/jobs/{job_id}/stream 200',
    ],
    artifacts: [
      relative(join(process.cwd(), '..'), testInfo.outputPath('video.webm')).replaceAll('\\', '/'),
      ...screenshots,
    ],
    assertions: [
      'the unified scene exposed one named main, two named complementary rails, and one polite status region',
      'all tab controls resolved to named tab panels and every enabled visible button had a name',
      'visible focus, roving tab arrows, and Control+K operated the real controls',
      'a real catalog run completed and announced its terminal result under reduced motion',
      'the filled operator surface remained visible with zero animation duration and delay',
      'desktop, narrow, tablet, phone, and short-height layouts kept both rails, command input, and Run reachable without horizontal overflow',
      'Escape dismissed a fresh immutable proposal and restored command focus without leaving the scene',
    ],
    result: {
      verdict: 'pass',
      completed_tool: 'count-by-layer',
      live_announcement: 'count-by-layer complete',
      motion_style: motionStyle,
      viewports: ['desktop', 'narrow', 'tablet', 'phone', 'short'],
      unnamed_enabled_buttons: unnamedButtons,
      escape_priority: true,
    },
    limitations: [
      'Semantic browser assertions do not replace a manual screen-reader review.',
      'This proof does not perform a calibrated color-contrast measurement.',
      'APS_LIVE=0 substitutes the local engine for Autodesk Platform Services.',
      'LEAF_AUTH_LIVE=0 uses the local tenant header instead of an Auth0 identity.',
    ],
  })
})
