import { createHash } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

// Real rendered Studio, fixture-backed page persistence. Export bytes come from
// the production Python template; this suite does not prove a live API or build.
const API_HOST = 'leaf-proof.invalid'
const TOKEN = `proof.${Buffer.from(JSON.stringify({ iss: 'https://page-proof.invalid/', sub: 'page-proof-user' })).toString('base64url')}.fixture`
const ORG = 'cat-proof-org'
const PROJECT_ID = '5a1f0c3e-2b7d-4c8a-9e61-0d4f7b2a9c11'
const PROJECT = { project_id: PROJECT_ID, name: 'Jessica page proof', status: 'active' }
const PAGE_PATH = `/api/projects/${PROJECT_ID}/page`
const CORS = { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*', 'access-control-allow-methods': '*' }
const canonical = document => JSON.stringify(Object.fromEntries(Object.entries(document).sort(([a], [b]) => a.localeCompare(b))))
const digest = document => createHash('sha256').update(canonical(document)).digest('hex')
// In-memory image fixture; no public photo endpoint is used by the preview.
const PREVIEW = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aXioAAAAASUVORK5CYII=', 'base64')
const PHOTOS = ['33333333-3333-4333-8333-333333333333', '44444444-4444-4444-8444-444444444444'].map((material_id, index) => ({
  material_id, name: index ? 'Bread.jpg' : 'Cake.jpg', sha256: createHash('sha256').update(`original-${index}`).digest('hex'),
  preview_sha256: createHash('sha256').update(PREVIEW).digest('hex'),
}))
const portfolioImport = () => ({ schema: 'leaf.portfolio.import.v1', name: 'Jessica Sargent', headline: 'Selected pastry work',
  introduction: 'Cakes and breads, made with care.', about: 'A pastry chef and baker.', contact: { email: 'hello@example.com', location: 'Portland' },
  resume: [{ heading: 'Experience', entries: [{ title: 'Pastry chef', subtitle: 'Kitchen', dates: '2024 to 2026', description: 'Daily baking.' }] }], theme: 'light',
  categories: [{ slug: 'cakes', title: 'Cakes', description: 'Celebration bakes', hero: 0, images: PHOTOS.map(photo => ({ source_sha256: photo.sha256, title: photo.name, alt: photo.name })) }],
})

function sourceArchive(saved, previews = {}, target = 'web') {
  const producer = fileURLToPath(new URL(target === 'ios' ? '../../platform/portfolio_ios_template.py' : '../../platform/web_page_template.py', import.meta.url))
  const result = spawnSync(process.platform === 'win32' ? 'python' : 'python3', ['-c', [
    'import base64,importlib.util,json,sys',
    'spec=importlib.util.spec_from_file_location("page_template",sys.argv[1])',
    'module=importlib.util.module_from_spec(spec)',
    'spec.loader.exec_module(module)',
    'payload=json.load(sys.stdin)',
    'saved=payload["saved"]',
    'previews={mid:base64.b64decode(data) for mid,data in payload["previews"].items()}',
    'sys.stdout.buffer.write(module.source_archive(saved["document"],saved["revision"],previews))',
  ].join(';'), producer], { input: JSON.stringify({ saved, previews }), timeout: 20_000, maxBuffer: 80 * 1024 * 1024 })
  if (result.error || result.status !== 0) throw new Error(`Page source fixture producer failed: ${result.error?.message || result.stderr.toString()}`)
  return result.stdout
}

async function installBackend(page) {
  const state = makeCatProofState()
  const backend = { calls: [], foreign: [], revisions: [], downloads: [], photoCalls: [] }
  await page.addInitScript(({ token, org }) => {
    localStorage.setItem('leaf.jwt', token)
    localStorage.setItem('leaf.org_id', org)
  }, { token: TOKEN, org: ORG })
  await page.route(url => /^https?:$/.test(url.protocol) && !['127.0.0.1', API_HOST].includes(url.hostname), route => {
    backend.foreign.push(route.request().url())
    return route.abort()
  })
  await page.route(url => url.hostname === API_HOST, async route => {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()
    if (method === 'OPTIONS') return route.fulfill({ status: 204, headers: CORS })
    const body = request.postData() ? request.postDataJSON() : null
    const query = Object.fromEntries(url.searchParams)
    let reply
    if (url.pathname.startsWith(PAGE_PATH)) {
      backend.calls.push({ method, path: url.pathname, query, body, headers: await request.allHeaders() })
      const current = backend.revisions.at(-1) || null
      if (url.pathname === PAGE_PATH && method === 'GET') reply = { status: 200, body: { page: current } }
      else if (url.pathname === PAGE_PATH && method === 'PUT') {
        const nextDigest = digest(body.document)
        if (current?.digest === nextDigest && body.expected_revision === current.revision - 1) {
          reply = { status: 200, body: { page: current } }
        } else if (body.expected_revision !== (current?.revision || 0)) {
          reply = { status: 409, body: { error: 'page_conflict' } }
        } else {
          const saved = { revision: (current?.revision || 0) + 1, digest: nextDigest, document: structuredClone(body.document) }
          backend.revisions.push(saved)
          reply = { status: 200, body: { page: saved } }
        }
      } else if (url.pathname === `${PAGE_PATH}/source` && method === 'GET') {
        const saved = backend.revisions.find(row => String(row.revision) === query.revision)
        if (!saved) reply = { status: 404, body: { error: 'revision_not_found' } }
        else {
          const previews = saved.document.schema ? Object.fromEntries(PHOTOS.map(photo => [photo.material_id, PREVIEW.toString('base64')])) : {}
          const bytes = sourceArchive(saved, previews, query.target)
          backend.downloads.push({ revision: saved.revision, target: query.target || 'web', bytes })
          return route.fulfill({ status: 200, headers: CORS, contentType: 'application/zip', body: bytes })
        }
      }
      reply ||= { status: 404, body: { error: 'unexpected_page_route' } }
    } else if (method === 'GET' && url.pathname.startsWith(`/api/projects/${PROJECT_ID}/materials`)) {
      backend.photoCalls.push({ path: url.pathname, query, headers: await request.allHeaders() })
      if (url.pathname.endsWith('/content')) return route.fulfill({ status: 200, headers: CORS, contentType: 'image/png', body: PREVIEW })
      reply = { status: 200, body: query.cursor ? { materials: [PHOTOS[1]] } : { materials: [PHOTOS[0]], next_cursor: 'second-page' } }
    } else if (method === 'GET' && url.pathname === '/api/projects') {
      reply = { status: 200, body: { projects: [PROJECT] } }
    } else if (method === 'GET' && url.pathname === `/api/projects/${PROJECT_ID}`) {
      reply = { status: 200, body: { project: PROJECT, drawing_versions: [], jobs: [], built_tools: [], drawing_artifacts: [] } }
    } else if (method === 'GET' && url.pathname === '/api/campaigns') {
      reply = { status: 200, body: { campaigns: [] } }
    } else reply = catProofResponse({ method, path: url.pathname, body: body || {}, query }, state)
    return route.fulfill({ status: reply.status, headers: CORS, contentType: 'application/json',
      body: reply.body == null ? '' : JSON.stringify(reply.body) })
  })
  return backend
}

async function openProject(page) {
  await page.getByRole('button', { name: 'Open project or material', exact: true }).click()
  const sidebar = page.getByRole('region', { name: 'Project sidebar', exact: true })
  const projects = sidebar.getByRole('region', { name: 'Workspace projects', exact: true })
  await projects.getByRole('button', { name: PROJECT.name, exact: true }).click({ timeout: 30_000 })
  await page.getByRole('button', { name: 'Open project or material', exact: true }).click()
  const editor = sidebar.getByRole('region', { name: 'Page editor', exact: true })
  await expect(editor).toBeVisible()
  return editor
}

async function expectInsideViewport(locator, viewport) {
  await expect(locator).toBeVisible()
  const bounds = await locator.boundingBox()
  expect(bounds.x).toBeGreaterThanOrEqual(0)
  expect(bounds.y).toBeGreaterThanOrEqual(0)
  expect(bounds.width).toBeGreaterThan(100)
  expect(bounds.x + bounds.width).toBeLessThanOrEqual(viewport.width + 1)
  expect(bounds.y + bounds.height).toBeLessThanOrEqual(viewport.height + 1)
}

for (const [size, viewport] of [['desktop', { width: 1440, height: 1000 }], ['phone', { width: 390, height: 844 }]]) {
  test(`portfolio import, galleries, resume, private previews and export at ${size}`, async ({ page }, testInfo) => {
    test.setTimeout(120_000)
    page.setDefaultTimeout(15_000)
    await page.setViewportSize(viewport)
    const backend = await installBackend(page)
    await page.goto('/app?surface=browser')
    let editor = await openProject(page)
    await editor.getByLabel('Import portfolio content').setInputFiles({ name: 'portfolio.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(portfolioImport())) })
    const preview = page.locator('[data-ground="browser-asset"] .portfolio')
    await expect(preview.getByRole('heading', { name: 'Selected pastry work' })).toBeVisible()
    await expect(preview.locator('img').first()).toBeVisible()
    await expect.poll(() => preview.locator('img').first().evaluate(image => image.naturalWidth)).toBeGreaterThan(0)
    await page.getByRole('button', { name: 'Project', exact: true }).click()
    await editor.getByRole('textbox', { name: 'Caption 1', exact: true }).fill('Chocolate celebration')
    await editor.getByRole('combobox', { name: 'Hero image', exact: true }).selectOption('1')
    await editor.getByRole('button', { name: 'Move image 2 up', exact: true }).click()
    await editor.getByRole('textbox', { name: 'About', exact: true }).fill('Pastry chef. Cakes, breads and chocolates.')
    await editor.getByRole('button', { name: 'Save page', exact: true }).click()
    await expect(editor.getByRole('status').filter({ hasText: /^Saved$/ })).toBeVisible()
    expect(backend.revisions[0].document.categories[0]).toMatchObject({ hero: 0, images: [{ material_id: PHOTOS[1].material_id }, { material_id: PHOTOS[0].material_id, title: 'Chocolate celebration' }] })
    await page.getByRole('button', { name: 'Close Project', exact: true }).click()
    await expectInsideViewport(preview.getByRole('navigation'), viewport)
    await preview.getByRole('button', { name: 'Cakes', exact: true }).click()
    const imageTrigger = preview.getByRole('button', { name: 'View Cake.jpg', exact: true })
    await imageTrigger.click()
    await expect(preview.getByRole('dialog')).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(preview.getByRole('dialog')).toHaveCount(0)
    await expect(imageTrigger).toBeFocused()
    await preview.getByRole('button', { name: 'About / Contact', exact: true }).click()
    await expect(preview.getByText('Pastry chef. Cakes, breads and chocolates.', { exact: true })).toBeVisible()
    await expect(preview.getByText('hello@example.com', { exact: true })).toBeVisible()
    await preview.getByRole('button', { name: 'Resume', exact: true }).click()
    await expect(preview.getByRole('heading', { name: 'Pastry chef', exact: true })).toBeVisible()
    await page.getByRole('combobox', { name: 'Workspace profile', exact: true }).selectOption('cad')
    await expect(page.locator('.workspace-environment')).toHaveAttribute('data-profile', 'cad')
    await expect(preview).toBeHidden()
    await page.getByRole('combobox', { name: 'Workspace profile', exact: true }).selectOption('browser')
    await expect(page.locator('.workspace-environment')).toHaveAttribute('data-profile', 'browser')
    await expect(preview.getByRole('heading', { name: 'Pastry chef', exact: true })).toBeVisible()
    await preview.getByRole('button', { name: 'Cakes', exact: true }).click()
    await expect(page.locator('.environment-backdrop')).toHaveCSS('opacity', '0')
    await expect(page.locator('.environment-chooser')).toHaveCSS('opacity', '0')
    await expect.poll(() => preview.locator('img').evaluateAll(images => images.length === 2 && images.every(image => image.complete && image.naturalWidth > 0))).toBe(true)
    await page.screenshot({ path: testInfo.outputPath(`portfolio-gallery-${size}.png`) })
    expect(await preview.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true)
    await page.reload()
    editor = await openProject(page)
    await editor.getByRole('button', { name: 'Open page', exact: true }).click()
    await expect(preview.getByRole('heading', { name: 'Selected pastry work' })).toBeVisible()
    await page.getByRole('button', { name: 'Project', exact: true }).click()
    await expect(editor.getByRole('textbox', { name: 'Caption 2', exact: true })).toHaveValue('Chocolate celebration')
    const event = page.waitForEvent('download')
    await editor.getByRole('button', { name: 'Download Next.js source', exact: true }).click()
    const download = await event
    expect((await readFile(await download.path())).equals(backend.downloads[0].bytes)).toBe(true)
    expect(backend.downloads[0].revision).toBe(1)
    const nativeEvent = page.waitForEvent('download')
    await editor.getByRole('button', { name: 'Download native iOS source', exact: true }).click()
    const nativeDownload = await nativeEvent
    expect((await readFile(await nativeDownload.path())).equals(backend.downloads[1].bytes)).toBe(true)
    expect(backend.downloads[1]).toMatchObject({ revision: 1, target: 'ios' })
    for (const call of backend.photoCalls) expect(call.headers.authorization).toBe(`Bearer ${TOKEN}`)
    expect(backend.foreign).toEqual([])
    await page.evaluate(() => { localStorage.removeItem('leaf.jwt'); window.dispatchEvent(new StorageEvent('storage', { key: 'leaf.jwt' })) })
    await expect(preview).toHaveCount(0)
  })

  test(`project page create, edit, save, reload and source export at ${size}`, async ({ page }, testInfo) => {
    test.setTimeout(120_000)
    await page.setViewportSize(viewport)
    const backend = await installBackend(page)
    await page.goto('/app?surface=browser')
    let editor = await openProject(page)
    await editor.getByRole('button', { name: 'Create a page', exact: true }).click()
    const preview = page.locator('[data-ground="browser-asset"] .project-page')
    const editHeadline = preview.getByRole('button', { name: 'Edit headline', exact: true })
    await expect(editHeadline).toHaveText('Coming soon')
    await expect(page.getByRole('region', { name: 'Project sidebar', exact: true })).toBeHidden()
    await editHeadline.click()
    const headline = editor.getByRole('textbox', { name: 'Headline', exact: true })
    await expect(headline).toBeFocused()
    await expectInsideViewport(headline, viewport)
    await page.screenshot({ path: testInfo.outputPath(`page-inspector-${size}.png`) })
    await editor.getByRole('textbox', { name: 'Page label', exact: true }).fill('Jessica Sargent')
    await headline.fill('Something sweet is on the way.')
    await editor.getByRole('textbox', { name: 'Description', exact: true }).fill('Cakes, pastries, chocolates and breads.\nMade with care.')
    await editor.getByRole('textbox', { name: 'Status text', exact: true }).fill('Portfolio coming soon')
    await editor.getByRole('combobox', { name: 'Theme', exact: true }).selectOption('dark')
    await editor.getByRole('combobox', { name: 'Alignment', exact: true }).selectOption('left')
    const download = editor.getByRole('button', { name: 'Download Next.js source', exact: true })
    await expect(editor.getByRole('status').filter({ hasText: /^Unsaved changes$/ })).toBeVisible()
    await expect(download).toBeDisabled()
    expect(backend.downloads).toEqual([])
    await editor.getByRole('button', { name: 'Save page', exact: true }).click()
    await expect(editor.getByRole('status').filter({ hasText: /^Saved$/ })).toBeVisible()
    expect(backend.revisions).toHaveLength(1)
    expect(backend.revisions[0].document).toMatchObject({ label: 'Jessica Sargent', headline: 'Something sweet is on the way.', theme: 'dark', alignment: 'left' })
    await expect(download).toBeEnabled()
    await page.getByRole('button', { name: 'Close Project', exact: true }).click()
    await expect(preview).toHaveAttribute('data-theme', 'dark')
    await expect(preview).toHaveAttribute('data-alignment', 'left')
    await expectInsideViewport(preview, viewport)
    await page.screenshot({ path: testInfo.outputPath(`page-preview-${size}.png`) })

    const profiles = page.getByRole('combobox', { name: 'Workspace profile', exact: true })
    await profiles.selectOption('cad')
    await expect(page.locator('.workspace-environment')).toHaveAttribute('data-profile', 'cad')
    await expect(preview).toBeHidden()
    await profiles.selectOption('browser')
    await expect(page.locator('.workspace-environment')).toHaveAttribute('data-profile', 'browser')
    await expect(editHeadline).toHaveText('Something sweet is on the way.')
    await expect(preview).toBeVisible()
    expect(backend.revisions).toHaveLength(1)

    await page.reload()
    editor = await openProject(page)
    await expect(page.locator('[data-ground="browser-asset"] .project-page')).toHaveCount(0)
    await editor.getByRole('button', { name: 'Open page', exact: true }).click()
    await expect(editHeadline).toHaveText('Something sweet is on the way.')
    await editHeadline.click()
    await expect(editor.getByRole('textbox', { name: 'Headline', exact: true })).toBeFocused()
    await expect(editor.getByRole('status').filter({ hasText: /^Saved$/ })).toBeVisible()
    await editor.getByRole('textbox', { name: 'Status text', exact: true }).fill('More bakes are coming')
    await expect(editor.getByRole('button', { name: 'Download Next.js source', exact: true })).toBeDisabled()
    expect(backend.downloads).toEqual([])
    await editor.getByRole('button', { name: 'Save page', exact: true }).click()
    await expect(editor.getByRole('status').filter({ hasText: /^Saved$/ })).toBeVisible()
    const downloadEvent = page.waitForEvent('download')
    await editor.getByRole('button', { name: 'Download Next.js source', exact: true }).click()
    const archive = await downloadEvent
    expect(archive.suggestedFilename()).toBe('nextjs-page-r2.zip')
    const bytes = await readFile(await archive.path())
    expect(bytes.subarray(0, 4).toString('hex')).toBe('504b0304')
    expect(backend.downloads).toHaveLength(1)
    expect(backend.downloads[0].revision).toBe(2)
    expect(bytes.equals(backend.downloads[0].bytes)).toBe(true)
    expect(backend.calls.filter(call => call.method === 'PUT').map(call => call.body.expected_revision)).toEqual([0, 1])
    expect(backend.revisions.map(row => row.revision)).toEqual([1, 2])
    expect(backend.calls.filter(call => call.path.endsWith('/source')).map(call => call.query)).toEqual([{ revision: '2' }])
    for (const call of backend.calls) {
      expect(call.headers.authorization).toBe(`Bearer ${TOKEN}`)
      expect(call.headers['x-tenant-id']).toBeUndefined() // tenant derives from verified bearer
      expect(call.headers['x-org-id']).toBe(ORG)
      expect([PAGE_PATH, `${PAGE_PATH}/source`]).toContain(call.path)
    }
    expect(backend.foreign).toEqual([])
  })
}
