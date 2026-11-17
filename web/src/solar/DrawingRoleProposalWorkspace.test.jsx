import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import ProjectSolarWorkspace from './ProjectSolarWorkspace.jsx'
import { createSolarProjectClient } from './solarProjectClient.js'

const binding = { kind: 'project', organization_id: '11111111-1111-1111-1111-111111111111',
  project_id: '22222222-2222-2222-2222-222222222222', drawing_id: '33333333-3333-3333-3333-333333333333',
  input_version_id: '44444444-4444-4444-4444-444444444444' }
const row = { block: 'Synthetic Panel', layer: 'MODULES', count: 7, insertion_bounds: [0, 0, 6, 12] }
const context = { context: { ...binding, head_version_id: binding.input_version_id, is_head: true }, intake: {},
  role_discovery: { schema: 'leaf.drawing-role-discovery.v1',
    module: { first_choice: row, candidates: [row], competing: false }, roof: [], obstruction: [] } }
const clients = []
afterEach(() => { cleanup(); clients.splice(0).forEach(client => client.dispose()) })

function setup(fetchImpl = vi.fn(async () => ({ ok: true, status: 200, json: async () => context }))) {
  const client = createSolarProjectClient({ fetchImpl })
  client.setScope(binding, 'synthetic-authority')
  clients.push(client)
  const props = { solarBinding: binding, authorityKey: 'synthetic-authority', projectClient: client,
    projectCatalog: null, isCurrent: () => true }
  return { client, props, fetchImpl }
}

it('shows the first choice from the real scoped context client while catalog is unavailable', async () => {
  const h = setup()
  render(<ProjectSolarWorkspace {...h.props} />)
  expect(await screen.findByRole('heading', { name: 'Drawing roles' })).toBeTruthy()
  expect(screen.getByText(/Module first choice:/).textContent).toContain('Synthetic Panel')
  expect(screen.getByText(/Module first choice:/).textContent).toContain('MODULES')
  expect(screen.getByRole('cell', { name: '7' })).toBeTruthy()
  expect(h.fetchImpl.mock.calls).toHaveLength(1)
  expect(h.fetchImpl.mock.calls[0][0]).toContain('/context?')
  expect(h.fetchImpl.mock.calls[0][1].method).toBe('GET')
  expect(screen.queryAllByRole('button')).toHaveLength(0)
})

it('rejects a late context after the current project changes', async () => {
  const replies = []
  const h = setup(vi.fn(() => new Promise(done => { replies.push(done) })))
  const view = render(<ProjectSolarWorkspace {...h.props} />)
  await act(async () => {})
  const next = { ...binding, project_id: '55555555-5555-5555-5555-555555555555' }
  await act(async () => { h.client.setScope(next, 'synthetic-authority') })
  view.rerender(<ProjectSolarWorkspace {...h.props} solarBinding={next} />)
  await act(async () => { replies[0]({ ok: true, status: 200, json: async () => context }) })
  expect(screen.queryByText('Synthetic Panel')).toBeNull()
  expect(screen.queryByRole('heading', { name: 'Drawing roles' })).toBeNull()
})

it('does not read drawing roles for an invalid or retired project scope', async () => {
  const h = setup()
  const view = render(<ProjectSolarWorkspace {...h.props} isCurrent={() => false} />)
  await act(async () => {})
  expect(h.fetchImpl).not.toHaveBeenCalled()
  view.rerender(<ProjectSolarWorkspace {...h.props} solarBinding={null} />)
  await act(async () => {})
  expect(h.fetchImpl).not.toHaveBeenCalled()
  expect(screen.queryByRole('heading', { name: 'Drawing roles' })).toBeNull()
})
