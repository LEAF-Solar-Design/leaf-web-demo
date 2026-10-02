import { test, expect, runProbe } from './fixtures.mjs'
import { CONTROL_CENSUS_BATCH, requireControlCensusBatch, resolveProbe } from './probes.mjs'
import { censusControls, enumerateControls } from './controlCensus.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { censusFailure, controlKey, controlNameAttributes, resolveCensus } from '../../walk/controlInventory.mjs'
import { writeFileSync } from 'node:fs'

const map = buildFeatureMap()
const recorded = new Map()

// Keep both observations in one worker. Record mode replaces the destination
// with this run's union, rather than preserving rows from an earlier run.
test.describe('rendered control census', () => {
  test.describe.configure({ mode: 'serial' })
  for (const state of ['ready', 'failed-load']) {
    test(`control-census:studio [${state}] @desktop`, async ({ page, stack, walkEvidence }, testInfo) => {
      const entry = map.entries.find((row) => row.id === (state === 'ready' ? 'tab:drafting:draw' : 'action:fit'))
      expect(entry, 'census setup feature must exist').toBeTruthy()
      const probe = resolveProbe(entry, state === 'ready' ? 'ready' : 'no-drawing')
      walkEvidence.censusRecipe = probe.setup
      await runProbe(probe, { page, stack, evidence: walkEvidence, testInfo })
      // Positive controls prevent an empty/dead workspace from satisfying a
      // census or an absence assertion in the failed-load recipe.
      await expect(page.getByRole('combobox', { name: 'Command bar', exact: true })).toBeVisible()
      if (state === 'ready') await expect(page.getByRole('tablist', { name: 'Ribbon', exact: true })).toBeVisible()
      else await expect(page.getByRole('alert').filter({ hasText: /Couldn['’]t load drawing/ })).toBeVisible()
      const census = await censusControls(page, map, { state, viewport: 'desktop' })
      walkEvidence.censuses = [census]
      expect(census.total, 'a real workspace must render interactive controls').toBeGreaterThan(0)
      for (const tab of ['annotate', 'draw', 'insert', 'manage', 'model', 'view']) {
        expect(census.resolved.some((row) => row.feature_id === `tab:drafting:${tab}`), `Ribbon ${tab} must resolve`).toBe(true)
      }
      if (state === 'ready') expect(census.resolved.some((row) => row.feature_id === 'action:fit'
        && row.scope === 'toolbar:"View"'), 'viewer Fit must resolve to its registry action').toBe(true)
      expect(requireControlCensusBatch(census, { state, viewport: 'desktop' })).toBe(true)
      const applicableBatch = CONTROL_CENSUS_BATCH.filter((row) => row.states.includes(state))
      expect(census.resolved.filter((row) => applicableBatch.some((expected) => expected.feature_id === row.feature_id)))
        .toHaveLength(applicableBatch.length)
      const path = process.env.LEAF_WALK_CENSUS_RECORD
      if (path) {
        for (const control of [...census.unmapped, ...census.baselined]) {
          const key = controlKey(control)
          if (!recorded.has(key)) recorded.set(key, { scope: control.scope, role: control.role, name: controlNameAttributes(control.name).name_key,
            reason: control.reason || `No feature-map coverage yet: ${control.problem}`, states: [], viewports: ['desktop'] })
          const row = recorded.get(key)
          if (!row.states.includes(state)) row.states.push(state)
        }
        const baseline_unmapped = [...recorded.values()].sort((a, b) => controlKey(a).localeCompare(controlKey(b)))
        writeFileSync(path, JSON.stringify({ version: 1, mappings: [], baseline_unmapped }, null, 2) + '\n')
        walkEvidence.censusRecord = { state, count: census.unmapped.length }
      } else expect(census.ok, censusFailure(census)).toBe(true)
    })
  }
})

test('control-census:dom [enumeration] @desktop', async ({ page }) => {
  await page.setContent(`<main aria-label="Studio"><div role="toolbar" aria-label="Tools" tabindex="0">
    <button disabled aria-labelledby="fit-label"><span aria-hidden="true">icon</span></button>
    <span id="fit-label">Fit drawing</span>
    <label for="title">Drawing title</label><input id="title">
    <div role="menuitem" aria-label="Details" tabindex="-1"></div>
    <div tabindex="0" aria-label="Focus target"></div>
    <div role="tablist" aria-label="Fixture tabs" tabindex="0"><button role="tab">Visible tab</button></div>
    <div role="group" aria-label="Fixture group" tabindex="0"></div>
    <nav aria-label="Fixture navigation" tabindex="0"></nav>
    <section role="region" aria-label="Fixture region" tabindex="0"></section>
    <button disabled>Redo version (unavailable: nothing to redo)</button>
    <button>Panels 12</button>
    <button style="display:none">Hidden</button>
  </div></main>`)
  const controls = await enumerateControls(page)
  const fit = controls.find((row) => row.name === 'Fit drawing')
  expect(fit).toMatchObject({ role: 'button', scope: 'main:"Studio" > toolbar:"Tools"', disabled: true, visible: true })
  expect(controls.find((row) => row.name === 'Drawing title')).toMatchObject({ role: 'textbox', visible: true })
  expect(controls.find((row) => row.name === 'Details')).toMatchObject({ role: 'menuitem', visible: true })
  expect(controls.find((row) => row.name === 'Focus target')).toMatchObject({ visible: true })
  expect(controls.find((row) => row.name === 'Hidden')).toMatchObject({ visible: false })
  expect(controls.some((row) => ['toolbar', 'tablist', 'group', 'navigation', 'region', 'main'].includes(row.role))).toBe(false)
  expect(controls.find((row) => row.name === 'Visible tab')).toMatchObject({ role: 'tab',
    scope: 'main:"Studio" > toolbar:"Tools" > tablist:"Fixture tabs"', visible: true })
  expect(controls.find((row) => row.name_key === 'Redo version')).toMatchObject({ disabled: true,
    raw_name: 'Redo version (unavailable: nothing to redo)', disabled_reason: 'nothing to redo' })
  expect(controls.find((row) => row.name === 'Panels 12')).toMatchObject({ name_key: 'Panels {n}', raw_name: 'Panels 12' })
  const inventory = { version: 1, mappings: [], baseline_unmapped: controls.filter((row) => row.visible && row !== fit)
    .map(({ scope, role, name }) => ({ scope, role, name, reason: 'DOM fixture control' })) }
  const derived = [{ index: fit.index, feature_id: 'action:fit' }]
  expect(resolveCensus(controls, derived, map, inventory).ok).toBe(true)
  await page.getByRole('toolbar', { name: 'Tools', exact: true }).evaluate((node) => {
    const button = document.createElement('button')
    button.textContent = 'Injected new control'
    node.append(button)
  })
  const injected = resolveCensus(await enumerateControls(page), derived, map, inventory)
  expect(injected.resolved.some((row) => row.feature_id === 'action:fit')).toBe(true)
  expect(injected.ok).toBe(false)
  expect(injected.unmapped.map((row) => row.name)).toEqual(['Injected new control'])
})
