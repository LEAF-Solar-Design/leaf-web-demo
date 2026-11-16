import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import DrawingRoleProposal from './DrawingRoleProposal.jsx'

afterEach(cleanup)
const row = { block: 'Synthetic Panel', layer: 'MODULES', count: 7, insertion_bounds: [0, 0, 6, 12] }
const reply = { ok: true, data: { role_discovery: { schema: 'leaf.drawing-role-discovery.v1',
  module: { first_choice: row, candidates: [row], competing: false }, roof: [], obstruction: [] } } }
const props = { authorityKey: 'synthetic', solarBinding: { project_id: 'one' }, isCurrent: () => true }

it('names the module block and layer as first choice without a user action', async () => {
  render(<DrawingRoleProposal {...props} projectClient={{ readContext: vi.fn().mockResolvedValue(reply) }} />)
  expect(await screen.findByRole('heading', { name: 'Drawing roles' })).toBeTruthy()
  expect(screen.getByText(/Module first choice:/).textContent).toContain('Synthetic Panel')
  expect(screen.getByText(/Module first choice:/).textContent).toContain('MODULES')
  expect(screen.getByRole('cell', { name: '7' })).toBeTruthy()
})

it('does not let a retired project response populate the next project', async () => {
  let resolve
  const old = { readContext: () => new Promise(done => { resolve = done }) }
  const next = { readContext: vi.fn().mockResolvedValue({ ok: false }) }
  const view = render(<DrawingRoleProposal {...props} projectClient={old} />)
  await act(async () => {})
  view.rerender(<DrawingRoleProposal {...props} solarBinding={{ project_id: 'two' }} projectClient={next} />)
  await act(async () => { resolve(reply) })
  expect(screen.queryByRole('heading', { name: 'Drawing roles' })).toBeNull()
  expect(await screen.findByText('Drawing roles could not be read.')).toBeTruthy()
})
