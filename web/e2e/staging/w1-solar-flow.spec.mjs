import { mkdirSync } from 'node:fs'
import { expect, test } from '@playwright/test'
import { assertPageOnAllowedOrigin, stagingProofPath } from './stagingConfig.mjs'

// Rendered proof for the guided Solar step rail (record sf-solar-flow-rail, A3).
// It needs a staging build with VITE_SOLAR_FLOW_RAIL=1 and VITE_SOLAR_SETTINGS_FORM=1,
// which a later fence-on record ships, and a signed-in drafter on a Solar drawing
// holding its checkout. Until then it skips: set LEAF_E2E_SOLAR_FLOW_RAIL=1 and
// LEAF_E2E_SOLAR_FLOW_PATH to the drawing's app path to run it.
//
// Nothing here writes to the drawing. Availability is the server's own
// /api/capabilities answer with the W1 readiness fields rewritten per phase, and
// the one run the rail submits is answered at /api/run with a refusal, so the
// pending, failed, retry and invalidated states are all reached without a commit.

const ENABLED = process.env.LEAF_E2E_SOLAR_FLOW_RAIL === '1'
const DRAWING_PATH = process.env.LEAF_E2E_SOLAR_FLOW_PATH || ''
const VIEWPORTS = [{ width: 1600, height: 1000 }, { width: 1280, height: 800 }]
const W1_ORDER = [
  'solar-settings', 'solar-size-strings', 'solar-panel-groups', 'solar-solve-proposal', 'solar-commit-solve',
  'solar-correct-string', 'solar-assign-equipment', 'solar-homeruns', 'solar-schedule',
]
const READY = { entitled: true, implemented: true, engine_ready: true, input_ready: true, refusal_reasons: [] }
const blocked = (code) => ({ entitled: true, implemented: true, engine_ready: true, input_ready: false, refusal_reasons: [code] })

// Phase 'ready': settings, sizing and panel groups are ready, so the rail resumes at panel groups.
// Phase 'invalidated': panel groups lost its sizing, so the step that was ready needs a rerun.
function availabilityFor(phase, name) {
  const index = W1_ORDER.indexOf(name)
  if (phase === 'invalidated' && name === 'solar-panel-groups') return blocked('sizing_confirmation_required')
  return index <= 2 ? READY : blocked('sized_panel_groups_required')
}

test.describe('W1 Solar step rail', () => {
  test.skip(!ENABLED || !DRAWING_PATH, 'Needs a fence-on staging build and LEAF_E2E_SOLAR_FLOW_PATH (follow-on record).')

  for (const viewport of VIEWPORTS) {
    test(`captures pending, failed, retry and invalidated at ${viewport.width}x${viewport.height}`, async ({ page }) => {
      test.setTimeout(180_000)
      const outDir = stagingProofPath('w1-solar-flow', `${viewport.width}x${viewport.height}`)
      mkdirSync(outDir, { recursive: true })
      await page.setViewportSize(viewport)

      let phase = 'ready'
      await page.route('**/api/capabilities**', async (route) => {
        const response = await route.fetch()
        const body = await response.json()
        for (const family of Array.isArray(body?.families) ? body.families : []) {
          for (const tool of Array.isArray(family?.capabilities) ? family.capabilities : []) {
            if (W1_ORDER.includes(tool?.name)) tool.availability = availabilityFor(phase, tool.name)
          }
        }
        await route.fulfill({ response, json: body })
      })
      let releaseRun
      const runHeld = new Promise((resolve) => { releaseRun = resolve })
      let runCount = 0
      await page.route('**/api/run', async (route) => {
        if (route.request().method() !== 'POST') return route.continue()
        runCount += 1
        if (runCount === 1) await runHeld
        await route.fulfill({ status: 409, json: { ok: false, reason_code: 'stale_rev', error: { error_code: 'stale_rev' } } })
      })

      await page.goto(DRAWING_PATH, { waitUntil: 'networkidle', timeout: 60_000 })
      assertPageOnAllowedOrigin(page)
      const rail = page.getByTestId('solar-flow-rail')
      await expect(rail).toBeVisible({ timeout: 30_000 })
      await expect(rail.locator('ol > li')).toHaveCount(9)
      const groups = rail.locator('#solar-step-solar-panel-groups')
      await expect(groups).toHaveAttribute('aria-current', 'step')

      await groups.click()
      const editor = page.locator('#solar-step-editor')
      await expect(editor).toBeVisible()
      await editor.getByRole('button', { name: 'Review & run' }).click()
      await page.getByRole('button', { name: 'Run solar-panel-groups' }).click()
      const groupsItem = rail.locator('li', { has: groups })
      await expect(groupsItem).toHaveAttribute('data-status', 'pending')
      await page.screenshot({ path: `${outDir}/pending.png`, fullPage: false })

      phase = 'invalidated'
      releaseRun()
      await expect(groupsItem).toHaveAttribute('data-status', 'failed', { timeout: 30_000 })
      await expect(rail.getByRole('status')).toContainText('failed')
      await page.screenshot({ path: `${outDir}/failed.png`, fullPage: false })

      await expect(editor.getByRole('button', { name: 'Retry' })).toBeVisible()
      await page.screenshot({ path: `${outDir}/retry.png`, fullPage: false })

      await expect(groupsItem).toHaveAttribute('data-invalidated', 'true', { timeout: 60_000 })
      await expect(groupsItem).toContainText('Needs rerun: ')
      await page.screenshot({ path: `${outDir}/invalidated.png`, fullPage: false })
    })
  }
})
