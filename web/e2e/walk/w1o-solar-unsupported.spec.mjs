import { test, expect, runProbe, setupStep, seedSolarChain } from './fixtures.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { resolveProbe, SOLAR_TOOL_SETUP } from './probes.mjs'

const entry = buildFeatureMap().entries.find((entry) => entry.id === 'tool:solar-correct-string')
const probeFor = (state) => resolveProbe(entry, state)

test.describe('Solar run decisions declare missing producers and prepare reachable inputs', () => {
  test.describe.configure({ mode: 'serial' })

  for (const name of ['solar-panel-groups', 'solar-assign-equipment', 'solar-homeruns', 'solar-schedule',
    'solar-solve-proposal', 'solar-commit-solve']) {
    test(`tool:${name} [ready] @desktop`, async ({ page, stack, walkEvidence }, testInfo) => {
      const toolEntry = buildFeatureMap().entries.find((row) => row.source_id === name)
      const setup = SOLAR_TOOL_SETUP[name]
      const reason = `${setup.reason}: ${name}; ${setup.gate}`
      const started = performance.now()
      const result = await runProbe(resolveProbe(toolEntry, 'ready'), { page, stack, evidence: walkEvidence, testInfo })
      expect(performance.now() - started).toBeLessThan(5000)
      expect(result).toEqual({ unsupported: true, reason })
      expect(testInfo.annotations).toContainEqual({ type: 'unsupported_local', description: reason })
      expect(walkEvidence.result.result).toBe('unsupported_local')
      expect(walkEvidence.result.reason).not.toContain('input_ready is false')
      expect(walkEvidence.steps).toEqual([])
    })
  }

  test('tool:solar-correct-string [ready] @desktop', async ({ page, stack, walkEvidence }, testInfo) => {
    const result = await runProbe(probeFor('ready'), { page, stack, evidence: walkEvidence, testInfo })
    expect(result?.unsupported).not.toBe(true)
    expect(walkEvidence.result.result).toBe('passed')
    expect(walkEvidence.solarChain.requestedStage).toBe('strung')
    expect(walkEvidence.solarChain.stages.map((row) => row.stage)).toEqual(['panels', 'sized', 'strung'])
    expect(walkEvidence.solarChain.stages.every((row) => row.elapsedMs >= 0)).toBe(true)
    expect(walkEvidence.steps.some((step) => step.kind === 'seed-solar-chain')).toBe(true)
  })

  test('WL15 routed prerequisite proof @desktop', async ({ page, stack, walkEvidence }, testInfo) => {
    const probe = probeFor('ready')
    const runtime = { page, stack, evidence: walkEvidence, testInfo }
    await setupStep(probe, runtime, { kind: 'open-private-drawing', surface: 'solar' })
    let refusal
    try { await seedSolarChain(runtime, { stage: 'routed' }) } catch (error) { refusal = error.message }
    expect(refusal).toContain('seed-solar-chain equipped: needs_live_sizing: server/product_capability_availability.py:432-452')
    expect(walkEvidence.solarChain.completedStage).toBe('strung')
    const query = `/api/capabilities?drawing_id=${encodeURIComponent(runtime.drawingId)}&drawing_version=${runtime.drawingVersion}`
    const response = await page.request.get(query)
    expect(response.ok()).toBe(true)
    const catalog = await response.json()
    const names = ['solar-string-flip', 'solar-assign-equipment', 'solar-homeruns', 'solar-schedule']
    const readiness = Object.fromEntries(names.map((name) => [name,
      catalog.families.flatMap((family) => family.capabilities).find((row) => row.name === name)?.availability]))
    expect(readiness['solar-string-flip']?.input_ready).toBe(true)
    for (const name of names.slice(1)) {
      expect(readiness[name]?.input_ready).toBe(false)
      expect(SOLAR_TOOL_SETUP[name].reason).toBe('needs_live_sizing')
    }
    const proof = { drawingId: runtime.drawingId, version: runtime.drawingVersion, query, refusal, readiness,
      chain: walkEvidence.solarChain }
    await testInfo.attach('wl15-routed-prerequisites', { contentType: 'application/json', body: Buffer.from(JSON.stringify(proof)) })
    console.log(`WL15_ROUTED_PROOF ${JSON.stringify(proof)}`)
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
