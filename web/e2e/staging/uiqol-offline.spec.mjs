import { expect, test } from '@playwright/test'
import { assertPageOnAllowedOrigin } from './stagingConfig.mjs'
import { captureStagingIdentity } from './stagingIdentity.mjs'

test('anonymous /try shows one offline banner under the header and clears it on reconnect', async ({ page, context, request }, testInfo) => {
  const identity = await captureStagingIdentity(request)
  await testInfo.attach('staging-identity', {
    body: JSON.stringify(identity, null, 2),
    contentType: 'application/json',
  })
  await page.goto('/try', { waitUntil: 'networkidle' })
  assertPageOnAllowedOrigin(page)
  expect(new URL(page.url()).pathname).toBe('/try')
  const header = page.locator('.stage-root .tc-product-nav')
  const banner = page.getByTestId('connection-banner')
  await expect(header).toBeVisible()
  await expect(header).toHaveCSS('transform', 'none')
  await expect(banner).toHaveCount(0)

  try {
    await context.setOffline(true)
    await expect(banner).toHaveCount(1)
    await expect(banner).toBeVisible()
    await expect(banner).toHaveAttribute('role', 'status')
    await expect(banner).toHaveText('Offline. Leaf Automation reconnects on its own.')
    expect(await banner.getAttribute('aria-live')).toBeNull()
    await expect(banner.locator('button, [role="button"], a, .pulse, .live')).toHaveCount(0)
    const dot = banner.locator('.dot.square')
    await expect(dot).toHaveCount(1)
    await expect(dot).toHaveCSS('width', '6px')
    await expect(dot).toHaveCSS('height', '6px')
    await expect(dot).toHaveCSS('border-radius', '1.5px')
    await expect(dot).toHaveCSS('animation-name', 'none')
    const colors = await dot.evaluate((node) => {
      const probe = document.createElement('span')
      probe.style.color = 'var(--status-warning)'
      node.appendChild(probe)
      const expected = getComputedStyle(probe).color
      probe.remove()
      return { actual: getComputedStyle(node).backgroundColor, expected }
    })
    expect(colors.actual).toBe(colors.expected)
    await expect.poll(async () => {
      const head = await header.boundingBox()
      const strip = await banner.boundingBox()
      return head && strip ? Math.abs(strip.y - (head.y + head.height)) : Infinity
    }).toBeLessThanOrEqual(1)

    await context.setOffline(false)
    await expect(banner).toHaveCount(0)
  } finally {
    await context.setOffline(false)
  }
})
