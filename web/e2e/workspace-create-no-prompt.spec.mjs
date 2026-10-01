// stu2-h: org and project creation never open a native dialog.
//
// App.jsx used to fall back to window.prompt when a creator was called with
// no name, and the ribbon's Create project button reached that fallback with
// its click event. Both creations now go through the header ProjectSwitcher's
// inline form. This spec drives both through /app with mocked APIs and proves
// no dialog handler ever fires: the page-level `dialog` event and an in-page
// wrapper over prompt, confirm and alert both stay empty.
//
// Run from web/ with LEAF_NATIVE_GATE_WORKER set so the dev server port
// (5185 + 100 * N) does not collide with parallel lanes.

import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

const API = 'http://leaf-proof.invalid/api/**'
const CORS = { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' }
const UNBOUND = { detail: 'verified subject has no active platform identity binding' }

test('org and project creation go through the inline switcher form with no native dialog', async ({ page }) => {
  test.setTimeout(90_000)
  await page.addInitScript(() => {
    localStorage.setItem('leaf.jwt', 'fixture-token')
    localStorage.setItem('leaf.coach.dismissed.v1', '1')
    localStorage.removeItem('leaf.org_id')
    window.__nativeDialogCalls = []
    for (const kind of ['prompt', 'confirm', 'alert']) {
      const real = window[kind]
      window[kind] = (...args) => {
        window.__nativeDialogCalls.push(kind)
        return real.apply(window, args)
      }
    }
  })
  const dialogs = []
  page.on('dialog', async (dialog) => {
    dialogs.push({ type: dialog.type(), message: dialog.message() })
    await dialog.dismiss()
  })

  const state = makeCatProofState()
  const calls = []
  const projects = []
  let orgId = null
  await page.route(API, async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()
    const path = url.pathname
    let body = {}
    if (request.postData()) {
      try { body = request.postDataJSON() } catch { body = {} }
    }
    const reply = (status, value) => route.fulfill({
      status,
      contentType: value == null ? undefined : 'application/json',
      body: value == null ? '' : JSON.stringify(value),
      headers: CORS,
    })
    if (method === 'OPTIONS') return reply(204, null)
    // A verified session with no workspace org: the switcher starts unbound.
    if (path === '/api/session') {
      return reply(200, { intake: state.base, tenant_id: 'cat-litmus-tenant', tier: 'proof' })
    }
    if (path === '/api/orgs' && method === 'POST') {
      calls.push(['org', body])
      orgId = 'stu2h-org'
      return reply(200, { org: { org_id: orgId, name: body.name, tier: 'hosted_starter', status: 'active' } })
    }
    if (path === '/api/projects' && method === 'GET') {
      return orgId ? reply(200, { projects }) : reply(403, UNBOUND)
    }
    if (path === '/api/projects' && method === 'POST') {
      calls.push(['project', body, request.headers()['x-org-id'] || null])
      const project = { project_id: 'stu2h-project', name: body.name, status: 'active' }
      projects.push(project)
      return reply(200, { project })
    }
    if (path === '/api/projects/stu2h-project' && method === 'GET') {
      return reply(200, { project: projects[0], drawing_versions: [], jobs: [], built_tools: [] })
    }
    const result = catProofResponse({ method, path, body, query: Object.fromEntries(url.searchParams) }, state)
    return reply(result.status, result.body ?? null)
  })

  await page.goto('/app?surface=browser')
  const board = page.locator('[data-ground="browser"]')
  await expect(board).toBeVisible({ timeout: 20_000 })
  const switcher = page.locator('header.top .proj-switch')
  const chip = switcher.locator('button.proj-chip')
  const openTool = page.locator('.ribbon-tool[data-tool="project:open"]')
  const createTool = page.locator('.ribbon-tool[data-tool="project:create"]')

  // Org: no org yet, so Create project waits and the switcher offers the
  // inline workspace form.
  await expect(createTool).toBeDisabled({ timeout: 20_000 })
  await expect(openTool).toBeEnabled({ timeout: 20_000 })
  await openTool.click()
  await expect(chip).toHaveAttribute('aria-expanded', 'true')
  const orgField = switcher.getByLabel('Workspace name')
  await expect(orgField).toBeVisible()
  await orgField.fill('Ridge workspace')
  await switcher.getByRole('button', { name: 'Create workspace org' }).click()
  await expect.poll(() => calls.filter(([kind]) => kind === 'org')).toEqual([['org', { name: 'Ridge workspace' }]])
  await expect.poll(() => page.evaluate(() => localStorage.getItem('leaf.org_id'))).toBe('stu2h-org')

  // Project: the ribbon's Create project now opens the same switcher on its
  // focused inline field instead of a native prompt.
  await chip.click()
  await expect(chip).toHaveAttribute('aria-expanded', 'false')
  await expect(createTool).toBeEnabled({ timeout: 20_000 })
  await createTool.click()
  await expect(chip).toHaveAttribute('aria-expanded', 'true')
  const projectField = switcher.getByLabel('New project')
  await expect(projectField).toBeVisible()
  await expect(projectField).toBeFocused()
  await projectField.fill('Maple roof')
  await switcher.getByRole('button', { name: 'Create project', exact: true }).click()
  await expect.poll(() => calls.filter(([kind]) => kind === 'project'))
    .toEqual([['project', { name: 'Maple roof' }, 'stu2h-org']])
  await expect(board).toHaveAttribute('data-project-state', 'project', { timeout: 20_000 })

  // No dialog handler ran at any point.
  expect(dialogs).toEqual([])
  expect(await page.evaluate(() => window.__nativeDialogCalls)).toEqual([])
})
