import { randomUUID } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import { test, expect } from './_harness.mjs'

test('creating a project from this drawing attaches its first drawing version', async ({ page, signedInUser }) => {
  const uploaded = await page.request.post('/api/drawings/upload', {
    headers: signedInUser.headers,
    multipart: { file: { name: 'pp3b-sizing-panels.dxf', mimeType: 'application/dxf',
      buffer: await readFile(new URL('../project/pp3b-sizing-panels.dxf', import.meta.url)) } },
    timeout: 60_000,
  })
  expect(uploaded.status()).toBe(202)
  const { drawing_id: drawingId } = await uploaded.json()
  expect(drawingId).toBeTruthy()
  await expect.poll(async () => {
    const response = await page.request.get(`/api/drawings/${drawingId}/upload-status`, { headers: signedInUser.headers })
    expect(response.ok()).toBe(true)
    const status = await response.json()
    expect(status.status).not.toBe('failed')
    return status.status
  }, { timeout: 60_000 }).toBe('ready')
  await page.goto(`/app?surface=browser&drawing=${encodeURIComponent(drawingId)}`)
  const environment = page.locator('section.workspace-environment')
  await expect(environment).toBeAttached()
  if (await environment.getAttribute('data-entered') !== 'true') {
    const resume = environment.getByRole('button', { name: 'Continue workspace', exact: true })
    const describe = environment.getByRole('button', { name: 'Describe what to create', exact: true })
    await expect(resume.or(describe).first()).toBeVisible()
    if (await resume.isVisible()) await resume.click()
    else await describe.click()
    await expect(environment).toHaveAttribute('data-entered', 'true')
  }
  await page.getByRole('navigation', { name: 'Workspace controls', exact: true }).getByRole('button', { name: 'Project', exact: true }).click()
  const projectRegion = page.getByRole('region', { name: 'Project workspace', exact: true })
  await expect(projectRegion).toBeVisible()
  await projectRegion.getByLabel('Project name', { exact: true }).fill(`Signed-in drawing ${randomUUID()}`)
  const create = projectRegion.getByRole('button', { name: 'Create project from this drawing', exact: true })
  await expect(create).toBeEnabled()
  const pending = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/projects')
  await create.click()
  const created = await pending
  expect(created.ok()).toBe(true)
  expect(await created.request().headerValue('x-org-id')).toBe(signedInUser.orgId)
  const { project } = await created.json()
  expect(project.project_id).toBeTruthy()
  const response = await page.request.get(`/api/projects/${project.project_id}`, { headers: signedInUser.headers })
  expect(response.ok()).toBe(true)
  const workspace = await response.json()
  expect(Array.isArray(workspace.drawing_versions)).toBe(true)
  test.fail(true, 'finding 1: Create project from this drawing leaves the project with zero drawing versions')
  expect(workspace.drawing_versions.length).toBeGreaterThanOrEqual(1)
})
