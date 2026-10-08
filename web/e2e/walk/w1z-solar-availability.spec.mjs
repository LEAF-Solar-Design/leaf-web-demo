import { test, expect, runProbe } from './fixtures.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { resolveProbe } from './probes.mjs'

const entries = buildFeatureMap().entries
if (process.env.LEAF_WALK_PROOF === '1') {
  test.describe('W1z Solar availability', () => {
    for (const [name, reasonCode] of [
      ['solar-cable-export', 'solar_output_not_current'],
      ['solar-electrical-schedules', 'solar_output_not_current'],
      ['solar-solaredge-accept', 'frames_required'],
      ['solar-equipment-move', 'equipment_assignment_required'],
    ]) {
      const featureId = `tool:${name}`
      const state = 'ready'
      test(`${featureId} [${state}] @desktop`, async ({ page, stack, walkEvidence }, testInfo) => {
        test.skip(testInfo.project.name !== 'desktop', 'Solar availability proof uses the desktop rail')
        const entry = entries.find((entry) => entry.id === featureId)
        expect(entry, featureId).toBeTruthy()
        const probe = resolveProbe(entry, state)
        expect(probe.assertion.target).toBe('solar-step-editor')
        let result
        let failure
        try { result = await runProbe(probe, { page, stack, evidence: walkEvidence, testInfo }) }
        catch (error) { failure = error }
        await testInfo.attach('w1z-solar-availability-proof', { contentType: 'application/json', body: Buffer.from(JSON.stringify({
          featureId, state, recipe: probe.setup, expected: probe.assertion,
          effective: walkEvidence.effectiveEffect, catalog: walkEvidence.catalog,
          solarSeed: walkEvidence.solarSeed, setup: walkEvidence.setupCompleted,
          oracleReached: walkEvidence.oracleReached, cleanupCompleted: walkEvidence.cleanupCompleted,
          result, failure: failure ? { message: failure.message } : null,
        })) })
        if (failure) throw failure
        expect(result?.unsupported, result?.reason).not.toBe(true)
        expect(walkEvidence.effectiveEffect.kind).toBe('disabled_with_reason')
        expect(walkEvidence.effectiveEffect.reason_code).toBe(reasonCode)
        expect(walkEvidence.oracleReached).toBe(walkEvidence.effectiveEffect.assertionId)
        expect(walkEvidence.cleanupCompleted).toBe(true)
      })
    }
  })
}
