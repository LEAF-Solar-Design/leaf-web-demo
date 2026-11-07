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
const SAVED_PAGE = { revision: 17, digest: 'e'.repeat(64), document: {
  label: 'Fixture saved page', headline: 'A saved page for preview', description: 'Rendered fixture proof.',
  status: 'Private preview', theme: 'cream', alignment: 'center' } }
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
  let releasePermissionReadback
  const permissionReadbackGate = new Promise(resolve => { releasePermissionReadback = resolve })
  const backend = { counts: {}, foreign: [], unexpected: [], mutations: [], readbacks: [],
    providerNavigations: 0, callbackDocuments: 0, callbackScripts: 0, completions: 0,
    finalReadbackPending: false, releaseReadback, releaseDestinationReadback,
    destinationReadbackPending: false, destinationSaves: 0, destination: null,
    releasePermissionReadback, permissionReadbackPending: false, permissionReadbacks: [],
    previewEnables: 0, previewCreates: 0, previewObservations: 0, observationAllowed: false,
    previewRun: null, savedPageReads: 0 }
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
      } else if (path === `${INTEGRATIONS}/connections/${backend.connection?.id}/vercel/preview-permission` && ['GET', 'PUT'].includes(method)) {
        expect(url.search).toBe('')
        expect(backend.destinationSaves).toBe(1)
        if (method === 'PUT') {
          expect(body).toEqual({ expectedConnectionRevision: 3, expectedGrantRevision: 1 })
          expect(backend.connection).toMatchObject({ revision: 3, grant_revision: 1 })
          backend.previewEnables += 1
          expect(backend.previewEnables).toBe(1)
          backend.connection = { ...backend.connection, revision: 4, grant_revision: 2,
            connector_ref: { ...backend.connection.connector_ref, version: 2, digest: 'd'.repeat(64) },
            operation_ids: ['preview-create', 'preview-read'] }
        }
        const permission = { enabled: backend.previewEnables === 1, revision: backend.connection.revision,
          grantRevision: backend.connection.grant_revision, connectorRef: backend.connection.connector_ref,
          account: { ownerId: 'user_fixture', teamId: null }, destination: backend.destination }
        if (method === 'GET') {
          if (permission.enabled) {
            backend.permissionReadbackPending = true
            await permissionReadbackGate
          }
          backend.permissionReadbacks.push(structuredClone(permission))
        }
        reply = json(permission)
      } else if (path === `${INTEGRATIONS}/vercel-preview-runs` && method === 'POST') {
        expect(url.search).toBe('')
        expect(body.runId).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
        expect(body).toEqual({ runId: body.runId, savedRevision: 17, connectionId: backend.connection.id,
          expectedConnectionRevision: 4, expectedGrantRevision: 2 })
        expect(backend.savedPageReads).toBeGreaterThan(0)
        expect(backend.permissionReadbacks.at(-1)).toMatchObject({ enabled: true, revision: 4, grantRevision: 2 })
        backend.previewCreates += 1
        expect(backend.previewCreates).toBe(1)
        backend.previewRun = { runId: body.runId, savedRevision: 17, status: 'submitted',
          deploymentId: 'dpl_saved', url: null, reason: null }
        reply = json(backend.previewRun)
      } else if (path === `${INTEGRATIONS}/vercel-preview-runs/${backend.previewRun?.runId}/observe` && method === 'POST') {
        expect(backend.observationAllowed).toBe(true)
        expect(url.search).toBe('')
        expect(body).toEqual({})
        backend.previewObservations += 1
        expect(backend.previewObservations).toBe(1)
        expect(backend.previewRun.status).toBe('submitted')
        backend.previewRun = { ...backend.previewRun, status: 'ready', url: 'https://saved-page.vercel.app', reason: null }
        reply = json(backend.previewRun)
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
    } else if (method === 'GET' && path === `/api/projects/${PROJECT_ID}/page`) {
      backend.savedPageReads += 1
      reply = json({ page: SAVED_PAGE })
    } else if (method === 'GET' && path === '/api/projects') reply = json({ projects: [PROJECT] })
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
  test(`fixture-backed Vercel account linking and saved-page preview in Next Studio at ${size}`, async ({ page, context, baseURL }, testInfo) => {
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
      await expect.poll(() => popup.isClosed()).toBe(true)
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
      const preview = panel.getByRole('region', { name: 'Preview', exact: true })
      const access = preview.getByRole('button', { name: 'Check preview access', exact: true })
      const enable = preview.getByRole('button', { name: 'Enable previews', exact: true })
      const submit = preview.getByRole('button', { name: 'Preview saved page', exact: true })
      const open = preview.getByRole('link', { name: 'Open preview', exact: true })
      const clickPreviewControl = async control => {
        await actionable(control)
        expect(await control.evaluate(node => node.getBoundingClientRect().height)).toBeGreaterThanOrEqual(44)
        await noHorizontalOverflow(page)
        await control.click()
      }
      expect(backend.previewEnables).toBe(0)
      expect(backend.previewCreates).toBe(0)
      await expect(submit).toHaveCount(0)
      await expect(open).toHaveCount(0)
      await clickPreviewControl(access)
      await expect(enable).toBeVisible()
      expect(backend.permissionReadbacks).toEqual([{ enabled: false, revision: 3, grantRevision: 1,
        connectorRef: backend.published.reference, account: { ownerId: 'user_fixture', teamId: null }, destination: backend.destination }])
      await clickPreviewControl(enable)
      await expect.poll(() => backend.permissionReadbackPending).toBe(true)
      await expect(submit).toHaveCount(0)
      expect(backend.previewCreates).toBe(0)
      backend.releasePermissionReadback()
      await expect(preview.getByText('Private previews are enabled for this destination.', { exact: true })).toBeVisible()
      await actionable(submit)
      expect(backend.permissionReadbacks.at(-1)).toEqual({ enabled: true, revision: 4, grantRevision: 2,
        connectorRef: { ...backend.published.reference, version: 2, digest: 'd'.repeat(64) },
        account: { ownerId: 'user_fixture', teamId: null }, destination: backend.destination })
      const permissionPath = `${INTEGRATIONS}/connections/${backend.connection.id}/vercel/preview-permission`
      expect(backend.counts[`PUT ${permissionPath}`]).toBe(1)
      expect(backend.counts[`GET ${permissionPath}`]).toBe(3)
      for (const control of [search, load]) await expect(control).toBeDisabled()
      await expect(projects).toHaveCount(0)
      await expect(save).toHaveCount(0)
      await noHorizontalOverflow(page)
      await testInfo.attach(`${size}-preview-enabled`, { body: await page.screenshot(), contentType: 'image/png' })
      const savedPageReadsBeforeSubmit = backend.savedPageReads
      await clickPreviewControl(submit)
      await expect(preview.getByText('Preview submitted. The build is not ready yet.', { exact: true })).toBeVisible()
      await expect(preview.getByText('Saved revision: 17', { exact: true })).toBeVisible()
      expect(backend.savedPageReads).toBe(savedPageReadsBeforeSubmit + 1)
      expect(backend.previewCreates).toBe(1)
      expect(backend.previewRun).toMatchObject({ savedRevision: 17, status: 'submitted', deploymentId: 'dpl_saved', url: null, reason: null })
      const submittedRunId = backend.previewRun.runId
      await expect(submit).toHaveCount(0)
      await expect(open).toHaveCount(0)
      const observe = preview.getByRole('button', { name: 'Check build status', exact: true })
      await actionable(observe)
      expect(await observe.evaluate(node => node.getBoundingClientRect().height)).toBeGreaterThanOrEqual(44)
      await noHorizontalOverflow(page)
      await testInfo.attach(`${size}-preview-submitted`, { body: await page.screenshot(), contentType: 'image/png' })
      expect(backend.previewObservations).toBe(0)
      backend.observationAllowed = true
      await observe.click()
      await expect(preview.getByText('Your private preview is ready.', { exact: true })).toBeVisible()
      await actionable(open)
      expect(await open.evaluate(node => node.getBoundingClientRect().height)).toBeGreaterThanOrEqual(44)
      await expect(open).toHaveAttribute('href', 'https://saved-page.vercel.app')
      await expect(open).toHaveAttribute('target', '_blank')
      await expect(open).toHaveAttribute('rel', 'noopener noreferrer')
      expect(backend.previewRun).toEqual({ runId: submittedRunId, savedRevision: 17, status: 'ready',
        deploymentId: 'dpl_saved', url: 'https://saved-page.vercel.app', reason: null })
      await noHorizontalOverflow(page)
      await testInfo.attach(`${size}-preview-ready`, { body: await page.screenshot(), contentType: 'image/png' })
      expect(backend.previewEnables).toBe(1)
      expect(backend.previewCreates).toBe(1)
      expect(backend.previewObservations).toBe(1)
      expect(backend.counts[`POST ${INTEGRATIONS}/vercel-preview-runs`]).toBe(1)
      expect(backend.counts[`POST ${INTEGRATIONS}/vercel-preview-runs/${submittedRunId}/observe`]).toBe(1)
      expect(backend.mutations).toEqual([
        `${INTEGRATIONS}/connectors/import`, `${INTEGRATIONS}/connectors/${backend.reference.id}/publish`,
        `${INTEGRATIONS}/connections`, `${INTEGRATIONS}/connections/${backend.connection.id}/vercel/attempt`,
        `${INTEGRATIONS}/connections/${backend.connection.id}/vercel/complete`,
        `${INTEGRATIONS}/connections/${backend.connection.id}/vercel/destination`, permissionPath,
        `${INTEGRATIONS}/vercel-preview-runs`, `${INTEGRATIONS}/vercel-preview-runs/${submittedRunId}/observe`,
      ].map(path => `${path === permissionPath || path.endsWith('/destination') ? 'PUT' : 'POST'} ${path}`))
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
      backend.releasePermissionReadback()
      // Explicit bounded projection: no request bodies, headers, URLs with queries,
      // callback state/code, token values, or provider response bodies.
      const evidence = { fixtureBacked: true, liveProviderConsent: false, viewport, projectId: PROJECT_ID,
        connector: backend.published && { reference: backend.published.reference, revision: backend.published.revision },
        connection: backend.connection && { id: backend.connection.id, revision: backend.connection.revision,
          grantRevision: backend.connection.grant_revision, connectorRef: backend.connection.connector_ref,
          ownerId: backend.connection.owner_id, credentialPurposes: backend.connection.credential_purposes },
        readbacks: backend.readbacks.map(value => ({ id: value.id, revision: value.revision, credentialPurposes: value.credential_purposes })),
        destination: backend.destination, destinationSaves: backend.destinationSaves,
        preview: { enables: backend.previewEnables, creates: backend.previewCreates, observations: backend.previewObservations,
          savedPageReads: backend.savedPageReads, run: backend.previewRun && { runId: backend.previewRun.runId,
            savedRevision: backend.previewRun.savedRevision, status: backend.previewRun.status,
            deploymentId: backend.previewRun.deploymentId },
          permissionReadbacks: backend.permissionReadbacks.map(value => ({ enabled: value.enabled,
            revision: value.revision, grantRevision: value.grantRevision, connectorRef: value.connectorRef })) },
        completions: backend.completions, providerNavigations: backend.providerNavigations,
        callbackDocuments: backend.callbackDocuments, callbackScripts: backend.callbackScripts,
        requestCounts: backend.counts, refused: backend.foreign, unexpected: backend.unexpected }
      const bytes = Buffer.from(JSON.stringify(evidence, null, 2))
      expect(bytes.length).toBeLessThan(16_384)
      await testInfo.attach(`${size}-fixture-evidence`, { body: bytes, contentType: 'application/json' })
    }
  })
}
