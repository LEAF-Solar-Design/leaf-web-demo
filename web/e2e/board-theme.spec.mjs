import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'
import { setRail } from './local/railFlag.mjs'

test('SSD1-24E board theme: dark by default, light on request, readable, remembered', async ({ page }) => {
  test.setTimeout(60_000)
  await setRail(page, '1')
  const state = makeCatProofState()
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const result = catProofResponse({ method: request.method(), path: new URL(request.url()).pathname }, state)
    await route.fulfill({ status: result.status, contentType: 'application/json', body: JSON.stringify(result.body || {}) })
  })
  await page.goto('/app?surface=browser')
  await page.evaluate(() => localStorage.removeItem('leaf.boardTheme'))
  await page.goto('/app?surface=browser')
  const board = page.locator('[data-ground="browser"]')
  await expect(board).toBeVisible({ timeout: 20_000 })
  await expect(board).not.toHaveCSS('background-color', 'rgb(233, 231, 224)')
  const toggle = page.getByRole('button', { name: 'Light board', exact: true })
  await expect(board).toHaveAttribute('data-board-layout', 'contained')
  await expect(board.locator('.ground-board-header > :last-child')).toHaveClass('ground-theme-toggle')
  await expect(toggle).toHaveAttribute('aria-pressed', 'false')
  await toggle.click()
  await expect(board).toHaveCSS('background-color', 'rgb(233, 231, 224)')
  const tile = board.locator('.ground-tile').first()
  await expect(tile).toHaveCSS('background-color', 'rgb(255, 255, 255)')
  const ratios = await tile.evaluate((element) => {
    const luminance = (color) => {
      const [r, g, b] = color.match(/[\d.]+/g).slice(0, 3).map((value) => {
        const srgb = Number(value) / 255
        return srgb <= 0.04045 ? srgb / 12.92 : ((srgb + 0.055) / 1.055) ** 2.4
      })
      return r * 0.2126 + g * 0.7152 + b * 0.0722
    }
    const contrast = (a, b) => {
      const values = [luminance(a), luminance(b)].sort((x, y) => y - x)
      return (values[0] + 0.05) / (values[1] + 0.05)
    }
    const style = getComputedStyle(element)
    const boardElement = element.closest('[data-ground="browser"]')
    const header = boardElement.querySelector('.ground-board-header')
    const headerBackground = getComputedStyle(header).backgroundColor
    const toggleStyle = getComputedStyle(header.querySelector('.ground-theme-toggle'))
    return {
      heading: contrast(getComputedStyle(element.querySelector('h3')).color, style.backgroundColor),
      body: contrast(getComputedStyle(element.querySelector('p, .ground-empty')).color, style.backgroundColor),
      boundary: contrast(style.borderTopColor, getComputedStyle(element.closest('[data-ground="browser"]')).backgroundColor),
      boardHeading: contrast(getComputedStyle(header.querySelector('h1')).color, headerBackground),
      workspace: contrast(getComputedStyle(header.querySelector('[data-testid="surface-project-state"]')).color, headerBackground),
      toggle: contrast(toggleStyle.color, toggleStyle.backgroundColor),
    }
  })
  expect(ratios.heading).toBeGreaterThanOrEqual(4.5)
  expect(ratios.body).toBeGreaterThanOrEqual(4.5)
  expect(ratios.boundary).toBeGreaterThanOrEqual(3)
  expect(ratios.boardHeading).toBeGreaterThanOrEqual(4.5)
  expect(ratios.workspace).toBeGreaterThanOrEqual(4.5)
  expect(ratios.toggle).toBeGreaterThanOrEqual(4.5)
  await page.reload()
  await expect(board).toHaveAttribute('data-board-theme', 'light')
  await expect(board).toHaveCSS('background-color', 'rgb(233, 231, 224)')
  await page.goto('/app?surface=cad')
  await page.locator('.doc-tab-start').click()
  await expect(board).toBeVisible()
  await expect(board).toHaveAttribute('data-board-layout', 'contained')
  await expect(board).not.toHaveAttribute('data-board-theme')
  await expect(board).not.toHaveClass(/leaf-light/)
  await expect(board).not.toHaveCSS('background-color', 'rgb(233, 231, 224)')
  await expect(page.getByRole('button', { name: 'Light board', exact: true })).toHaveCount(0)
  expect(await page.evaluate(() => localStorage.getItem('leaf.boardTheme'))).toBe('light')
  await test.step('stored light survives a drawing-to-Browser profile switch without reload', async () => {
    await page.getByRole('tab', { name: 'Browser', exact: true }).click()
    await expect(board).toBeVisible()
    await expect(board).toHaveAttribute('data-board-theme', 'light')
    await expect(toggle).toHaveAttribute('aria-pressed', 'true')
  })
})
