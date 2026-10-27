import { expect, test } from '@playwright/test'

async function openTour(page, viewport) {
  await page.setViewportSize(viewport)
  await page.goto('/app?demo=tour')
  await page.getByRole('navigation', { name: 'Workspace controls' }).getByRole('button', { name: 'Studio', exact: true }).click()
  const tour = page.getByRole('dialog', { name: 'Guided demo tour' })
  await expect(tour).toBeVisible()
  return tour
}

test('the version toast ignores a captured drag-away before Undo activates normally', async ({ page }) => {
  const tour = await openTour(page, { width: 1280, height: 800 })
  await tour.getByRole('button', { name: 'Skip', exact: true }).click()

  const command = page.getByRole('combobox', { name: 'Command bar' })
  await command.fill('delete panel 7FA3')
  await command.press('Enter')
  await page.getByRole('button', { name: 'Run delete-marked-panel', exact: true }).click()
  await expect(page.locator('body')).toContainText(/Entities\s*2,344/)

  const toast = page.locator('.toast[role="status"]')
  await expect(toast).toContainText('Version 2 created')
  const undo = toast.getByRole('button', { name: 'Undo', exact: true })
  const box = await undo.boundingBox()
  expect(box).not.toBeNull()
  const x = box.x + box.width / 2
  const y = box.y + box.height / 2
  await page.mouse.move(x, y)
  await page.mouse.down()
  await page.mouse.move(x, Math.max(1, box.y - 80), { steps: 5 })
  await page.mouse.up()

  await expect(page.locator('body')).toContainText(/Entities\s*2,344/)
  await expect(toast).toContainText('Version 2 created')
  await undo.click()

  await expect(page.locator('body')).toContainText(/Entities\s*2,345/)
  await expect(page.locator('.toast[role="status"]')).toContainText('Reverted to version 1')
})

test('Back recovers phone tour focus to visible Skip before the next Tab', async ({ page }) => {
  const tour = await openTour(page, { width: 390, height: 844 })
  await tour.getByRole('button', { name: 'More', exact: true }).click()
  await tour.getByRole('button', { name: 'Next', exact: true }).click()
  await expect(tour).toContainText('Step 2 of 9')

  const back = tour.getByRole('button', { name: 'Back', exact: true })
  await back.focus()
  await page.keyboard.press('Enter')
  await expect(tour).toContainText('Step 1 of 9')
  await expect(back).toBeDisabled()
  const skip = tour.getByRole('button', { name: 'Skip', exact: true })
  await expect(skip).toBeFocused()
  expect(await skip.evaluate((element) => {
    const style = getComputedStyle(element)
    return element.matches(':focus-visible')
      && (style.outlineStyle !== 'none' || style.boxShadow !== 'none')
  })).toBe(true)
  await page.keyboard.press('Tab')
  await expect(tour.getByRole('button', { name: 'Next', exact: true })).toBeFocused()
})
