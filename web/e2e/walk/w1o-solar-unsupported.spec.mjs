import { test, expect, runProbe } from './fixtures.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { resolveProbe } from './probes.mjs'

const entry = buildFeatureMap().entries.find((entry) => entry.id === 'tool:solar-correct-string')
const probeFor = (state) => resolveProbe(entry, state)

test.describe('solar availability is declared before setup only for run decisions', () => {
  test.describe.configure({ mode: 'serial' })

  test('tool:solar-correct-string [ready] @desktop', async ({ page, stack, walkEvidence }, testInfo) => {
    const started = performance.now()
    const result = await runProbe(probeFor('ready'), { page, stack, evidence: walkEvidence, testInfo })
    expect(performance.now() - started).toBeLessThan(5000)
    expect(result?.unsupported).toBe(true)
    expect(testInfo.annotations).toContainEqual({ type: 'unsupported_local', description: result.reason })
    expect(walkEvidence.result.result).toBe('unsupported_local')
    expect(walkEvidence.result.tool).toBe('solar-correct-string')
    expect(walkEvidence.result.availability_fields_false).toContain('input_ready')
    expect(walkEvidence.result.refusal_codes).toContain('drawing_context_required')
    expect(walkEvidence.steps).toEqual([])
  })

  test('tool:solar-correct-string [write-locked] @desktop', async ({ page, stack, walkEvidence }, testInfo) => {
    const probe = probeFor('write-locked')
    expect(probe.assertion.kind).toBe('disabled_with_reason')
    try {
      const result = await runProbe(probe, { page, stack, evidence: walkEvidence, testInfo })
      expect(result?.unsupported).not.toBe(true)
      expect(walkEvidence.result.result).toBe('passed')
    } finally {
      expect(testInfo.annotations.some((annotation) => annotation.type === 'unsupported_local')).toBe(false)
      expect(walkEvidence.result?.result).not.toBe('unsupported_local')
      expect(walkEvidence.steps.some((step) => step.kind === 'foreign-checkout')).toBe(true)
    }
  })
})
