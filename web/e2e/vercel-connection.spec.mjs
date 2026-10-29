import { expect, test } from '@playwright/test'
import { catProofResponse, makeCatProofState } from './catProofFixture.mjs'

// Actual Next Studio and standalone callback, with fixture-backed APIs and
// intercepted consent navigation. This is not evidence of live provider consent.
// Disable trace/video so callback material cannot enter retained diagnostics.
test.use({ trace: 'off', video: 'off', screenshot: 'off', serviceWorkers: 'block' })
const TOKEN = `proof.${Buffer.from(JSON.stringify({ iss: 'https://vercel-proof.invalid/', sub: 'vercel-proof-user' })).toString('base64url')}.fixture`
const PROJECT_ID = '5a1f0c3e-2b7d-4c8a-9e61-0d4f7b2a9c11'
const OWNER_ID = '6a1f0c3e-2b7d-4c8a-9e61-0d4f7b2a9c12'
const PROJECT = { project_id: PROJECT_ID, name: 'Fixture Vercel connection proof', status: 'active' }
const ROOT = `/api/v1/projects/${PROJECT_ID}`
const INTEGRATIONS = `${ROOT}/integrations`
const CORS = { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*', 'access-control-allow-methods': '*' }
const scope = { tenantId: PROJECT_ID, projectId: PROJECT_ID, platformUserId: OWNER_ID,
  bindingId: '7a1f0c3e-2b7d-4c8a-9e61-0d4f7b2a9c13', projectRole: 'owner',
  externalAuthority: 'auth0', externalSubject: 'vercel-proof-user' }
const operation = { schema_version: 1, id: 'normalized-project-read', title: 'List projects',
  method: 'GET', path: '/v10/projects', effect: 'read', pagination: { kind: 'none', records_pointer: '' },
  limits: { timeout_ms: 1000, max_response_bytes: 131072, max_records: 250, max_pages: 1 }, idempotency: { kind: 'read' } }

async function installBackend(context, baseURL) {
  const fixture = makeCatProofState()
  const localOrigin = new URL(baseURL).origin
  const state = 's'.repeat(43)
  const code = 'fixture_code'
  const configurationId = 'icfg_fixture'
  const authorizationUrl = `https://vercel.com/integrations/leaf/new?state=${state}`
  const callbackUri = `${localOrigin}/vercel-callback.html`
  const callbackUrl = `${callbackUri}?${new URLSearchParams({ state, code, configurationId })}`
  let releaseReadback
  const readbackGate = new Promise(resolve => { releaseReadback = resolve })
  let releaseDestinationReadback
  const destinationReadbackGate = new Promise(resolve => { releaseDestinationReadback = resolve })
  const backend = { counts: {}, foreign: [], unexpected: [], mutations: [], readbacks: [],
    providerNavigations: 0, callbackDocuments: 0, callbackScripts: 0, completions: 0,
    finalReadbackPending: false, releaseReadback, releaseDestinationReadback,
    destinationReadbackPending: false, destinationSaves: 0, destination: null }
  // Redirect hops emit request events but do not invoke the route handler again.
  context.on('request', request => {
    const url = new URL(request.url())
    if (url.origin === localOrigin && url.pathname === '/vercel-callback.html' && request.isNavigationRequest()) backend.callbackDocuments += 1
  })
  const count = (method, path) => {
    const key = `${method} ${path}`
    backend.counts[key] = (backend.counts[key] || 0) + 1
  }
  await context.addInitScript(({ token }) => {
    if (location.pathname !== '/app') return
    localStorage.setItem('leaf.jwt', token)
    localStorage.setItem('leaf.org_id', 'cat-proof-org')
  }, { token: TOKEN })
  await context.route('**/*', async route => {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()
    const path = url.pathname
    if (url.origin === localOrigin) {
      if (path === '/vercel-callback.js') backend.callbackScripts += 1
      return route.continue()
    }
    if (url.href === authorizationUrl && method === 'GET' && request.isNavigationRequest()) {
      backend.providerNavigations += 1
      return route.fulfill({ status: 302, headers: { location: callbackUrl, 'cache-control': 'no-store' }, body: '' })
    }
    if (!['http://leaf-proof.invalid', 'https://ops-proof.invalid'].includes(url.origin)) {
      // Never retain query strings, headers or bodies from refused traffic.
      backend.foreign.push(`${method} ${url.origin}${path}`)
      return route.abort('blockedbyclient')
    }
    count(method, path)
    if (method === 'OPTIONS') return route.fulfill({ status: 204, headers: CORS })
    const body = request.postData() ? request.postDataJSON() : {}
    let reply
    const json = value => ({ status: 200, body: value })
    if (url.origin === 'https://ops-proof.invalid') {
      if (method !== 'GET') backend.mutations.push(`${method} ${path}`)
      if (path === `${ROOT}/scope` && method === 'GET') reply = json(scope)
      else if ([`${INTEGRATIONS}/datasets`, `${INTEGRATIONS}/elements`].includes(path) && method === 'GET') {
        reply = json({ records: [], nextCursor: null })
      } else if (path === `${INTEGRATIONS}/connectors/import` && method === 'POST') {
        expect(body.options.title).toBe('Vercel account connection')
        expect(body.document.servers).toEqual([{ url: 'https://api.vercel.com' }])
        expect(body.document.paths['/v10/projects'].get.responses[200].content['application/json'].schema.type).toBe('array')
        backend.reference = { id: body.options.id, version: 1, digest: 'a'.repeat(64) }
        backend.draft = { id: body.options.id, title: body.options.title, state: 'draft',
          reference: backend.reference, revision: 1, auth_kind: 'bearer', operation_count: 1,
          draft_definition: { schema_version: 1, id: body.options.id, title: body.options.title, version: 1,
            state: 'draft', protocol: 'https-rest', origins: ['https://api.vercel.com'], auth: { kind: 'bearer' }, operations: [operation] } }
        reply = json({ reference: backend.reference, revision: 1, issues: [] })
      } else if (path === `${INTEGRATIONS}/connectors/${backend.reference?.id}` && method === 'GET') {
        reply = json(backend.draft)
      } else if (path === `${INTEGRATIONS}/connectors/${backend.reference?.id}/publish` && method === 'POST') {
        expect(body).toEqual({ version: 1, expectedRevision: 1, readOperationIds: [operation.id] })
        backend.published = { ...backend.draft, reference: { ...backend.reference, digest: 'b'.repeat(64) },
          state: 'published', revision: 2, definition: { ...backend.draft.draft_definition, state: 'published' },
          operationReferences: [{ ...backend.reference, id: operation.id, digest: 'c'.repeat(64) }] }
        reply = json({ reference: backend.published.reference, revision: 2 })
      } else if (path === `${INTEGRATIONS}/connectors/${backend.reference?.id}/versions/1` && method === 'GET') {
        reply = json(backend.published)
      } else if (path === `${INTEGRATIONS}/connections` && method === 'POST') {
        expect(body).toEqual({ id: body.id, title: 'Vercel account', connectorRef: backend.published.reference,
          origin: 'https://api.vercel.com', operationIds: [operation.id] })
        backend.connection = { id: body.id, title: body.title, revision: 1, state: 'setup',
          connector_ref: body.connectorRef, origin: body.origin, auth_kind: 'bearer', credential_purposes: [],
          operation_ids: body.operationIds, grant_revision: 1, owner_id: OWNER_ID, provider_account_id: null,
          identity_verified: false, health: { checked_at: null, code: 'unknown' }, cleanup: 'complete' }
        reply = json(backend.connection)
      } else if (path === `${INTEGRATIONS}/connections/${backend.connection?.id}/vercel/attempt` && method === 'POST') {
        expect(body).toEqual({ expectedRevision: 1 })
        reply = json({ state, expiresAt: new Date(Date.now() + 120_000).toISOString(), authorizationUrl, callbackUri })
      } else if (path === `${INTEGRATIONS}/connections/${backend.connection?.id}/vercel/complete` && method === 'POST') {
        backend.completions += 1
        // Boolean assertion deliberately keeps callback material out of failures.
        expect(backend.providerNavigations === 1 && backend.callbackDocuments === 1 && backend.callbackScripts === 1
          && body.expectedRevision === 1 && body.state === state && body.code === code
          && body.configurationId === configurationId && Object.keys(body).length === 4).toBe(true)
        expect(backend.completions).toBe(1)
        backend.connection = { ...backend.connection, revision: 2, credential_purposes: ['access_token'],
          identity_verified: true, provider_account_id: 'user_fixture' }
        reply = json({ configurationId, integrationId: 'oac_fixture', userId: 'user_fixture', teamId: null,
          ownerId: 'user_fixture', revision: 2, cleanup: 'complete' })
      } else if (path === `${INTEGRATIONS}/connections/${backend.connection?.id}/vercel/projects` && method === 'GET') {
        expect(Object.fromEntries(url.searchParams)).toEqual({ expectedRevision: '2', search: 'Jessica' })
        reply = json({ revision: 2, account: { ownerId: 'user_fixture', teamId: null },
          projects: [{ id: 'prj_fixture_jessica', name: 'Synthetic provider — Jessica web' }], truncated: false })
      } else if (path === `${INTEGRATIONS}/connections/${backend.connection?.id}/vercel/destination` && method === 'PUT') {
        expect(body).toEqual({ expectedRevision: 2, projectId: 'prj_fixture_jessica', search: 'Jessica' })
        backend.destinationSaves += 1
        expect(backend.destinationSaves).toBe(1)
        backend.destination = { projectId: 'prj_fixture_jessica', accountId: 'user_fixture', name: 'Synthetic provider — Jessica web' }
        backend.connection = { ...backend.connection, revision: 3 }
        reply = json({ revision: 3, account: { ownerId: 'user_fixture', teamId: null }, destination: backend.destination })
      } else if (path === `${INTEGRATIONS}/connections/${backend.connection?.id}/vercel/destination` && method === 'GET') {
        expect(url.search).toBe('')
        backend.destinationReadbackPending = true
        await destinationReadbackGate
        reply = json({ revision: 3, account: { ownerId: 'user_fixture', teamId: null }, destination: backend.destination })
      } else if (path === `${INTEGRATIONS}/connections/${backend.connection?.id}` && method === 'GET') {
        const summary = structuredClone(backend.connection)
        if (backend.completions) {
          backend.finalReadbackPending = true
          await readbackGate
        }
        backend.readbacks.push(summary)
        reply = json(summary)
      } else {
        backend.unexpected.push(`${method} ${path}`)
        reply = { status: 404, body: { error: 'unexpected_fixture_route' } }
      }
    } else if (method === 'GET' && path === `/api/projects/${PROJECT_ID}/page`) reply = json({ page: null })
    else if (method === 'GET' && path === '/api/projects') reply = json({ projects: [PROJECT] })
    else if (method === 'GET' && path === `/api/projects/${PROJECT_ID}`) {
      reply = json({ project: PROJECT, drawing_versions: [], jobs: [], built_tools: [], drawing_artifacts: [] })
    } else if (method === 'GET' && path === '/api/campaigns') reply = json({ campaigns: [] })
    else {
      if (method !== 'GET' && path !== '/api/telemetry') backend.unexpected.push(`${method} ${path}`)
      reply = catProofResponse({ method, path, body, query: Object.fromEntries(url.searchParams) }, fixture)
    }
    return route.fulfill({ status: reply.status, headers: CORS, contentType: 'application/json',
      body: reply.body == null ? '' : JSON.stringify(reply.body) })
  })
  return backend
}

async function actionable(locator) {
  await locator.scrollIntoViewIfNeeded()
  await expect(locator).toBeVisible()
  await expect(locator).toBeEnabled()
  // Place the control in the reading area, clear of the fixed command bar.
  // Keep the hit-target and trial-click checks after this ordinary scroll.
  await locator.evaluate(node => node.scrollIntoView({ block: 'center', inline: 'nearest' }))
  await expect.poll(() => locator.evaluate(node => {
    const box = node.getBoundingClientRect()
    const x = box.x + box.width / 2, y = box.y + box.height / 2
    const hit = document.elementFromPoint(x, y)
    return x >= 0 && y >= 0 && x < innerWidth && y < innerHeight && (hit === node || node.contains(hit))
  })).toBe(true)
  await locator.click({ trial: true })
}

async function noHorizontalOverflow(page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
}

for (const [size, viewport] of [['desktop', { width: 1440, height: 900 }], ['phone', { width: 390, height: 844 }]]) {
  test(`fixture-backed Vercel account linking in Next Studio at ${size}`, async ({ page, context, baseURL }, testInfo) => {
    test.setTimeout(120_000)
    page.setDefaultTimeout(20_000)
    await page.setViewportSize(viewport)
    const backend = await installBackend(context, baseURL)
    try {
      await page.goto('/app?surface=ops')
      const environment = page.getByRole('region', { name: 'ops environment', exact: true })
      await environment.getByRole('button', { name: 'Open project or material', exact: true }).click()
      const sidebar = page.getByRole('region', { name: 'Project sidebar', exact: true })
      await sidebar.getByRole('region', { name: 'Workspace projects', exact: true })
        .getByRole('button', { name: PROJECT.name, exact: true }).click({ timeout: 30_000 })
      // Opening a new project changes the environment's authority scope and
      // returns to its chooser, just as in project-page-editor.spec.mjs.
      await environment.getByRole('button', { name: 'Open project or material', exact: true }).click()
      await sidebar.getByRole('button', { name: 'Close Project', exact: true }).click()
      await expect(environment).toHaveAttribute('data-entered', 'true')
      const panel = page.getByRole('region', { name: 'Connect Vercel', exact: true })
      const generic = page.getByRole('form', { name: 'Import connector JSON', exact: true })
      await expect(generic).toBeAttached()
      await expect(generic.getByLabel('OpenAPI JSON', { exact: true })).toBeEditable()
      const create = panel.getByRole('button', { name: 'Create Vercel connection', exact: true })
      await actionable(create)
      await create.click()
      const authorize = panel.getByRole('button', { name: 'Authorize Vercel', exact: true })
      await actionable(authorize)
      expect(backend.mutations.map(value => value.split('/').at(-1))).toEqual(['import', 'publish', 'connections'])
      expect(backend.readbacks.at(-1)).toEqual(backend.connection)
      await noHorizontalOverflow(page)
      await testInfo.attach(`${size}-pre-authorize`, { body: await page.screenshot(), contentType: 'image/png' })
      const popupPromise = context.waitForEvent('page')
      await authorize.click()
      const popup = await popupPromise
      await expect.poll(() => backend.finalReadbackPending).toBe(true)
      await expect(panel.getByText(/Verified personal Vercel account/)).toHaveCount(0)
      // The actual callback clears its query before delivering the message.
      await expect.poll(() => popup.url() === `${baseURL}/vercel-callback.html` || popup.isClosed()).toBe(true)
      expect(backend.completions).toBe(1)
      backend.releaseReadback()
      const linked = panel.getByText('Verified personal Vercel account. Cleanup complete.', { exact: true })
      await expect(linked).toBeVisible()
      await linked.scrollIntoViewIfNeeded()
      expect(backend.readbacks.at(-1)).toEqual(backend.connection)
      expect(backend.readbacks.at(-1)).toMatchObject({ revision: 2, credential_purposes: ['access_token'],
        connector_ref: backend.published.reference, owner_id: OWNER_ID, operation_ids: [operation.id] })
      expect(backend.readbacks.map(value => value.revision)).toEqual([1, 1, 1, 1, 2])
      expect(backend.mutations.map(value => value.split('/').at(-1))).toEqual(['import', 'publish', 'connections', 'attempt', 'complete'])
      expect(backend.counts[`GET ${INTEGRATIONS}/connectors/${backend.reference.id}`]).toBe(1)
      expect(backend.counts[`GET ${INTEGRATIONS}/connectors/${backend.reference.id}/versions/1`]).toBe(1)
      await expect(generic).toBeAttached()
      await expect(page.getByRole('form', { name: 'Define typed dataset', exact: true })).toHaveCount(0)
      await noHorizontalOverflow(page)
      await testInfo.attach(`${size}-linked`, { body: await page.screenshot(), contentType: 'image/png' })
      const search = panel.getByRole('searchbox', { name: 'Search Vercel projects', exact: true })
      const load = panel.getByRole('button', { name: 'Load Vercel projects', exact: true })
      await actionable(search)
      await search.fill('Jessica')
      await actionable(load)
      await load.click()
      const projects = panel.getByRole('combobox', { name: 'Vercel project', exact: true })
      await actionable(projects)
      await projects.selectOption('prj_fixture_jessica')
      const save = panel.getByRole('button', { name: 'Save destination', exact: true })
      await actionable(save)
      for (const control of [search, load, projects, save]) {
        expect(await control.evaluate(node => node.getBoundingClientRect().height)).toBeGreaterThanOrEqual(44)
      }
      await noHorizontalOverflow(page)
      await testInfo.attach(`${size}-destination-choice`, { body: await page.screenshot(), contentType: 'image/png' })
      await save.click()
      await expect.poll(() => backend.destinationReadbackPending).toBe(true)
      await expect(panel.getByText(/Confirmed destination:/)).toHaveCount(0)
      await expect(load).toBeDisabled()
      backend.releaseDestinationReadback()
      const confirmed = panel.getByText('Confirmed destination: Synthetic provider — Jessica web', { exact: true })
      await expect(confirmed).toBeVisible()
      await confirmed.scrollIntoViewIfNeeded()
      await noHorizontalOverflow(page)
      await testInfo.attach(`${size}-destination-confirmed`, { body: await page.screenshot(), contentType: 'image/png' })
      expect(backend.destinationSaves).toBe(1)
      expect(backend.connection.revision).toBe(3)
      expect(backend.mutations.map(value => value.split('/').at(-1))).toEqual(['import', 'publish', 'connections', 'attempt', 'complete', 'destination'])
      expect(backend.completions).toBe(1)
      expect(backend.providerNavigations).toBe(1)
      expect(backend.callbackDocuments).toBe(1)
      expect(backend.callbackScripts).toBe(1)
      expect(backend.foreign).toEqual([])
      expect(backend.unexpected).toEqual([])
      expect(Object.keys(backend.counts).some(key => /\/sync(?:$|\/)|\/execute(?:$|\/)|\/api\/run$|\/action-runs|\/manual-workflow-runs/.test(key))).toBe(false)
    } finally {
      backend.releaseReadback()
      backend.releaseDestinationReadback()
      // Explicit bounded projection: no request bodies, headers, URLs with queries,
      // callback state/code, token values, or provider response bodies.
      const evidence = { fixtureBacked: true, liveProviderConsent: false, viewport, projectId: PROJECT_ID,
        connector: backend.published && { reference: backend.published.reference, revision: backend.published.revision },
        connection: backend.connection && { id: backend.connection.id, revision: backend.connection.revision,
          ownerId: backend.connection.owner_id, credentialPurposes: backend.connection.credential_purposes },
        readbacks: backend.readbacks.map(value => ({ id: value.id, revision: value.revision, credentialPurposes: value.credential_purposes })),
        destination: backend.destination, destinationSaves: backend.destinationSaves,
        completions: backend.completions, providerNavigations: backend.providerNavigations,
        callbackDocuments: backend.callbackDocuments, callbackScripts: backend.callbackScripts,
        requestCounts: backend.counts, refused: backend.foreign, unexpected: backend.unexpected }
      const bytes = Buffer.from(JSON.stringify(evidence, null, 2))
      expect(bytes.length).toBeLessThan(16_384)
      await testInfo.attach(`${size}-fixture-evidence`, { body: bytes, contentType: 'application/json' })
    }
  })
}
