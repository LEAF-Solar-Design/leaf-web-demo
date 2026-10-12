import { expect, test } from '@playwright/test'

async function openTour(page, viewport) {
  await page.setViewportSize(viewport)
  await page.goto('/app?demo=tour')
  const tour = page.getByRole('dialog', { name: 'Guided demo tour' })
  await expect(tour).toBeVisible()
  return tour
}

test('the version toast Undo activates by pointer and restores the synthetic delete', async ({ page }) => {
  const tour = await openTour(page, { width: 1280, height: 800 })
  await tour.getByRole('button', { name: 'Skip', exact: true }).click()

  const command = page.getByRole('combobox', { name: 'Command bar' })
  await command.fill('delete panel 7FA3')
  await command.press('Enter')
  await page.getByRole('button', { name: 'Run delete-marked-panel', exact: true }).click()
  await expect(page.locator('body')).toContainText(/Entities\s*2,344/)

  const toast = page.locator('.toast[role="status"]')
  await expect(toast).toContainText('Version 2 created')
  await toast.getByRole('button', { name: 'Undo', exact: true }).click()

  await expect(page.locator('body')).toContainText(/Entities\s*2,345/)
  await expect(page.locator('.toast[role="status"]')).toContainText('Reverted to version 1')
})

test('Back recovers phone tour focus to the dialog root before the next Tab', async ({ page }) => {
  const tour = await openTour(page, { width: 390, height: 844 })
  await tour.getByRole('button', { name: 'More', exact: true }).click()
  await tour.getByRole('button', { name: 'Next', exact: true }).click()
  await expect(tour).toContainText('Step 2 of 9')

  const back = tour.getByRole('button', { name: 'Back', exact: true })
  await back.click()
  await expect(tour).toContainText('Step 1 of 9')
  await expect(back).toBeDisabled()
  await expect(tour).toBeFocused()
  await page.keyboard.press('Tab')

  expect(await page.evaluate(() => {
    const active = document.activeElement
    return !!active && !!active.closest('[role="dialog"][aria-label="Guided demo tour"]')
  })).toBe(true)
})
