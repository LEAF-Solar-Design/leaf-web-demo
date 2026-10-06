import { expect, test } from '@playwright/test'
import { assertPageOnAllowedOrigin } from './stagingConfig.mjs'
import { captureStagingIdentity } from './stagingIdentity.mjs'

// S24 (A14): the URL keeps the open drawer. Opening Details pushes
// drawer=details beside the boot flags (demo=off survives byte for byte), a
// reload reopens it, and Back closes it.
const search = (page) => new URL(page.url()).search
const param = (page, key) => new URL(page.url()).searchParams.get(key)

test('Details rides the URL: a reload keeps it open and Back closes it', async ({ page, request }, testInfo) => {
  const identity = await captureStagingIdentity(request)
  await testInfo.attach('staging-identity', {
    body: JSON.stringify(identity, null, 2),
    contentType: 'application/json',
  })
  await page.goto('/app?demo=off', { waitUntil: 'networkidle', timeout: 30_000 })
  assertPageOnAllowedOrigin(page)
  expect(new URL(page.url()).pathname).toBe('/app')
  const drawer = page.getByRole('dialog', { name: 'Session · provenance' })

  await page.getByTitle(/^Session details/).click()
  await expect(drawer).toBeVisible()
  await expect.poll(() => param(page, 'drawer')).toBe('details')
  expect(search(page).startsWith('?demo=off&')).toBe(true)

  await page.reload({ waitUntil: 'networkidle', timeout: 30_000 })
  assertPageOnAllowedOrigin(page)
  await expect(drawer).toBeVisible()
  expect(param(page, 'drawer')).toBe('details')
  expect(search(page).startsWith('?demo=off&')).toBe(true)

  await page.goBack()
  await expect(drawer).toHaveCount(0)
  expect(new URL(page.url()).pathname).toBe('/app')
  expect(param(page, 'drawer')).toBeNull()
  expect(search(page).startsWith('?demo=off')).toBe(true)
})
