import { test, expect, runProbe } from './fixtures.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { resolveProbe } from './probes.mjs'

const entries = buildFeatureMap().entries
if (process.env.LEAF_WALK_PROOF === '1') {
  test.describe('W1z operation-specific walk oracles', () => {
    test.describe.configure({ mode: 'default' })
    for (const [featureId, state] of [
      ['action:bar-escape', 'ready'],
      ['action:bar-retry', 'ready'],
      ['action:clipboard-copy-clip', 'placed-dimension'],
      ['action:clipboard-cut-clip', 'placed-dimension'],
      ['action:modify-explode', 'placed-dimension'],
      ['action:clipboard-copy-clip', 'ready'],
      ['action:modify-explode', 'ready'],
      ['action:undo', 'ready'],
      ['action:redo', 'ready'],
      ['action:zoom-out', 'ready'],
      ['action:bar-escape', 'job-running'],
      ['tool:solar-solve-proposal', 'ready'],
      ['drawer:jobs', 'closed'],
      ['drawer:jobs', 'open'],
    ]) {
      test(`${featureId} [${state}] W1z @desktop`, async ({ page, stack, walkEvidence }, testInfo) => {
        const entry = entries.find((entry) => entry.id === featureId)
        expect(entry, featureId).toBeTruthy()
        const probe = resolveProbe(entry, state)
        let failure
        let result
        try { result = await runProbe(probe, { page, stack, evidence: walkEvidence, testInfo }) }
        catch (error) { failure = error }
        await testInfo.attach('w1z-oracles-proof', { contentType: 'application/json', body: Buffer.from(JSON.stringify({
          featureId, state, assertionId: probe.assertion.assertionId,
          setup: walkEvidence.setupCompleted, oracleReached: walkEvidence.oracleReached,
          cleanupCompleted: walkEvidence.cleanupCompleted, expected: probe.assertion,
          noRung: walkEvidence.noRung, engineRefusal: walkEvidence.engineRefusal,
          clipboardPaste: walkEvidence.clipboardPaste, versionTransition: walkEvidence.versionTransition,
          zoomBaseline: walkEvidence.zoomBaseline, pendingRun: walkEvidence.pendingRun,
          catalog: walkEvidence.catalog, result,
          productVerdict: failure ? { message: failure.message, expected: failure.matcherResult?.expected,
            observed: failure.matcherResult?.actual } : null,
        })) })
        expect(result?.unsupported, result?.reason).not.toBe(true)
        expect(walkEvidence.setupCompleted, failure?.message).toBeTruthy()
        expect(walkEvidence.cleanupCompleted, failure?.message).toBe(true)
        expect(walkEvidence.oracleReached, failure?.message).toBe(probe.assertion.assertionId)
        if (failure) {
          if (!failure.matcherResult || walkEvidence.failure?.message !== failure.message) throw failure
          const description = `${probe.assertion.assertionId}: ${failure.message}`
          console.log('ORACLE:', description, JSON.stringify({ expected: failure.matcherResult.expected,
            observed: failure.matcherResult.actual }))
          testInfo.annotations.push({ type: 'product_verdict', description })
          test.fail(true, description)
          throw failure
        }
      })
    }
  })
}
