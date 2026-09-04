import { expect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

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
    if (found) expect(url.searchParams.get('dwg')).toBe(drawingId)
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
  await expect(page.getByTestId('version-head')).toHaveText('No version')
  await expect(page.getByRole('tab', { name: 'Author', exact: true })).toBeDisabled()
  releaseSession()
  await expect(page.getByTestId('operator-phase')).toContainText('Backend ready')
  await expect(page.getByTestId('version-head')).toHaveText('Version 1')
  await expect(page.getByRole('tab', { name: 'Author', exact: true })).toBeEnabled()
  await expect(page.locator('.tc-bar-proj')).toHaveText(drawingId)
  await page.reload()
  await expect(page.getByTestId('operator-phase')).toContainText('Backend ready')
  await expect(page.getByRole('tab', { name: 'Author', exact: true })).toBeEnabled()
  await expect(page.locator('.tc-bar-proj')).toHaveText(drawingId)
})

test('live surface stays empty until upload and binds every controller to the uploaded drawing', async ({ page }) => {
  const state = makeCatProofState()
  const calls = []
  const conversationBodies = []
  await page.addInitScript(() => localStorage.setItem('leaf.jwt', 'fixture-token'))

  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const body = request.postData() && request.headers()['content-type']?.includes('application/json')
      ? request.postDataJSON()
      : {}
    calls.push({ method: request.method(), path: url.pathname, body })
    if (request.method() === 'POST' && url.pathname === '/api/sessions') conversationBodies.push(body)
    const result = catProofResponse({
      method: request.method(),
      path: url.pathname,
      body,
      query: Object.fromEntries(url.searchParams),
    }, state)

    await route.fulfill({
      status: result.status,
      contentType: 'application/json',
      body: JSON.stringify(result.body || {}),
      headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
    })
  })

  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toHaveText(/No drawing yet/)
  await expect(page.getByTestId('version-head')).toHaveText('No version')
  await expect(page.locator('.tc-bar-proj')).toHaveText('No drawing')
  await expect(page.getByLabel('Command bar', { exact: true })).toHaveValue('')
  await expect(page.getByText('Live services')).toBeVisible()
  await expect(page.getByText('Requests are not preloaded or simulated.')).toBeVisible()
  await expect(page.getByText('Deterministic browser proof.')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Run', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Take edit lock' })).toBeDisabled()
  await expect(page.getByRole('tab', { name: /Versions/ })).toBeDisabled()
  expect(calls.filter((call) => call.path === '/api/session')).toEqual([])
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem('leaf.cat.workbench.id.v1'))).toBeNull()

  await page.getByLabel('Drawing file').setInputFiles({
    name: 'customer-roof.dxf',
    mimeType: 'application/dxf',
    buffer: Buffer.from('0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n'),
  })
  await expect(page.locator('.drawing-upload')).toContainText('Drawing ready')
  await expect(page.getByTestId('operator-phase')).toContainText('Backend ready')
  await expect(page.getByTestId('version-head')).toHaveText('Version 1')
  await expect(page.locator('.tc-bar-proj')).toHaveText('uploaded-cat')
  await expect(page.getByRole('button', { name: 'Run', exact: true })).toBeEnabled()
  await expect(page.getByRole('button', { name: 'Take edit lock' })).toBeEnabled()
  await expect(page.getByRole('tab', { name: /Versions/ })).toBeEnabled()
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem('leaf.cat.workbench.id.v1'))).toBe('uploaded-cat')
  expect(calls.filter((call) => call.path === '/api/session')).toEqual([])

  await page.getByRole('button', { name: 'Take edit lock' }).click()
  await expect.poll(() => calls.some((call) => call.method === 'POST' && call.path === '/api/drawings/uploaded-cat/checkout')).toBe(true)
  await page.getByLabel('Command bar', { exact: true }).fill('Inspect this uploaded drawing.')
  await page.getByRole('button', { name: 'Run', exact: true }).click()
  await expect.poll(() => conversationBodies.length).toBe(1)
  expect(conversationBodies[0]).toMatchObject({ drawing_id: 'uploaded-cat' })
})

test('a replacement upload starts a conversation scoped to the replacement drawing', async ({ page }) => {
  const state = makeCatProofState()
  const drawingIds = ['uploaded-a', 'uploaded-b']
  const sessionBodies = []
  const messagePaths = []
  let uploadCount = 0
  await page.addInitScript(() => localStorage.setItem('leaf.jwt', 'fixture-token'))

  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const body = request.postData() && request.headers()['content-type']?.includes('application/json')
      ? request.postDataJSON()
      : {}

    if (request.method() === 'POST' && url.pathname === '/api/sessions') {
      sessionBodies.push(body)
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ session_id: `session-${body.drawing_id}`, status: 'idle', created_at: '2026-07-24T12:00:00Z' }),
        headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
      })
      return
    }
    if (request.method() === 'POST' && /^\/api\/sessions\/session-uploaded-[ab]\/messages$/.test(url.pathname)) {
      messagePaths.push(url.pathname)
      await route.fulfill({
        status: 400,
        contentType: 'application/json',
        body: JSON.stringify({ error: { error_code: 'TEST_STOP', message: 'message recorded', retryable: false } }),
        headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
      })
      return
    }

    let fixturePath = url.pathname
    const requestedDrawing = drawingIds.find((drawingId) => fixturePath.includes(drawingId))
    if (requestedDrawing) fixturePath = fixturePath.replace(requestedDrawing, 'uploaded-cat')
    const result = catProofResponse({
      method: request.method(),
      path: fixturePath,
      body,
      query: Object.fromEntries(url.searchParams),
    }, state)
    let responseBody = result.body
    if (request.method() === 'POST' && url.pathname === '/api/drawings/upload') {
      responseBody = { ...responseBody, drawing_id: drawingIds[uploadCount] }
      uploadCount += 1
    } else if (requestedDrawing && responseBody) {
      responseBody = JSON.parse(JSON.stringify(responseBody).replaceAll('uploaded-cat', requestedDrawing))
    }
    await route.fulfill({
      status: result.status,
      contentType: 'application/json',
      body: JSON.stringify(responseBody || {}),
      headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
    })
  })

  await page.goto('/try')
  const upload = async (name, drawingId) => {
    if (!(await page.getByLabel('Drawing file').isVisible())) {
      await page.getByRole('tab', { name: 'Operator', exact: true }).click()
    }
    await page.getByLabel('Drawing file').setInputFiles({
      name,
      mimeType: 'application/dxf',
      buffer: Buffer.from('0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n'),
    })
    await expect(page.locator('.tc-bar-proj')).toHaveText(drawingId)
  }
  const send = async (text) => {
    await page.getByLabel('Command bar', { exact: true }).fill(text)
    await page.getByRole('button', { name: 'Run', exact: true }).click()
  }

  await upload('drawing-a.dxf', 'uploaded-a')
  await send('Inspect drawing A.')
  await expect.poll(() => messagePaths).toEqual(['/api/sessions/session-uploaded-a/messages'])

  await upload('drawing-b.dxf', 'uploaded-b')
  await send('Inspect drawing B.')
  await expect.poll(() => sessionBodies.map((body) => body.drawing_id)).toEqual(['uploaded-a', 'uploaded-b'])
  await expect.poll(() => messagePaths).toEqual([
    '/api/sessions/session-uploaded-a/messages',
    '/api/sessions/session-uploaded-b/messages',
  ])
})
