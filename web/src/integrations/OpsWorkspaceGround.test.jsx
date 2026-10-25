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
const workflowSnapshot = {
  definition: { schema_version: 1, id: reference.id, version: reference.version, title: 'Existing order workflow', trigger: 'manual', authority_mode: 'caller_bound',
    steps: [{ id: 'send', kind: 'action', connection_id: actionElement.connection_id, connector_ref: reference, operation_ref: actionElement.operation_ref, arguments: {} }] },
  run: { schema_version: 1, run_id: otherProject, revision: 1, workflow_ref: reference, submission_digest: 'b'.repeat(64), authority_mode: 'caller_bound',
    authority_binding_digest: 'c'.repeat(64), created_at: actionRun.created_at, updated_at: actionRun.updated_at, state: 'accepted' },
  steps: [{ run_id: otherProject, step_id: 'send', state: 'pending' }],
}
const workflowSnapshotV2 = { ...workflowSnapshot, run: { ...workflowSnapshot.run, schema_version: 2 } }
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
const uncertainAction = { ...actionRun, revision: 2, state: 'uncertain', effect_outcome: 'unknown' }
const unknownReceipt = { schema_version: 1, receipt_id: projectId, run_id: actionRun.run_id, sequence: 1,
  submission_digest: actionRun.submission_digest, operation_ref: actionRun.operation_ref,
  observed_at: actionRun.updated_at, effect_outcome: 'unknown' }
const unknownSnapshot = { run: uncertainAction, receipts: [unknownReceipt] }
const reconciledSnapshot = { run: { ...actionRun, revision: 3, state: 'succeeded', effect_outcome: 'applied' },
  receipts: [{ ...unknownReceipt, receipt_id: otherProject, sequence: 2, effect_outcome: 'applied' }] }
const reconciliationPosts = (fetchImpl) => fetchImpl.mock.calls.filter(([url, options]) => url.endsWith('/reconcile') && options.method === 'POST')
function setupReconciliation() {
  const harness = setupAction()
  const original = harness.fetchImpl.getMockImplementation()
  harness.fetchImpl.mockImplementation((url, options) => {
    if (url.endsWith('/reconcile')) return Promise.resolve(response(reconciledSnapshot))
    if (url.includes('/integrations/actions/')) return Promise.resolve(response(unknownSnapshot))
    return original(url, options)
  })
  return harness
}
async function observeUncertainAction() {
  await openAction()
  submitAction()
  fireEvent.click(await screen.findByRole('button', { name: 'Check status' }))
  await screen.findByText('Uncertain. Outcome unknown.')
}
beforeEach(() => { vi.stubGlobal('TextDecoder', TextDecoder); vi.stubGlobal('TextEncoder', TextEncoder) })
afterEach(() => { cleanup(); vi.unstubAllGlobals() })
async function loaded() { await screen.findByRole('option', { name: 'Authorized orders · version 3' }) }
const browsedDraftRef = { id: 'schedule-draft-existing', version: 2, digest: 'd'.repeat(64) }
const browsedDraft = { schema_version: 1, id: browsedDraftRef.id, version: browsedDraftRef.version, title: 'Previously saved draft',
  workflow_ref: reference, state: 'draft', schedule: { kind: 'daily', timezone: 'US/Eastern', hour: 9, minute: 5,
    gap_policy: 'next_valid_minute', fold_policy: 'first_occurrence' } }
function draftRoutes(harness, projectRole = 'owner') {
  const original = harness.fetchImpl.getMockImplementation()
  harness.fetchImpl.mockImplementation((url, options) => {
    if (url.endsWith('/scope')) return Promise.resolve(response({ ...scope, projectRole }))
    if (url.includes('/manual-workflow-schedule-drafts?')) return Promise.resolve(response({ references: [browsedDraftRef], nextCursor: null }))
    if (url.includes('/manual-workflow-schedule-drafts/')) return Promise.resolve(response(browsedDraft))
    return original(url, options)
  })
}
const draftReads = fetchImpl => fetchImpl.mock.calls.filter(([url]) => url.includes('/manual-workflow-schedule-drafts'))
async function chooseExistingDraft() {
  fireEvent.click(screen.getByRole('button', { name: 'Load drafts' }))
  fireEvent.click(await screen.findByRole('button', { name: `Select draft ${browsedDraftRef.id}, version 2` }))
}
it.each(['owner', 'editor'])('places the inert saved draft browser first for returning %s with explicit reads only', async projectRole => {
  const harness = setup(); draftRoutes(harness, projectRole)
  const { props, fetchImpl } = harness
  const { rerender } = render(<OpsWorkspaceGround {...props} mode="dark" />); await loaded()
  rerender(<OpsWorkspaceGround {...props} mode="dark" />)
  expect(draftReads(fetchImpl)).toHaveLength(0)
  const browser = screen.getByRole('region', { name: 'Saved schedule drafts' })
  const preview = screen.getByRole('heading', { name: 'Daily schedule preview' })
  expect(browser.compareDocumentPosition(preview) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  await chooseExistingDraft()
  await screen.findByText(browsedDraft.title)
  expect(draftReads(fetchImpl).map(([url]) => url.split('/integrations/')[1])).toEqual([
    'manual-workflow-schedule-drafts?limit=25',
    `manual-workflow-schedule-drafts/${browsedDraftRef.id}/versions/2?digest=${browsedDraftRef.digest}`,
  ])
  expect(fetchImpl.mock.calls.every(([, request]) => request.method === 'GET')).toBe(true)
  expect(screen.getByText('09:05')).toBeInTheDocument()
  expect(screen.getByText('This is a saved draft. Nothing is scheduled or authorized to run.')).toBeInTheDocument()
})
it.each(['reviewer', 'read_only'])('does not offer draft discovery to %s', async projectRole => {
  const harness = setup(); draftRoutes(harness, projectRole)
  render(<OpsWorkspaceGround {...harness.props} />); await loaded()
  expect(screen.queryByRole('button', { name: 'Load drafts' })).toBeNull()
  expect(draftReads(harness.fetchImpl)).toHaveLength(0)
})
it.each(['tenantId', 'projectId', 'platformUserId', 'bindingId', 'projectRole', 'externalAuthority', 'externalSubject'])('blocks saved discovery before dispatch on complete scope %s drift', async key => {
  const harness = setup(); draftRoutes(harness)
  render(<OpsWorkspaceGround {...harness.props} />); await loaded()
  const drift = key === 'projectRole' ? 'reviewer' : key === 'externalSubject' ? 'auth0|other' : key === 'externalAuthority' ? 'other' : otherProject
  vi.spyOn(harness.props.client, 'getScope').mockResolvedValueOnce({ ...scope, [key]: drift })
  fireEvent.click(screen.getByRole('button', { name: 'Load drafts' }))
  await screen.findByText('You do not have permission to view these integrations.')
  expect(draftReads(harness.fetchImpl)).toHaveLength(0)
  expect(screen.queryByRole('region', { name: 'Saved schedule drafts' })).toBeNull()
})
it.each(['session', 'account', 'project', 'client', 'inactive'])('clears an actual Ground draft read on %s change and discards late completion', async kind => {
  const harness = setup(); draftRoutes(harness)
  const { props, fetchImpl, sessionController } = harness
  const original = fetchImpl.getMockImplementation()
  let finish
  fetchImpl.mockImplementation((url, options) => url.includes('/manual-workflow-schedule-drafts/') ?
    new Promise(resolve => { finish = resolve }) : original(url, options))
  const { rerender } = render(<OpsWorkspaceGround {...props} />); await loaded(); await chooseExistingDraft()
  await waitFor(() => expect(finish).toBeTypeOf('function'))
  expect(screen.getByText('Loading saved draft.')).toBeInTheDocument()
  const readSignal = draftReads(fetchImpl)[1][1].signal
  if (kind === 'session') act(() => sessionController.change({ status: 'expired', generation: 2 }))
  else rerender(<OpsWorkspaceGround {...props} {...({ account: { accountScope: 'account-b' }, project: { projectId: otherProject },
    client: { client: { ...props.client } }, inactive: { active: false } })[kind]} />)
  expect(readSignal.aborted).toBe(true)
  await act(async () => finish(response(browsedDraft)))
  expect(screen.queryByText(browsedDraft.title)).toBeNull()
  expect(screen.queryByRole('button', { name: 'Retry draft read' })).toBeNull()
})
it.each([401, 403])('routes saved draft HTTP %s through Ground forbidden handling', async status => {
  const harness = setup(); draftRoutes(harness)
  const original = harness.fetchImpl.getMockImplementation()
  harness.fetchImpl.mockImplementation((url, options) => url.includes('/manual-workflow-schedule-drafts/') ?
    Promise.resolve(response({}, status)) : original(url, options))
  render(<OpsWorkspaceGround {...harness.props} />); await loaded(); await chooseExistingDraft()
  await screen.findByText('You do not have permission to view these integrations.')
  expect(screen.queryByRole('region', { name: 'Saved schedule drafts' })).toBeNull()
})
const webhookReceipt = { registrationId: 'Forge.Reg_1', registrationRevision: 1, connectionId: 'Forge.Conn_1', provider: 'forgejo',
  deliveryId: 'Delivery:AbC-1', payloadSha256: 'a'.repeat(64), byteLength: 123, connectionRevision: 2,
  createdBy: otherProject, createdAt: '2026-10-23 09:30:00+00' }
function fillWebhookReceipt() {
  fireEvent.change(screen.getByLabelText('Registration ID'), { target: { value: webhookReceipt.registrationId } })
  fireEvent.change(screen.getByLabelText('Delivery ID'), { target: { value: webhookReceipt.deliveryId } })
}
const receiptGets = (fetchImpl) => fetchImpl.mock.calls.filter(([url]) => url.includes('/webhook-registrations/'))
const receiptEvidence = () => screen.queryByRole('region', { name: 'Stored admission evidence' })
it.each(['owner', 'editor', 'reviewer', 'read_only'])('mounts the read-only receipt inspector only for owner/editor: %s', async (projectRole) => {
  const { props, fetchImpl } = setup()
  const original = fetchImpl.getMockImplementation()
  fetchImpl.mockImplementation((url, options) => {
    if (url.endsWith('/scope')) return Promise.resolve(response({ ...scope, projectRole }))
    if (url.includes('/webhook-registrations/')) return Promise.resolve(response(webhookReceipt))
    return original(url, options)
  })
  render(<OpsWorkspaceGround {...props} />); await loaded()
  expect(receiptGets(fetchImpl)).toHaveLength(0)
  if (!['owner', 'editor'].includes(projectRole)) {
    expect(screen.queryByRole('heading', { name: 'Webhook admission receipt' })).toBeNull()
    return
  }
  fillWebhookReceipt()
  expect(receiptGets(fetchImpl)).toHaveLength(0)
  const before = fetchImpl.mock.calls.length
  fireEvent.click(screen.getByRole('button', { name: 'Look up receipt' }))
  await screen.findByRole('region', { name: 'Stored admission evidence' })
  expect(fetchImpl.mock.calls.slice(before).map(([url]) => url.split('/').at(-1))).toEqual(['scope', webhookReceipt.deliveryId, 'scope'])
  expect(receiptGets(fetchImpl)).toHaveLength(1)
  expect(fetchImpl.mock.calls.every(([, options]) => options.method === 'GET')).toBe(true)
})
it.each(['tenantId', 'projectId', 'platformUserId', 'bindingId', 'projectRole', 'externalAuthority', 'externalSubject'])('checks full receipt scope %s before and after lookup', async (key) => {
  for (const phase of ['preflight', 'completion']) {
    const { props } = setup()
    const get = vi.spyOn(props.client, 'getWebhookReceipt').mockResolvedValue(webhookReceipt)
    const view = render(<OpsWorkspaceGround {...props} />); await loaded(); fillWebhookReceipt()
    const drift = { ...scope, [key]: key === 'projectRole' ? 'read_only' : key === 'externalSubject' ? 'auth0|other' : key === 'externalAuthority' ? 'other' : otherProject }
    const fresh = vi.spyOn(props.client, 'getScope')
    if (phase === 'completion') fresh.mockResolvedValueOnce(scope).mockResolvedValueOnce(drift)
    else fresh.mockResolvedValueOnce(drift)
    fireEvent.click(screen.getByRole('button', { name: 'Look up receipt' }))
    await screen.findByText('You do not have permission to view these integrations.')
    expect(get).toHaveBeenCalledTimes(phase === 'preflight' ? 0 : 1)
    expect(receiptEvidence()).toBeNull()
    expect(screen.queryByLabelText('Registration ID')).toBeNull()
    view.unmount()
  }
})
it.each([401, 403])('removes protected receipt evidence on HTTP %s', async (status) => {
  const { props } = setup()
  vi.spyOn(props.client, 'getWebhookReceipt').mockResolvedValueOnce(webhookReceipt).mockRejectedValueOnce({ status })
  render(<OpsWorkspaceGround {...props} />); await loaded(); fillWebhookReceipt()
  fireEvent.click(screen.getByRole('button', { name: 'Look up receipt' }))
  await screen.findByRole('region', { name: 'Stored admission evidence' })
  fireEvent.click(screen.getByRole('button', { name: 'Look up receipt' }))
  await screen.findByText('You do not have permission to view these integrations.')
  expect(receiptEvidence()).toBeNull()
  expect(screen.queryByLabelText('Registration ID')).toBeNull()
})
it.each(['project', 'account', 'generation', 'status', 'inactive', 'unmount'])('aborts pending receipt lookup and suppresses late promises on %s change', async (kind) => {
  const { props, sessionController } = setup()
  let finish
  const get = vi.spyOn(props.client, 'getWebhookReceipt').mockImplementation(() => new Promise((resolve) => { finish = resolve }))
  const view = render(<OpsWorkspaceGround {...props} />); await loaded(); fillWebhookReceipt()
  fireEvent.click(screen.getByRole('button', { name: 'Look up receipt' }))
  await waitFor(() => expect(finish).toBeTypeOf('function'))
  const signal = get.mock.calls[0][3].signal
  if (kind === 'unmount') view.unmount()
  else if (kind === 'generation') act(() => sessionController.change({ generation: 2 }))
  else if (kind === 'status') act(() => sessionController.change({ status: 'expired' }))
  else view.rerender(<OpsWorkspaceGround {...props} {...(kind === 'project' ? { projectId: otherProject } : kind === 'account' ? { accountScope: 'account-b' } : { active: false })} />)
  expect(signal.aborted).toBe(true)
  await act(async () => finish(webhookReceipt))
  expect(receiptEvidence()).toBeNull()
})
it.each(['project', 'account', 'generation', 'role'])('clears loaded receipt evidence and IDs on %s change', async (kind) => {
  const { props, sessionController } = setup()
  vi.spyOn(props.client, 'getWebhookReceipt').mockResolvedValue(webhookReceipt)
  const view = render(<OpsWorkspaceGround {...props} />); await loaded(); fillWebhookReceipt()
  fireEvent.click(screen.getByRole('button', { name: 'Look up receipt' }))
  await screen.findByRole('region', { name: 'Stored admission evidence' })
  if (kind === 'role') {
    vi.spyOn(props.client, 'getScope').mockResolvedValueOnce({ ...scope, projectRole: 'reviewer' })
    fireEvent.click(screen.getByRole('button', { name: 'Look up receipt' }))
    await screen.findByText('You do not have permission to view these integrations.')
  } else if (kind === 'generation') act(() => sessionController.change({ generation: 2 }))
  else view.rerender(<OpsWorkspaceGround {...props} {...(kind === 'project' ? { projectId: otherProject } : { accountScope: 'account-b' })} />)
  expect(receiptEvidence()).toBeNull()
  const registration = screen.queryByLabelText('Registration ID')
  if (registration) expect(registration).toHaveValue('')
})
const schedulePreview = { schema_version: 1, preview_only: true, occurrence: { scheduledLocalDate: '2026-01-01',
  instant: '2026-01-01T09:30:00Z', timezone: 'UTC', hour: 9, minute: 30,
  occurrenceKey: JSON.stringify(['daily', 'UTC', 9, 30, '2026-01-01']) } }
function fillSchedulePreview() {
  for (const [label, value] of [['IANA timezone', 'UTC'], ['Daily hour', '9'], ['Daily minute', '30'], ['After (UTC)', '2026-01-01T00:00:00Z']]) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } })
  }
}
const previewPosts = (fetchImpl) => fetchImpl.mock.calls.filter(([url]) => url.endsWith('/workflow-schedule-preview'))
it.each(['owner', 'editor', 'reviewer', 'read_only'])('allows an explicit guarded daily preview for %s without workflow effects', async (projectRole) => {
  const { props, fetchImpl } = setup()
  const original = fetchImpl.getMockImplementation()
  fetchImpl.mockImplementation((url, options) => {
    if (url.endsWith('/scope')) return Promise.resolve(response({ ...scope, projectRole }))
    if (url.endsWith('/workflow-schedule-preview')) return Promise.resolve(response(schedulePreview))
    return original(url, options)
  })
  render(<OpsWorkspaceGround {...props} />); await loaded()
  expect(screen.getByRole('heading', { name: 'Daily schedule preview' })).toBeInTheDocument()
  fillSchedulePreview()
  expect(previewPosts(fetchImpl)).toHaveLength(0)
  const before = fetchImpl.mock.calls.length
  fireEvent.click(screen.getByRole('button', { name: 'Preview next occurrence' }))
  await screen.findByRole('region', { name: 'Next scheduled occurrence' })
  expect(previewPosts(fetchImpl)).toHaveLength(1)
  expect(fetchImpl.mock.calls.slice(before).map(([url]) => url.split('/').at(-1))).toEqual(['scope', 'workflow-schedule-preview', 'scope'])
  expect(fetchImpl.mock.calls.filter(([, options]) => options.method === 'POST')).toHaveLength(1)
})
it.each(['tenantId', 'projectId', 'platformUserId', 'bindingId', 'projectRole', 'externalAuthority', 'externalSubject'])('rejects daily preview on complete read identity %s drift', async (key) => {
  const { props, fetchImpl } = setup()
  render(<OpsWorkspaceGround {...props} />); await loaded(); fillSchedulePreview()
  const drift = key === 'projectRole' ? 'reviewer' : key === 'externalSubject' ? 'auth0|other' : key === 'externalAuthority' ? 'other' : otherProject
  vi.spyOn(props.client, 'getScope').mockResolvedValue({ ...scope, [key]: drift })
  fireEvent.click(screen.getByRole('button', { name: 'Preview next occurrence' }))
  await screen.findByText('You do not have permission to view these integrations.')
  expect(previewPosts(fetchImpl)).toHaveLength(0)
  expect(screen.queryByRole('region', { name: 'Next scheduled occurrence' })).toBeNull()
})
it('discards a preview when the completion scope differs', async () => {
  const { props, fetchImpl } = setup()
  render(<OpsWorkspaceGround {...props} />); await loaded(); fillSchedulePreview()
  const original = fetchImpl.getMockImplementation()
  let completed = false
  fetchImpl.mockImplementation((url, options) => {
    if (url.endsWith('/workflow-schedule-preview')) { completed = true; return Promise.resolve(response(schedulePreview)) }
    if (url.endsWith('/scope')) return Promise.resolve(response({ ...scope, externalSubject: completed ? 'auth0|other' : scope.externalSubject }))
    return original(url, options)
  })
  fireEvent.click(screen.getByRole('button', { name: 'Preview next occurrence' }))
  await screen.findByText('You do not have permission to view these integrations.')
  expect(previewPosts(fetchImpl)).toHaveLength(1)
  expect(screen.queryByRole('region', { name: 'Next scheduled occurrence' })).toBeNull()
})
it.each([401, 403])('uses the forbidden path on preview HTTP %s', async (status) => {
  const { props, fetchImpl } = setup()
  render(<OpsWorkspaceGround {...props} />); await loaded(); fillSchedulePreview()
  const original = fetchImpl.getMockImplementation()
  fetchImpl.mockImplementation((url, options) => url.endsWith('/workflow-schedule-preview') ? Promise.resolve(response({}, status)) : original(url, options))
  fireEvent.click(screen.getByRole('button', { name: 'Preview next occurrence' }))
  await screen.findByText('You do not have permission to view these integrations.')
  expect(screen.queryByRole('heading', { name: 'Daily schedule preview' })).toBeNull()
})
it.each(['session', 'project', 'account'])('aborts a pending daily preview on %s context changes', async (kind) => {
  const { props, fetchImpl, sessionController } = setup()
  const original = fetchImpl.getMockImplementation()
  let finish
  fetchImpl.mockImplementation((url, options) => url.endsWith('/workflow-schedule-preview') ? new Promise((resolve) => { finish = resolve }) : original(url, options))
  const { rerender } = render(<OpsWorkspaceGround {...props} />); await loaded(); fillSchedulePreview()
  fireEvent.click(screen.getByRole('button', { name: 'Preview next occurrence' }))
  await waitFor(() => expect(finish).toBeTypeOf('function'))
  const signal = previewPosts(fetchImpl)[0][1].signal
  if (kind === 'session') act(() => sessionController.change({ generation: 2 }))
  else rerender(<OpsWorkspaceGround {...props} {...(kind === 'project' ? { projectId: otherProject } : { accountScope: 'account-b' })} />)
  expect(signal.aborted).toBe(true)
  await act(async () => finish(response(schedulePreview)))
  expect(screen.queryByRole('region', { name: 'Next scheduled occurrence' })).toBeNull()
})
it.each(['owner', 'editor', 'reviewer', 'read_only'])('offers first dataset setup only to the verified %s scope', async (projectRole) => {
  const { props, fetchImpl } = setup()
  const original = fetchImpl.getMockImplementation()
  fetchImpl.mockImplementation((url, options) => url.endsWith('/scope') ? Promise.resolve(response({ ...scope, projectRole })) : original(url, options))
  render(<OpsWorkspaceGround {...props} />)
  await loaded()
  expect(Boolean(screen.queryByRole('heading', { name: 'Set up an HTTP dataset' }))).toBe(projectRole === 'owner')
  expect(screen.getByRole('form', { name: 'Compose native view' })).toBeInTheDocument()
})
it.each(['bindingId', 'platformUserId', 'tenantId', 'externalSubject', 'projectRole'])('invalidates setup before an import on fresh %s drift', async (key) => {
  const { props, fetchImpl } = setup()
  const original = fetchImpl.getMockImplementation()
  let changed = false
  fetchImpl.mockImplementation((url, options) => url.endsWith('/scope')
    ? Promise.resolve(response(changed ? { ...scope, [key]: key === 'projectRole' ? 'editor' : key === 'externalSubject' ? 'auth0|other' : otherProject } : scope)) : original(url, options))
  render(<OpsWorkspaceGround {...props} />)
  await loaded()
  fireEvent.change(screen.getByLabelText('Connector stable ID'), { target: { value: 'new-api' } })
  fireEvent.change(screen.getByLabelText('Connector title'), { target: { value: 'New API' } })
  fireEvent.change(screen.getByLabelText('OpenAPI JSON'), { target: { value: '{"openapi":"3.1.0"}' } })
  changed = true
  fireEvent.click(screen.getByRole('button', { name: 'Import JSON' }))
  await screen.findByText('You do not have permission to view these integrations.')
  expect(screen.queryByRole('heading', { name: 'Set up an HTTP dataset' })).toBeNull()
  expect(fetchImpl.mock.calls.some(([url]) => url.endsWith('/connectors/import'))).toBe(false)
})
it.each(['project', 'account', 'generation', 'inactive', 'unmount'])('aborts first setup and ignores late import on %s change', async (change) => {
  const { props, fetchImpl, sessionController } = setup()
  const original = fetchImpl.getMockImplementation()
  let finish
  fetchImpl.mockImplementation((url, options) => url.endsWith('/connectors/import') ? new Promise((resolve) => { finish = resolve }) : original(url, options))
  const { rerender, unmount } = render(<OpsWorkspaceGround {...props} />)
  await loaded()
  fireEvent.change(screen.getByLabelText('Connector stable ID'), { target: { value: 'new-api' } })
  fireEvent.change(screen.getByLabelText('Connector title'), { target: { value: 'Prior tenant API' } })
  fireEvent.change(screen.getByLabelText('OpenAPI JSON'), { target: { value: '{"openapi":"3.1.0"}' } })
  fireEvent.click(screen.getByRole('button', { name: 'Import JSON' }))
  await waitFor(() => expect(finish).toBeTypeOf('function'))
  const signal = fetchImpl.mock.calls.find(([url]) => url.endsWith('/connectors/import'))[1].signal
  if (change === 'unmount') unmount()
  else if (change === 'generation') act(() => sessionController.change({ generation: 2 }))
  else rerender(<OpsWorkspaceGround {...props} {...(change === 'project' ? { projectId: otherProject } : change === 'account' ? { accountScope: 'other' } : { active: false })} />)
  expect(signal.aborted).toBe(true)
  await act(async () => finish(response({ reference: { ...reference, id: 'new-api', version: 1 }, revision: 1, issues: [] })))
  expect(screen.queryByText(/Imported connector: Prior tenant API/)).toBeNull()
  expect(screen.queryByDisplayValue('Prior tenant API')).toBeNull()
  expect(fetchImpl.mock.calls.some(([url]) => url.endsWith('/connectors/new-api'))).toBe(false)
})
it('takes a blank organization through setup and refreshes the existing native composer after explicit sync', async () => {
  const { props, fetchImpl } = setup()
  const connectorRef = { ...reference, id: 'new-api', version: 1 }
  const operationRef = { ...connectorRef, id: 'normalized-read' }
  const datasetRef = { ...connectorRef, id: 'new-data' }
  const operation = { schema_version: 1, id: operationRef.id, title: 'Read rows', method: 'GET', path: '/rows', effect: 'read',
    pagination: { kind: 'none', records_pointer: '' }, limits: { timeout_ms: 1000, max_response_bytes: 131072, max_records: 250, max_pages: 1 }, idempotency: { kind: 'read' } }
  const definition = { schema_version: 1, id: connectorRef.id, version: 1, title: 'New API', state: 'draft', protocol: 'https-rest',
    origins: ['https://source.example'], auth: { kind: 'none' }, operations: [operation] }
  const connector = { id: connectorRef.id, title: definition.title, state: 'draft', reference: connectorRef, revision: 1, auth_kind: 'none', operation_count: 1 }
  const dataFields = [{ id: 'key', label: 'Key', type: { kind: 'identifier' }, nullable: false, sensitivity: 'internal' }]
  const receipt = { id: projectId, actor_id: scope.platformUserId, binding_id: scope.bindingId,
    started_at: '2026-10-20T00:00:00Z', finished_at: '2026-10-20T00:00:01Z',
    connector_ref: connectorRef, operation_ref: operationRef, dataset_ref: datasetRef, transform_ref: { ...connectorRef, id: 'new-transform' },
    source_outcome: 'success', outcome: 'success', row_count: 1, failure_code: null }
  fetchImpl.mockImplementation(async (url, options) => {
    if (url.endsWith('/scope')) return response(scope)
    if (url.includes('/datasets?') || url.includes('/elements?')) return response(page([]))
    if (url.endsWith('/connectors/import')) return response({ reference: connectorRef, revision: 1, issues: [] })
    if (url.endsWith('/connectors/new-api')) return response({ ...connector, draft_definition: definition })
    if (url.endsWith('/publish')) return response({ reference: connectorRef, revision: 2 })
    if (url.endsWith('/connectors/new-api/versions/1')) return response({ ...connector, revision: 2, state: 'published', definition: { ...definition, state: 'published' }, operationReferences: [operationRef] })
    if (url.endsWith('/connections')) return response({ id: 'new-connection', title: 'New connection', revision: 1, state: 'active', connector_ref: connectorRef,
      origin: 'https://source.example', auth_kind: 'none', credential_purposes: [], operation_ids: [operationRef.id], grant_revision: 1,
      owner_id: projectId, provider_account_id: null, identity_verified: false, health: { checked_at: null, code: 'unknown' }, cleanup: 'complete' })
    if (url.endsWith('/datasets')) return response({ reference: datasetRef, revision: 1 })
    if (url.endsWith('/sync')) return response({ reference: datasetRef, revision: 2, receipt, status: 'ready' })
    if (url.endsWith('/datasets/new-data/versions/1')) return response({ ...metadata, reference: datasetRef, title: 'New data', fields: dataFields, lastAttempt: receipt })
    if (url.endsWith('/elements')) return response({ element: JSON.parse(options.body), revision: 1 })
    if (url.endsWith('/query')) return response({ ...read, dataset_ref: datasetRef, fields: dataFields, rows: [{ key: 'one', values: { key: 'one' } }] })
    throw new Error('Unexpected route')
  })
  render(<OpsWorkspaceGround {...props} />)
  await screen.findByRole('heading', { name: 'Set up an HTTP dataset' })
  const enter = (label, value) => fireEvent.change(screen.getByLabelText(label), { target: { value } })
  enter('Connector stable ID', 'new-api'); enter('Connector title', 'New API'); enter('OpenAPI JSON', '{"openapi":"3.1.0"}')
  fireEvent.click(screen.getByRole('button', { name: 'Import JSON' }))
  await screen.findByLabelText('Supported read operation')
  enter('Supported read operation', operationRef.id)
  fireEvent.click(screen.getByRole('button', { name: 'Publish selected read' }))
  await screen.findByLabelText('Connection stable ID')
  enter('Connection stable ID', 'new-connection'); enter('Connection title', 'New connection')
  fireEvent.click(screen.getByRole('button', { name: 'Create connection' }))
  await screen.findByLabelText('Dataset stable ID')
  enter('Dataset stable ID', 'new-data'); enter('Dataset title', 'New data')
  enter('Field 1 ID', 'key'); enter('Field 1 label', 'Key'); enter('Field 1 type', 'identifier'); enter('Field 1 JSON pointer', '/id')
  fireEvent.click(screen.getByLabelText('Field 1 stable key'))
  fireEvent.click(screen.getByRole('button', { name: 'Register dataset' }))
  const sync = await screen.findByRole('button', { name: 'Synchronize dataset' })
  expect(fetchImpl.mock.calls.some(([url]) => url.endsWith('/sync'))).toBe(false)
  fireEvent.click(sync)
  await screen.findByRole('option', { name: 'New data · version 1' })
  await waitFor(() => expect(screen.getByLabelText('Dataset')).toHaveValue(JSON.stringify([datasetRef.id, datasetRef.version, datasetRef.digest])))
  enter('Title', 'First table'); enter('Stable ID', 'first-table')
  fireEvent.click(screen.getByRole('button', { name: 'Save view' }))
  await screen.findByRole('table', { name: 'First table data' })
  expect(fetchImpl.mock.calls.filter(([url]) => url.endsWith('/sync'))).toHaveLength(1)
  expect(fetchImpl.mock.calls.filter(([url]) => url.endsWith('/scope'))).toHaveLength(6)
})
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
  expect(await screen.findByRole('alert')).toHaveTextContent('The service is temporarily unavailable.')
  expect(screen.getByRole('alert')).toHaveFocus()
  expect(form).toContainElement(screen.getByRole('alert'))
  expect(screen.queryByRole('button', { name: 'Retry integrations' })).toBeNull()
  expect(screen.getByLabelText('Title')).toHaveValue('New order view')
  expect(screen.getByLabelText('Stable ID')).toHaveValue('new-order-view')
  expect(screen.getByRole('button', { name: 'Save view' })).toBeEnabled()
  fetchImpl.mockImplementation(original)
  fireEvent.submit(form)
  await screen.findByText('Saved New order view, revision 3.')
  const saves = fetchImpl.mock.calls.filter(([url, options]) => url.endsWith('/elements') && options.method === 'POST')
  expect(saves).toHaveLength(2)
  expect(saves[1][1].body).toBe(saves[0][1].body)
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
it('reconciles an uncertain run once per explicit click without resubmission and retains its history', async () => {
  const { props, fetchImpl } = setupReconciliation()
  let finish
  const original = fetchImpl.getMockImplementation()
  fetchImpl.mockImplementation((url, options) => url.endsWith('/reconcile')
    ? new Promise((resolve) => { finish = resolve }) : original(url, options))
  render(<OpsWorkspaceGround {...props} />)
  await observeUncertainAction()
  expect(reconciliationPosts(fetchImpl)).toHaveLength(0)
  const before = fetchImpl.mock.calls.length
  const button = screen.getByRole('button', { name: 'Reconcile outcome' })
  fireEvent.click(button)
  fireEvent.click(button)
  await waitFor(() => expect(reconciliationPosts(fetchImpl)).toHaveLength(1))
  expect(screen.getByText('Reconciliation pending. The action outcome is still unknown.')).toBeInTheDocument()
  expect(button).toBeDisabled()
  expect(screen.getByRole('list', { name: 'Action history' }).querySelectorAll('li')).toHaveLength(1)
  expect(fetchImpl.mock.calls.slice(before).map(([url]) => url.split('/').at(-1))).toEqual(['scope', 'orders-connection', 'reconcile'])
  await act(async () => finish(response(reconciledSnapshot)))
  await screen.findByText('The action succeeded and was applied.')
  const history = screen.getByRole('list', { name: 'Action history' })
  expect(history.querySelectorAll('li')).toHaveLength(2)
  expect(history).toHaveTextContent('Action applied')
  expect(screen.queryByRole('button', { name: 'Reconcile outcome' })).toBeNull()
  expect(actionPosts(fetchImpl)).toHaveLength(1)
  expect(globalThis.crypto.randomUUID).toHaveBeenCalledTimes(1)
  const statusCalls = fetchImpl.mock.calls.filter(([url]) => url.endsWith(`/actions/${actionRun.run_id}`))
  expect(statusCalls).toHaveLength(1)
  expect(statusCalls[0][1].method).toBe('GET')
})
it.each(['unknown', 'failure', 'unsupported', 'forbidden', 'missing-client'])('retains uncertainty after %s reconciliation', async (outcome) => {
  const { props, fetchImpl } = setupReconciliation()
  const original = fetchImpl.getMockImplementation()
  fetchImpl.mockImplementation((url, options) => url.endsWith('/reconcile')
    ? Promise.resolve(response(outcome === 'unknown' ? unknownSnapshot : { secret: 'private reconciliation failure' },
      outcome === 'failure' ? 503 : outcome === 'unsupported' ? 404 : outcome === 'forbidden' ? 403 : 200)) : original(url, options))
  if (outcome === 'missing-client') props.client.reconcileAction = undefined
  render(<OpsWorkspaceGround {...props} />)
  await observeUncertainAction()
  fireEvent.click(screen.getByRole('button', { name: 'Reconcile outcome' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Check status' })).toBeInTheDocument())
  expect(screen.getByText('Uncertain. Outcome unknown.')).toBeInTheDocument()
  expect(screen.getByRole('list', { name: 'Action history' }).querySelectorAll('li')).toHaveLength(1)
  expect(screen.queryByRole('button', { name: 'Start another action' })).toBeNull()
  expect(screen.queryByText(/The action was not submitted\./)).toBeNull()
  expect(document.body.textContent).not.toContain('private reconciliation failure')
  expect(actionPosts(fetchImpl)).toHaveLength(1)
  expect(reconciliationPosts(fetchImpl)).toHaveLength(outcome === 'missing-client' ? 0 : 1)
  if (outcome === 'forbidden') expect(screen.getByRole('button', { name: 'Reconcile outcome' })).toBeDisabled()
  if (outcome === 'unknown') {
    fireEvent.click(screen.getByRole('button', { name: 'Reconcile outcome' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Check status' })).toBeInTheDocument())
    expect(reconciliationPosts(fetchImpl)).toHaveLength(2)
    expect(actionPosts(fetchImpl)).toHaveLength(1)
    expect(globalThis.crypto.randomUUID).toHaveBeenCalledTimes(1)
  }
})
it.each(['scope', 'role', 'grant', 'revoked', 'ref', 'operation'])('refuses reconciliation before dispatch on fresh %s drift', async (change) => {
  const { props, fetchImpl, authority } = setupReconciliation()
  render(<OpsWorkspaceGround {...props} />)
  await observeUncertainAction()
  if (change === 'scope') authority.scope = { ...scope, bindingId: otherProject }
  if (change === 'role') authority.scope = { ...scope, projectRole: 'read_only' }
  if (change === 'grant') authority.connection = { ...actionConnection, grant_revision: 8 }
  if (change === 'revoked') authority.connection = { ...actionConnection, state: 'revoked' }
  if (change === 'ref') authority.connection = { ...actionConnection, connector_ref: { ...reference, version: 4 } }
  if (change === 'operation') authority.connection = { ...actionConnection, operation_ids: ['other'] }
  fireEvent.click(screen.getByRole('button', { name: 'Reconcile outcome' }))
  await screen.findByRole('button', { name: 'Retry action authority' })
  expect(screen.getByRole('button', { name: 'Reconcile outcome' })).toBeDisabled()
  expect(screen.getByText('Uncertain. Outcome unknown.')).toBeInTheDocument()
  expect(screen.queryByText(/The action was not submitted\./)).toBeNull()
  expect(reconciliationPosts(fetchImpl)).toHaveLength(0)
  expect(actionPosts(fetchImpl)).toHaveLength(1)
})
it('prevents reconciliation dispatch after an ignored late authority read in an invalidated context', async () => {
  const { props, fetchImpl } = setupReconciliation()
  const { rerender } = render(<OpsWorkspaceGround {...props} />)
  await observeUncertainAction()
  let finish
  const getScope = vi.spyOn(props.client, 'getScope').mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  fireEvent.click(screen.getByRole('button', { name: 'Reconcile outcome' }))
  await waitFor(() => expect(finish).toBeTypeOf('function'))
  const signal = getScope.mock.calls[0][1].signal
  rerender(<OpsWorkspaceGround {...props} active={false} />)
  expect(signal.aborted).toBe(true)
  await act(async () => finish(scope))
  expect(reconciliationPosts(fetchImpl)).toHaveLength(0)
  expect(screen.queryByRole('region', { name: 'Action run status' })).toBeNull()
})
it.each(['project', 'account', 'generation', 'status', 'profile', 'unmount'])('aborts reconciliation and rejects ignored late results on %s invalidation', async (change) => {
  const { props, fetchImpl, sessionController } = setupReconciliation()
  let finish
  const reconcile = vi.spyOn(props.client, 'reconcileAction').mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  const { rerender, unmount } = render(<OpsWorkspaceGround {...props} />)
  await observeUncertainAction()
  fireEvent.click(screen.getByRole('button', { name: 'Reconcile outcome' }))
  await waitFor(() => expect(reconcile).toHaveBeenCalledTimes(1))
  const signal = reconcile.mock.calls[0][2].signal
  if (change === 'unmount') unmount()
  else if (change === 'generation') act(() => sessionController.change({ generation: 2 }))
  else if (change === 'status') act(() => sessionController.change({ status: 'expired' }))
  else rerender(<OpsWorkspaceGround {...props} {...(change === 'project' ? { projectId: otherProject } :
    change === 'account' ? { accountScope: 'account-b' } : { active: false })} />)
  expect(signal.aborted).toBe(true)
  await act(async () => finish(reconciledSnapshot))
  expect(screen.queryByRole('region', { name: 'Action run status' })).toBeNull()
  expect(screen.queryByText('The action succeeded and was applied.')).toBeNull()
  expect(actionPosts(fetchImpl)).toHaveLength(1)
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
it.each(['scope', 'role', 'grant', 'revoked', 'ref', 'operation'])('rechecks %s drift before submission and never refreshes the frozen grant', async (change) => {
  const { props, fetchImpl, authority } = setupAction()
  render(<OpsWorkspaceGround {...props} />)
  await openAction()
  if (change === 'scope') authority.scope = { ...scope, bindingId: otherProject }
  if (change === 'role') authority.scope = { ...scope, projectRole: 'reviewer' }
  if (change === 'grant') authority.connection = { ...actionConnection, grant_revision: 8 }
  if (change === 'revoked') authority.connection = { ...actionConnection, state: 'revoked' }
  if (change === 'ref') authority.connection = { ...actionConnection, connector_ref: { ...reference, version: 4 } }
  if (change === 'operation') authority.connection = { ...actionConnection, operation_ids: ['other'] }
  submitAction()
  const retry = await screen.findByRole('button', { name: 'Retry action authority' })
  expect(actionPosts(fetchImpl)).toHaveLength(0)
  expect(screen.getByLabelText('Order note (required)')).toHaveValue('Ship today')
  expect(await screen.findByText('The action was not submitted. Retry submission when the action is available.')).toBeInTheDocument()
  expect(screen.queryByText(/Outcome unknown\./)).toBeNull()
  expect(screen.getByRole('button', { name: 'Retry submission' })).toBeDisabled()
  if (change === 'grant') {
    fireEvent.click(retry)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retry submission' })).toBeDisabled())
    expect(actionPosts(fetchImpl)).toHaveLength(0)
  }
})
it.each([
  ['getScope', undefined], ['getConnection', undefined], ['getScope', 403], ['getConnection', 403],
])('reports failed %s preflight reads (%s) as not submitted with fixed copy', async (method, status) => {
  const { props, fetchImpl } = setupAction()
  render(<OpsWorkspaceGround {...props} />)
  await openAction()
  vi.spyOn(props.client, method).mockRejectedValueOnce(Object.assign(new Error('private preflight failure'), { status }))
  submitAction()
  await screen.findByText('The action was not submitted. Retry submission when the action is available.')
  expect(actionPosts(fetchImpl)).toHaveLength(0)
  expect(screen.getByLabelText('Order note (required)')).toHaveValue('Ship today')
  expect(screen.queryByText(/Outcome unknown\./)).toBeNull()
  expect(document.body.textContent).not.toContain('private preflight failure')
  if (status === 403) expect(screen.getByRole('button', { name: 'Retry submission' })).toBeDisabled()
})
it.each([403, 503])('keeps a submission HTTP %s failure unknown after calling the client', async (status) => {
  const { props, fetchImpl } = setupAction()
  const original = fetchImpl.getMockImplementation()
  fetchImpl.mockImplementation((url, options) => url.endsWith('/integrations/actions')
    ? Promise.resolve(response({ secret: 'private write failure' }, status)) : original(url, options))
  render(<OpsWorkspaceGround {...props} />)
  await openAction()
  submitAction()
  await screen.findByText(/Outcome unknown\./)
  expect(actionPosts(fetchImpl)).toHaveLength(1)
  expect(screen.queryByText(/The action was not submitted\./)).toBeNull()
  expect(screen.getByLabelText('Order note (required)')).toHaveValue('Ship today')
  expect(document.body.textContent).not.toContain('private write failure')
  if (status === 403) expect(screen.getByRole('button', { name: 'Recover submission' })).toBeDisabled()
})
it('keeps a synchronous submission client failure unknown', async () => {
  const { props, fetchImpl } = setupAction()
  render(<OpsWorkspaceGround {...props} />)
  await openAction()
  vi.spyOn(props.client, 'submitAction').mockImplementationOnce(() => { throw new Error('private client failure') })
  submitAction()
  await screen.findByText(/Outcome unknown\./)
  expect(actionPosts(fetchImpl)).toHaveLength(0)
  expect(screen.queryByText(/The action was not submitted\./)).toBeNull()
})
it.each(['scope', 'grant', 'revoked'])('preserves uncertainty and the original key when %s drift refuses recovery', async (change) => {
  const { props, fetchImpl, authority } = setupAction()
  const original = fetchImpl.getMockImplementation()
  render(<OpsWorkspaceGround {...props} />)
  await openAction()
  fetchImpl.mockImplementation((url, options) => url.endsWith('/integrations/actions')
    ? Promise.reject(new Error('lost response')) : original(url, options))
  submitAction()
  const recover = await screen.findByRole('button', { name: 'Recover submission' })
  const originalBody = actionPosts(fetchImpl)[0][1].body
  if (change === 'scope') authority.scope = { ...scope, bindingId: otherProject }
  if (change === 'grant') authority.connection = { ...actionConnection, grant_revision: 8 }
  if (change === 'revoked') authority.connection = { ...actionConnection, state: 'revoked' }
  fireEvent.click(recover)
  await screen.findByRole('button', { name: 'Retry action authority' })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Recover submission' })).toBeDisabled())
  expect(screen.getByText(/Outcome unknown\./)).toBeInTheDocument()
  expect(screen.queryByText(/The action was not submitted\./)).toBeNull()
  expect(actionPosts(fetchImpl)).toHaveLength(1)
  expect(screen.getByLabelText('Order note (required)')).toHaveValue('Ship today')
  authority.scope = scope
  authority.connection = actionConnection
  fetchImpl.mockImplementation(original)
  fireEvent.click(screen.getByRole('button', { name: 'Retry action authority' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Recover submission' })).toBeEnabled())
  expect(actionPosts(fetchImpl)).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: 'Recover submission' }))
  await screen.findByText('Submission accepted. Execution is pending.')
  expect(actionPosts(fetchImpl)).toHaveLength(2)
  expect(actionPosts(fetchImpl)[1][1].body).toBe(originalBody)
  expect(globalThis.crypto.randomUUID).toHaveBeenCalledTimes(1)
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
  expect(await screen.findByText(/Outcome unknown\. The action/)).toBeInTheDocument()
  expect(screen.queryByText(/The action was not submitted\./)).toBeNull()
  expect(actionPosts(fetchImpl)).toHaveLength(1)
})
it.each([403, 503])('never reports not submitted when status lookup fails with HTTP %s', async (status) => {
  const { props, fetchImpl } = setupAction()
  const original = fetchImpl.getMockImplementation()
  fetchImpl.mockImplementation((url, options) => url.includes('/integrations/actions/')
    ? Promise.resolve(response({ secret: 'private status failure' }, status)) : original(url, options))
  render(<OpsWorkspaceGround {...props} />)
  await openAction()
  submitAction()
  fireEvent.click(await screen.findByRole('button', { name: 'Check status' }))
  await screen.findByText(/Outcome unknown\. The action/)
  expect(screen.queryByText(/The action was not submitted\./)).toBeNull()
  expect(screen.getByRole('region', { name: 'Action run status' })).toBeInTheDocument()
  expect(actionPosts(fetchImpl)).toHaveLength(1)
  expect(fetchImpl.mock.calls.filter(([url]) => url.includes('/integrations/actions/'))).toHaveLength(1)
  expect(document.body.textContent).not.toContain('private status failure')
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
it.each(['owner', 'editor', 'reviewer', 'read_only'])('exposes the separate existing workflow viewer only to owner/editor (%s)', async (role) => {
  const { props, authority } = setupAction()
  authority.scope = { ...scope, projectRole: role }
  render(<OpsWorkspaceGround {...props} />)
  await loaded()
  if (['owner', 'editor'].includes(role)) {
    expect(screen.getByRole('region', { name: 'Create manual workflow' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Existing manual workflow' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Start manual workflow' })).toBeInTheDocument()
    expect(screen.getByLabelText('Run UUID')).toBeInTheDocument()
  } else {
    expect(screen.queryByRole('region', { name: 'Create manual workflow' })).toBeNull()
    expect(screen.queryByRole('region', { name: 'Existing manual workflow' })).toBeNull()
    expect(screen.queryByRole('region', { name: 'Start manual workflow' })).toBeNull()
  }
})
const catalogAction = { connection_id: actionElement.connection_id, connection_title: 'Orders connection', connector_ref: reference,
  operation_ref: actionElement.operation_ref, title: 'Send order', form: { fields: [], submit_label: 'Send' } }
async function composeWorkflow() {
  fireEvent.click(screen.getByRole('button', { name: 'Open action catalog' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Save workflow definition' })).toBeEnabled())
  fireEvent.change(screen.getByLabelText('Definition title'), { target: { value: 'Order workflow' } })
  fireEvent.change(screen.getByLabelText('Definition stable ID'), { target: { value: reference.id } })
  fireEvent.change(screen.getByLabelText('Definition version'), { target: { value: String(reference.version) } })
  fireEvent.change(screen.getByLabelText('Step 1 action'), { target: { value: JSON.stringify([
    catalogAction.connection_id, catalogAction.connector_ref, catalogAction.operation_ref]) } })
}
async function composeActionForm() {
  fireEvent.click(screen.getByRole('button', { name: 'Load permitted actions' }))
  await screen.findByRole('option', { name: /Send order · Orders connection/ })
  fireEvent.change(screen.getByLabelText('Form title'), { target: { value: 'New order form' } })
  fireEvent.change(screen.getByLabelText('Form stable ID'), { target: { value: 'new-order-form' } })
  fireEvent.change(screen.getByLabelText('Permitted action'), { target: {
    value: screen.getByRole('option', { name: /Send order · Orders connection/ }).value } })
}
function setupActionComposer() {
  const harness = setupAction()
  const original = harness.fetchImpl.getMockImplementation()
  harness.fetchImpl.mockImplementation((url, options) => url.includes('/action-catalog?')
    ? Promise.resolve(response({ actions: [{ ...catalogAction, form: actionElement.form }], next_cursor: null })) : original(url, options))
  return harness
}
it.each(['owner', 'editor', 'reviewer', 'read_only'])('admits the action form composer only for current owner/editor (%s)', async (projectRole) => {
  const { props, authority, fetchImpl } = setupActionComposer()
  authority.scope = { ...scope, projectRole }
  render(<OpsWorkspaceGround {...props} />)
  await loaded()
  expect(Boolean(screen.queryByRole('region', { name: 'Create an action form' }))).toBe(['owner', 'editor'].includes(projectRole))
  expect(fetchImpl.mock.calls.some(([url]) => url.includes('/action-catalog?'))).toBe(false)
})
it.each(['owner', 'editor'])('adds the exact created record to Saved views and opens the native form inertly as %s', async (projectRole) => {
  const { props, authority, fetchImpl } = setupActionComposer()
  authority.scope = { ...scope, projectRole }
  const submit = vi.spyOn(props.client, 'submitAction')
  const reconcile = vi.spyOn(props.client, 'reconcileAction')
  const start = vi.spyOn(props.client, 'submitManualWorkflow')
  const saveElement = vi.spyOn(props.client, 'saveElement')
  render(<OpsWorkspaceGround {...props} />)
  await loaded(); await composeActionForm()
  fireEvent.click(screen.getByRole('button', { name: 'Save action form' }))
  const open = await screen.findByRole('button', { name: 'New order form · revision 3' })
  expect(screen.getAllByRole('button', { name: 'New order form · revision 3' })).toHaveLength(1)
  expect(saveElement).toHaveBeenCalledExactlyOnceWith(projectId, { ...actionElement, id: 'new-order-form', title: 'New order form' },
    { signal: expect.any(AbortSignal) })
  expect(screen.getByRole('button', { name: 'Order view · revision 2' })).toBeInTheDocument()
  expect(screen.getByRole('region', { name: 'Create manual workflow' })).toBeInTheDocument()
  expect(screen.getByRole('form', { name: 'Compose native view' })).toBeInTheDocument()
  fireEvent.click(open)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Send order now' })).toBeEnabled())
  expect(screen.getByLabelText('Order note (required)')).toBeInTheDocument()
  expect(submit).not.toHaveBeenCalled()
  expect(reconcile).not.toHaveBeenCalled()
  expect(start).not.toHaveBeenCalled()
  expect(fetchImpl.mock.calls.filter(([, options]) => options.method === 'POST').map(([url]) => url.split('/integrations/')[1])).toEqual(['elements'])
})
it('keeps one current action form when later saved-view pages repeat its ID', async () => {
  const { props } = setupActionComposer()
  const created = { element: { ...actionElement, id: 'new-order-form', title: 'New order form' }, revision: 3 }
  const unseen = { element: { ...element, id: 'other-view', title: 'Other view' }, revision: 1 }
  const stale = { element: { ...created.element, title: 'Stale form' }, revision: 2 }
  const list = vi.spyOn(props.client, 'listElements').mockResolvedValueOnce({ records: [saved], nextCursor: 'next' })
    .mockResolvedValueOnce({ records: [stale, created, unseen, unseen], nextCursor: null })
  const submit = vi.spyOn(props.client, 'submitAction')
  const reconcile = vi.spyOn(props.client, 'reconcileAction')
  const start = vi.spyOn(props.client, 'submitManualWorkflow')
  render(<OpsWorkspaceGround {...props} />)
  await loaded(); await composeActionForm()
  fireEvent.click(screen.getByRole('button', { name: 'Save action form' }))
  await screen.findByRole('button', { name: 'New order form · revision 3' })
  fireEvent.click(screen.getByRole('button', { name: 'Load more saved views' }))
  await screen.findByRole('button', { name: 'Other view · revision 1' })
  expect(screen.getAllByRole('button', { name: 'New order form · revision 3' })).toHaveLength(1)
  expect(screen.getAllByRole('button', { name: 'Other view · revision 1' })).toHaveLength(1)
  expect(screen.queryByRole('button', { name: /Stale form/ })).toBeNull()
  expect(screen.getByRole('button', { name: 'Order view · revision 2' })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Load more saved views' })).toBeNull()
  expect(list.mock.calls[1][1]).toMatchObject({ after: 'next' })
  fireEvent.click(screen.getByRole('button', { name: 'New order form · revision 3' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Send order now' })).toBeEnabled())
  expect(submit).not.toHaveBeenCalled()
  expect(reconcile).not.toHaveBeenCalled()
  expect(start).not.toHaveBeenCalled()
})
it.each(['tenantId', 'projectId', 'platformUserId', 'bindingId', 'projectRole', 'externalAuthority', 'externalSubject'])('blocks action form creation and clears input on fresh %s drift', async (field) => {
  const { props, authority } = setupActionComposer()
  const saveElement = vi.spyOn(props.client, 'saveElement')
  render(<OpsWorkspaceGround {...props} />)
  await loaded(); await composeActionForm()
  authority.scope = { ...scope, [field]: field === 'projectRole' ? 'reviewer' : field === 'externalSubject' ? 'auth0|other' : field === 'externalAuthority' ? 'other' : otherProject }
  if (field === 'projectId' || field === 'externalAuthority') vi.spyOn(props.client, 'getScope').mockResolvedValueOnce(authority.scope)
  fireEvent.click(screen.getByRole('button', { name: 'Save action form' }))
  await screen.findByText('You do not have permission to view these integrations.')
  expect(saveElement).not.toHaveBeenCalled()
  expect(screen.queryByLabelText('Form title')).toBeNull()
})
it.each(['project', 'account', 'generation', 'status', 'profile', 'unmount'])('aborts action form creation and ignores its late record on %s change', async (change) => {
  const { props, sessionController } = setupActionComposer()
  let finish
  const saveElement = vi.spyOn(props.client, 'saveElement').mockImplementation(() => new Promise((resolve) => { finish = resolve }))
  const { rerender, unmount } = render(<OpsWorkspaceGround {...props} />)
  await loaded(); await composeActionForm()
  fireEvent.click(screen.getByRole('button', { name: 'Save action form' }))
  await waitFor(() => expect(finish).toBeTypeOf('function'))
  const signal = saveElement.mock.calls[0][2].signal
  if (change === 'unmount') unmount()
  else if (change === 'generation') act(() => sessionController.change({ generation: 2 }))
  else if (change === 'status') act(() => sessionController.change({ status: 'expired' }))
  else rerender(<OpsWorkspaceGround {...props} {...(change === 'project' ? { projectId: otherProject } : change === 'account' ? { accountScope: 'account-b' } : { active: false })} />)
  expect(signal.aborted).toBe(true)
  await act(async () => finish({ element: { ...actionElement, id: 'new-order-form', title: 'New order form' }, revision: 1 }))
  expect(screen.queryByRole('button', { name: /New order form · revision/ })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Check saved form' })).toBeNull()
})
it('routes a forbidden action catalog result through host permission handling', async () => {
  const { props } = setupActionComposer()
  vi.spyOn(props.client, 'getActionCatalog').mockRejectedValue({ status: 403 })
  render(<OpsWorkspaceGround {...props} />)
  await loaded()
  fireEvent.click(screen.getByRole('button', { name: 'Load permitted actions' }))
  await screen.findByText('You do not have permission to view these integrations.')
  expect(screen.queryByRole('region', { name: 'Create an action form' })).toBeNull()
})
function setupComposer() {
  const harness = setupAction()
  const original = harness.fetchImpl.getMockImplementation()
  harness.fetchImpl.mockImplementation((url, options) => {
    if (url.includes('/action-catalog?')) return Promise.resolve(response({ actions: [catalogAction], next_cursor: null }))
    if (url.endsWith('/manual-workflows')) return Promise.resolve(response(reference))
    return original(url, options)
  })
  return harness
}
it('registers from the shared workspace without starting and leaves existing Start review explicit', async () => {
  const { props, fetchImpl } = setupComposer()
  render(<OpsWorkspaceGround {...props} />)
  await loaded()
  expect(fetchImpl.mock.calls.some(([url]) => url.includes('/action-catalog?'))).toBe(false)
  await composeWorkflow()
  fireEvent.click(screen.getByRole('button', { name: 'Save workflow definition' }))
  await screen.findByRole('region', { name: 'Saved immutable definition' })
  expect(screen.getByLabelText('Workflow definition ID')).toHaveValue('')
  expect(screen.getByRole('button', { name: 'Load workflow review' })).toBeEnabled()
  expect(fetchImpl.mock.calls.filter(([, options]) => options.method === 'POST').map(([url]) => url.split('/integrations/')[1])).toEqual(['manual-workflows'])
})
it.each(['tenantId', 'projectId', 'platformUserId', 'bindingId', 'projectRole', 'externalAuthority', 'externalSubject'])('refuses definition dispatch and clears input on fresh %s drift', async (field) => {
  const { props, authority, fetchImpl } = setupComposer()
  render(<OpsWorkspaceGround {...props} />)
  await loaded(); await composeWorkflow()
  authority.scope = { ...scope, [field]: field === 'projectRole' ? 'reviewer' : field === 'externalSubject' ? 'auth0|other' : field === 'externalAuthority' ? 'other' : otherProject }
  if (field === 'projectId' || field === 'externalAuthority') vi.spyOn(props.client, 'getScope').mockResolvedValueOnce(authority.scope)
  fireEvent.click(screen.getByRole('button', { name: 'Save workflow definition' }))
  await screen.findByText('You do not have permission to view these integrations.')
  expect(fetchImpl.mock.calls.some(([url]) => url.endsWith('/manual-workflows'))).toBe(false)
  expect(screen.queryByLabelText('Definition title')).toBeNull()
})
it.each(['project', 'account', 'generation', 'status', 'profile', 'unmount'])('aborts pending definition registration and clears recovery on %s change', async (change) => {
  const { props, fetchImpl, sessionController } = setupComposer()
  const original = fetchImpl.getMockImplementation()
  let finish
  fetchImpl.mockImplementation((url, options) => url.endsWith('/manual-workflows')
    ? new Promise((resolve) => { finish = resolve }) : original(url, options))
  const { rerender, unmount } = render(<OpsWorkspaceGround {...props} />)
  await loaded(); await composeWorkflow()
  fireEvent.click(screen.getByRole('button', { name: 'Save workflow definition' }))
  await waitFor(() => expect(finish).toBeTypeOf('function'))
  const signal = fetchImpl.mock.calls.find(([url]) => url.endsWith('/manual-workflows'))[1].signal
  if (change === 'unmount') unmount()
  else if (change === 'generation') act(() => sessionController.change({ generation: 2 }))
  else if (change === 'status') act(() => sessionController.change({ status: 'expired' }))
  else rerender(<OpsWorkspaceGround {...props} {...(change === 'project' ? { projectId: otherProject } : change === 'account' ? { accountScope: 'account-b' } : { active: false })} />)
  expect(signal.aborted).toBe(true)
  await act(async () => finish(response(reference)))
  expect(screen.queryByRole('region', { name: 'Saved immutable definition' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Retry identical workflow definition' })).toBeNull()
})
async function reviewWorkflow() {
  fireEvent.change(screen.getByLabelText('Workflow definition ID'), { target: { value: reference.id } })
  fireEvent.change(screen.getByLabelText('Workflow version'), { target: { value: String(reference.version) } })
  fireEvent.change(screen.getByLabelText('Workflow digest'), { target: { value: reference.digest } })
  fireEvent.click(screen.getByRole('button', { name: 'Load workflow review' }))
  await screen.findByRole('region', { name: 'Immutable workflow review' })
}
it('hands the canonical admitted run to the viewer once and recovers a failed first GET without another submission', async () => {
  const { props } = setupAction()
  vi.spyOn(props.client, 'getManualWorkflowDefinition').mockResolvedValue(workflowSnapshot.definition)
  const submit = vi.spyOn(props.client, 'submitManualWorkflow').mockResolvedValue({ status: 'admitted', run: workflowSnapshot.run, replayed: false })
  const get = vi.spyOn(props.client, 'getManualWorkflowRunV2').mockRejectedValueOnce(new Error('lost')).mockResolvedValue(workflowSnapshotV2)
  const view = render(<OpsWorkspaceGround {...props} />)
  await loaded(); await reviewWorkflow()
  expect(submit).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Start workflow', exact: true }))
  await screen.findByText(/The workflow could not be verified/)
  expect(screen.getByLabelText('Run UUID')).toHaveValue(otherProject)
  view.rerender(<OpsWorkspaceGround {...props} projectLabel="Renamed project" />)
  expect(get).toHaveBeenCalledOnce()
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await screen.findByRole('region', { name: 'Manual workflow run status' })
  expect(submit).toHaveBeenCalledOnce()
  expect(get).toHaveBeenCalledTimes(2)
})
it.each(['tenantId', 'projectId', 'platformUserId', 'bindingId', 'projectRole', 'externalAuthority', 'externalSubject'])('rejects full Start identity %s drift after review with no POST', async (field) => {
  const { props, authority } = setupAction()
  vi.spyOn(props.client, 'getManualWorkflowDefinition').mockResolvedValue(workflowSnapshot.definition)
  const submit = vi.spyOn(props.client, 'submitManualWorkflow')
  render(<OpsWorkspaceGround {...props} />)
  await loaded(); await reviewWorkflow()
  authority.scope = { ...scope, [field]: field === 'projectRole' ? 'reviewer' : field === 'externalSubject' ? 'auth0|other' : field === 'externalAuthority' ? 'other' : otherProject }
  if (field === 'projectId' || field === 'externalAuthority') vi.spyOn(props.client, 'getScope').mockResolvedValueOnce(authority.scope)
  fireEvent.click(screen.getByRole('button', { name: 'Start workflow', exact: true }))
  await screen.findByText('You do not have permission to view these integrations.')
  expect(submit).not.toHaveBeenCalled()
  expect(screen.queryByRole('region', { name: 'Immutable workflow review' })).toBeNull()
})
it.each(['project', 'account', 'generation', 'status', 'profile', 'unmount'])('aborts a pending Start and suppresses its late handoff on %s invalidation', async (change) => {
  const { props, sessionController } = setupAction()
  vi.spyOn(props.client, 'getManualWorkflowDefinition').mockResolvedValue(workflowSnapshot.definition)
  let finish
  const submit = vi.spyOn(props.client, 'submitManualWorkflow').mockImplementation(() => new Promise((resolve) => { finish = resolve }))
  const get = vi.spyOn(props.client, 'getManualWorkflowRunV2')
  const { rerender, unmount } = render(<OpsWorkspaceGround {...props} />)
  await loaded(); await reviewWorkflow()
  fireEvent.click(screen.getByRole('button', { name: 'Start workflow', exact: true }))
  await waitFor(() => expect(submit).toHaveBeenCalledOnce())
  const signal = submit.mock.calls[0][2].signal
  if (change === 'unmount') unmount()
  else if (change === 'generation') act(() => sessionController.change({ generation: 2 }))
  else if (change === 'status') act(() => sessionController.change({ status: 'expired' }))
  else rerender(<OpsWorkspaceGround {...props} {...(change === 'project' ? { projectId: otherProject } : change === 'account' ? { accountScope: 'account-b' } : { active: false })} />)
  expect(signal.aborted).toBe(true)
  await act(async () => finish({ status: 'admitted', run: workflowSnapshot.run, replayed: false }))
  expect(get).not.toHaveBeenCalled()
  expect(screen.queryByRole('region', { name: 'Immutable workflow review' })).toBeNull()
})
it.each(['tenantId', 'projectId', 'platformUserId', 'bindingId', 'projectRole', 'externalAuthority', 'externalSubject'])('refuses fresh workflow scope %s drift and clears protected evidence', async (field) => {
  const { props, authority, fetchImpl } = setupAction()
  vi.spyOn(props.client, 'getManualWorkflowRunV2').mockResolvedValue(workflowSnapshotV2)
  render(<OpsWorkspaceGround {...props} />)
  await loaded()
  fireEvent.change(screen.getByLabelText('Run UUID'), { target: { value: otherProject } })
  fireEvent.click(screen.getByRole('button', { name: 'Load', exact: true }))
  await screen.findByRole('region', { name: 'Manual workflow run status' })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled())
  authority.scope = { ...scope, [field]: field === 'projectRole' ? 'reviewer' : field === 'externalSubject' ? 'auth0|different' : field === 'externalAuthority' ? 'other' : otherProject }
  // getScope itself rejects malformed authority or a different requested project; a
  // parsed fresh identity still has to match every field before loading evidence.
  if (field === 'projectId' || field === 'externalAuthority') vi.spyOn(props.client, 'getScope').mockResolvedValueOnce(authority.scope)
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await screen.findByText('You do not have permission to view these integrations.')
  expect(props.client.getManualWorkflowRunV2).toHaveBeenCalledTimes(1)
  expect(screen.queryByRole('region', { name: 'Manual workflow run status' })).toBeNull()
  expect(screen.queryByLabelText('Run UUID')).toBeNull()
  expect(fetchImpl.mock.calls.some(([url]) => url.includes('manual-workflow-runs'))).toBe(false)
})
it.each(['project', 'account', 'generation', 'status', 'profile', 'unmount'])('aborts the workflow and discards late results on %s invalidation', async (change) => {
  const { props, sessionController } = setupAction()
  let finish
  const get = vi.spyOn(props.client, 'getManualWorkflowRunV2').mockImplementation(() => new Promise((resolve) => { finish = resolve }))
  const { rerender, unmount } = render(<OpsWorkspaceGround {...props} />)
  await loaded()
  fireEvent.change(screen.getByLabelText('Run UUID'), { target: { value: otherProject } })
  fireEvent.click(screen.getByRole('button', { name: 'Load', exact: true }))
  await waitFor(() => expect(get).toHaveBeenCalledTimes(1))
  const signal = get.mock.calls[0][2].signal
  if (change === 'unmount') unmount()
  else if (change === 'generation') act(() => sessionController.change({ generation: 2 }))
  else if (change === 'status') act(() => sessionController.change({ status: 'expired' }))
  else rerender(<OpsWorkspaceGround {...props} {...(change === 'project' ? { projectId: otherProject } : change === 'account' ? { accountScope: 'account-b' } : { active: false })} />)
  expect(signal.aborted).toBe(true)
  expect(screen.queryByRole('region', { name: 'Manual workflow run status' })).toBeNull()
  await act(async () => finish(workflowSnapshotV2))
  expect(screen.queryByRole('region', { name: 'Manual workflow run status' })).toBeNull()
})
it('clears loaded workflow evidence immediately when the active account changes', async () => {
  const { props } = setupAction()
  vi.spyOn(props.client, 'getManualWorkflowRunV2').mockResolvedValue(workflowSnapshotV2)
  const { rerender } = render(<OpsWorkspaceGround {...props} />)
  await loaded()
  fireEvent.change(screen.getByLabelText('Run UUID'), { target: { value: otherProject } })
  fireEvent.click(screen.getByRole('button', { name: 'Load', exact: true }))
  await screen.findByRole('region', { name: 'Manual workflow run status' })
  rerender(<OpsWorkspaceGround {...props} accountScope="account-b" />)
  expect(screen.queryByRole('region', { name: 'Manual workflow run status' })).toBeNull()
})
it('keeps the shared display key bounded for a long verified external identity', async () => {
  const { props, authority } = setupAction()
  authority.scope = { ...scope, externalSubject: 's'.repeat(500) }
  vi.spyOn(props.client, 'getManualWorkflowRunV2').mockResolvedValue(workflowSnapshotV2)
  render(<OpsWorkspaceGround {...props} />)
  await loaded()
  fireEvent.change(screen.getByLabelText('Run UUID'), { target: { value: otherProject } })
  fireEvent.click(screen.getByRole('button', { name: 'Load', exact: true }))
  await screen.findByRole('region', { name: 'Manual workflow run status' })
  expect(screen.queryByText('Workflow status is unavailable.')).toBeNull()
})
it('rejects late workflow preflight continuations when a supplied client ignores cancellation', async () => {
  const { props } = setupAction()
  let finish
  vi.spyOn(props.client, 'getScope').mockResolvedValueOnce(scope).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  const get = vi.spyOn(props.client, 'getManualWorkflowRunV2')
  const { rerender } = render(<OpsWorkspaceGround {...props} />)
  await loaded()
  fireEvent.change(screen.getByLabelText('Run UUID'), { target: { value: otherProject } })
  fireEvent.click(screen.getByRole('button', { name: 'Load', exact: true }))
  await waitFor(() => expect(finish).toBeTypeOf('function'))
  rerender(<OpsWorkspaceGround {...props} active={false} />)
  await act(async () => finish(scope))
  expect(get).not.toHaveBeenCalled()
  expect(screen.queryByRole('region', { name: 'Manual workflow run status' })).toBeNull()
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
