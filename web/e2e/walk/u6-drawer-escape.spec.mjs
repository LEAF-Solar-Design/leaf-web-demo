import { test, expect, runProbe, setupStep } from './fixtures.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { resolveProbe } from './probes.mjs'

const shell = '.app[data-studio-shell="cockpit"][data-surface="cad"]'

if (process.env.LEAF_WALK_PROOF === '1') {
  test('u6 drawer:none [drawer-open] Escape hides the Catalog drawer @phone',
    async ({ page, stack, walkEvidence }, testInfo) => {
      const entry = buildFeatureMap().entries.find(entry => entry.id === 'drawer:none')
      expect(entry).toBeTruthy()
      const probe = resolveProbe(entry, 'drawer-open')
      const result = await runProbe(probe, { page, stack, evidence: walkEvidence, testInfo })
      expect(result?.unsupported, result?.reason).not.toBe(true)
      expect(walkEvidence.setupCompleted).toBeTruthy()
      expect(walkEvidence.oracleReached).toBe(probe.assertion.assertionId)
      expect(walkEvidence.cleanupCompleted).toBe(true)
      await expect(page.getByRole('group', { name: 'Workspace panels', exact: true })
        .getByRole('button', { name: 'Catalog', exact: true })).toHaveAttribute('aria-expanded', 'false')
      await expect(page.locator(`${shell} > aside.nav`)).toBeHidden()
      await expect(page.locator(shell)).toHaveAttribute('data-drawer', 'none')
      await testInfo.attach('u6-phone-escape', {
        contentType: 'application/json', body: Buffer.from(JSON.stringify({
          featureId: entry.id, state: 'drawer-open', assertionId: probe.assertion.assertionId,
          setup: walkEvidence.setupCompleted, oracleReached: walkEvidence.oracleReached,
          cleanupCompleted: walkEvidence.cleanupCompleted, expanded: false, contentHidden: true,
        })),
      })
    })

  test('u6 Escape preserves the expanded desktop Catalog rail @desktop',
    async ({ page, stack, walkEvidence }, testInfo) => {
      const probe = { setup: { steps: [] } }
      const runtime = { page, stack, evidence: walkEvidence, testInfo, cleanup: [] }
      await setupStep(probe, runtime, { kind: 'open-private-drawing', surface: 'cad' })
      await setupStep(probe, runtime, { kind: 'tool-rail-state', open: true })
      const rail = page.locator(`${shell} > aside.nav`)
      const collapse = page.getByRole('button', { name: 'Collapse the tool rail to a spine', exact: true })
      await expect(rail).toBeVisible()
      await expect(collapse).toBeVisible()
      await expect(page.locator(shell)).toHaveAttribute('data-drawer', 'nav')
      // origin/main's global ladder reads only the legacy Details drawer;
      // the expanded docked Catalog rail does not consume or close on Escape.
      await page.keyboard.press('Escape')
      await expect(rail).toBeVisible()
      await expect(collapse).toBeVisible()
      await expect(page.locator(shell)).toHaveAttribute('data-drawer', 'nav')
      await testInfo.attach('u6-desktop-escape', {
        contentType: 'application/json', body: Buffer.from(JSON.stringify({
          drawingId: runtime.drawingId, drawer: 'nav', contentVisible: true, collapseVisible: true,
        })),
      })
    })
}
