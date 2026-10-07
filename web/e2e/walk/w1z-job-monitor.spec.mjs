import { test, expect, runProbe } from './fixtures.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { resolveProbe } from './probes.mjs'

const entries = buildFeatureMap().entries
if (process.env.LEAF_WALK_PROOF === '1') {
  test.describe('W1z J1 Job monitor URL', () => {
    const entry = entries.find((entry) => entry.id === 'control:job-monitor-expand')
    for (const state of entry.states) {
      test(`control:job-monitor-expand [${state}] J1 @desktop`, async ({ page, stack, walkEvidence }, testInfo) => {
        const probe = resolveProbe(entry, state)
        let result
        let failure
        try { result = await runProbe(probe, { page, stack, evidence: walkEvidence, testInfo }) }
        catch (error) { failure = error }
        await testInfo.attach('w1z-job-monitor-proof', { contentType: 'application/json', body: Buffer.from(JSON.stringify({
          featureId: entry.id, state, oracleReached: walkEvidence.oracleReached,
          jobMonitorUrl: walkEvidence.jobMonitorUrl, failure: failure ? { message: failure.message } : null,
        })) })
        if (failure) throw failure
        expect(result?.unsupported, result?.reason).not.toBe(true)
        expect(walkEvidence.oracleReached).toBe(probe.assertion.assertionId)
        expect(walkEvidence.cleanupCompleted).toBe(true)
        expect(typeof walkEvidence.jobMonitorUrl?.after).toBe('string')
      })
    }
  })
}
