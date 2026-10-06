import { test, expect, runProbe } from './fixtures.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { requireControlCensusBatch, resolveProbe } from './probes.mjs'
import { censusControls } from './controlCensus.mjs'
import { censusFailure } from '../../walk/controlInventory.mjs'

const entries = buildFeatureMap().entries
if (process.env.LEAF_WALK_PROOF === '1') {
  test.describe('W1z C3 Object snap modes census', () => {
    const entry = entries.find((entry) => entry.id === 'control:object-snap-modes')
    for (const state of entry.states) {
      test(`control:object-snap-modes [${state}] C3 @desktop`, async ({ page, stack, walkEvidence }, testInfo) => {
        const probe = resolveProbe(entry, state)
        let result
        let failure
        try { result = await runProbe(probe, { page, stack, evidence: walkEvidence, testInfo }) }
        catch (error) { failure = error }
        await testInfo.attach('w1z-census-snap-proof', { contentType: 'application/json', body: Buffer.from(JSON.stringify({
          featureId: entry.id, state, recipe: probe.setup, expected: probe.assertion,
          setup: walkEvidence.setupCompleted, oracleReached: walkEvidence.oracleReached,
          cleanupCompleted: walkEvidence.cleanupCompleted, disclosure: walkEvidence.censusDisclosure,
          result, failure: failure ? { message: failure.message } : null,
        })) })
        if (failure) throw failure
        expect(result?.unsupported, result?.reason).not.toBe(true)
        expect(walkEvidence.oracleReached).toBe(probe.assertion.assertionId)
        expect(walkEvidence.cleanupCompleted).toBe(true)
        expect(walkEvidence.censusDisclosure.cleaned).toBe(true)
        await expect(page.getByRole('toolbar', { name: 'Drafting settings', exact: true })
          .getByRole('button', { name: 'Object snap modes', exact: true })).toHaveAttribute('aria-expanded', 'false')
        await expect(page.locator('.cockpit-status-toggles .object-snap-menu')).toHaveCount(0)
      })
    }

    test('control-census:studio [ready] C3 @desktop', async ({ page, stack, walkEvidence }, testInfo) => {
      const probe = resolveProbe(entries.find((entry) => entry.id === 'tab:drafting:draw'), 'ready')
      const result = await runProbe(probe, { page, stack, evidence: walkEvidence, testInfo })
      expect(result?.unsupported, result?.reason).not.toBe(true)
      await expect(page.getByRole('combobox', { name: 'Command bar', exact: true })).toBeVisible()
      await expect(page.getByRole('tablist', { name: 'Ribbon', exact: true })).toBeVisible()
      await expect(page.getByRole('button', { name: 'Claude accounts not linked', exact: true })).toHaveAttribute('aria-expanded', 'false')
      await expect(page.getByRole('button', { name: /^Linked services [0-9]+ linked$/ })).toHaveAttribute('aria-expanded', 'false')
      const census = await censusControls(page, buildFeatureMap(), { state: 'ready', viewport: 'desktop' })
      walkEvidence.censuses = [census]
      expect(census.total).toBeGreaterThan(0)
      expect(requireControlCensusBatch(census, { state: 'ready', viewport: 'desktop' })).toBe(true)
      expect(census.resolved.some((row) => row.feature_id === entry.id
        && row.scope === 'toolbar:"Drafting settings"')).toBe(true)
      const assertionId = 'control-census:studio/ready/renders'
      walkEvidence.assertionId = assertionId
      walkEvidence.expectedEffect = { kind: 'renders', target: 'mapped-control-inventory', assertionId }
      await test.step(assertionId, async () => {
        expect(census.ok, censusFailure(census)).toBe(true)
        walkEvidence.oracleReached = assertionId
      })
      walkEvidence.result = { result: 'passed', featureId: 'control-census:studio', state: 'ready' }
      await testInfo.attach('w1z-census-snap-inventory', { contentType: 'application/json',
        body: Buffer.from(JSON.stringify({ census, oracleReached: walkEvidence.oracleReached })) })
    })
  })
}
