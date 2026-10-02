import { test, expect, runProbe, withProbeSetup } from './fixtures.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { resolveProbe } from './probes.mjs'
import { assertControlCensus, censusControls, resolveCensus } from './controlCensus.mjs'
import { readFileSync } from 'node:fs'

const map = buildFeatureMap()
const entryFor = (id) => map.entries.find((entry) => entry.id === id)
const fixture = (name) => JSON.parse(readFileSync(new URL(`../../walk/fixtures/control-census.${name}.json`, import.meta.url), 'utf8'))

test('control-census:fixtures [good-bad-ambiguous] @desktop', async ({ page }) => {
  const good = fixture('good'), bad = fixture('bad')
  expect(resolveCensus(good.controls, good.mappings, good.map).ok).toBe(true)
  expect(resolveCensus(bad.controls, bad.mappings, bad.map).ok).toBe(false)
  // Real DOM enumeration also has to see an unknown control, including one
  // whose label resembles a shell command. Its text is evidence only.
  await page.setContent('<button id="known" disabled title="Unavailable">Known</button><button id="extra">C:/tmp/example; echo unexpected</button>')
  const inventory = { entries: [], presentations: [] }
  const result = await censusControls(page, good.map, { inventory })
  expect(result.ok).toBe(false)
  expect(result.errors.some((error) => error.reason === 'missing inventory mapping')).toBe(true)
  const ambiguous = structuredClone(good)
  ambiguous.mappings.push({ ...ambiguous.mappings[0], key: 'second-reviewed-selector' })
  expect(resolveCensus(ambiguous.controls, ambiguous.mappings, ambiguous.map).errors[0].reason).toBe('ambiguous mapping')
})

for (const state of ['valid', 'invalid', 'incomplete', 'busy']) {
  test(`control-census:prompt [${state}] @desktop`, async ({ page, stack, walkEvidence }, testInfo) => {
    const probe = resolveProbe(entryFor('control:prompt-run'), state)
    const states = Object.fromEntries(map.entries.filter((entry) => entry.id.startsWith('control:prompt-')).map((entry) => [entry.id, state]))
    await withProbeSetup(probe, { page, stack, evidence: walkEvidence, testInfo }, async (recipe) => {
      if (recipe.kind === 'open-private-drawing' || recipe.kind === 'ribbon-tab' || recipe.kind === 'prompt-state') {
        const census = await assertControlCensus(page, map, { viewport: 'desktop', states }, walkEvidence)
        expect(census.total).toBeGreaterThan(0)
      }
    })
    // The probe executes its named oracle, not just a census visibility check.
    await runProbe(probe, { page, stack, evidence: walkEvidence, testInfo })
    await runProbe(resolveProbe(entryFor('control:prompt-cancel'), state), { page, stack, evidence: walkEvidence, testInfo })
  })
}

for (const name of ['Draw', 'Annotate', 'Insert']) {
  test(`control-census:reference-${name.toLowerCase()} [rendered] @desktop`, async ({ page, stack, walkEvidence }, testInfo) => {
    const probe = resolveProbe(entryFor('action:draw-create-line'), 'ready')
    probe.setup.steps.push({ kind: 'reference-panel', name })
    await withProbeSetup(probe, { page, stack, evidence: walkEvidence, testInfo }, async (recipe) => {
      if (recipe.kind === 'open-private-drawing' || recipe.kind === 'ribbon-tab' || recipe.kind === 'reference-panel') {
        const census = await assertControlCensus(page, map, { viewport: 'desktop' }, walkEvidence)
        expect(census.total).toBeGreaterThan(0)
        for (const row of census.resolved.filter((row) => row.featureId.startsWith('control:reference-'))) {
          const entry = entryFor(row.featureId)
          expect(row.disabled).toBe(true)
          const locator = page.locator(entry.locator.selector)
          await expect(locator).toBeDisabled()
          await expect(locator.locator('xpath=ancestor-or-self::*[@title][1]')).toHaveAttribute('title', entry.expected_effect[row.state].reason)
        }
      }
    })
  })
}
