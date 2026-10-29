import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

test.use({ viewport: { width: 390, height: 844 }, serviceWorkers: 'block' })

test('tool rail reveals its Studio host on phone', async ({ page, context, baseURL }, testInfo) => {
  test.setTimeout(60_000)
  page.setDefaultTimeout(10_000)
  const local = new URL(baseURL)
  expect(['127.0.0.1', 'localhost', '[::1]']).toContain(local.hostname)
  expect(local.protocol).toBe('http:')
  const state = makeCatProofState()
  const unexpected = []
  const reads = new Set()
  const cors = { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' }
  await context.addInitScript(() => {
    localStorage.setItem('leaf.jwt', 'fixture-token') // Redacted test-only session.
    localStorage.setItem('leaf.coach.dismissed.v1', '1')
  })
  await context.route('**/*', async route => {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()
    if (!['http:', 'https:'].includes(url.protocol)) return route.continue()
    const fixture = url.origin === 'http://leaf-proof.invalid' && url.pathname.startsWith('/api/')
    if (fixture && method === 'OPTIONS') return route.fulfill({ status: 204, headers: cors })
    const read = ['GET', 'HEAD'].includes(method)
    const telemetry = fixture && method === 'POST' && url.pathname === '/api/telemetry'
    // Never let a local API proxy, external service, or material write escape the fixture.
    if ((!fixture && (url.origin !== local.origin || url.pathname.startsWith('/api/')))
      || (!read && !telemetry)) {
      unexpected.push(`${method} ${url.origin}${url.pathname}`)
      return route.abort('blockedbyclient')
    }
    if (!fixture) return route.continue()
    if (read) reads.add(url.pathname)
    const reply = catProofResponse({ method, path: url.pathname,
      query: Object.fromEntries(url.searchParams),
      body: request.postData() ? request.postDataJSON() : {} }, state)
    return route.fulfill({ status: reply.status, headers: cors,
      contentType: 'application/json', body: JSON.stringify(reply.body ?? null) })
  })

  const expectCenterHit = async control => {
    await expect(control).toBeVisible()
    await expect.poll(() => control.evaluate(el => {
      const b = el.getBoundingClientRect()
      const hit = document.elementFromPoint(b.x + b.width / 2, b.y + b.height / 2)
      return b.width > 0 && b.height > 0 && b.x >= 0 && b.y >= 0
        && b.right <= innerWidth && b.bottom <= innerHeight
        && (hit === el || el.contains(hit))
    }), { timeout: 10_000 }).toBe(true)
  }

  await page.goto('/app?surface=cad')
  const app = page.locator('.app[data-studio-shell="cockpit"]')
  await expect(app).toHaveAttribute('data-surface', 'cad')
  const controls = page.getByRole('navigation', { name: 'Workspace controls', exact: true })
  await controls.getByRole('button', { name: 'Studio', exact: true }).click()
  const studio = page.getByRole('region', { name: 'Studio sidebar', exact: true, includeHidden: true })
  await expect(studio).toBeVisible()
  await expect(page.locator('.viewer-title')).toContainText('cat.dwg')
  const project = controls.getByRole('button', { name: 'Project', exact: true })
  await project.click()
  await expect(project).toHaveAttribute('aria-expanded', 'true')
  await expect(page.getByRole('region', { name: 'Project sidebar', exact: true })).toBeVisible()
  await expect(studio).toBeHidden()
  await expect(app).toHaveAttribute('data-environment-panel', 'project')

  const toolRail = page.getByRole('button', { name: 'Tool rail', exact: true })
  await expect(toolRail).toHaveAttribute('aria-expanded', 'false')
  await expectCenterHit(toolRail)
  await toolRail.click()
  await expect(studio).toBeVisible()
  await expect(app).toHaveAttribute('data-environment-panel', 'studio')
  await expect(toolRail).toHaveAttribute('aria-expanded', 'true')
  await expect(project).toHaveAttribute('aria-expanded', 'false')
  const nav = studio.locator('aside.nav')
  await expect(nav).toBeVisible()
  await expect(nav.locator('.fam-title')).toContainText('Catalog')
  await expectCenterHit(nav.getByRole('button', { name: 'Collapse the tool rail to a spine', exact: true }))
  await expectCenterHit(studio.getByRole('button', { name: 'Close Studio', exact: true }))
  await expectCenterHit(toolRail)
  await expect.poll(() => studio.evaluate(el => {
    const b = el.getBoundingClientRect()
    return b.width > 0 && b.height > 0 && b.x >= 0 && b.y >= 0
      && b.right <= innerWidth && b.bottom <= innerHeight
      && document.documentElement.scrollWidth <= innerWidth
      && document.body.scrollWidth <= innerWidth
  }), { timeout: 10_000 }).toBe(true)
  expect([...reads]).toEqual(expect.arrayContaining(['/api/session', '/api/capabilities']))
  expect(unexpected).toEqual([])
  await page.screenshot({ path: testInfo.outputPath('toolrail-studio-phone.png') })
})
