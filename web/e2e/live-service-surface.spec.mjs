import { expect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const INTAKE = JSON.parse(readFileSync(join(HERE, '..', '..', 'data', 'rooftop_demo.intake.json'), 'utf8'))

test('live surface honors the exact session-seeded drawing id', async ({ page }) => {
  const drawingId = 'acceptance-catflow-20260730-1107-a'
  let releaseSession
  const sessionReleased = new Promise((resolve) => { releaseSession = resolve })
  await page.addInitScript((seededDrawingId) => {
    window.sessionStorage.setItem('leaf.cat.workbench.id.v1', seededDrawingId)
  }, drawingId)

  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const found = request.method() === 'GET' && url.pathname === '/api/session'
    if (found) await sessionReleased
    await route.fulfill({
      status: found ? 200 : 404,
      contentType: 'application/json',
      body: JSON.stringify(found ? { intake: INTAKE } : {
        error: { error_code: 'NOT_FOUND', message: 'not found', retryable: false },
      }),
      headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
    })
  })

  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Connecting backend')
  await expect(page.getByRole('tab', { name: 'Author', exact: true })).toBeDisabled()
  releaseSession()
  await expect(page.getByTestId('operator-phase')).toContainText('Backend ready')
  await expect(page.getByRole('tab', { name: 'Author', exact: true })).toBeEnabled()
  await expect(page.locator('.tc-bar-proj')).toHaveText(drawingId)
  await page.reload()
  await expect(page.getByTestId('operator-phase')).toContainText('Backend ready')
  await expect(page.getByRole('tab', { name: 'Author', exact: true })).toBeEnabled()
  await expect(page.locator('.tc-bar-proj')).toHaveText(drawingId)
})

test('live surface starts empty and reports the real Claude grant gate', async ({ page }) => {
  let messageBody = null
  let drawingId = null

  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    let status = 200
    let body = {}

    if (request.method() === 'GET' && url.pathname === '/api/session') {
      expect(url.searchParams.get('dwg')).toBe('rooftop_demo')
      body = { intake: INTAKE }
    } else if (request.method() === 'GET' && url.pathname === '/api/tenant/claude-grant') {
      body = { linked: false, linked_at: null, kind: null }
    } else if (request.method() === 'POST' && url.pathname === '/api/sessions') {
      drawingId = request.postDataJSON().drawing_id
      expect(drawingId).toMatch(/^cat-workbench-[0-9a-z-]+$/)
      body = { session_id: 'live-session', status: 'active', created_at: '2026-07-25T00:00:00Z' }
    } else if (request.method() === 'POST' && url.pathname === '/api/sessions/live-session/messages') {
      messageBody = request.postDataJSON()
      status = 401
      body = {
        grant_required: true,
        error: { error_code: 'GRANT_REQUIRED', message: 'sign in with Claude', retryable: false },
        degraded_mode: false,
      }
    } else {
      status = 404
      body = { error: { error_code: 'NOT_FOUND', message: 'not found', retryable: false } }
    }

    await route.fulfill({
      status,
      contentType: 'application/json',
      body: JSON.stringify(body),
      headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
    })
  })

  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText('Backend ready')
  await expect(page.getByRole('textbox', { name: 'Command bar' })).toHaveValue('')
  await expect(page.getByText('Live services')).toBeVisible()
  await expect(page.getByText('Requests are not preloaded or simulated.')).toBeVisible()
  await expect(page.getByText('Deterministic browser proof.')).toHaveCount(0)
  const workbench = await page.locator('.tc-bar-proj').textContent()
  await page.reload()
  await expect(page.locator('.tc-bar-proj')).toHaveText(workbench)
  await expect(page.getByRole('textbox', { name: 'Command bar' })).toHaveValue('')

  const request = 'Rearrange these panels into a sitting cat.'
  await page.getByRole('textbox', { name: 'Command bar' }).fill(request)
  await page.getByRole('button', { name: 'Run', exact: true }).click()

  await expect(page.getByText('Link a Claude account to plan this request. Nothing has run.')).toBeVisible()
  await expect(page.getByRole('dialog', { name: 'Claude account' })).toBeVisible()
  expect(drawingId).not.toBe('cat-workbench')
  expect(messageBody).toEqual({ text: request })
})
