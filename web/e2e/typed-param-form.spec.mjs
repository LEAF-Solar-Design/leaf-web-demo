import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

const TYPED_TOOL = {
  name: 'typed-param-fixture',
  version: '1.0.0',
  description: 'Prove JSON Schema parameter types.',
  kind: 'script',
  capabilities: ['drawing.read'],
  params: {
    type: 'object',
    properties: {
      spheres: { type: 'array', items: { type: 'object' }, default: 1 },
      sphere_options: { type: 'object' },
      marker_size: { type: ['number', 'null'], default: 2 },
      nullable_size: { type: ['number', 'null'], default: 3 },
      dry_run: { type: 'boolean', default: false },
      count: { type: 'number', default: 1 },
      source_layer: { type: 'string', default: 'Panels' },
    },
  },
  provenance: { author: 'user' },
}

test('catalog form emits structured, nullable numeric, boolean, and scalar JSON Schema types', async ({ page }) => {
  const proofState = makeCatProofState()
  let runBody = null

  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    let body = {}
    if (request.postData()) {
      try { body = request.postDataJSON() } catch { body = {} }
    }

    if (url.pathname === '/api/run' && body.tool === TYPED_TOOL.name) {
      runBody = body
      await route.fulfill({ status: 202, contentType: 'application/json', body: JSON.stringify({ job_id: 'typed-job' }) })
      return
    }

    const result = catProofResponse({
      method: request.method(),
      path: url.pathname,
      body,
      query: Object.fromEntries(url.searchParams),
    }, proofState)
    if (url.pathname === '/api/tools') result.body.tools.push(TYPED_TOOL)
    if (url.pathname === '/api/capabilities') result.body.families[0].capabilities.push(TYPED_TOOL)
    await route.fulfill({
      status: result.status,
      contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
    })
  })

  await page.goto('/try?proof=1')
  await page.getByRole('tab', { name: /Catalog/ }).click()
  const card = page.locator('.tool-card').filter({ hasText: TYPED_TOOL.name })
  await card.getByRole('button', { name: new RegExp(TYPED_TOOL.name) }).click()

  await expect(card.getByLabel('Spheres')).toHaveValue('[]')
  await card.getByLabel('Spheres').fill('[{"center":[0,0,0],"radius":10}]')
  await card.getByLabel('Sphere options').fill('{"segments":24}')
  await card.getByLabel('Marker size').fill('2.5')
  await card.getByLabel('Nullable size').fill('')
  await card.getByLabel('Dry run').check()
  await card.getByLabel('Count').fill('4')
  await card.getByLabel('Source layer').fill('Roofs')
  await card.getByRole('button', { name: 'Review & run' }).click()
  await page.getByRole('button', { name: `Run ${TYPED_TOOL.name}` }).click()

  await expect.poll(() => runBody).not.toBeNull()
  expect(runBody.params).toEqual({
    spheres: [{ center: [0, 0, 0], radius: 10 }],
    sphere_options: { segments: 24 },
    marker_size: 2.5,
    nullable_size: null,
    dry_run: true,
    count: 4,
    source_layer: 'Roofs',
  })
})
