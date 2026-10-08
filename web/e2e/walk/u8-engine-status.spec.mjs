import { test, expect, setupStep, exposedCalibrationPoints } from './fixtures.mjs'

if (process.env.LEAF_WALK_PROOF === '1') {
  test('u8 engine confirmations and Repeat refusals are visible with import closed @desktop',
    async ({ page, stack, walkEvidence }, testInfo) => {
      const probe = { setup: { steps: [] } }
      const runtime = { page, stack, evidence: walkEvidence, testInfo, cleanup: [], workerFacts: {} }
      const workbench = page.getByTestId('cad-edit-workbench')
      const status = page.getByTestId('cockpit-engine-status')
      const shell = page.locator('.app[data-studio-shell="cockpit"][data-surface="cad"]')
      const requireImportClosed = async () => {
        await expect(shell).toBeVisible()
        await expect(page.locator('.workspace-card[data-import-open]')).toHaveCount(0)
        await expect(workbench).toBeHidden()
        await expect(status).toHaveAttribute('role', 'status')
        await expect(status).toHaveAttribute('aria-live', 'polite')
        await expect(status).toBeVisible()
      }

      await setupStep(probe, runtime, { kind: 'open-private-drawing', surface: 'cad' })
      await setupStep(probe, runtime, { kind: 'engine-ready' })
      await setupStep(probe, runtime, { kind: 'ribbon-tab', name: 'Draw' })
      const before = Number(await page.getByTestId('cad-edit-entity-count').innerText())
      await page.getByRole('toolbar', { name: 'Drafting tools', exact: true })
        .locator('button[data-tool="draw:createLine"]').click()
      await expect(page.getByTestId('cockpit-prompt')).toHaveAccessibleName('LINE command')
      await page.getByLabel('ribbon x', { exact: true }).fill('0,0')
      const end = page.getByLabel('ribbon x2', { exact: true })
      await end.fill('10,0')
      await end.press('Enter')
      await expect(page.getByTestId('cad-edit-entity-count')).toHaveText(String(before + 1))
      await expect(status).toHaveText(/^createLine applied: entity .+ drawn\./)
      await page.keyboard.press('Escape')
      await requireImportClosed()
      await page.screenshot({ path: testInfo.outputPath('u8-line-applied.png') })

      // A new private drawing gets a new provider/session with no remembered command.
      await setupStep(probe, runtime, { kind: 'open-private-drawing', surface: 'cad' })
      await setupStep(probe, runtime, { kind: 'engine-ready' })
      const drawing = page.getByRole('region', { name: 'Drawing', exact: true })
      const canvas = drawing.locator('canvas:visible')
      const points = await canvas.evaluate(exposedCalibrationPoints)
      expect(points).not.toBeNull()
      await page.mouse.click(points[0].x, points[0].y)
      await page.keyboard.press('Enter')
      await expect(status).toHaveText('No drawing command to repeat yet.')
      await requireImportClosed()
      await page.screenshot({ path: testInfo.outputPath('u8-repeat-refused.png') })
    })
}
