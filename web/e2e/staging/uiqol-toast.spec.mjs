import { expect, test } from '@playwright/test'
import { assertPageOnAllowedOrigin, assertResponseOnAllowedOrigin } from './stagingConfig.mjs'

// Fable's D4 probe checks health.json source_sha ancestry against the merge
// within the 60 min deployment bound before this browser check runs. Keep
// the observed frontend identity with the result, including the signed-out
// identity-only case; that case makes no toast-behavior claim.
test('a deployed toast survives 7 s of hover and closes within 6 s after leaving', async ({ page, request }, testInfo) => {
  test.setTimeout(60_000)
  const healthResponse = await request.get('/health.json', { timeout: 10_000 })
  assertResponseOnAllowedOrigin(healthResponse)
  expect(healthResponse.ok()).toBe(true)
  const health = await healthResponse.json()
  expect(health.source_sha).toMatch(/^[0-9a-f]{7,64}$/i)
  await testInfo.attach('frontend-identity', {
    body: JSON.stringify(health, null, 2),
    contentType: 'application/json',
  })

  await page.goto('/app?demo=off', { waitUntil: 'networkidle', timeout: 30_000 })
  assertPageOnAllowedOrigin(page)
  expect(new URL(page.url()).pathname).toBe('/app')
  await expect(page.locator('.app')).toBeVisible()
  await expect(page.locator('body')).not.toContainText('Application error')

  const command = page.getByTestId('command-bar')
  const engineSeats = page.locator('#cockpit-properties-slot, #cockpit-clipboard-slot, #cockpit-script-slot, #cockpit-solar-panels-slot')
  const canRaise = await command.isVisible()
    && await command.isEditable()
    && await engineSeats.count() > 0

  if (!canRaise) {
    // The point-without-command notice needs the browser CAD engine's
    // dispatch adapter. A gated command bar or flag-off engine leaves the
    // other completed-event notices behind authentication. Do not turn a
    // missing toast AFTER dispatch into this permitted identity-only case.
    const session = await request.get('/api/session', { timeout: 10_000 })
    assertResponseOnAllowedOrigin(session)
    expect(session.status()).toBe(401)
    testInfo.annotations.push({
      type: 'identity-only',
      description: 'Signed out: the command bar or browser-engine command adapter is unavailable; no completed-event toast can be raised on this surface.',
    })
    await testInfo.attach('signed-out-surface', { body: await page.screenshot(), contentType: 'image/png' })
    return
  }

  // App's drawingCommand adapter raises this local notice before routing or
  // authentication. It neither runs a tool nor modifies a drawing. Use the
  // actual command bar, without a mock bus, DOM injection, or API interception.
  await command.fill('0,0')
  await command.press('Enter')
  const toast = page.locator('.toast[role="status"]')
  await expect(toast).toHaveText('Start a drawing command before entering a point.')
  await expect(toast).toBeVisible()
  await expect(toast).not.toHaveAttribute('aria-live', /.+/)
  await toast.hover()
  await page.waitForTimeout(7000)
  await expect(toast).toBeVisible()
  await expect(toast).toHaveClass('toast enter')
  await page.mouse.move(0, 0)
  await expect(toast).toHaveCount(0, { timeout: 6000 })
  testInfo.annotations.push({ type: 'toast-behavior', description: 'Hovered for 7 s, then removed within 6 s of leaving.' })
})
