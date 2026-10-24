import { test, expect } from '@playwright/test'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { auditPage, summarize, WIDTHS } from './sweepLib.mjs'
import { PAGES } from './sweepPages.mjs'

const studio = process.env.LEAF_SWEEP_STUDIO_URL || null
const site = process.env.LEAF_SWEEP_SITE_URL || null
const bases = { studio, site }
const pages = PAGES.filter(surface => bases[surface.base])

test.skip(!studio && !site, 'Set LEAF_SWEEP_STUDIO_URL or LEAF_SWEEP_SITE_URL to opt in')

test('advisory staging sweep', async ({ browser }) => {
  test.setTimeout(pages.length * WIDTHS.length * 80_000 + 30_000)
  const rows = []
  for (const surface of pages) {
    for (const [width, height] of WIDTHS) {
      const context = await browser.newContext({ viewport: { width, height } })
      const row = { id: surface.id, base: surface.base, path: surface.path, tab: surface.tab ?? null,
        width, height, tabSwitched: false }
      const errors = []
      try {
        const page = await context.newPage()
        let navigated = false
        try {
          row.url = new URL(surface.path, bases[surface.base]).href
          await page.goto(row.url, { waitUntil: 'domcontentloaded', timeout: 60_000 })
          navigated = true
        } catch (error) {
          errors.push(`navigation: ${error.message}`)
        }
        if (navigated) {
          await page.waitForTimeout(3500)
          if (surface.tab) {
            try {
              await page.locator('.cockpit-ribbon-tabs button, [role=tab]')
                .filter({ hasText: surface.tab }).first().click({ timeout: 6000 })
              row.tabSwitched = true
            } catch (error) {
              errors.push(`tab: ${error.message}`)
            }
          }
        }
        try {
          Object.assign(row, await page.evaluate(auditPage))
        } catch (error) {
          errors.push(`audit: ${error.message}`)
        }
      } finally {
        await context.close()
      }
      if (errors.length) row.error = errors.join('\n')
      rows.push(row)
    }
  }
  const output = resolve(process.env.LEAF_SWEEP_OUT || '../artifacts/standard-sweep/report.json')
  const report = { schema: 'leaf.standard-sweep.v1', at: new Date().toISOString(), studio, site,
    rows, summary: summarize(rows) }
  mkdirSync(dirname(output), { recursive: true })
  writeFileSync(output, `${JSON.stringify(report, null, 2)}\n`)
  const written = JSON.parse(readFileSync(output, 'utf8'))
  expect(written.rows).toHaveLength(pages.length * WIDTHS.length)
})
