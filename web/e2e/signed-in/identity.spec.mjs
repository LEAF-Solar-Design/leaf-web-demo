import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createTestIdentity } from '../../walk/testIdentity.mjs'
import { test, expect } from './_harness.mjs'

test('a verified token reaches a real account and the browser project surface', async ({ page, signedInUser }) => {
  const response = await page.request.get(`/api/orgs/${signedInUser.orgId}`, { headers: signedInUser.headers })
  expect(response.status()).toBe(200)
  expect((await response.json()).org.org_id).toBe(signedInUser.orgId)
  await page.goto('/app?surface=browser')
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
  const projects = page.getByRole('region', { name: 'Workspace projects', exact: true })
  await expect(projects).toBeVisible()
  await expect(projects.getByRole('heading', { name: 'Open a project', exact: true })).toBeVisible()
  expect(await page.evaluate(() => localStorage.getItem('leaf.jwt'))).toBe(signedInUser.bearer)
})

test('the server refuses absent, wrong-key and expired credentials', async ({ stack, page, signedInUser }) => {
  const path = `/api/orgs/${signedInUser.orgId}`
  const absent = await page.request.get(path, { headers: { 'X-Org-Id': signedInUser.orgId } })
  expect(absent.status()).toBe(401)
  const root = await mkdtemp(join(tmpdir(), 'leaf-wrong-key-'))
  try {
    // Keep the trusted JWKS untouched: this key shares only the issuer/audience.
    const second = createTestIdentity({ root, runId: stack.identity.runId })
    expect(second.issuer).toBe(stack.identity.issuer)
    expect(second.audience).toBe(stack.identity.audience)
    const claims = { subject: signedInUser.subject, tenantId: signedInUser.orgId, orgId: signedInUser.orgId, tier: 'hosted_free' }
    const wrongKey = await page.request.get(path, { headers: { ...signedInUser.headers, Authorization: `Bearer ${second.mint(claims)}` } })
    expect(wrongKey.status()).toBe(401)
    const expired = stack.identity.mint({ ...claims, lifetimeSeconds: 60, now: Math.floor(Date.now() / 1000) - 3600 })
    const response = await page.request.get(path, { headers: { ...signedInUser.headers, Authorization: `Bearer ${expired}` } })
    expect(response.status()).toBe(401)
  } finally { await rm(root, { recursive: true, force: true }) }
})
