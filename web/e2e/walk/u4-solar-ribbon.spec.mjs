import { test, expect, setupStep } from './fixtures.mjs'

test('u4-solar-ribbon solar-correct-string is disabled with its reason on CAD Manage @desktop',
  async ({ page, stack, walkEvidence }, testInfo) => {
    const probe = { setup: { steps: [] } }
    const runtime = { page, stack, evidence: walkEvidence, testInfo, cleanup: [] }
    // Use the walk's real upload and session recipe, with no mocked catalog.
    await setupStep(probe, runtime, { kind: 'open-private-drawing', surface: 'cad' })
    await expect(page.locator('.app[data-studio-shell="cockpit"][data-surface="cad"]')).toBeVisible()
    await setupStep(probe, runtime, { kind: 'ribbon-tab', name: 'Manage' })
    await expect(page.getByRole('tablist', { name: 'Ribbon', exact: true })
      .getByRole('tab', { name: 'Manage', exact: true })).toHaveAttribute('aria-selected', 'true')

    const button = page.getByRole('toolbar', { name: 'Drafting tools', exact: true })
      .locator('button[data-tool="solar-correct-string"]')
    const reason = 'Open a drawing to use this solar tool'
    await expect(button).toBeVisible()
    await expect(button).toBeDisabled()
    await expect(button).toHaveAttribute('title', reason)
    await expect(button).toHaveAccessibleName(`solar-correct-string (unavailable: ${reason})`)
    await testInfo.attach('u4-solar-ribbon', {
      body: Buffer.from(JSON.stringify({ drawingId: runtime.drawingId, surface: 'cad', tab: 'Manage',
        tool: 'solar-correct-string', disabled: await button.isDisabled(), reason: await button.getAttribute('title') })),
      contentType: 'application/json',
    })
  })
