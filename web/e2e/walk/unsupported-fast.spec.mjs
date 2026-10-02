import { test as walkTest, expect, runProbe, ENGINE_SETUP_KINDS } from './fixtures.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { resolveProbe } from './probes.mjs'

// Use the same registry expansion and viewport as features.spec.mjs.
const map = buildFeatureMap()
const probeFor = (id, state) => resolveProbe(map.entries.find((entry) => entry.id === id), state)
const first = probeFor('action:bar-escape', 'selection-present')
const candidates = map.entries.flatMap((entry) => entry.viewports.includes('desktop')
  ? entry.states.map((state) => resolveProbe(entry, state)) : [])
  .filter((probe) => !probe.certification && !(probe.featureId === first.featureId && probe.state === first.state)
    && probe.setup.steps.some((step) => ENGINE_SETUP_KINDS.has(step.kind))
    && !probe.setup.steps.some((step) => step.kind === 'require-local-state'))
const seen = new Set()
const later = candidates.filter((probe) => {
  const key = JSON.stringify([probe.featureId, probe.state])
  if (seen.has(key)) return false
  seen.add(key)
  return true
}).slice(0, 5)
if (later.length < 5) throw new Error(`The feature map must provide five distinct engine-state timing probes; found ${later.length}`)
const reason = 'The production bundle has no mounted browser editing engine'
const test = walkTest.extend({
  completedProbes: [async ({}, use) => { await use([]) }, { scope: 'worker' }],
})

test.describe('unsupported probes finish before setup on the same stack', () => {
  test.describe.configure({ mode: 'serial' })

  test.afterEach(async ({ completedProbes, walkEvidence }, testInfo) => {
    expect(testInfo.status).toBe('passed')
    // walk-evidence is attached at fixture teardown, after this hook.
    const evidence = walkEvidence
    expect(evidence.stack.ready).toBe(true)
    if (completedProbes.length) expect(evidence.stack.instance).toBe(completedProbes[0].instance)
    completedProbes.push({ instance: evidence.stack.instance, result: evidence.result.result })
  })

  test('observe action:bar-escape [selection-present] @desktop', async ({ page, stack, walkEvidence, workerFacts, completedProbes }, testInfo) => {
    expect(completedProbes).toHaveLength(0)
    expect(workerFacts.engineMounted).toBeUndefined()
    expect(await runProbe(first, { page, stack, evidence: walkEvidence, testInfo })).toEqual({ unsupported: true, reason })
    expect(workerFacts.engineMounted).toBe(false)
    expect(testInfo.annotations).toContainEqual({ type: 'unsupported_local', description: reason })
  })

  for (const [index, probe] of later.entries()) {
    test(`fast ${probe.featureId} [${probe.state}] @desktop`, async ({ page, stack, walkEvidence, workerFacts, completedProbes }, testInfo) => {
      expect(completedProbes).toHaveLength(index + 1)
      expect(workerFacts.engineMounted).toBe(false)
      const started = performance.now()
      const result = await runProbe(probe, { page, stack, evidence: walkEvidence, testInfo })
      expect(performance.now() - started).toBeLessThan(3000)
      expect(result).toEqual({ unsupported: true, reason })
      expect(testInfo.annotations).toContainEqual({ type: 'unsupported_local', description: reason })
      expect(walkEvidence.steps).toEqual([])
      expect(walkEvidence.ux_observations).toBeUndefined()
    })
  }

  test('supported control:session-details [ready] @desktop', async ({ page, stack, walkEvidence, completedProbes }, testInfo) => {
    expect(completedProbes).toHaveLength(6)
    expect(completedProbes.every((row) => row.result === 'unsupported_local')).toBe(true)
    await runProbe(probeFor('control:session-details', 'ready'), { page, stack, evidence: walkEvidence, testInfo })
    expect(walkEvidence.result.result).toBe('passed')
    expect(testInfo.annotations.some((row) => row.type === 'unsupported_local')).toBe(false)
    expect(walkEvidence.stack.instance).toBe(completedProbes[0].instance)
  })
})
