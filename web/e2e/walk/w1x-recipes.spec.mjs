import { test, expect, runProbe, VERSIONLESS_DRAWING_REASON, SOLAR_PANEL_CALIBRATION_REASON } from './fixtures.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { resolveProbe } from './probes.mjs'

const entries = buildFeatureMap().entries
if (process.env.LEAF_WALK_PROOF === '1') {
  test.describe('W1x remaining recipes', () => {
    test.describe.configure({ mode: 'default' })
    for (const [featureId, state] of [
      ['tool:solar-panel-add', 'unsaved-engine-edits'],
      ['tool:solar-design-presets', 'write-unentitled'],
      // The rectangle probe reaches the product's solar readiness and is left to the sweep's confirmation path.
      ['action:clipboard-copy-clip', 'no-selection'],
      ['action:clipboard-cut-clip', 'multiple-selected'],
      ['action:redo', 'ready'],
      ['action:rail-expand', 'ready'],
    ]) {
      test(`${featureId} [${state}] W1x @desktop`, async ({ page, stack, walkEvidence }, testInfo) => {
        const probe = resolveProbe(entries.find((entry) => entry.id === featureId), state)
        let failure
        try {
          const result = await runProbe(probe, { page, stack, evidence: walkEvidence, testInfo })
          expect(result?.unsupported).not.toBe(true)
        } catch (error) { failure = error }
        const errors = [failure?.message,
          ...(walkEvidence.cleanupErrors || []).map((error) => error.message),
          ...walkEvidence.pageErrors.map((error) => error.message),
          ...walkEvidence.consoleErrors.map((error) => error.text)].filter(Boolean)
        expect(errors.join('\n')).not.toContain('Route is already handled')
        expect(walkEvidence.setupCompleted, failure?.message).toBeTruthy()
        expect(walkEvidence.cleanupCompleted, failure?.message).toBe(true)
        expect(walkEvidence.oracleReached, failure?.message).toBe(probe.assertion.assertionId)
        await testInfo.attach('w1x-recipes-proof', { contentType: 'application/json', body: Buffer.from(JSON.stringify({
          featureId, state, setup: walkEvidence.setupCompleted, oracleReached: walkEvidence.oracleReached,
          cleanupCompleted: walkEvidence.cleanupCompleted,
          productVerdict: failure ? { message: failure.message } : null,
        })) })
        if (failure) {
          if (!failure.matcherResult || walkEvidence.failure?.message !== failure.message) throw failure
          testInfo.annotations.push({ type: 'product_verdict', description: `${probe.assertion.assertionId}: ${failure.message}` })
          test.fail(true, failure.message)
          throw failure
        }
      })
    }
    for (const [featureId, state, reason] of [
      ['action:solar-panels-move', 'ready', SOLAR_PANEL_CALIBRATION_REASON],
      ['action:history', 'no-versioned-drawing', VERSIONLESS_DRAWING_REASON],
      ['action:redo', 'no-versioned-drawing', VERSIONLESS_DRAWING_REASON],
    ]) {
      test(`${featureId} [${state}] W1x declaration @desktop`, async ({ page, stack, walkEvidence }, testInfo) => {
        const probe = resolveProbe(entries.find((entry) => entry.id === featureId), state)
        const started = Date.now()
        const result = await runProbe(probe, { page, stack, evidence: walkEvidence, testInfo })
        expect(Date.now() - started).toBeLessThan(5_000)
        expect(result).toEqual({ unsupported: true, reason })
        expect(walkEvidence.result.result).toBe('unsupported_local')
        expect(walkEvidence.result.reason).toBe(reason)
        expect(walkEvidence.steps).toEqual([])
        expect(walkEvidence.setupCompleted).toBeUndefined()
        expect(walkEvidence.oracleReached).toBeUndefined()
        expect(walkEvidence.cleanupCompleted).toBe(true)
      })
    }
  })
}
