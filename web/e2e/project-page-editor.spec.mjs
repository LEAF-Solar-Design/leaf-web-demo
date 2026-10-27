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

function sourceArchive(saved) {
  const producer = fileURLToPath(new URL('../../platform/web_page_template.py', import.meta.url))
  const result = spawnSync(process.platform === 'win32' ? 'python' : 'python3', ['-c', [
    'import importlib.util,json,sys',
    'spec=importlib.util.spec_from_file_location("page_template",sys.argv[1])',
    'module=importlib.util.module_from_spec(spec)',
    'spec.loader.exec_module(module)',
    'saved=json.load(sys.stdin)',
    'sys.stdout.buffer.write(module.source_archive(saved["document"],saved["revision"]))',
  ].join(';'), producer], { input: JSON.stringify(saved), timeout: 20_000, maxBuffer: 8 * 1024 * 1024 })
  if (result.error || result.status !== 0) throw new Error(`Page source fixture producer failed: ${result.error?.message || result.stderr.toString()}`)
  return result.stdout
}

async function installBackend(page) {
  const state = makeCatProofState()
  const backend = { calls: [], foreign: [], revisions: [], downloads: [] }
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
          const bytes = sourceArchive(saved)
          backend.downloads.push({ revision: saved.revision, bytes })
          return route.fulfill({ status: 200, headers: CORS, contentType: 'application/zip', body: bytes })
        }
      }
      reply ||= { status: 404, body: { error: 'unexpected_page_route' } }
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
