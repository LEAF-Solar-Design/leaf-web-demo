import { expect, test } from '@playwright/test'
import { assertProdResponse, requireProdTarget, resolveProdBaseUrl } from './prodConfig.mjs'

const USER_AGENT = 'leaf-prod-readonly-smoke/1.0'
test.use({
  userAgent: USER_AGENT,
  extraHTTPHeaders: { 'User-Agent': USER_AGENT, Authorization: '' },
  storageState: { cookies: [], origins: [] },
  trace: 'off',
})

test.beforeEach(() => {
  test.skip(!process.env.LEAF_E2E_PROD_BASE_URL, 'LEAF_E2E_PROD_BASE_URL is not set')
  test.skip(!process.env.LEAF_E2E_PROD_ACCEPTANCE_TOKEN, 'LEAF_E2E_PROD_ACCEPTANCE_TOKEN is not set')
  requireProdTarget()
  resolveProdBaseUrl()
})

async function get(request, path) {
  const response = await request.get(new URL(path, resolveProdBaseUrl()).href, {
    headers: {
      'User-Agent': USER_AGENT,
      Authorization: `Bearer ${process.env.LEAF_E2E_PROD_ACCEPTANCE_TOKEN}`,
    },
    maxRedirects: 0,
  })
  assertProdResponse(response.url())
  return response
}

// platform/api.py:403 list_projects returns { projects: [...] }.
async function listProjects(request) {
  const response = await get(request, '/api/projects')
  expect(response.status()).toBe(200)
  const body = await response.json()
  expect(body !== null && typeof body === 'object' && !Array.isArray(body)).toBe(true)
  expect(Array.isArray(body.projects)).toBe(true)
  return body.projects
}

test('P-146 production projects are readable', async ({ request }) => {
  const projects = await listProjects(request)
  test.info().annotations.push({ type: 'project-count', description: String(projects.length) })
})

test('P-058 production solar_template_beta is on', async ({ request }) => {
  // server/routers/templates.py:223 exposes solar_template_beta through this read.
  const response = await get(request, '/api/templates')
  if (response.status() === 404) {
    const body = await response.json()
    if (body.error?.message === 'solar template beta is not enabled') {
      throw new Error('solar_template_beta is off: solar template beta is not enabled')
    }
  }
  expect(response.status(), 'solar_template_beta must be on').toBe(200)
  const body = await response.json()
  expect(body !== null && typeof body === 'object' && !Array.isArray(body)).toBe(true)
  expect(Array.isArray(body.templates)).toBe(true)
  test.info().annotations.push({ type: 'feature-flag', description: 'solar_template_beta=on' })
})

test('P-058 production conv_durable is on', async ({ request }) => {
  const projects = await listProjects(request)
  test.skip(projects.length === 0, 'no project to probe')
  const projectId = projects[0].project_id
  expect(typeof projectId).toBe('string')
  expect(projectId.length > 0).toBe(true)
  // server/routers/conversations.py:320-341 exposes conv_durable through a read-only GET.
  const response = await get(request, `/api/projects/${encodeURIComponent(projectId)}/conversations/recovery/tail?limit=1`)
  if (response.status() === 404) {
    const body = await response.json()
    if (body.error?.message === 'conversation persistence is not enabled') {
      throw new Error('conv_durable is off: conversation persistence is not enabled')
    }
  }
  expect(response.status(), 'conv_durable must be on').toBe(200)
  test.info().annotations.push({ type: 'feature-flag', description: 'conv_durable=on' })
})
