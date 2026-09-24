import { expect, test } from '@playwright/test'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { makeProofReceipt } from '../proofReceipt.mjs'
import {
  COMMIT_RESULT_SCHEMA,
  SEED_RESULT_SCHEMA,
  correctionParams,
  expectHeadAdvanced,
  runBody,
  seedParams,
  settingsChangeParams,
  unseededSettingsParams,
} from '../solarGraphCommitProof.mjs'
import { requireLocalReady } from './requireReady.mjs'

// W6-E05: the Solar local graph commit over real HTTP against the managed
// stack's real upload, checkout, availability gate, broker, worker and version
// store. The in-process contract is server/tests/test_w1_seed_product_path.py;
// this row replays its whole walk on the stack and then reads the committed
// head back from the rendered product. Studio's ParamForm cannot compose a
// solar-settings request (no expected_rev, changes or initialize), so the runs
// go through the page's own request context under the same demo tenant the UI
// uses, and the rendered /try Solar surface is where the result is read back.

const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const TENANT_HEADERS = { 'X-Tenant-Id': 'demo-tenant' }
const DRAWING = join(process.cwd(), 'e2e', 'fixtures', 'distinctive-panel.dxf')
const PROOF_DIR = join(process.cwd(), '..', 'artifacts', 'unified-surface-proof', 'local')
const REQUEST_TIMEOUT = 15_000
// ?wait=1 blocks on the job; a local graph commit is seconds, so 45 s bounds a stall.
const RUN_TIMEOUT = 45_000

test('E05 solar local graph commit: seed, change, stale refusal, correction refusal, rendered head', async ({ page }) => {
  test.setTimeout(180_000)
  const api = page.request
  await requireLocalReady(api, test, API_BASE)

  const observed = []
  const note = (method, path, response) => {
    observed.push(`${method} ${path.replace(/\/api\/drawings\/[^/]+\//, '/api/drawings/{id}/')} ${response.status()}`)
    return response
  }
  const get = async (path) => note('GET', path, await api.get(`${API_BASE}${path}`, {
    headers: TENANT_HEADERS, timeout: REQUEST_TIMEOUT,
  }))
  const readJson = async (path) => {
    const response = await get(path)
    expect(response.status(), `GET ${path}`).toBe(200)
    return response.json()
  }
  const versionsOf = (drawingId) => readJson(`/api/drawings/${drawingId}/versions`)

  const health = await readJson('/api/health')
  expect(health.source_sha, 'the managed stack serves a known source sha').toMatch(/^[0-9a-f]{40}$/)

  const tools = (await readJson('/api/tools')).tools
  const digestOf = (name) => {
    const row = tools.find((tool) => tool.name === name)
    expect(row, `${name} is in the served catalog`).toBeTruthy()
    return row.catalog_digest
  }
  const settingsDigest = digestOf('solar-settings')
  const correctionDigest = digestOf('solar-correct-string')

  const run = async (drawingId, { tool = 'solar-settings', params, capability }) => {
    const headers = { ...TENANT_HEADERS, 'Content-Type': 'application/json' }
    if (capability) headers['X-Checkout-Capability'] = capability
    const data = runBody({
      tool, drawingId, params,
      catalogDigest: tool === 'solar-settings' ? settingsDigest : correctionDigest,
    })
    const response = await api.post(`${API_BASE}/api/run?wait=1`, { headers, data, timeout: RUN_TIMEOUT })
    return { status: response.status(), body: await note('POST', '/api/run', response).json() }
  }
  const expectRefusal = (reply, status, reason) => {
    expect(reply.status, JSON.stringify(reply.body)).toBe(status)
    expect(reply.body.reason_code).toBe(reason)
  }

  // a. Upload the tracked sample DXF as a new drawing: one version, no graph.
  const upload = note('POST', '/api/drawings/upload', await api.post(`${API_BASE}/api/drawings/upload`, {
    headers: TENANT_HEADERS,
    multipart: {
      file: { name: 'distinctive-panel.dxf', mimeType: 'application/dxf', buffer: readFileSync(DRAWING) },
    },
    timeout: REQUEST_TIMEOUT,
  }))
  expect(upload.status()).toBe(202)
  const receipt = await upload.json()
  expect(receipt).toMatchObject({ tenant_id: 'demo-tenant', tenant_kind: 'account' })
  const drawingId = receipt.drawing_id
  expect(drawingId).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)

  await expect.poll(async () => (await readJson(`/api/drawings/${drawingId}/upload-status`)).status, {
    timeout: 30_000, message: 'upload extraction settles',
  }).toMatch(/^(ready|failed)$/)
  expect((await readJson(`/api/drawings/${drawingId}/upload-status`)).status).toBe('ready')

  const uploaded = await versionsOf(drawingId)
  expect(uploaded).toMatchObject({ drawing_id: drawingId, head: 1, latest: 1 })
  expect(uploaded.versions).toHaveLength(1)
  const parent = uploaded.versions[0]
  expect(parent.v).toBe(1)
  expect(parent.sha256).toMatch(/^[0-9a-f]{64}$/)
  const uploadedIntake = (await readJson(`/api/drawings/${drawingId}/intake`)).intake
  expect(uploadedIntake.solar_design_graph ?? null, 'the upload carries no embedded graph').toBeNull()

  // b. An ordinary settings run on the graphless upload names the seed.
  expectRefusal(await run(drawingId, { params: unseededSettingsParams() }), 409, 'graph_seed_required')
  expect((await versionsOf(drawingId)).head).toBe(1)

  // c. Take the drawing's checkout through the real checkout route.
  const checkout = note('POST', `/api/drawings/${drawingId}/checkout`, await api.post(
    `${API_BASE}/api/drawings/${drawingId}/checkout`,
    { headers: TENANT_HEADERS, data: { holder: 'drafter' }, timeout: REQUEST_TIMEOUT },
  ))
  expect(checkout.status()).toBe(200)
  const lease = await checkout.json()
  expect(lease).toMatchObject({ acquired: true, holder: 'drafter' })
  const capability = lease.checkout_capability
  expect(typeof capability === 'string' && capability.length > 0).toBe(true)

  // d. The seed, bound to version 1's own intake sha256: version 2, graph rev 1.
  const seed = await run(drawingId, { params: seedParams(parent), capability })
  expect(seed.status, JSON.stringify(seed.body)).toBe(200)
  expect(seed.body.ok).toBe(true)
  expect(seed.body.result).toMatchObject({
    schema_version: SEED_RESULT_SCHEMA,
    new_version: { drawing_id: drawingId, version: 2, parent: 1 },
    after_rev: 1,
    replayed: false,
  })
  const seeded = await versionsOf(drawingId)
  expectHeadAdvanced(uploaded, seeded)
  const seededGraph = (await readJson(`/api/drawings/${drawingId}/intake`)).intake.solar_design_graph
  expect(seededGraph).toMatchObject({ rev: 1, source_hash: parent.sha256, settings: { panels_in_sequence: 3 } })

  // e. An ordinary change against rev 1: version 3, graph rev 2.
  const change = await run(drawingId, { params: settingsChangeParams(1), capability })
  expect(change.status, JSON.stringify(change.body)).toBe(200)
  expect(change.body.ok).toBe(true)
  expect(change.body.result).toMatchObject({
    schema_version: COMMIT_RESULT_SCHEMA,
    new_version: { drawing_id: drawingId, version: 3, parent: 2 },
    before_rev: 1,
    after_rev: 2,
  })
  const changed = await versionsOf(drawingId)
  expectHeadAdvanced(seeded, changed)
  const changedGraph = (await readJson(`/api/drawings/${drawingId}/intake`)).intake.solar_design_graph
  expect(changedGraph).toMatchObject({ rev: 2, settings: { panels_in_sequence: 3, num_mppt: 2 } })

  // f. The same change with the now-stale expected_rev 1 is admitted, then fails
  // STALE_GRAPH_REVISION (test_w1_local_graph_rail.py); the head stays at 3.
  const stale = await run(drawingId, { params: settingsChangeParams(1), capability })
  expectRefusal(stale, 400, 'STALE_GRAPH_REVISION')
  expect(stale.body.ok).toBe(false)
  expect(stale.body.error).toMatchObject({ error_code: 'BAD_PARAMS', message: 'STALE_GRAPH_REVISION' })
  expect((await versionsOf(drawingId))).toMatchObject({ head: 3, latest: 3 })

  // g. The correction is refused strings_required: nothing produces strings yet.
  const correction = await run(drawingId, {
    tool: 'solar-correct-string', params: correctionParams(2), capability,
  })
  expectRefusal(correction, 409, 'strings_required')
  expect((await versionsOf(drawingId))).toMatchObject({ head: 3, latest: 3 })

  // h. Release the checkout with the capability that proves it.
  const release = note('DELETE', `/api/drawings/${drawingId}/checkout`, await api.delete(
    `${API_BASE}/api/drawings/${drawingId}/checkout`,
    { headers: { ...TENANT_HEADERS, 'X-Checkout-Capability': capability }, timeout: REQUEST_TIMEOUT },
  ))
  expect(release.status()).toBe(200)

  // i. The rendered Solar product names version 3 as the head.
  page.on('response', (response) => {
    if (!response.url().startsWith(API_BASE)) return
    const url = new URL(response.url())
    note(response.request().method(), url.pathname, response)
  })
  await page.addInitScript((id) => {
    sessionStorage.setItem('leaf.cat.workbench.id.v1', id)
  }, drawingId)
  await page.goto('/try?surface=solar')
  await expect(page.getByTestId('operator-phase')).toContainText(/ready/i, { timeout: 30_000 })
  await expect(page.getByTestId('version-head')).toHaveText('Version 3', { timeout: 30_000 })
  await page.getByRole('tab', { name: /Versions/ }).click()
  const history = page.getByRole('region', { name: 'Version history' })
  await expect(history.getByTestId('try-version-v3')).toContainText('head', { timeout: 15_000 })
  for (const older of [1, 2]) {
    const row = history.getByTestId(`try-version-v${older}`)
    await expect(row).toBeVisible()
    await expect(row).not.toContainText('head')
  }

  // j. The proof receipt, bound to the served source and this drawing.
  const proof = makeProofReceipt({
    capability_ids: ['ID-04', 'RN-01', 'VR-01', 'VR-02'],
    evidence_tier: 'local-e2e',
    route: '/try?surface=solar',
    runtime: 'real local Vite, FastAPI, upload extraction, checkout, availability gate, broker, worker, and version store',
    source_commit: health.source_sha,
    api_endpoints: observed,
    assertions: [
      'a real DXF upload produced one version with no embedded design graph',
      'an ordinary solar-settings run on the graphless upload was refused graph_seed_required with no new version',
      'the real checkout route issued a capability for the drawing',
      'the initialize seed bound to version 1 intake sha256 answered leaf.solar-graph-seed.v1 at version 2, graph rev 1',
      'an ordinary settings change at expected_rev 1 answered leaf.solar-graph-commit.v1 at version 3, graph rev 2',
      'the same change at the stale expected_rev 1 failed STALE_GRAPH_REVISION and the head stayed at version 3',
      'solar-correct-string was refused strings_required and the head stayed at version 3',
      'the checkout was released with its capability',
      'the rendered /try Solar surface named version 3 as the head in its header and version history',
    ],
    result: {
      verdict: 'pass',
      drawing_id: drawingId,
      source_sha: health.source_sha,
      head: 3,
      graph_rev: 2,
    },
    limitations: [
      'APS_LIVE=0: local graph commits never reach Autodesk APS by design.',
      'LEAF_AUTH_LIVE=0 proves the account-scoped demo tenant, not a signed live-auth identity.',
      'The runs are composed in the page request context because Studio ParamForm cannot build a solar-settings request.',
    ],
  })
  expect(proof.source_commit).toBe(health.source_sha)
  mkdirSync(PROOF_DIR, { recursive: true })
  writeFileSync(join(PROOF_DIR, 'solar-local-graph-commit-receipt.json'), `${JSON.stringify(proof, null, 2)}\n`)
})
