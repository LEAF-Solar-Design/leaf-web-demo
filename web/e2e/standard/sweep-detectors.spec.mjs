import { test, expect } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { auditPage, summarize, WIDTHS } from './sweepLib.mjs'

const countFields = ['offscreenCount', 'clippedCount', 'smallTextCount', 'smallTargetsCount']
const pairs = [
  ['offscreen', 'offscreenCount'],
  ['clipped', 'clippedCount'],
  ['small-text', 'smallTextCount'],
  ['small-target', 'smallTargetsCount'],
  ['page-scroll', 'pageScrollsX'],
]

function fixture(name) {
  return readFileSync(new URL(`./fixtures/${name}.html`, import.meta.url), 'utf8')
}

for (const [name, detector] of pairs) {
  test(`${name} positive and negative fixtures`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    for (const variant of ['positive', 'negative']) {
      await page.setContent(fixture(`${name}-${variant}`))
      const audit = await page.evaluate(auditPage)
      if (detector === 'pageScrollsX') expect(audit[detector]).toBe(variant === 'positive')
      else if (variant === 'positive') expect(audit[detector]).toBeGreaterThanOrEqual(1)
      else expect(audit[detector]).toBe(0)
      for (const field of countFields.filter(field => field !== detector)) {
        expect(audit[field], `${name}-${variant}: ${field}`).toBe(0)
      }
      if (name === 'clipped' && variant === 'negative') expect(audit.ellipsisCount).toBe(1)
    }
  })
}

for (const [width, height] of WIDTHS) {
  test(`clean fixture at ${width}x${height}`, async ({ page }) => {
    await page.setViewportSize({ width, height })
    await page.setContent(fixture('clean'))
    const audit = await page.evaluate(auditPage)
    for (const field of [...countFields, 'ellipsisCount']) expect(audit[field], field).toBe(0)
    expect(audit.pageScrollsX).toBe(false)
  })
}

test('summarize totals and worst rows', () => {
  const first = { id: 'first', pageScrollsX: true, offscreenCount: 2, clippedCount: 0,
    ellipsisCount: 1, smallTextCount: 4, smallTargetsCount: 1 }
  const second = { id: 'second', pageScrollsX: false, offscreenCount: 1, clippedCount: 3,
    ellipsisCount: 2, smallTextCount: 0, smallTargetsCount: 5 }
  expect(summarize([first, second])).toEqual({
    totals: { pageScrollsX: 1, offscreenCount: 3, clippedCount: 3, ellipsisCount: 3,
      smallTextCount: 4, smallTargetsCount: 6 },
    worst: { pageScrollsX: first, offscreenCount: first, clippedCount: second,
      ellipsisCount: second, smallTextCount: first, smallTargetsCount: second },
  })
})
