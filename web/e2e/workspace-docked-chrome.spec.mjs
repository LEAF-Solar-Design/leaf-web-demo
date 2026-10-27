import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

// Actual Next.js rendering with fixture-backed APIs. No live account mutations.
const CORS = { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' }
async function backend(page) {
  const state = makeCatProofState()
  await page.addInitScript(() => {
    localStorage.setItem('leaf.jwt', 'fixture-token')
    localStorage.setItem('leaf.coach.dismissed.v1', '1')
  })
  await page.route('http://leaf-proof.invalid/api/**', async route => {
    const req = route.request(), url = new URL(req.url())
    if (req.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: CORS })
    const reply = catProofResponse({ method: req.method(), path: url.pathname,
      query: Object.fromEntries(url.searchParams), body: req.postData() ? req.postDataJSON() : {} }, state)
    await route.fulfill({ status: reply.status, headers: CORS, contentType: 'application/json', body: JSON.stringify(reply.body ?? null) })
  })
}

for (const [size, viewport] of [['desktop', { width: 1440, height: 900 }], ['phone', { width: 390, height: 844 }]]) {
  test(`docked workspace chrome remains usable at ${size}`, async ({ page }, info) => {
    test.setTimeout(120_000)
    page.setDefaultTimeout(15_000)
    await page.setViewportSize(viewport)
    await backend(page)
    await page.goto('/app?surface=browser')
    const controls = page.getByRole('navigation', { name: 'Workspace controls' })
    await controls.getByRole('button', { name: 'Studio', exact: true }).click()
    const rail = page.getByRole('region', { name: 'Studio sidebar', exact: true })
    await expect(rail).toBeVisible()
    await expect(page.locator('.environment-backdrop')).toHaveCSS('opacity', '0')
    await expect(page.locator('.environment-chooser')).toHaveCSS('opacity', '0')
    await page.screenshot({ path: info.outputPath(`docked-${size}-open.png`) })
    const geometry = await page.evaluate(() => {
      const rect = selector => {
        const el = document.querySelector(selector)
        if (!el) throw new Error(`Missing geometry target: ${selector}`)
        const b = el.getBoundingClientRect(), s = getComputedStyle(el)
        return { x:b.x, y:b.y, width:b.width, height:b.height, right:b.right, bottom:b.bottom, font:s.fontFamily, background:s.backgroundColor }
      }
      return { width:innerWidth, scroll:document.documentElement.scrollWidth,
        header:rect('header.top'), panel:rect('#project-workspace-activity'), command:rect('.bar-dock'), controls:rect('.environment-strip') }
    })
    await info.attach('geometry', { body: JSON.stringify(geometry, null, 2), contentType:'application/json' })
    expect(geometry.scroll).toBeLessThanOrEqual(viewport.width)
    expect(geometry.header.x).toBe(0)
    expect(geometry.header.width).toBeGreaterThanOrEqual(viewport.width - 1)
    for (const rect of [geometry.panel, geometry.command, geometry.controls]) {
      expect(rect.x).toBeGreaterThanOrEqual(0)
      expect(rect.y).toBeGreaterThanOrEqual(0)
      expect(rect.right).toBeLessThanOrEqual(viewport.width + 1)
      expect(rect.bottom).toBeLessThanOrEqual(viewport.height + 1)
    }
    await rail.getByRole('button', { name:'Close Studio', exact:true }).click()
    await expect(rail).toBeHidden()
    await expect(controls.getByRole('button', { name:'Studio', exact:true })).toBeFocused()
    await expect(page.locator('header.top')).toBeVisible()
    await expect(page.locator('.bar-dock')).toBeVisible()
    expect(await page.locator('.app[data-studio-shell]').getAttribute('inert')).toBeNull()
    await controls.getByRole('button', { name:'Project', exact:true }).click()
    await expect(page.getByRole('region', { name:'Project sidebar', exact:true })).toBeVisible()
    await page.getByRole('button', { name:'Close Project', exact:true }).click()
    await controls.getByRole('combobox', { name:'Workspace profile', exact:true }).selectOption('cad')
    await controls.getByRole('button', { name:'Studio', exact:true }).click()
    await expect(page.locator('#drafting-ribbon')).toBeVisible()
    await page.screenshot({ path:info.outputPath(`docked-${size}-cad.png`) })
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(viewport.width)
  })
}
