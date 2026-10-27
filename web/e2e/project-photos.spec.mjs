import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

const PROJECT_ID = '5a1f0c3e-2b7d-4c8a-9e61-0d4f7b2a9c11'
const MATERIAL_ID = '7c2e9a4b-1d3f-4e5a-8b6c-9d0e1f2a3b4c'
const PROJECT = { project_id: PROJECT_ID, name: 'Photo intake proof', status: 'active' }
const TOKEN = `e30.${Buffer.from(JSON.stringify({ sub: 'photo-proof', iss: 'proof' })).toString('base64url')}.proof`
const CORS = { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*', 'access-control-allow-methods': '*' }
const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aXioAAAAASUVORK5CYII=', 'base64')

test('project photos upload through Material and fit the phone panel', async ({ page }, testInfo) => {
  const state = makeCatProofState()
  const photos = []
  const uploaded = []
  await page.addInitScript(({ token }) => {
    localStorage.setItem('leaf.jwt', token)
    localStorage.setItem('leaf.org_id', 'cat-proof-org')
  }, { token: TOKEN })
  await page.route(url => /^https?:$/.test(url.protocol) && !['127.0.0.1', 'leaf-proof.invalid'].includes(url.hostname), route => route.abort())
  await page.route(url => url.hostname === 'leaf-proof.invalid', async route => {
    const request = route.request()
    const url = new URL(request.url())
    if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: CORS })
    let body
    try { body = request.postDataJSON() } catch { body = {} }
    let reply
    if (url.pathname === '/api/projects') reply = { status: 200, body: { projects: [PROJECT] } }
    else if (url.pathname === `/api/projects/${PROJECT_ID}`) reply = { status: 200, body: { project: PROJECT, drawing_versions: [], jobs: [], built_tools: [], drawing_artifacts: [] } }
    else if (url.pathname === `/api/projects/${PROJECT_ID}/materials`) reply = { status: 200, body: { materials: photos, next_cursor: null } }
    else if (url.pathname.endsWith('/materials/images') && request.method() === 'POST') {
      expect((await request.allHeaders()).authorization).toBe(`Bearer ${TOKEN}`)
      uploaded.push(request.postDataBuffer())
      const material = { material_id: MATERIAL_ID, name: url.searchParams.get('filename'), media_type: 'image/png', width: 1, height: 1 }
      photos.push(material)
      reply = { status: 201, body: { material } }
    } else if (url.pathname.endsWith(`/materials/${MATERIAL_ID}/content`)) {
      expect((await request.allHeaders()).authorization).toBe(`Bearer ${TOKEN}`)
      return route.fulfill({ status: 200, contentType: 'image/png', body: PNG, headers: CORS })
    } else reply = catProofResponse({ method: request.method(), path: url.pathname, body: body || {}, query: Object.fromEntries(url.searchParams) }, state)
    return route.fulfill({ status: reply.status, contentType: 'application/json', body: JSON.stringify(reply.body), headers: CORS })
  })
  await page.goto('/app?surface=browser')
  await page.getByRole('button', { name: 'Open project or material', exact: true }).click()
  const sidebar = page.getByRole('region', { name: 'Project sidebar', exact: true })
  await sidebar.getByRole('button', { name: PROJECT.name, exact: true }).click()
  await page.getByRole('button', { name: 'Open project or material', exact: true }).click()
  await sidebar.getByRole('button', { name: 'Photos', exact: true }).click()
  const panel = sidebar.getByRole('region', { name: 'Project photos', exact: true })
  await expect(panel).toBeVisible()
  await panel.getByLabel('Photo files').setInputFiles({ name: 'Original cake photo.png', mimeType: 'image/png', buffer: PNG })
  await expect(panel.getByText('1 photo ready.', { exact: true })).toBeVisible()
  await expect(panel.getByAltText('Original cake photo.png')).toBeVisible()
  expect(uploaded).toHaveLength(1)
  expect(uploaded[0].equals(PNG)).toBe(true)
  await page.setViewportSize({ width: 390, height: 844 })
  await panel.getByRole('button', { name: 'Add photos' }).scrollIntoViewIfNeeded()
  const box = await panel.boundingBox()
  expect(box.x).toBeGreaterThanOrEqual(0)
  expect(box.x + box.width).toBeLessThanOrEqual(390)
  await page.screenshot({ path: testInfo.outputPath('photos-phone.png') })
})
