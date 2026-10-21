import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ReadableStream } from 'node:stream/web'
import { TextDecoder, TextEncoder } from 'node:util'
import { randomUUID } from 'node:crypto'
import { OpsWorkspaceGround, scopeCacheIdentity } from './OpsWorkspaceGround.jsx'
import { createIntegrationsClient } from './client.js'

vi.mock('../api.js', () => ({ authHeaders: () => ({ Authorization: 'Bearer current' }), noteUnauthorized: vi.fn() }))
// Keep the real dataset chart, formatter, and table. Only supply layout dimensions.
vi.mock('recharts', async (original) => {
  const actual = await original()
  const { cloneElement } = await import('react')
  return { ...actual, ResponsiveContainer: ({ children }) => cloneElement(children, { width: 500, height: 240 }) }
})
const projectId = '11111111-1111-4111-8111-111111111111'
const otherProject = '22222222-2222-4222-8222-222222222222'
const reference = { id: 'orders', version: 3, digest: 'a'.repeat(64) }
const fields = [
  { id: 'day', label: 'Day', type: { kind: 'date' }, nullable: false, sensitivity: 'internal' },
  { id: 'total', label: 'Total', type: { kind: 'count' }, nullable: false, sensitivity: 'internal' },
]
const element = { schema_version: 1, id: 'order-view', title: 'Order view', dataset_ref: reference,
  query: { fields: ['day', 'total'], limit: 100 }, kind: 'dataset.table', props: { columns: ['day', 'total'], row_limit: 100 } }
const saved = { element, revision: 2 }
const actionElement = { schema_version: 1, id: 'send-order', title: 'Send order', kind: 'integration.action',
  connection_id: 'orders-connection', connector_ref: reference, operation_ref: { ...reference, id: 'send' },
  form: { fields: [{ id: 'note', label: 'Order note', kind: 'text', required: true, nullable: false }], submit_label: 'Send order now' } }
const savedAction = { element: actionElement, revision: 4 }
const actionConnection = { id: actionElement.connection_id, state: 'active', connector_ref: reference, operation_ids: ['send'], grant_revision: 7 }
const actionRun = { schema_version: 1, run_id: projectId, revision: 1, connection_id: actionElement.connection_id,
  connector_ref: reference, operation_ref: actionElement.operation_ref, grant_revision: 7, submission_digest: 'b'.repeat(64),
  authority_binding_digest: 'c'.repeat(64), created_at: '2026-10-20T12:00:00Z', updated_at: '2026-10-20T12:00:00Z',
  state: 'accepted', effect_outcome: 'not_dispatched' }
const scope = { tenantId: projectId, projectId, platformUserId: projectId, bindingId: projectId,
  projectRole: 'owner', externalAuthority: 'auth0', externalSubject: 'auth0|one' }
const read = { schema_version: 1, dataset_ref: reference, fields,
  rows: [{ key: 'one', values: { day: '2026-10-20', total: 1234 } }], next_cursor: null, status: 'ready',
  freshness: { source_updated_at: null, synced_at: null, fetched_at: '2026-10-20T12:00:00Z' } }
const metadata = { reference, revision: 1, title: 'Authorized orders', fields, lastAttempt: null, freshness: read.freshness }
const page = (records) => ({ records, nextCursor: null })
function response(value, status = 200) {
  return { ok: status === 200, status, headers: { get: (key) => key === 'content-type' ? 'application/json' : null },
    body: new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode(JSON.stringify(value))); controller.close() } }) }
}
function controller() {
  let snapshot = { status: 'active', generation: 1 }
  const listeners = new Set()
  return { getSnapshot: () => snapshot, subscribe: (fn) => { listeners.add(fn); return () => listeners.delete(fn) },
    change: (next) => { snapshot = { ...snapshot, ...next }; listeners.forEach((fn) => fn()) } }
}
function setup() {
  const sessionController = controller()
  const fetchImpl = vi.fn(async (url, options) => {
    if (url.endsWith('/scope')) return response({ ...scope, projectId: url.split('/projects/')[1].split('/')[0] })
    if (url.includes('/datasets?')) return response(page([metadata]))
    if (url.includes('/elements?')) return response(page([saved]))
    if (url.endsWith('/query')) return response(read)
    if (options.method === 'POST' || options.method === 'PUT') return response({ element: JSON.parse(options.body), revision: 3 })
    if (url.includes('/elements/order-view')) return response({ element, revision: Number(new URL(url).searchParams.get('revision') || 2) })
    throw new Error('Unexpected route')
  })
  const client = createIntegrationsClient({ origin: 'https://ops.example', fetchImpl })
  const props = { active: true, projectId, signedIn: true, accountScope: 'account-a',
    session: { ...sessionController.getSnapshot(), controller: sessionController }, client }
  return { props, fetchImpl, sessionController }
}
function setupAction() {
  vi.stubGlobal('crypto', { randomUUID: vi.fn(randomUUID) })
  const harness = setup()
  const original = harness.fetchImpl.getMockImplementation()
  const authority = { scope, connection: actionConnection }
  harness.fetchImpl.mockImplementation((url, options) => {
    if (url.endsWith('/scope')) return Promise.resolve(response(authority.scope))
    if (url.includes('/elements?')) return Promise.resolve(response(page([saved, savedAction])))
    if (url.includes('/connections/')) return Promise.resolve(response(authority.connection))
    if (url.endsWith('/integrations/actions')) return Promise.resolve(response({ status: 'admitted', run: actionRun, replayed: false }))
    if (url.includes('/integrations/actions/')) return Promise.resolve(response({ run: actionRun, receipts: [] }))
    if (url.includes('/elements/send-order')) return Promise.resolve(response({ ...savedAction, revision: 1 }))
    return original(url, options)
  })
  return { ...harness, authority }
}
async function openAction() {
  await loaded()
  fireEvent.click(screen.getByRole('button', { name: 'Send order · revision 4' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Send order now' })).toBeEnabled())
}
function submitAction() {
  fireEvent.change(screen.getByLabelText('Order note (required)'), { target: { value: 'Ship today' } })
  fireEvent.click(screen.getByRole('button', { name: 'Send order now' }))
}
const actionPosts = (fetchImpl) => fetchImpl.mock.calls.filter(([url, options]) => url.endsWith('/integrations/actions') && options.method === 'POST')
beforeEach(() => { vi.stubGlobal('TextDecoder', TextDecoder) })
afterEach(() => { cleanup(); vi.unstubAllGlobals() })
async function loaded() { await screen.findByRole('option', { name: 'Authorized orders · version 3' }) }
function fill(kind = 'table') {
  fireEvent.change(screen.getByLabelText('Dataset'), { target: { value: JSON.stringify([reference.id, reference.version, reference.digest]) } })
  fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'New order view' } })
  fireEvent.change(screen.getByLabelText('Stable ID'), { target: { value: 'new-order-view' } })
  if (kind === 'line') {
    fireEvent.change(screen.getByLabelText('View type'), { target: { value: 'line' } })
    fireEvent.change(screen.getByLabelText('X field'), { target: { value: 'day' } })
    fireEvent.change(screen.getByLabelText('Y field'), { target: { value: 'total' } })
  } else {
    fireEvent.click(screen.getByLabelText('Day'))
    fireEvent.click(screen.getByLabelText('Total'))
  }
}
it('renders real native saved data with shared formatting and keeps display preferences separate from the query', async () => {
  const { props, fetchImpl } = setup()
  render(<OpsWorkspaceGround {...props} />)
  await loaded()
  fireEvent.click(screen.getByRole('button', { name: 'Order view · revision 2' }))
  expect(await screen.findByRole('table', { name: 'Order view data' })).toBeInTheDocument()
  expect(screen.getByText('1,234')).toBeInTheDocument()
  const queries = fetchImpl.mock.calls.filter(([url]) => url.endsWith('/query'))
  expect(JSON.parse(queries[0][1].body)).toEqual(element.query)
  fireEvent.change(screen.getByLabelText('Display locale'), { target: { value: 'de-DE' } })
  fireEvent.change(screen.getByLabelText('Display timezone'), { target: { value: 'Asia/Tokyo' } })
  fireEvent.change(screen.getByLabelText('Hour cycle'), { target: { value: '12h' } })
  expect(screen.getByText('1.234')).toBeInTheDocument()
  expect(fetchImpl.mock.calls.filter(([url]) => url.endsWith('/query'))).toHaveLength(1)
  expect(screen.getByText(/Dataset reporting timezone is unchanged/)).toBeInTheDocument()
  expect(fetchImpl.mock.calls.some(([url]) => url.includes('/sync'))).toBe(false)
})
it.each(['table', 'line'])('saves strict native %s recipes with exact dataset refs', async (kind) => {
  const { props, fetchImpl } = setup()
  render(<OpsWorkspaceGround {...props} />)
  await loaded()
  fill(kind)
  fireEvent.click(screen.getByRole('button', { name: 'Save view' }))
  await screen.findByText('Saved New order view, revision 3.')
  const saves = fetchImpl.mock.calls.filter(([url, options]) => url.endsWith('/elements') && options.method === 'POST')
  expect(saves).toHaveLength(1)
  expect(JSON.parse(saves[0][1].body)).toEqual({ schema_version: 1, id: 'new-order-view', title: 'New order view', dataset_ref: reference,
    query: { fields: ['day', 'total'], limit: 100 }, kind: `dataset.${kind}`,
    props: kind === 'table' ? { columns: ['day', 'total'], row_limit: 100 } : { x_field: 'day', y_field: 'total' } })
  expect(saves[0][1].headers['If-Match']).toBeUndefined()
  expect(screen.getByText('Saved New order view, revision 3.')).toHaveFocus()
})
it('prevents duplicate pending submissions and retains form inputs after failure', async () => {
  const { props, fetchImpl } = setup()
  const original = fetchImpl.getMockImplementation()
  let finish
  fetchImpl.mockImplementation((url, options) => url.endsWith('/elements') && options.method === 'POST'
    ? new Promise((resolve) => { finish = resolve }) : original(url, options))
  render(<OpsWorkspaceGround {...props} />)
  await loaded()
  fill()
  const form = screen.getByRole('form', { name: 'Compose native view' })
  fireEvent.submit(form)
  fireEvent.submit(form)
  expect(screen.getByRole('button', { name: 'Save view' })).toBeDisabled()
  expect(fetchImpl.mock.calls.filter(([url, options]) => url.endsWith('/elements') && options.method === 'POST')).toHaveLength(1)
  await act(async () => finish(response({}, 503)))
  expect(await screen.findByRole('alert')).toHaveTextContent('The integration request failed.')
  expect(screen.getByLabelText('Title')).toHaveValue('New order view')
  expect(screen.getByLabelText('Stable ID')).toHaveValue('new-order-view')
  expect(screen.getByRole('button', { name: 'Save view' })).toBeEnabled()
})
it('updates only explicitly selected current recipes with If-Match and labels older recipes as live', async () => {
  const { props, fetchImpl } = setup()
  render(<OpsWorkspaceGround {...props} />)
  await loaded()
  fireEvent.click(screen.getByRole('button', { name: 'Order view · revision 2' }))
  await screen.findByRole('table', { name: 'Order view data' })
  fireEvent.click(screen.getByRole('button', { name: 'Edit current recipe' }))
  expect(screen.getByLabelText('Stable ID')).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: 'Update view' }))
  await screen.findByText('Saved Order view, revision 3.')
  const update = fetchImpl.mock.calls.find(([, options]) => options.method === 'PUT')
  expect(update[0]).toContain('/elements/order-view')
  expect(update[1].headers['If-Match']).toBe('2')
  await waitFor(() => expect(screen.getByRole('button', { name: 'Read revision' })).toBeEnabled())
  fireEvent.change(screen.getByLabelText('Earlier recipe revision'), { target: { value: '1' } })
  fireEvent.click(screen.getByRole('button', { name: 'Read revision' }))
  expect(await screen.findByText(/Recipe revision 1 · Historical recipe/)).toHaveTextContent('Views query live data')
  expect(screen.queryByRole('button', { name: 'Edit current recipe' })).toBeNull()
})
it.each(['project', 'account', 'logout', 'generation', 'status'])('aborts and hides old data on %s changes, ignoring late responses', async (change) => {
  const { props, fetchImpl, sessionController } = setup()
  const original = fetchImpl.getMockImplementation()
  let finish
  fetchImpl.mockImplementation((url, options) => url.endsWith('/query') ? new Promise((resolve) => { finish = resolve }) : original(url, options))
  const { rerender } = render(<OpsWorkspaceGround {...props} />)
  await loaded()
  fireEvent.click(screen.getByRole('button', { name: 'Order view · revision 2' }))
  const request = fetchImpl.mock.calls.find(([url]) => url.endsWith('/query'))
  const next = { ...props }
  if (change === 'project') next.projectId = otherProject
  if (change === 'account') next.accountScope = 'account-b'
  if (change === 'logout') next.signedIn = false
  if (change === 'generation') act(() => sessionController.change({ generation: 2 }))
  if (change === 'status') act(() => sessionController.change({ status: 'ended' }))
  rerender(<OpsWorkspaceGround {...next} />)
  expect(request[1].signal.aborted).toBe(true)
  expect(screen.queryByRole('table')).toBeNull()
  await act(async () => finish(response(read)))
  expect(screen.queryByText('1,234')).toBeNull()
})
it('clears all inaccessible metadata and recipes on a forbidden response', async () => {
  const { props, fetchImpl } = setup()
  const original = fetchImpl.getMockImplementation()
  fetchImpl.mockImplementation((url, options) => url.endsWith('/query') ? Promise.resolve(response({}, 403)) : original(url, options))
  render(<OpsWorkspaceGround {...props} />)
  await loaded()
  fireEvent.click(screen.getByRole('button', { name: 'Order view · revision 2' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('You do not have permission')
  expect(screen.queryByText('Authorized orders · version 3')).toBeNull()
  expect(screen.queryByRole('form', { name: 'Compose native view' })).toBeNull()
})
it('stays mounted and dormant off-profile, then revalidates scope on return without changing parent identity', async () => {
  const { props, fetchImpl } = setup()
  const { container, rerender } = render(<OpsWorkspaceGround {...props} />)
  await loaded()
  const node = container.querySelector('[data-ground="ops"]')
  rerender(<OpsWorkspaceGround {...props} active={false} />)
  expect(node).toHaveAttribute('hidden')
  const requests = fetchImpl.mock.calls.length
  await act(async () => {})
  expect(fetchImpl).toHaveBeenCalledTimes(requests)
  rerender(<OpsWorkspaceGround {...props} />)
  await loaded()
  expect(container.querySelector('[data-ground="ops"]')).toBe(node)
  expect(fetchImpl.mock.calls.filter(([url]) => url.endsWith('/scope'))).toHaveLength(2)
  expect(props.projectId).toBe(projectId)
  expect(props.session.controller.getSnapshot()).toEqual({ generation: 1, status: 'active' })
})
it('names demo and empty setup honestly without fake provider activity', async () => {
  const { props, fetchImpl } = setup()
  const { rerender } = render(<OpsWorkspaceGround {...props} mock />)
  expect(screen.getByText('Integrations are unavailable in demo mode.')).toBeInTheDocument()
  expect(fetchImpl).not.toHaveBeenCalled()
  const original = fetchImpl.getMockImplementation()
  fetchImpl.mockImplementation((url, options) => url.includes('/datasets?') || url.includes('/elements?') ? Promise.resolve(response(page([]))) : original(url, options))
  rerender(<OpsWorkspaceGround {...props} />)
  expect(await screen.findByText('No connection or dataset is available. Integration setup is needed.')).toBeInTheDocument()
})
it('partitions cache identity by verified tenant, principal, binding, role, project, and session generation', () => {
  const original = scopeCacheIdentity(scope, 1)
  for (const key of ['tenantId', 'projectId', 'platformUserId', 'bindingId', 'projectRole', 'externalSubject']) {
    expect(scopeCacheIdentity({ ...scope, [key]: 'different' }, 1)).not.toBe(original)
  }
  expect(scopeCacheIdentity(scope, 2)).not.toBe(original)
})
it('opens mixed recipes independently, shares duplicate protection and sends exact frozen action arguments', async () => {
  const { props, fetchImpl } = setupAction()
  render(<OpsWorkspaceGround {...props} />)
  await openAction()
  expect(fetchImpl.mock.calls.filter(([url]) => url.endsWith('/query'))).toHaveLength(0)
  expect(screen.queryByRole('button', { name: 'Edit current recipe' })).toBeNull()
  expect(screen.queryByText(/Views query live data/)).toBeNull()
  fireEvent.change(screen.getByLabelText('Order note (required)'), { target: { value: 'Ship today' } })
  const form = screen.getByRole('button', { name: 'Send order now' }).closest('form')
  fireEvent.submit(form)
  fireEvent.submit(form)
  await screen.findByText('Submission accepted. Execution is pending.')
  expect(actionPosts(fetchImpl)).toHaveLength(1)
  expect(globalThis.crypto.randomUUID).toHaveBeenCalledTimes(1)
  expect(JSON.parse(actionPosts(fetchImpl)[0][1].body)).toEqual({ schema_version: 1,
    connection_id: actionElement.connection_id, connector_ref: reference, operation_ref: actionElement.operation_ref,
    expected_grant_revision: 7, arguments: { note: 'Ship today' }, idempotency_key: expect.stringMatching(/^[0-9a-f-]{36}$/i) })
  expect(fetchImpl.mock.calls.filter(([url]) => url.endsWith('/scope'))).toHaveLength(2)
  expect(fetchImpl.mock.calls.filter(([url]) => url.includes('/connections/'))).toHaveLength(2)
  fireEvent.click(screen.getByRole('button', { name: 'Order view · revision 2' }))
  await screen.findByRole('table', { name: 'Order view data' })
  expect(screen.queryByLabelText('Order note (required)')).toBeNull()
})
it('retains inputs and recovers an ambiguous submission only explicitly with the identical request and key', async () => {
  const { props, fetchImpl } = setupAction()
  const original = fetchImpl.getMockImplementation()
  let attempts = 0
  fetchImpl.mockImplementation((url, options) => url.endsWith('/integrations/actions') && ++attempts === 1
    ? Promise.reject(new Error('private provider error')) : original(url, options))
  render(<OpsWorkspaceGround {...props} />)
  await openAction()
  submitAction()
  const recover = await screen.findByRole('button', { name: 'Recover submission' })
  expect(screen.getByLabelText('Order note (required)')).toHaveValue('Ship today')
  expect(screen.getByText(/Local cancellation does not undo an action/)).toBeInTheDocument()
  expect(screen.queryByText('private provider error')).toBeNull()
  expect(actionPosts(fetchImpl)).toHaveLength(1)
  fireEvent.click(recover)
  await screen.findByText('Submission accepted. Execution is pending.')
  expect(actionPosts(fetchImpl)).toHaveLength(2)
  expect(actionPosts(fetchImpl)[1][1].body).toBe(actionPosts(fetchImpl)[0][1].body)
  expect(globalThis.crypto.randomUUID).toHaveBeenCalledTimes(1)
})
it('checks a known run using GET and renders canonical receipts with personal display preferences', async () => {
  const { props, fetchImpl } = setupAction()
  const original = fetchImpl.getMockImplementation()
  const completed = { ...actionRun, revision: 2, state: 'succeeded', effect_outcome: 'applied' }
  const receipt = { schema_version: 1, receipt_id: otherProject, run_id: actionRun.run_id, sequence: 1,
    submission_digest: actionRun.submission_digest, operation_ref: actionRun.operation_ref,
    observed_at: actionRun.updated_at, effect_outcome: 'applied' }
  fetchImpl.mockImplementation((url, options) => url.includes('/integrations/actions/')
    ? Promise.resolve(response({ run: completed, receipts: [receipt] })) : original(url, options))
  render(<OpsWorkspaceGround {...props} />)
  await openAction()
  submitAction()
  fireEvent.click(await screen.findByRole('button', { name: 'Check status' }))
  const history = await screen.findByRole('list', { name: 'Action history' })
  expect(history).toHaveTextContent('Action applied')
  const times = [...history.querySelectorAll('time')].map((node) => node.textContent)
  fireEvent.change(screen.getByLabelText('Display timezone'), { target: { value: 'Asia/Tokyo' } })
  expect([...history.querySelectorAll('time')].map((node) => node.textContent)).not.toEqual(times)
  expect(actionPosts(fetchImpl)).toHaveLength(1)
  const statusCalls = fetchImpl.mock.calls.filter(([url]) => url.includes('/integrations/actions/'))
  expect(statusCalls).toHaveLength(1)
  expect(statusCalls[0][1].method).toBe('GET')
})
it.each(['reviewer', 'read_only'])('never loads execution authority for a %s', async (projectRole) => {
  const { props, fetchImpl, authority } = setupAction()
  authority.scope = { ...scope, projectRole }
  render(<OpsWorkspaceGround {...props} />)
  await loaded()
  fireEvent.click(screen.getByRole('button', { name: 'Send order · revision 4' }))
  await screen.findByText('You do not have permission to perform this action.')
  expect(fetchImpl.mock.calls.some(([url]) => url.includes('/connections/') || url.endsWith('/integrations/actions'))).toBe(false)
  expect(screen.queryByRole('button', { name: 'Send order now' })).toBeNull()
})
it('opens historical actions read-only without reading execution authority or querying a dataset', async () => {
  const { props, fetchImpl } = setupAction()
  render(<OpsWorkspaceGround {...props} />)
  await openAction()
  const connections = fetchImpl.mock.calls.filter(([url]) => url.includes('/connections/')).length
  fireEvent.change(screen.getByLabelText('Earlier recipe revision'), { target: { value: '1' } })
  fireEvent.click(screen.getByRole('button', { name: 'Read revision' }))
  await screen.findByText(/Historical actions are read-only/)
  expect(screen.queryByRole('button', { name: 'Send order now' })).toBeNull()
  expect(fetchImpl.mock.calls.filter(([url]) => url.includes('/connections/'))).toHaveLength(connections)
  expect(fetchImpl.mock.calls.some(([url]) => url.endsWith('/query'))).toBe(false)
})
it.each([
  { state: 'revoked' }, { connector_ref: { ...reference, digest: 'd'.repeat(64) } }, { operation_ids: ['other'] },
])('refuses unavailable or mismatched current connection authority %j', async (change) => {
  const { props, fetchImpl, authority } = setupAction()
  authority.connection = { ...actionConnection, ...change }
  render(<OpsWorkspaceGround {...props} />)
  await loaded()
  fireEvent.click(screen.getByRole('button', { name: 'Send order · revision 4' }))
  await screen.findByRole('button', { name: 'Retry action authority' })
  expect(screen.queryByRole('button', { name: 'Send order now' })).toBeNull()
  expect(actionPosts(fetchImpl)).toHaveLength(0)
})
it.each(['scope', 'grant', 'revoked', 'ref'])('rechecks %s drift before submission and never refreshes the frozen grant', async (change) => {
  const { props, fetchImpl, authority } = setupAction()
  render(<OpsWorkspaceGround {...props} />)
  await openAction()
  if (change === 'scope') authority.scope = { ...scope, bindingId: otherProject }
  if (change === 'grant') authority.connection = { ...actionConnection, grant_revision: 8 }
  if (change === 'revoked') authority.connection = { ...actionConnection, state: 'revoked' }
  if (change === 'ref') authority.connection = { ...actionConnection, connector_ref: { ...reference, version: 4 } }
  submitAction()
  const retry = await screen.findByRole('button', { name: 'Retry action authority' })
  expect(actionPosts(fetchImpl)).toHaveLength(0)
  expect(screen.getByLabelText('Order note (required)')).toHaveValue('Ship today')
  if (change === 'grant') {
    fireEvent.click(retry)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Recover submission' })).toBeDisabled())
    expect(actionPosts(fetchImpl)).toHaveLength(0)
  }
})
it('rechecks authority before known-run status and refuses grant drift without a GET', async () => {
  const { props, fetchImpl, authority } = setupAction()
  render(<OpsWorkspaceGround {...props} />)
  await openAction()
  submitAction()
  const check = await screen.findByRole('button', { name: 'Check status' })
  authority.connection = { ...actionConnection, grant_revision: 8 }
  fireEvent.click(check)
  await screen.findByRole('button', { name: 'Retry action authority' })
  expect(fetchImpl.mock.calls.filter(([url]) => url.includes('/integrations/actions/'))).toHaveLength(0)
})
it.each(['project', 'account', 'generation', 'profile', 'unmount'])('aborts action calls and discards late responses on %s invalidation', async (change) => {
  const { props, fetchImpl, sessionController } = setupAction()
  const original = fetchImpl.getMockImplementation()
  let finish
  fetchImpl.mockImplementation((url, options) => url.endsWith('/integrations/actions')
    ? new Promise((resolve) => { finish = resolve }) : original(url, options))
  const { rerender, unmount } = render(<OpsWorkspaceGround {...props} />)
  await openAction()
  submitAction()
  await waitFor(() => expect(actionPosts(fetchImpl)).toHaveLength(1))
  const signal = actionPosts(fetchImpl)[0][1].signal
  if (change === 'unmount') unmount()
  else if (change === 'generation') act(() => sessionController.change({ generation: 2 }))
  else rerender(<OpsWorkspaceGround {...props} {...(change === 'project' ? { projectId: otherProject } :
    change === 'account' ? { accountScope: 'account-b' } : { active: false })} />)
  expect(signal.aborted).toBe(true)
  expect(screen.queryByLabelText('Order note (required)')).toBeNull()
  await act(async () => finish(response({ status: 'admitted', run: actionRun, replayed: false })))
  expect(screen.queryByRole('region', { name: 'Action run status' })).toBeNull()
})
it('links the shared caller timeout to host I/O and rejects an ignored late response without claiming rollback', async () => {
  const { props, fetchImpl } = setupAction()
  const original = fetchImpl.getMockImplementation()
  let finish
  fetchImpl.mockImplementation((url, options) => url.endsWith('/integrations/actions')
    ? new Promise((resolve) => { finish = resolve }) : original(url, options))
  render(<OpsWorkspaceGround {...props} />)
  await openAction()
  vi.useFakeTimers()
  try {
    await act(async () => submitAction())
    expect(actionPosts(fetchImpl)).toHaveLength(1)
    const signal = actionPosts(fetchImpl)[0][1].signal
    await act(async () => vi.advanceTimersByTime(45000))
    expect(signal.aborted).toBe(true)
    expect(screen.getByText(/Local cancellation does not undo an action/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Recover submission' })).toBeEnabled()
    await act(async () => finish(response({ status: 'admitted', run: actionRun, replayed: false })))
    expect(actionPosts(fetchImpl)).toHaveLength(1)
    expect(screen.queryByText('Submission accepted. Execution is pending.')).toBeNull()
  } finally { vi.useRealTimers() }
})
it('rejects stale callback continuations even when a supplied client ignores cancellation', async () => {
  const { props, fetchImpl } = setupAction()
  let finish
  vi.spyOn(props.client, 'getScope').mockResolvedValueOnce(scope).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  const { rerender } = render(<OpsWorkspaceGround {...props} />)
  await openAction()
  submitAction()
  await waitFor(() => expect(finish).toBeTypeOf('function'))
  rerender(<OpsWorkspaceGround {...props} active={false} />)
  await act(async () => finish(scope))
  expect(actionPosts(fetchImpl)).toHaveLength(0)
  expect(fetchImpl.mock.calls.filter(([url]) => url.includes('/connections/'))).toHaveLength(1)
  expect(screen.queryByLabelText('Order note (required)')).toBeNull()
})
it('offers a fixed-copy retry for failed connection reads and admits only the current connection', async () => {
  const { props, fetchImpl } = setupAction()
  const original = fetchImpl.getMockImplementation()
  let failConnection = true
  fetchImpl.mockImplementation((url, options) => url.includes('/connections/') && failConnection
    ? Promise.resolve(response({ secret: 'provider secret' }, 503)) : original(url, options))
  render(<OpsWorkspaceGround {...props} />)
  await loaded()
  fireEvent.click(screen.getByRole('button', { name: 'Send order · revision 4' }))
  const retry = await screen.findByRole('button', { name: 'Retry action authority' })
  await waitFor(() => expect(retry).toBeEnabled())
  expect(screen.getByText('This action is unavailable.')).toBeInTheDocument()
  expect(screen.queryByText('provider secret')).toBeNull()
  failConnection = false
  fireEvent.click(retry)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Send order now' })).toBeEnabled())
  expect(actionPosts(fetchImpl)).toHaveLength(0)
})
