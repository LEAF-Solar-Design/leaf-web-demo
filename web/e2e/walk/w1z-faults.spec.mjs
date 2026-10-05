import { test, expect, runProbe } from './fixtures.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { resolveProbe } from './probes.mjs'

const entries = buildFeatureMap().entries
if (process.env.LEAF_WALK_PROOF === '1') {
  test.describe('W1z C1 transport faults', () => {
    for (const [featureId, state] of [
      ['action:history', 'version-changing'],
      ['action:undo', 'version-changing'],
      ['action:redo', 'version-changing'],
      ['action:undo', 'mutations-blocked'],
      ['action:redo', 'mutations-blocked'],
      ['action:bar-retry', 'retry-history'],
    ]) {
      test(`${featureId} [${state}] C1 @desktop`, async ({ page, stack, walkEvidence }, testInfo) => {
        const entry = entries.find((entry) => entry.id === featureId)
        expect(entry, featureId).toBeTruthy()
        const probe = resolveProbe(entry, state)
        let result
        let failure
        try { result = await runProbe(probe, { page, stack, evidence: walkEvidence, testInfo }) }
        catch (error) { failure = error }
        await testInfo.attach('w1z-faults-proof', { contentType: 'application/json', body: Buffer.from(JSON.stringify({
          featureId, state, recipe: probe.setup, expected: probe.assertion,
          setup: walkEvidence.setupCompleted, oracleReached: walkEvidence.oracleReached,
          cleanupCompleted: walkEvidence.cleanupCompleted, versionFault: walkEvidence.versionFault,
          historyFault: walkEvidence.historyFault, escapeProject: walkEvidence.escapeProject,
          result, failure: failure ? { message: failure.message } : null,
        })) })
        if (failure) throw failure
        expect(result?.unsupported, result?.reason).not.toBe(true)
        expect(walkEvidence.oracleReached).toBe(probe.assertion.assertionId)
        expect(walkEvidence.cleanupCompleted).toBe(true)
        if (walkEvidence.versionFault) expect(walkEvidence.versionFault.recovered.head).toBe(walkEvidence.versionFault.expected)
        if (walkEvidence.historyFault) expect(walkEvidence.historyFault.recovery).toHaveLength(1)
        if (walkEvidence.escapeProject) expect(walkEvidence.escapeProject.closed).toBe(true)
      })
    }
  })
}
