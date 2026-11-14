import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { ReadableStream } from 'node:stream/web'
import { TextDecoder, TextEncoder } from 'node:util'
import { OpsWorkspaceGround } from './OpsWorkspaceGround.jsx'
import { createIntegrationsClient } from './client.js'

vi.mock('../site/SurfaceFrame.jsx', () => ({ useSurfaceFrame: () => undefined }))
vi.mock('../api.js', () => ({ authHeaders: () => ({ Authorization: 'Bearer synthetic-only' }), noteUnauthorized: vi.fn() }))
vi.mock('../projects/api.js', () => ({ listProjectReferences: vi.fn(), readProjectReferenceOriginal: vi.fn() }))

// Public synthetic input only. No provider, credential or historical account data.
const projectId = '11111111-1111-4111-8111-111111111111'
const reference = { id: 'synthetic-capacity', version: 1, digest: 'a'.repeat(64) }
const records = [
  ['Fixture main Claude', 'Claude', 'main', 80, '2026-11-02T12:00:00Z'],
  ['Fixture main Codex', 'Codex', 'main', 65, '2026-11-03T12:00:00Z'],
  ['Fixture second Claude', 'Claude', 'second', 45, '2026-11-04T12:00:00Z'],
  ['Fixture second Codex', 'Codex', 'second', 30, '2026-11-05T12:00:00Z'],
  ['Fixture unpaired Claude', 'Claude', '', 20, '2026-11-06T12:00:00Z'],
  ['Fixture Kimi', 'Kimi', '', 90, '2026-11-07T12:00:00Z'],
]
const ids = ['account', 'provider', 'pair_key', 'remaining_pct', 'reset_at']
const fields = [
  { id: 'account', label: 'Account', type: { kind: 'text' }, nullable: false, sensitivity: 'internal' },
  { id: 'provider', label: 'Provider', type: { kind: 'text' }, nullable: false, sensitivity: 'internal' },
  { id: 'pair_key', label: 'Pair key', type: { kind: 'text' }, nullable: true, sensitivity: 'internal' },
  { id: 'remaining_pct', label: 'Remaining', type: { kind: 'ratio', scale: 'percent' }, nullable: false, sensitivity: 'internal' },
  { id: 'reset_at', label: 'Reset', type: { kind: 'instant' }, nullable: false, sensitivity: 'internal' },
]
const freshness = { source_updated_at: null, synced_at: null, fetched_at: '2026-11-01T12:00:00Z' }
const metadata = { reference, revision: 1, title: 'Synthetic capacity', fields, lastAttempt: null, freshness }
const read = { schema_version: 1, dataset_ref: reference, fields, next_cursor: null, status: 'ready', freshness,
  query_coverage: { kind: 'complete_materialization', matching_rows: 6, offset: 0 },
  // Deliberately interleaved input proves grouping rather than a pre-arranged view.
  rows: [5, 1, 4, 3, 0, 2].map(index => ({ key: `record-${index}`,
    values: Object.fromEntries(ids.map((id, column) => [id, records[index][column]])) })),
}
function response(value) {
  return { ok: true, status: 200, headers: { get: key => key === 'content-type' ? 'application/json' : null },
    body: new ReadableStream({ start(controller) {
      controller.enqueue(new TextEncoder().encode(JSON.stringify(value))); controller.close()
    } }) }
}
function harness(initial) {
  let stored = initial
  const authority = { subject: 'synthetic-subject', projectId }
  const calls = []
  const fetchImpl = vi.fn(async (raw, options) => {
    const url = new URL(raw)
    calls.push({ path: url.pathname, method: options.method, body: options.body })
    expect(url.origin).toBe('https://fixture.invalid')
    expect(url.pathname).toContain(`/projects/${authority.projectId}/`)
    if (url.pathname.endsWith('/scope')) return response({ tenantId: projectId, projectId: authority.projectId,
      platformUserId: projectId, bindingId: projectId, projectRole: 'owner',
      externalAuthority: 'auth0', externalSubject: authority.subject })
    if (url.pathname.endsWith('/datasets')) return response({ records: [metadata], nextCursor: null })
    if (url.pathname.endsWith('/elements') && options.method === 'GET') return response({ records: stored ? [stored] : [], nextCursor: null })
    if (url.pathname.endsWith('/elements') && options.method === 'POST') {
      stored = { element: JSON.parse(options.body), revision: 1 }
      return response(stored)
    }
    if (url.pathname.endsWith('/elements/capacity-view')) return response(stored)
    if (url.pathname.endsWith('/query')) return response(read)
    throw new Error(`Unexpected inert route ${options.method} ${url.pathname}`)
  })
  const snapshot = { status: 'active', generation: 1 }
  const controller = { getSnapshot: () => snapshot, subscribe: () => () => {} }
  return { calls, authority, snapshot, saved: () => stored, removeSaved: () => { stored = null }, props: { active: true, signedIn: true, projectId,
    accountScope: 'synthetic-account', session: { ...snapshot, controller },
    client: createIntegrationsClient({ origin: 'https://fixture.invalid', fetchImpl }) } }
}
async function expectSixRecords() {
  const main = await screen.findByRole('region', { name: 'Pair main' })
  const second = screen.getByRole('region', { name: 'Pair second' })
  const unpaired = screen.getByRole('region', { name: 'Unpaired accounts' })
  for (const [region, indices] of [[main, [0, 1]], [second, [2, 3]], [unpaired, [5, 4]]]) {
    const rows = within(region).getAllByRole('row').slice(1)
    expect(rows).toHaveLength(2)
    indices.forEach((index, i) => {
      const [account, provider, key, pct] = records[index]
      const cells = within(rows[i]).getAllByRole('cell')
      expect(cells[0]).toHaveTextContent(account)
      expect(cells[1]).toHaveTextContent(provider)
      expect(cells[2].textContent).toBe(key)
      expect(cells[3].textContent).toBe(`${pct}%`)
      expect(cells[4].textContent).toBe(`Nov ${index + 2}, 2026, 12:00:00 UTC`)
    })
  }
  expect(main.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(second.compareDocumentPosition(unpaired) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(within(unpaired).getByRole('cell', { name: 'Fixture Kimi', exact: true }).closest('tr'))
    .not.toHaveTextContent('Fixture unpaired Claude')
}
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('saves, reopens and restores the six-record capacity view after an Ops profile round trip', async () => {
  vi.stubGlobal('TextDecoder', TextDecoder)
  vi.stubGlobal('TextEncoder', TextEncoder)
  const h = harness()
  const mounted = render(<OpsWorkspaceGround {...h.props} />)
  await screen.findByRole('option', { name: 'Synthetic capacity · version 1' })
  const dataset = screen.getByLabelText('Dataset')
  fireEvent.change(dataset, { target: { value: dataset.options[1].value } })
  fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Capacity view' } })
  fireEvent.change(screen.getByLabelText('Stable ID'), { target: { value: 'capacity-view' } })
  for (const label of ['Account', 'Provider', 'Pair key', 'Remaining', 'Reset']) {
    fireEvent.click(screen.getByRole('checkbox', { name: label, exact: true }))
  }
  fireEvent.click(screen.getByRole('checkbox', { name: 'Group verified provider pairs' }))
  for (const [label, value] of [['Pair key field', 'pair_key'], ['Provider field', 'provider'],
    ['First provider', 'Claude'], ['Second provider', 'Codex']]) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } })
  }
  fireEvent.click(screen.getByRole('button', { name: 'Save view', exact: true }))
  await expectSixRecords()
  expect(h.saved()).toMatchObject({ revision: 1, element: { dataset_ref: reference,
    props: { columns: ids, row_grouping: { kind: 'provider_pairs', pair_key_field: 'pair_key',
      provider_field: 'provider', providers: ['Claude', 'Codex'] } } } })
  mounted.unmount()
  const reopened = render(<OpsWorkspaceGround {...h.props} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Capacity view · revision 1' }))
  await expectSixRecords()
  reopened.rerender(<OpsWorkspaceGround {...h.props} active={false} />)
  expect(screen.queryByRole('region', { name: 'Pair main' })).toBeNull()
  reopened.rerender(<OpsWorkspaceGround {...h.props} active />)
  await expectSixRecords()
  expect(screen.getByText(/Recipe revision 1/)).toBeInTheDocument()
  expect(h.calls.filter(call => call.method === 'POST' && call.path.endsWith('/elements'))).toHaveLength(1)
  expect(h.calls.every(call => !/connections|actions|preview|providers/.test(call.path))).toBe(true)
  await waitFor(() => expect(screen.queryByText('Loading integrations…')).toBeNull())
})

const existing = () => ({ revision: 1, element: { schema_version: 1, id: 'capacity-view', title: 'Capacity view',
  dataset_ref: reference, query: { fields: ids, limit: 100 }, kind: 'dataset.table',
  props: { columns: ids, row_limit: 100, row_grouping: { kind: 'provider_pairs', pair_key_field: 'pair_key',
    provider_field: 'provider', providers: ['Claude', 'Codex'] } } } })

it.each(['account', 'scope', 'removed', 'session', 'project', 'signout'])('does not restore a capacity view after %s changes while hidden', async (change) => {
  vi.stubGlobal('TextDecoder', TextDecoder)
  vi.stubGlobal('TextEncoder', TextEncoder)
  const h = harness(existing())
  const mounted = render(<OpsWorkspaceGround {...h.props} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Capacity view · revision 1' }))
  await expectSixRecords()
  const queries = h.calls.filter(call => call.path.endsWith('/query')).length
  mounted.rerender(<OpsWorkspaceGround {...h.props} active={false} />)
  if (change === 'scope') h.authority.subject = 'another-subject'
  if (change === 'removed') h.removeSaved()
  if (change === 'session') h.snapshot.generation += 1
  if (change === 'project') h.authority.projectId = '22222222-2222-4222-8222-222222222222'
  if (change === 'signout') mounted.rerender(<OpsWorkspaceGround {...h.props} active={false} signedIn={false} />)
  mounted.rerender(<OpsWorkspaceGround {...h.props} projectId={h.authority.projectId}
    accountScope={change === 'account' ? 'other-account' : h.props.accountScope} active />)
  await screen.findByRole('option', { name: 'Synthetic capacity · version 1' })
  expect(screen.queryByRole('region', { name: 'Pair main' })).toBeNull()
  expect(h.calls.filter(call => call.path.endsWith('/query'))).toHaveLength(queries)
  // Dataset reads use POST /query. No save, action or other mutation occurred.
  expect(h.calls.filter(call => call.method !== 'GET').map(call => call.path))
    .toEqual(Array(queries).fill(`/api/v1/projects/${projectId}/integrations/datasets/${reference.id}/versions/1/query`))
})
