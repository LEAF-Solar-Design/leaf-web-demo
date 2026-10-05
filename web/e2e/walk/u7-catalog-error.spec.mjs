import { test, expect, setupStep } from './fixtures.mjs'

if (process.env.LEAF_WALK_PROOF === '1') {
  test('u7 failed Solar catalog shows its reason and Retry restores tools @desktop',
    async ({ page, stack, walkEvidence }, testInfo) => {
      const probe = { setup: { steps: [] } }
      const runtime = { page, stack, evidence: walkEvidence, testInfo, cleanup: [] }
      const pattern = '**/api/capabilities*'
      let failures = 0
      const failCatalog = async (route) => {
        failures += 1
        await route.abort('failed')
      }
      await page.route(pattern, failCatalog)
      await setupStep(probe, runtime, { kind: 'open-private-drawing', surface: 'solar' })
      await setupStep(probe, runtime, { kind: 'ribbon-tab', name: 'Solar' })
      const shell = page.locator('.app[data-studio-shell="cockpit"][data-surface="solar"]')
      const ribbon = page.getByRole('toolbar', { name: 'Drafting tools', exact: true })
      const status = ribbon.getByRole('status')
      const more = ribbon.getByRole('button', { name: 'More panels', exact: true })
      if (await more.isVisible() && await more.getAttribute('aria-expanded') === 'true') await more.click()
      await expect(shell).toBeVisible()
      await expect(shell.locator('> aside.nav')).toBeHidden()
      await expect(ribbon).toHaveAttribute('data-tab', 'solar')
      await expect(status).toBeVisible()
      await expect(status.locator('span')).toHaveText(/^Couldn't load tools: .+/)
      await expect(status.getByRole('button', { name: 'Retry', exact: true })).toBeVisible()
      expect(failures).toBeGreaterThan(0)
      await page.screenshot({ path: testInfo.outputPath('u7-catalog-failed.png') })

      await page.unroute(pattern, failCatalog)
      const failuresBeforeRetry = failures
      const retryResponse = page.waitForResponse(response =>
        new URL(response.url()).pathname === '/api/capabilities' && response.ok())
      await status.getByRole('button', { name: 'Retry', exact: true }).click()
      await retryResponse
      await expect(status).toHaveCount(0)
      if (await more.isVisible() && await more.getAttribute('aria-expanded') === 'false') await more.click()
      await expect(ribbon.locator('button[data-tool="solar-unit-sync"]')).toBeVisible()
      expect(failures).toBe(failuresBeforeRetry)
      await page.screenshot({ path: testInfo.outputPath('u7-catalog-recovered.png') })
    })
}
