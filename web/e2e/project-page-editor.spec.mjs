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
const NATIVE_PATH = `/api/projects/${PROJECT_ID}/native-previews`
const NATIVE_JOB = '77777777-7777-4777-8777-777777777777'
const CORS = { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*', 'access-control-allow-methods': '*' }
const canonical = document => JSON.stringify(Object.fromEntries(Object.entries(document).sort(([a], [b]) => a.localeCompare(b))))
const digest = document => createHash('sha256').update(canonical(document)).digest('hex')
// Synthetic portrait with a visible border and color bands exposes clipping in proof screenshots.
// No user photo or public photo endpoint is used by this fixture.
const PREVIEW = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAHgAAAC0CAIAAADQLH9KAAABcElEQVR4nO3QMQ3CUAAA0SqpCGbmikAJS0Uwo4m5StiR0OVDm+MlJ+Dypvly1Q+aDj/4k0CDbgX6IOjXfdGQQINuBRp0K9CgW4EG3Qo06FagQbcCDboVaNCtQINuBRp0K9CgW4EG3Qo06FagQbcCDboVaNCtQINuBRp0K9CgW4EG3Qo06FagQbcCDboVaNCtQINuBRp0K9CgW+1Av7enhgQadCvQoFuBBt0KNOhWoEG3Ag26FWjQrUCDbgUadCvQoFuBBt0KNOhWoEG3Ag26FWjQrUCDbgUadCvQoFuBBt0KNOhWoEG3Ag26FWjQrUCDbgUadCvQoFuBBt0KNOhWoEG3An0O6Ntj1ZBAg24FGnQr0KBbgQbdCjToVqBBtwINuhVo0K1Ag24FGnQr0KBbgQbdCjToVqBBtwINuhVo0K1Ag24FGnQr0KBbgQbdCjToVqBBtwINuhVo0K1Ag24FGnQr0KBb7UDrS4EG3Qo06FYfk1jcNsQZ95cAAAAASUVORK5CYII=', 'base64')
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
  const backend = { calls: [], foreign: [], revisions: [], downloads: [], photoCalls: [], nativeCalls: [], nativeStatuses: [], nativeJob: null }
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
    if (url.pathname.startsWith(NATIVE_PATH)) {
      const headers = await request.allHeaders()
      const call = { method, path: url.pathname, body, idempotencyKey: headers['idempotency-key'] || null,
        authenticated: headers.authorization === `Bearer ${TOKEN}`, org: headers['x-org-id'] }
      backend.nativeCalls.push(call)
      if (method === 'POST' && url.pathname === NATIVE_PATH) {
        const saved = backend.revisions.find(row => row.revision === body?.revision)
        if (!saved || !call.idempotencyKey) reply = { status: 422, body: {} }
        else {
          const previews = Object.fromEntries(PHOTOS.map(photo => [photo.material_id, PREVIEW.toString('base64')]))
          backend.nativeJob ||= { job_id: NATIVE_JOB, revision: saved.revision, document_sha256: saved.digest,
            archive_sha256: createHash('sha256').update(sourceArchive(saved, previews, 'ios')).digest('hex') }
          reply = { status: 202, body: { ...backend.nativeJob, status: 'queued' } }
        }
      } else if (method === 'GET' && url.pathname === `${NATIVE_PATH}/${NATIVE_JOB}` && backend.nativeJob) {
        const status = backend.nativeStatuses.includes('running') ? 'complete' : 'running'
        reply = { status: 200, body: { ...backend.nativeJob, status,
          ...(status === 'complete' ? { receipt: { kind: 'simulator', receipt_id: PROJECT_ID, sha256: 'c'.repeat(64) }, evidence_basis: 'authenticated-worker-report' } : {}) } }
      } else if (method === 'GET' && url.pathname === `${NATIVE_PATH}/${NATIVE_JOB}/screenshot` && backend.nativeStatuses.includes('complete')) {
        call.status = 200
        return route.fulfill({ status: 200, headers: CORS, contentType: 'image/png', body: PREVIEW })
      } else reply = { status: 404, body: {} }
      call.status = reply.status
      if (reply.body.status) backend.nativeStatuses.push(reply.body.status)
    } else if (url.pathname.startsWith(PAGE_PATH)) {
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
  test(`large portfolio disclosures and reachable Save at ${size}`, async ({ page }, testInfo) => {
    test.setTimeout(120_000)
    const editorViewport = { ...viewport, height: size === 'desktop' ? 900 : 844 }
    await page.setViewportSize(editorViewport)
    const backend = await installBackend(page)
    const original = portfolioImport()
    original.schema = 'leaf.portfolio.v1'
    original.categories = [24, 24, 23, 23].map((length, ci) => ({
      slug: `gallery-${ci + 1}`, title: `Gallery ${ci + 1} selected pastry photographs`, description: `Collection ${ci + 1}`, hero: 0,
      images: Array.from({ length }, (_, ii) => ({ ...PHOTOS[0], material_id: `33333333-3333-4333-8333-${String(ci * 24 + ii).padStart(12, '0')}`,
        title: `Pastry ${ci + 1}-${ii + 1}`, alt: `Pastry photograph ${ci + 1}-${ii + 1}` })).map(({ name, ...image }) => image),
    }))
    backend.revisions.push({ revision: 7, digest: digest(original), document: structuredClone(original) })
    await page.goto('/app?surface=browser')
    let editor = await openProject(page)
    await editor.getByRole('button', { name: 'Open page', exact: true }).click()
    await expect(page.locator('[data-ground="browser-asset"] .portfolio').getByRole('heading', { name: original.headline })).toBeVisible()
    await page.getByRole('button', { name: 'Project', exact: true }).click()
    const sidebar = page.getByRole('region', { name: 'Project sidebar', exact: true })
    const save = editor.getByRole('button', { name: 'Save page', exact: true })
    const state = editor.locator('.project-page-editor__save-row').getByRole('status')
    const reachable = async () => {
      await expect(save).toHaveCount(1)
      await expect(state).toBeVisible()
      await expect.poll(() => save.evaluate(button => {
        const box = button.getBoundingClientRect(), panel = button.closest('.browser-project-panel').getBoundingClientRect()
        const row = button.closest('.project-page-editor__save-row').getBoundingClientRect()
        const heading = button.closest('.browser-project-panel').querySelector('.browser-panel-heading').getBoundingClientRect()
        const editor = button.closest('.project-page-editor').getBoundingClientRect()
        return box.height >= 44 && box.top >= heading.bottom - 1 && box.bottom <= panel.bottom
          && box.left >= panel.left && box.right <= panel.right && row.top >= editor.top && row.bottom <= editor.bottom
          && button.contains(document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2))
      })).toBe(true)
      expect(await sidebar.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true)
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
    }
    await editor.getByRole('textbox', { name: 'Name', exact: true }).scrollIntoViewIfNeeded()
    await reachable()
    await expect(editor.locator('details[open]')).toHaveCount(0)
    await expect(editor.getByRole('textbox', { name: /^Caption / })).toHaveCount(0)
    // Compactness is measured on the real editor, with all 94 saved references present.
    expect(await editor.evaluate(node => node.getBoundingClientRect().height)).toBeLessThan(2400)
    for (let ci = 0; ci < 4; ci++) {
      const summary = editor.locator('.portfolio-editor__category > details > summary').nth(ci)
      await summary.scrollIntoViewIfNeeded()
      await expect(summary).toHaveText(`${original.categories[ci].title} · ${original.categories[ci].images.length} photos`)
      await reachable()
    }
    await page.screenshot({ path: testInfo.outputPath(`portfolio-94-compact-${size}.png`) })
    const lastGallery = editor.getByRole('region', { name: 'Gallery 4', exact: true })
    const gallerySummary = lastGallery.locator(':scope > details > summary')
    await gallerySummary.focus()
    await page.keyboard.press('Enter')
    const lastPhoto = lastGallery.locator('summary').filter({ hasText: /^Photo 23: Pastry 4-23$/ })
    await lastPhoto.scrollIntoViewIfNeeded()
    await lastPhoto.focus()
    await page.keyboard.press('Space')
    const caption = lastGallery.getByRole('textbox', { name: 'Caption 23', exact: true })
    await expect(caption).toHaveValue('Pastry 4-23')
    await expect(lastGallery.getByRole('textbox', { name: 'Alt text 23', exact: true })).toHaveValue('Pastry photograph 4-23')
    await expect(lastGallery.getByRole('button', { name: 'Remove image 23', exact: true })).toBeVisible()
    await caption.focus()
    await expect(caption).toBeFocused()
    await expect.poll(() => caption.evaluate(input => input.getBoundingClientRect().top >= input.closest('.project-page-editor').querySelector('.project-page-editor__save-row').getBoundingClientRect().bottom)).toBe(true)
    await reachable()
    expect(backend.calls.filter(call => call.method === 'PUT')).toEqual([])
    expect(backend.nativeCalls).toEqual([])
    const photoReads = backend.photoCalls.length
    await lastPhoto.click(); await lastPhoto.click()
    expect(backend.photoCalls).toHaveLength(photoReads)
    await caption.fill('Final pastry caption')
    await lastGallery.getByRole('combobox', { name: 'Hero image', exact: true }).selectOption('22')
    await caption.scrollIntoViewIfNeeded()
    await reachable()
    await page.screenshot({ path: testInfo.outputPath(`portfolio-94-last-photo-save-${size}.png`) })
    await editor.getByRole('button', { name: 'Add resume section', exact: true }).scrollIntoViewIfNeeded()
    await reachable()
    await editor.evaluate(node => node.lastElementChild.scrollIntoView({ block: 'end' }))
    await reachable()
    await save.click()
    await expect(state).toHaveText('Saved')
    const expected = structuredClone(original)
    expected.categories[3].images[22].title = 'Final pastry caption'; expected.categories[3].hero = 22
    const puts = backend.calls.filter(call => call.method === 'PUT')
    expect(puts).toHaveLength(1)
    expect(puts[0].body).toEqual({ expected_revision: 7, document: expected })
    expect(backend.revisions.at(-1).document).toEqual(expected)
    await editor.getByRole('button', { name: 'Reload saved page', exact: true }).click()
    await expect(editor.locator('details[open]')).toHaveCount(0)
    await editor.getByRole('region', { name: 'Gallery 4', exact: true }).locator(':scope > details > summary').click()
    await editor.locator('summary').filter({ hasText: /^Photo 23: Final pastry caption$/ }).click()
    await expect(editor.getByRole('textbox', { name: 'Caption 23', exact: true })).toHaveValue('Final pastry caption')
    expect(backend.calls.filter(call => call.path === PAGE_PATH && call.method === 'GET')).toHaveLength(2)
    expect(backend.foreign).toEqual([])
  })

  test(`native preview journey, ${size}${viewport.width}x${viewport.height}`, async ({ page }, testInfo) => {
    test.setTimeout(120_000)
    await page.setViewportSize(viewport)
    const backend = await installBackend(page)
    const document = portfolioImport()
    document.schema = 'leaf.portfolio.v1'
    document.categories[0].images = PHOTOS.map(photo => ({ material_id: photo.material_id, sha256: photo.sha256,
      preview_sha256: photo.preview_sha256, title: photo.name, alt: photo.name }))
    backend.revisions.push({ revision: 7, digest: digest(document), document })
    await page.goto('/app?surface=browser')
    const editor = await openProject(page)
    await editor.getByRole('button', { name: 'Open page', exact: true }).click()
    await expect(page.locator('[data-ground="browser-asset"] .portfolio').getByRole('heading', { name: 'Selected pastry work' })).toBeVisible()
    await page.getByRole('button', { name: 'Project', exact: true }).click()
    const native = editor.getByRole('region', { name: 'iOS preview', exact: true })
    const build = native.getByRole('button', { name: 'Build iOS preview', exact: true })
    await expect(build).toBeEnabled()
    await build.click()
    await expect(native.getByRole('status').filter({ hasText: 'Queued · Saved revision 7' })).toBeVisible()
    await expect(native.getByRole('status').filter({ hasText: 'Building · Saved revision 7' })).toBeVisible()
    await expect(native.getByRole('status').filter({ hasText: 'Ready · Saved revision 7' })).toBeVisible({ timeout: 20_000 })
    const image = native.getByRole('img', { name: 'iOS simulator preview of saved portfolio revision 7', exact: true })
    await image.scrollIntoViewIfNeeded()
    await expect.poll(() => image.evaluate(node => node.complete && node.naturalWidth > 0)).toBe(true)
    expect(await image.getAttribute('src')).toMatch(/^blob:/)
    const posts = backend.nativeCalls.filter(call => call.method === 'POST')
    expect(posts).toHaveLength(1)
    expect(posts[0]).toMatchObject({ path: NATIVE_PATH, body: { revision: 7 }, authenticated: true, org: ORG })
    expect(posts[0].body).toEqual({ revision: 7 })
    expect(posts[0].idempotencyKey).toMatch(/^[\x21-\x7e]{1,200}$/)
    expect(backend.nativeStatuses).toEqual(['queued', 'running', 'complete'])
    expect(backend.nativeCalls.filter(call => call.path.endsWith('/screenshot'))).toEqual([
      expect.objectContaining({ method: 'GET', authenticated: true, status: 200 }),
    ])
    for (const call of backend.nativeCalls) expect(call.authenticated).toBe(true)
    const geometry = await native.evaluate(node => ({
      viewport: { width: innerWidth, height: innerHeight }, pageWidth: document.documentElement.scrollWidth,
      region: { width: node.clientWidth, scrollWidth: node.scrollWidth },
      image: { width: node.querySelector('img').getBoundingClientRect().width, height: node.querySelector('img').getBoundingClientRect().height },
    }))
    expect(geometry.pageWidth).toBeLessThanOrEqual(viewport.width)
    expect(geometry.region.scrollWidth).toBeLessThanOrEqual(geometry.region.width)
    expect(geometry.image.width).toBeLessThanOrEqual(geometry.region.width)
    expect(geometry.image.height).toBeLessThanOrEqual(viewport.height * .6 + 1)
    await testInfo.attach(`native-preview-${size}`, { body: await page.screenshot(), contentType: 'image/png' })
    await testInfo.attach(`native-preview-region-${size}`, { body: await native.screenshot(), contentType: 'image/png' })
    await editor.getByRole('textbox', { name: 'Name', exact: true }).fill('Unsaved portfolio edit')
    await expect(build).toBeDisabled()
    await expect(image).toHaveAttribute('alt', 'iOS simulator preview of saved portfolio revision 7')
    expect(backend.nativeCalls.filter(call => call.method === 'POST')).toHaveLength(1)
    await testInfo.attach(`native-preview-evidence-${size}`, { body: JSON.stringify({
      kind: 'rendered-ui-fixture-proof', workerAcceptance: false, savedRevision: 7,
      requests: backend.nativeCalls, statuses: backend.nativeStatuses, geometry, dirtyBuildDisabled: await build.isDisabled(),
    }, null, 2), contentType: 'application/json' })
    expect(backend.foreign).toEqual([])
  })

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
    await editor.locator('summary').filter({ hasText: /^Cakes · 2 photos$/ }).click()
    await editor.locator('summary').filter({ hasText: /^Photo 1: Cake.jpg$/ }).click()
    await editor.getByRole('textbox', { name: 'Caption 1', exact: true }).fill('Chocolate celebration')
    await editor.getByRole('combobox', { name: 'Hero image', exact: true }).selectOption('1')
    await editor.locator('summary').filter({ hasText: /^Photo 2: Bread.jpg$/ }).click()
    await editor.getByRole('button', { name: 'Move image 2 up', exact: true }).click()
    await editor.locator('summary').filter({ hasText: /^About \/ Contact$/ }).click()
    await editor.getByRole('textbox', { name: 'About', exact: true }).fill('Pastry chef. Cakes, breads and chocolates.')
    await editor.getByRole('button', { name: 'Save page', exact: true }).click()
    await expect(editor.getByRole('status').filter({ hasText: /^Saved$/ })).toBeVisible()
    expect(backend.revisions[0].document.categories[0]).toMatchObject({ hero: 0, images: [{ material_id: PHOTOS[1].material_id }, { material_id: PHOTOS[0].material_id, title: 'Chocolate celebration' }] })
    await page.getByRole('button', { name: 'Close Project', exact: true }).click()
    await expectInsideViewport(preview.getByRole('navigation'), viewport)
    await preview.getByRole('button', { name: 'Cakes', exact: true }).click()
    const imageTrigger = preview.getByRole('button', { name: 'View Cake.jpg', exact: true })
    await imageTrigger.click()
    const viewer = preview.getByRole('dialog')
    const closeImage = viewer.getByRole('button', { name: 'Close image', exact: true })
    const viewerPhoto = viewer.locator('img')
    await expect(viewer).toBeVisible()
    await expect(closeImage).toBeFocused()
    await expect.poll(() => viewerPhoto.evaluateAll(images => images.length === 1 && images[0].complete && images[0].naturalWidth > 0 && images[0].naturalHeight > 0)).toBe(true)
    await expect(viewerPhoto).toHaveCSS('object-fit', 'contain')
    // Measure the rendered chrome rather than assuming header or dock heights.
    await expect.poll(() => viewer.evaluate(node => {
      const bounds = node.getBoundingClientRect()
      const controls = document.querySelector('.environment-strip').getBoundingClientRect()
      const dock = document.querySelector('.bar-dock').getBoundingClientRect()
      const image = node.querySelector('img').getBoundingClientRect()
      const close = node.querySelector('button').getBoundingClientRect()
      return bounds.top >= controls.bottom - 1 && bounds.bottom <= dock.top + 1
        && bounds.left >= 0 && bounds.right <= document.documentElement.clientWidth
        && close.top >= bounds.top && close.bottom <= image.top
        && image.left >= bounds.left && image.right <= bounds.right
        && image.top >= bounds.top && image.bottom <= bounds.bottom
        && image.width > 0 && image.height > 0
    })).toBe(true)
    expect(await closeImage.evaluate(button => {
      const bounds = button.getBoundingClientRect()
      return button.contains(document.elementFromPoint(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2))
    })).toBe(true)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
    await page.screenshot({ path: testInfo.outputPath(`portfolio-viewer-${size}.png`) })
    // Scrolling the bounded viewer must leave its close control usable.
    await viewer.evaluate(node => { node.scrollTop = node.scrollHeight })
    expect(await closeImage.evaluate(button => {
      const bounds = button.getBoundingClientRect()
      return button.contains(document.elementFromPoint(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2))
    })).toBe(true)
    await closeImage.click()
    await expect(viewer).toHaveCount(0)
    await expect(imageTrigger).toBeFocused()
    await imageTrigger.click()
    await expect(viewer).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(viewer).toHaveCount(0)
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
    // The docked preview scrolls within the workspace; load each lazy photo
    // through its visible viewport before asserting the completed gallery.
    const photos = preview.locator('.portfolio-photo')
    await expect(photos).toHaveCount(2)
    for (const photo of await photos.all()) {
      await photo.scrollIntoViewIfNeeded()
      await expect.poll(() => photo.locator('img').evaluateAll(images => images.length === 1 && images[0].complete && images[0].naturalWidth > 0)).toBe(true)
    }
    await preview.getByRole('navigation').scrollIntoViewIfNeeded()
    await expect.poll(() => preview.locator('img').evaluateAll(images => images.length === 2 && images.every(image => image.complete && image.naturalWidth > 0))).toBe(true)
    await page.screenshot({ path: testInfo.outputPath(`portfolio-gallery-${size}.png`) })
    expect(await preview.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true)
    await page.reload()
    editor = await openProject(page)
    await editor.getByRole('button', { name: 'Open page', exact: true }).click()
    await expect(preview.getByRole('heading', { name: 'Selected pastry work' })).toBeVisible()
    await page.getByRole('button', { name: 'Project', exact: true }).click()
    await editor.locator('summary').filter({ hasText: /^Cakes · 2 photos$/ }).click()
    await editor.locator('summary').filter({ hasText: /^Photo 2: Chocolate celebration$/ }).click()
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
