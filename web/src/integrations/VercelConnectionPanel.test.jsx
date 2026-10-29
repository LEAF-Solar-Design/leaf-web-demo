import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ReadableStream } from 'node:stream/web'
import { TextDecoder, TextEncoder } from 'node:util'
import { webcrypto } from 'node:crypto'
import { createIntegrationsClient } from './client.js'
import { IntegrationSetupPanel } from './IntegrationSetupPanel.jsx'
import { VercelConnectionPanel, vercelDocument } from './VercelConnectionPanel.jsx'

vi.mock('../api.js', () => ({ authHeaders: () => ({ Authorization: 'Bearer current' }), noteUnauthorized: vi.fn() }))
const projectId = '11111111-1111-4111-8111-111111111111'
const scope = { tenantId: projectId, projectId, platformUserId: projectId, bindingId: projectId,
  projectRole: 'owner', externalAuthority: 'auth0', externalSubject: 'auth0|owner' }
const state = 's'.repeat(43)
let channels, popup
function response(value, status = 200) {
  return { ok: status >= 200 && status < 300, status,
    headers: { get: (key) => key === 'content-type' ? 'application/json' : null },
    body: new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode(JSON.stringify(value))); controller.close() } }) }
}
beforeEach(() => {
  vi.stubGlobal('TextEncoder', TextEncoder); vi.stubGlobal('TextDecoder', TextDecoder)
  let sequence = 0
  vi.stubGlobal('crypto', { subtle: webcrypto.subtle, randomUUID: () => `00000000-0000-4000-8000-${String(++sequence).padStart(12, '0')}` })
  channels = []
  vi.stubGlobal('BroadcastChannel', class {
    constructor(name) { this.name = name; this.close = vi.fn(); channels.push(this) }
  })
  popup = { close: vi.fn(), location: { replace: vi.fn() } }
  vi.spyOn(window, 'open').mockReturnValue(popup)
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

// Replies preserve the host's import -> metadata -> publication -> connection topology.
// The pinned v10 array response normalizes to the existing importer's empty records pointer.
function setup(config = {}) {
  const controller = new AbortController()
  const op = { schema_version: 1, id: 'normalized-project-read', title: 'List projects', method: 'GET', path: '/v10/projects', effect: 'read',
    pagination: { kind: 'none', records_pointer: config.recordsPointer ?? '' }, limits: { timeout_ms: 1000, max_response_bytes: 131072, max_records: 250, max_pages: 1 }, idempotency: { kind: 'read' } }
  let ref, draft, published, connection, destination = null, issueSequence = 0
  const completion = { configurationId: 'icfg_123', integrationId: 'oac_123', userId: 'user_123', teamId: config.teamId ?? null,
    ownerId: config.teamId ?? 'user_123', revision: 2, cleanup: config.cleanup || 'complete' }
  const fetchImpl = vi.fn(async (url, request) => {
    const body = request.body ? JSON.parse(request.body) : null
    if (url.endsWith('/connectors/import')) {
      if (config.unsupportedImport) return response({ reference: null, revision: null,
        issues: [{ code: 'unsupported-document', summary: 'Import declined' }] })
      ref = { id: body.options.id, version: 1, digest: 'a'.repeat(64) }
      draft = { id: ref.id, title: body.options.title, state: 'draft', reference: ref, revision: 1, auth_kind: 'bearer', operation_count: 1,
        draft_definition: { schema_version: 1, id: ref.id, title: body.options.title, version: 1, state: 'draft', protocol: 'https-rest',
          origins: ['https://api.vercel.com'], auth: { kind: 'bearer' }, operations: [op] } }
      if (config.loseImport) throw new Error('transport lost')
      return response({ reference: ref, revision: 1, issues: [] })
    }
    if (url.endsWith(`/connectors/${ref?.id}`)) return response(draft)
    if (url.endsWith('/publish')) {
      published = { ...draft, reference: { ...ref, digest: 'b'.repeat(64) }, state: 'published', revision: 2,
        definition: { ...draft.draft_definition, state: 'published' }, operationReferences: [{ ...ref, id: op.id, digest: 'c'.repeat(64) }] }
      return response({ reference: published.reference, revision: 2 })
    }
    if (url.endsWith(`/connectors/${ref?.id}/versions/1`)) return response(published)
    if (url.endsWith('/connections')) {
      connection = { id: body.id, title: body.title, revision: 1, state: 'setup', connector_ref: body.connectorRef,
        origin: body.origin, auth_kind: 'bearer', credential_purposes: [], operation_ids: body.operationIds, grant_revision: 1,
        owner_id: projectId, provider_account_id: null, identity_verified: false, health: { checked_at: null, code: 'unknown' }, cleanup: 'complete' }
      return response(connection)
    }
    if (url.endsWith('/vercel/attempt')) {
      if (config.unconfigured) return response({ error: 'private detail' }, 503)
      if (config.loseIssue) throw new Error('transport lost')
      const issuedState = issueSequence++ === 0 || config.replayState ? state : 't'.repeat(43)
      return response({ state: issuedState, expiresAt: new Date(Date.now() + 60000).toISOString(),
        authorizationUrl: config.authorizationUrl || `https://vercel.com/integrations/leaf/new?state=${issuedState}`,
        callbackUri: config.callbackUri || `${window.location.origin}/vercel-callback.html` })
    }
    if (url.endsWith('/vercel/complete')) {
      connection = { ...connection, revision: 2, credential_purposes: config.noCustody ? [] : ['access_token'], cleanup: completion.cleanup,
        identity_verified: true, provider_account_id: completion.ownerId,
        owner_id: config.wrongStudioOwner ? '6a1f0c3e-2b7d-4c8a-9e61-0d4f7b2a9c12' : connection.owner_id }
      if (config.loseCompletion) throw new Error('transport lost')
      if (config.failCompletion) return response({ error: 'secret provider payload' }, 422)
      return response(completion)
    }
    const account = { ownerId: completion.ownerId, teamId: completion.teamId }
    if (url.includes('/vercel/projects?')) {
      await config.listGate
      return response({ revision: connection.revision, account: config.listAccount || account,
        projects: config.projects ?? [{ id: 'prj_jessica', name: '<Jessica & Studio>' }], truncated: config.truncated ?? false })
    }
    if (url.endsWith('/vercel/destination') && request.method === 'PUT') {
      if (config.conflictSelect) return response({}, 409)
      destination = { projectId: body.projectId, accountId: completion.ownerId, name: '<Jessica & Studio>' }
      connection = { ...connection, revision: connection.revision + 1 }
      if (config.loseSelect) throw new Error('transport lost')
      return response({ revision: connection.revision, account, destination })
    }
    if (url.endsWith('/vercel/destination')) {
      await config.destinationGate
      if (config.failDestinationRead) throw new Error('read unavailable')
      return response({ revision: connection.revision, account, destination: config.nullDestination ? null : destination,
        ...config.destinationReadback })
    }
    if (url.endsWith(`/connections/${connection?.id}`)) return response(connection)
    throw new Error('Unexpected request')
  })
  const props = { client: createIntegrationsClient({ origin: 'https://ops.example', fetchImpl }), projectId, scope,
    signal: controller.signal, guard: vi.fn(async () => {}), onForbidden: vi.fn() }
  return { props, controller, fetchImpl, drift: () => { connection = { ...connection, revision: connection.revision + 1 } } }
}
async function prepare() {
  fireEvent.click(screen.getByRole('button', { name: 'Create Vercel connection' }))
  await screen.findByRole('button', { name: 'Authorize Vercel' })
}
async function authorize() {
  fireEvent.click(screen.getByRole('button', { name: 'Authorize Vercel' }))
  await screen.findByRole('button', { name: 'Cancel authorization' })
}
async function callback(data = { state, outcome: 'authorized', code: 'code_123', configurationId: 'icfg_123' }) {
  await act(async () => { channels.at(-1).onmessage?.({ data }) })
}
const mutations = (fetchImpl) => fetchImpl.mock.calls.filter(([, request]) => request.method !== 'GET')
const completions = (fetchImpl) => fetchImpl.mock.calls.filter(([url]) => url.endsWith('/vercel/complete'))
const destinationSaves = (fetchImpl) => fetchImpl.mock.calls.filter(([url, request]) => url.endsWith('/vercel/destination') && request.method === 'PUT')
function deferred() {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}
async function linked(props) {
  render(<VercelConnectionPanel {...props} />)
  await prepare(); await authorize(); await callback()
  await screen.findByText(/Verified .*account/)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Load Vercel projects' }).disabled).toBe(false))
}
async function chooseProject() {
  const load = await screen.findByRole('button', { name: 'Load Vercel projects' })
  await waitFor(() => expect(load.disabled).toBe(false))
  fireEvent.click(load)
  await screen.findByRole('option', { name: '<Jessica & Studio>' })
  await waitFor(() => expect(screen.getByLabelText('Vercel project').disabled).toBe(false))
  fireEvent.change(screen.getByLabelText('Vercel project'), { target: { value: 'prj_jessica' } })
}

describe('Vercel destination selection', () => {
  it('lists only on request for the completed owner, sends search, and confirms only after delayed readback', async () => {
    const gate = deferred()
    const { props, fetchImpl } = setup({ teamId: 'team_123', destinationGate: gate.promise })
    await linked(props)
    expect(fetchImpl.mock.calls.some(([url]) => /vercel\/(projects|destination)/.test(url))).toBe(false)
    fireEvent.change(screen.getByLabelText('Search Vercel projects'), { target: { value: 'Jessica & Studio' } })
    await chooseProject()
    const listUrl = fetchImpl.mock.calls.find(([url]) => url.includes('/vercel/projects?'))[0]
    expect(new URL(listUrl).searchParams.get('search')).toBe('Jessica & Studio')
    fireEvent.click(screen.getByRole('button', { name: 'Save destination' }))
    await waitFor(() => expect(destinationSaves(fetchImpl)).toHaveLength(1))
    await waitFor(() => expect(fetchImpl.mock.calls.some(([url, request]) => url.endsWith('/vercel/destination') && request.method === 'GET')).toBe(true))
    expect(screen.queryByText(/Confirmed destination:/)).toBeNull()
    expect(screen.getByRole('button', { name: 'Load Vercel projects' }).disabled).toBe(true)
    expect(JSON.parse(destinationSaves(fetchImpl)[0][1].body)).toEqual({ expectedRevision: 2, projectId: 'prj_jessica', search: 'Jessica & Studio' })
    await act(async () => { gate.resolve() })
    await screen.findByText('Confirmed destination: <Jessica & Studio>')
    expect(screen.getByText(/does not publish your site/)).toBeTruthy()
    expect(document.querySelector('option a')).toBeNull()
    expect(completions(fetchImpl)).toHaveLength(1)
  })
  it('shows empty results and bounds truncated results with a search hint', async () => {
    const config = { projects: [] }
    const { props } = setup(config)
    await linked(props)
    fireEvent.click(screen.getByRole('button', { name: 'Load Vercel projects' }))
    await screen.findByText('No Vercel projects found.')
    expect(screen.queryByLabelText('Vercel project')).toBeNull()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Load Vercel projects' }).disabled).toBe(false))
    config.projects = Array.from({ length: 20 }, (_, i) => ({ id: `prj_${i}`, name: `Project ${i}` }))
    config.truncated = true
    fireEvent.click(screen.getByRole('button', { name: 'Load Vercel projects' }))
    await screen.findByText(/Showing at most 20 projects. Narrow your search/)
    expect(screen.getAllByRole('option')).toHaveLength(21)
    expect(screen.getByRole('button', { name: 'Save destination' }).disabled).toBe(true)
  })
  it('recovers an ambiguous dispatched PUT by GET without replaying PUT or OAuth', async () => {
    const { props, fetchImpl } = setup({ loseSelect: true })
    await linked(props); await chooseProject()
    fireEvent.click(screen.getByRole('button', { name: 'Save destination' }))
    const check = await screen.findByRole('button', { name: 'Check destination status' })
    await waitFor(() => expect(check.disabled).toBe(false))
    expect(screen.queryByText(/Confirmed destination:/)).toBeNull()
    fireEvent.click(check)
    await screen.findByText('Confirmed destination: <Jessica & Studio>')
    expect(destinationSaves(fetchImpl)).toHaveLength(1)
    expect(completions(fetchImpl)).toHaveLength(1)
  })
  it('leaves a null readback honestly unconfirmed and requires a new choice', async () => {
    const { props, fetchImpl } = setup({ nullDestination: true })
    await linked(props); await chooseProject()
    fireEvent.click(screen.getByRole('button', { name: 'Save destination' }))
    await screen.findByText(/intended destination is unconfirmed/)
    expect(screen.queryByText(/Confirmed destination:/)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Save destination' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Load Vercel projects' }).disabled).toBe(false)
    expect(destinationSaves(fetchImpl)).toHaveLength(1)
  })
  it.each([
    { destination: { projectId: 'other', accountId: 'user_123', name: '<Jessica & Studio>' } },
    { destination: { projectId: 'prj_jessica', accountId: 'user_123', name: 'Renamed elsewhere' } },
    { revision: 4 },
    { account: { ownerId: 'foreign', teamId: null }, destination: { projectId: 'prj_jessica', accountId: 'foreign', name: '<Jessica & Studio>' } },
  ])('does not accept a readback that differs from the acknowledged destination: %j', async (destinationReadback) => {
    const { props, fetchImpl } = setup({ destinationReadback })
    await linked(props); await chooseProject()
    fireEvent.click(screen.getByRole('button', { name: 'Save destination' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Load Vercel projects' }).disabled === false ||
      screen.queryByRole('alert') !== null).toBe(true))
    expect(screen.queryByText(/Confirmed destination:/)).toBeNull()
    expect(destinationSaves(fetchImpl)).toHaveLength(1)
  })
  it('retains an acknowledged selection when confirmation fails and retries only GET', async () => {
    const config = { failDestinationRead: true }
    const { props, fetchImpl } = setup(config)
    await linked(props); await chooseProject()
    fireEvent.click(screen.getByRole('button', { name: 'Save destination' }))
    const check = await screen.findByRole('button', { name: 'Check destination status' })
    await waitFor(() => expect(check.disabled).toBe(false))
    config.failDestinationRead = false
    fireEvent.click(check)
    await screen.findByText(/Confirmed destination:/)
    expect(destinationSaves(fetchImpl)).toHaveLength(1)
  })
  it('blocks after a conflict until explicit authoritative refresh, then uses the fresh revision', async () => {
    const config = { conflictSelect: true }
    const { props, fetchImpl, drift } = setup(config)
    await linked(props); await chooseProject()
    fireEvent.click(screen.getByRole('button', { name: 'Save destination' }))
    const refresh = await screen.findByRole('button', { name: 'Refresh destination status' })
    await waitFor(() => expect(refresh.disabled).toBe(false))
    expect(screen.getByRole('button', { name: 'Load Vercel projects' }).disabled).toBe(true)
    config.conflictSelect = false
    drift()
    fireEvent.click(refresh)
    await screen.findByText(/Destination status refreshed/)
    await chooseProject()
    fireEvent.click(screen.getByRole('button', { name: 'Save destination' }))
    await screen.findByText(/Confirmed destination:/)
    expect(destinationSaves(fetchImpl).map(([, request]) => JSON.parse(request.body).expectedRevision)).toEqual([2, 3])
  })
  it('rejects a foreign but internally valid account without offering selection', async () => {
    const { props, fetchImpl } = setup({ listAccount: { ownerId: 'foreign', teamId: null } })
    await linked(props)
    fireEvent.click(screen.getByRole('button', { name: 'Load Vercel projects' }))
    await screen.findByRole('button', { name: 'Refresh destination status' })
    expect(screen.queryByLabelText('Vercel project')).toBeNull()
    expect(destinationSaves(fetchImpl)).toHaveLength(0)
  })
  it('revalidates the connection before listing or saving and uses forbidden behavior', async () => {
    const { props, fetchImpl, drift } = setup()
    await linked(props)
    drift()
    fireEvent.click(screen.getByRole('button', { name: 'Load Vercel projects' }))
    await screen.findByRole('button', { name: 'Refresh destination status' })
    expect(fetchImpl.mock.calls.some(([url]) => url.includes('/vercel/projects?'))).toBe(false)
    props.guard.mockRejectedValue(Object.assign(new Error('forbidden'), { status: 403 }))
    fireEvent.click(screen.getByRole('button', { name: 'Refresh destination status' }))
    await waitFor(() => expect(props.onForbidden).toHaveBeenCalledOnce())
  })
  it.each(['scope', 'client', 'signal', 'unmount'])('aborts and ignores delayed project results on %s change', async (change) => {
    const gate = deferred()
    const { props, fetchImpl } = setup({ listGate: gate.promise })
    const mounted = render(<VercelConnectionPanel {...props} />)
    await prepare(); await authorize(); await callback()
    const load = await screen.findByRole('button', { name: 'Load Vercel projects' })
    await waitFor(() => expect(load.disabled).toBe(false))
    fireEvent.click(load)
    await waitFor(() => expect(fetchImpl.mock.calls.some(([url]) => url.includes('/vercel/projects?'))).toBe(true))
    const request = fetchImpl.mock.calls.find(([url]) => url.includes('/vercel/projects?'))[1]
    if (change === 'unmount') mounted.unmount()
    else mounted.rerender(<VercelConnectionPanel {...props}
      {...(change === 'scope' ? { scope: { ...scope, externalSubject: 'auth0|other' } } :
        change === 'client' ? { client: { ...props.client } } : { signal: new AbortController().signal })} />)
    expect(request.signal.aborted).toBe(true)
    await act(async () => { gate.resolve() })
    expect(screen.queryByLabelText('Vercel project')).toBeNull()
    expect(screen.queryByText(/Confirmed destination:/)).toBeNull()
    expect(destinationSaves(fetchImpl)).toHaveLength(0)
  })
  it.each(['scope', 'unmount'])('ignores a delayed destination confirmation after %s change', async (change) => {
    const gate = deferred()
    const { props, fetchImpl } = setup({ destinationGate: gate.promise })
    const mounted = render(<VercelConnectionPanel {...props} />)
    await prepare(); await authorize(); await callback()
    await chooseProject()
    fireEvent.click(screen.getByRole('button', { name: 'Save destination' }))
    await waitFor(() => expect(fetchImpl.mock.calls.some(([url, request]) => url.endsWith('/vercel/destination') && request.method === 'GET')).toBe(true))
    const request = fetchImpl.mock.calls.find(([url, request]) => url.endsWith('/vercel/destination') && request.method === 'GET')[1]
    if (change === 'unmount') mounted.unmount()
    else mounted.rerender(<VercelConnectionPanel {...props} scope={{ ...scope, bindingId: '22222222-2222-4222-8222-222222222222' }} />)
    expect(request.signal.aborted).toBe(true)
    await act(async () => { gate.resolve() })
    expect(screen.queryByText(/Confirmed destination:/)).toBeNull()
    expect(destinationSaves(fetchImpl)).toHaveLength(1)
  })
})

describe('Vercel connection flow', () => {
  it('describes the pinned v10 array response and stops if the host declines import', async () => {
    const { props, fetchImpl } = setup({ unsupportedImport: true })
    render(<VercelConnectionPanel {...props} />)
    fireEvent.click(screen.getByRole('button', { name: 'Create Vercel connection' }))
    await screen.findByText(/could not prepare the Vercel connection/)
    const schema = vercelDocument.paths['/v10/projects'].get.responses[200].content['application/json'].schema
    expect(schema).toEqual({ type: 'array', items: { type: 'object', additionalProperties: true } })
    expect(mutations(fetchImpl)).toHaveLength(1)
    expect(screen.queryByRole('button', { name: 'Authorize Vercel' })).toBeNull()
    expect(popup.location.replace).not.toHaveBeenCalled()
  })
  it('creates scoped real records and completes once, only after exact connection readback; generic setup remains separate', async () => {
    const { props, fetchImpl } = setup()
    render(<IntegrationSetupPanel {...props} />)
    expect(screen.getByRole('form', { name: 'Import connector JSON' })).toBeTruthy()
    await prepare()
    expect(mutations(fetchImpl).map(([url]) => url.split('/').at(-1))).toEqual(['import', 'publish', 'connections'])
    expect(JSON.parse(mutations(fetchImpl)[0][1].body).document).toEqual(vercelDocument)
    expect(vercelDocument.servers).toEqual([{ url: 'https://api.vercel.com' }])
    expect(vercelDocument.components.securitySchemes).toEqual({ bearerAuth: { type: 'http', scheme: 'bearer' } })
    expect(vercelDocument.security).toEqual([{ bearerAuth: [] }])
    expect(Object.keys(vercelDocument.paths)).toEqual(['/v10/projects'])
    expect(Object.keys(vercelDocument.paths['/v10/projects'])).toEqual(['get'])
    expect(vercelDocument.paths['/v10/projects'].get.parameters).toBeUndefined()
    expect(vercelDocument.paths['/v10/projects'].get.responses[200].content['application/json'].schema)
      .toEqual({ type: 'array', items: { type: 'object', additionalProperties: true } })
    const creation = JSON.parse(mutations(fetchImpl)[2][1].body)
    expect(creation.connectorRef.digest).toBe('b'.repeat(64))
    expect(creation.operationIds).toEqual(['normalized-project-read'])
    expect(fetchImpl.mock.calls.every(([url]) => url.startsWith('https://ops.example/'))).toBe(true)
    await authorize()
    expect(window.open).toHaveBeenCalledWith('about:blank', '_blank')
    expect(popup.location.replace).toHaveBeenCalledWith(`https://vercel.com/integrations/leaf/new?state=${state}`)
    const receiver = channels.at(-1).onmessage
    await callback()
    await screen.findByText(/Verified personal Vercel account/)
    await act(async () => { await receiver({ data: { state, outcome: 'authorized', code: 'again', configurationId: 'icfg_123' } }) })
    expect(completions(fetchImpl)).toHaveLength(1)
    expect(fetchImpl.mock.calls.at(-1)[1].method).toBe('GET')
    expect(channels.at(-1).close).toHaveBeenCalled()
    expect(screen.queryByRole('form', { name: 'Define typed dataset' })).toBeNull()
    expect(fetchImpl.mock.calls.some(([url]) => /datasets|sync|api\.vercel\.com/.test(url))).toBe(false)
  })
  it('rejects a mismatched wrapped-record definition before publishing or creating a connection', async () => {
    const { props, fetchImpl } = setup({ recordsPointer: '/projects' })
    render(<VercelConnectionPanel {...props} />)
    fireEvent.click(screen.getByRole('button', { name: 'Create Vercel connection' }))
    await screen.findByRole('alert')
    expect(mutations(fetchImpl).map(([url]) => url.split('/').at(-1))).toEqual(['import'])
    expect(screen.queryByRole('button', { name: 'Authorize Vercel' })).toBeNull()
    expect(popup.location.replace).not.toHaveBeenCalled()
  })
  it('retains exact IDs and never repeats an unknown import during status recovery', async () => {
    const { props, fetchImpl } = setup({ loseImport: true })
    render(<VercelConnectionPanel {...props} />)
    fireEvent.click(screen.getByRole('button', { name: 'Create Vercel connection' }))
    await screen.findByRole('button', { name: 'Check Vercel status' })
    const id = JSON.parse(mutations(fetchImpl)[0][1].body).options.id
    fireEvent.click(screen.getByRole('button', { name: 'Check Vercel status' }))
    await waitFor(() => expect(fetchImpl.mock.calls.at(-1)[0]).toMatch(new RegExp(`/connectors/${id}$`)))
    expect(mutations(fetchImpl)).toHaveLength(1)
    expect(screen.queryByRole('button', { name: 'Authorize Vercel' })).toBeNull()
  })
  it('cancels a settled attempt, closes only its popup and retains connection IDs for a new attempt', async () => {
    const { props, fetchImpl } = setup()
    render(<VercelConnectionPanel {...props} />)
    await prepare(); await authorize()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel authorization' }))
    expect(popup.close).toHaveBeenCalledOnce()
    await authorize()
    expect(mutations(fetchImpl).filter(([url]) => url.endsWith('/connections'))).toHaveLength(1)
    expect(mutations(fetchImpl).filter(([url]) => url.endsWith('/attempt'))).toHaveLength(2)
  })
  it.each([{ authorizationUrl: `https://evil.test/integrations/leaf/new?state=${state}` },
    { callbackUri: 'https://evil.test/vercel-callback.html' }])('does not navigate to mismatched destinations', async (config) => {
    const { props } = setup(config)
    render(<VercelConnectionPanel {...props} />)
    await prepare()
    fireEvent.click(screen.getByRole('button', { name: 'Authorize Vercel' }))
    await screen.findByRole('alert')
    expect(popup.location.replace).not.toHaveBeenCalled()
    expect(popup.close).toHaveBeenCalled()
  })
  it('keeps unconfigured authorization truthful and permits an explicit new attempt', async () => {
    const { props } = setup({ unconfigured: true })
    render(<VercelConnectionPanel {...props} />)
    await prepare()
    fireEvent.click(screen.getByRole('button', { name: 'Authorize Vercel' }))
    await screen.findByText(/administrator to finish setup/)
    expect(popup.location.replace).not.toHaveBeenCalled()
    expect(screen.queryByText(/account linked\./)).toBeNull()
  })
  it('rejects wrong state and revision drift before any completion', async () => {
    const { props, fetchImpl, drift } = setup()
    render(<VercelConnectionPanel {...props} />)
    await prepare(); await authorize()
    await callback({ state: 'x'.repeat(43), outcome: 'authorized', code: 'c', configurationId: 'icfg_123' })
    expect(completions(fetchImpl)).toHaveLength(0)
    drift(); await callback()
    await screen.findByRole('alert')
    expect(completions(fetchImpl)).toHaveLength(0)
  })
  it('expires an attempt without exchanging its callback', async () => {
    const { props, fetchImpl } = setup()
    render(<VercelConnectionPanel {...props} />)
    await prepare(); await authorize()
    const receiver = channels.at(-1).onmessage
    const now = Date.now()
    vi.spyOn(Date, 'now').mockReturnValue(now + 120000)
    await act(async () => { await receiver({ data: { state, outcome: 'authorized', code: 'c', configurationId: 'icfg_123' } }) })
    expect(completions(fetchImpl)).toHaveLength(0)
  })
  it('keeps an unknown issue read-only and never navigates or issues again', async () => {
    const { props, fetchImpl } = setup({ loseIssue: true })
    render(<VercelConnectionPanel {...props} />)
    await prepare()
    fireEvent.click(screen.getByRole('button', { name: 'Authorize Vercel' }))
    await screen.findByRole('button', { name: 'Check Vercel status' })
    fireEvent.click(screen.getByRole('button', { name: 'Check Vercel status' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Check Vercel status' }).disabled).toBe(false))
    expect(mutations(fetchImpl).filter(([url]) => url.endsWith('/attempt'))).toHaveLength(1)
    expect(popup.location.replace).not.toHaveBeenCalled()
  })
  it('denial consumes the attempt and offers an explicit new attempt without completion', async () => {
    const { props, fetchImpl } = setup()
    render(<VercelConnectionPanel {...props} />)
    await prepare(); await authorize()
    await callback({ state, outcome: 'denied' })
    await screen.findByText(/Authorization was declined/)
    expect(screen.getByRole('button', { name: 'Authorize Vercel' })).toBeTruthy()
    expect(completions(fetchImpl)).toHaveLength(0)
  })
  it('rejects a host attempt that reuses an already consumed state', async () => {
    const { props, fetchImpl } = setup({ replayState: true })
    render(<VercelConnectionPanel {...props} />)
    await prepare(); await authorize()
    await callback({ state, outcome: 'denied' })
    popup.location.replace.mockClear()
    fireEvent.click(screen.getByRole('button', { name: 'Authorize Vercel' }))
    await screen.findByRole('alert')
    expect(popup.location.replace).not.toHaveBeenCalled()
    expect(completions(fetchImpl)).toHaveLength(0)
  })
  it('a revoked guard prevents all later mutations even after resources have been created', async () => {
    const { props, fetchImpl } = setup()
    render(<VercelConnectionPanel {...props} />)
    await prepare()
    props.guard.mockRejectedValue(Object.assign(new Error('forbidden'), { status: 403 }))
    fireEvent.click(screen.getByRole('button', { name: 'Authorize Vercel' }))
    await waitFor(() => expect(props.onForbidden).toHaveBeenCalledOnce())
    expect(mutations(fetchImpl)).toHaveLength(3)
    expect(popup.location.replace).not.toHaveBeenCalled()
  })
  it.each([{ loseCompletion: true }, { noCustody: true }, { failCompletion: true }, { wrongStudioOwner: true }])('does not report linked from an uncertain or failed completion', async (config) => {
    const { props, fetchImpl } = setup(config)
    render(<VercelConnectionPanel {...props} />)
    await prepare(); await authorize(); await callback()
    await screen.findByRole('alert')
    fireEvent.click(screen.getByRole('button', { name: 'Check Vercel status' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Check Vercel status' }).disabled).toBe(false))
    expect(completions(fetchImpl)).toHaveLength(1)
    expect(screen.queryByText(/Verified personal/)).toBeNull()
  })
  it('reports cleanup attention after verified team installation without claiming publishing readiness', async () => {
    const { props } = setup({ teamId: 'team_123', cleanup: 'needs-attention' })
    render(<VercelConnectionPanel {...props} />)
    await prepare(); await authorize(); await callback()
    await screen.findByText(/Verified Vercel team account.*Cleanup needs attention/)
  })
  it.each(['editor', 'reviewer', 'read_only'])('dispatches nothing for %s', (projectRole) => {
    const { props, fetchImpl } = setup()
    render(<VercelConnectionPanel {...props} scope={{ ...scope, projectRole }} />)
    expect(screen.queryByRole('button')).toBeNull()
    expect(fetchImpl).not.toHaveBeenCalled()
  })
  it('clears callback listeners on a changed full scope and abort', async () => {
    const { props, fetchImpl, controller } = setup()
    const mounted = render(<VercelConnectionPanel {...props} />)
    await prepare(); await authorize()
    const receiver = channels.at(-1).onmessage
    mounted.rerender(<VercelConnectionPanel {...props} scope={{ ...scope, externalSubject: 'auth0|other' }} />)
    await act(async () => { await receiver({ data: { state, outcome: 'authorized', code: 'c', configurationId: 'icfg_123' } }) })
    controller.abort()
    expect(completions(fetchImpl)).toHaveLength(0)
    expect(channels.at(-1).close).toHaveBeenCalled()
  })
})
