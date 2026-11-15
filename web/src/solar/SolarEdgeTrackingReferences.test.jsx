import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import ProjectSolarReadPanel from './ProjectSolarReadPanel.jsx'

afterEach(() => { cleanup(); vi.restoreAllMocks() })
const binding = { kind: 'project', organization_id: '11111111-1111-4111-8111-111111111111',
  project_id: '22222222-2222-4222-8222-222222222222', drawing_id: '33333333-3333-4333-8333-333333333333',
  input_version_id: '44444444-4444-4444-8444-444444444444' }
const digest = 'sha256:' + 'c'.repeat(64)
const name = 'solar-solaredge-tracking-read'
const ref = index => `leaf:panel:00000000-0000-4000-8000-${String(index).padStart(12, '0')}`
function result(count = 210, overrides = {}) {
  const output = { schema: 'leaf.solar-project-solaredge-tracking-read.v1', accepted: true, labels_only: true,
    section: 'unassigned', page: { offset: 0, returned: count, total: count, next_offset: null },
    items: Array.from({ length: count }, (_, index) => ref(index)),
    acceptance: { mode: 'matched-only', status: 'partial', unresolved_groups: 1 },
    one_liner: 'Remaining groups need correction. Labels are not electrical verification.' }
  return { schema: 'leaf.solar-project-read.v1', adapter: 'project-local-graph-read',
    ...Object.fromEntries(Object.entries(binding).filter(([key]) => key !== 'kind')),
    tool: name, tool_manifest_sha256: digest, graph_project_id: 'synthetic-graph',
    head_version_id: binding.input_version_id, is_head: true, current_required: false,
    input_intake_sha256: 'a'.repeat(64), graph_sha256: 'b'.repeat(64), request_sha256: 'd'.repeat(64),
    output_sha256: 'e'.repeat(64), output_bytes: JSON.stringify(output).length,
    drawing_changed: false, output, ...overrides }
}
function setup(data = result()) {
  let next = data, snapshot = { binding, authorityKey: 'read-epoch' }
  const listeners = new Set()
  const client = { readSolar: vi.fn(async () => ({ ok: true, status: 200, data: { result: next } })),
    getSnapshot: () => snapshot, captureCatalogScope: () => () => snapshot.binding === binding,
    subscribe: listener => { listeners.add(listener); return () => listeners.delete(listener) },
    downloadArtifact: vi.fn() }
  const tool = { name, catalog_digest: digest, availability: { runnable: true }, params: { type: 'object',
    properties: { section: { type: 'string', enum: ['unassigned'], default: 'unassigned' },
      offset: { type: 'integer', minimum: 0, default: 0 } } } }
  const catalog = { binding, authorityKey: 'read-epoch', client,
    families: [{ capabilities: [tool] }], isCurrent: () => true }
  render(<ProjectSolarReadPanel tool={tool} solarBinding={binding} authorityKey="read-epoch"
    projectClient={client} projectCatalog={catalog} isCurrent={() => true} />)
  return { client, next: value => { next = value }, retire: () => {
    snapshot = { ...snapshot, binding: { ...binding, input_version_id: '55555555-5555-4555-8555-555555555555' } }
    for (const listener of listeners) listener()
  } }
}
const read = async () => {
  fireEvent.click(screen.getByRole('button', { name: 'Read project output' }))
  await waitFor(() => expect(screen.getByTestId('solar-read-result')).toBeInTheDocument())
}
const list = () => screen.getByRole('region', { name: 'Unassigned panel references' })

it('shows every returned unassigned reference through bounded local pages', async () => {
  const h = setup()
  await read()
  expect(within(list()).getAllByRole('row')).toHaveLength(101)
  expect(within(list()).getByText(ref(99))).toBeInTheDocument()
  expect(within(list()).queryByText(ref(100))).toBeNull()
  fireEvent.click(within(list()).getByRole('button', { name: 'Next references' }))
  expect(within(list()).getByText(ref(199))).toBeInTheDocument()
  fireEvent.click(within(list()).getByRole('button', { name: 'Next references' }))
  expect(within(list()).getAllByRole('row')).toHaveLength(11)
  expect(within(list()).getByText(ref(209))).toBeInTheDocument()
  expect(within(list()).getByRole('button', { name: 'Next references' })).toBeDisabled()
  fireEvent.click(within(list()).getByRole('button', { name: 'Previous references' }))
  expect(within(list()).getByText(ref(100))).toBeInTheDocument()
  expect(h.client.readSolar).toHaveBeenCalledTimes(1)
  expect(h.client.downloadArtifact).not.toHaveBeenCalled()
})

it('resets local navigation for a new saved read result and retires on binding change', async () => {
  const h = setup()
  await read()
  fireEvent.click(within(list()).getByRole('button', { name: 'Next references' }))
  h.next(result(3))
  await read()
  await waitFor(() => expect(within(list()).getAllByRole('row')).toHaveLength(4))
  expect(within(list()).getByText(ref(0))).toBeInTheDocument()
  act(() => h.retire())
  expect(screen.queryByRole('region', { name: 'Unassigned panel references' })).toBeNull()
})

it.each(['foreign', 'malformed-page', 'other-section'])('does not mount reference rows for %s output', async kind => {
  const data = result()
  if (kind === 'foreign') data.drawing_id = '66666666-6666-4666-8666-666666666666'
  if (kind === 'malformed-page') data.output.page.returned++
  if (kind === 'other-section') data.output.section = 'strings'
  const h = setup(data)
  fireEvent.click(screen.getByRole('button', { name: 'Read project output' }))
  await waitFor(() => expect(h.client.readSolar).toHaveBeenCalledTimes(1))
  await act(async () => { for (let i = 0; i < 8; i++) await Promise.resolve() })
  expect(screen.queryByRole('region', { name: 'Unassigned panel references' })).toBeNull()
})

it('allows an explicitly historical saved tracking page and exposes saved continuation honestly', async () => {
  const data = result(3, { head_version_id: '55555555-5555-4555-8555-555555555555', is_head: false })
  data.output.page = { offset: 1000, returned: 3, total: 1006, next_offset: 1003 }
  setup(data)
  await read()
  expect(within(list()).getByText(ref(2))).toBeInTheDocument()
  expect(within(list()).getByText(/1003.*Offset|Offset.*1003/)).toBeInTheDocument()
  expect(screen.getByText('Historical version.')).toBeInTheDocument()
})

it('does not call an empty page at the end an empty saved record', async () => {
  const data = result(0)
  data.output.page = { offset: 3, returned: 0, total: 3, next_offset: null }
  setup(data)
  await read()
  expect(within(list()).getByText('No unassigned panel references on this saved page.')).toBeInTheDocument()
  expect(within(list()).queryByRole('table')).toBeNull()
})
