import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import X07DrawingRoleReview from './X07DrawingRoleReview.jsx'

afterEach(cleanup)
const row = { block: 'Review Panel', layer: 'MODULES', count: 3, insertion_bounds: [0, 0, 2, 2] }
const reply = { ok: true, data: { role_discovery: { schema: 'leaf.drawing-role-discovery.v1',
  module: { first_choice: row, candidates: [row], competing: false }, roof: [], obstruction: [] } } }
const scope = { authorityKey: 'customer-a-session', solarBinding: { project_id: 'synthetic-project' }, isCurrent: () => true }

it('requires one explicit confirmation and passes captured authority and drawing binding', async () => {
  let finish
  const confirm = vi.fn(() => new Promise(resolve => { finish = resolve }))
  const client = { readContext: vi.fn().mockResolvedValue(reply) }
  render(<X07DrawingRoleReview {...scope} projectClient={client} onConfirm={confirm} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Confirm roles' }))
  fireEvent.submit(screen.getByRole('form', { name: 'Confirm drawing roles' }))
  await act(async () => {})
  expect(confirm).toHaveBeenCalledTimes(1)
  expect(confirm.mock.calls[0][0]).toEqual({ block: 'Review Panel', layer: 'MODULES', count: 3, design_window: [0, 0, 2, 2] })
  expect(confirm.mock.calls[0][1].authorityKey).toBe('customer-a-session')
  expect(confirm.mock.calls[0][1].solarBinding).toEqual(scope.solarBinding)
  await act(async () => { finish({ ok: true }) })
  expect(await screen.findByText('Roles confirmed.')).toBeTruthy()
  expect(confirm).toHaveBeenCalledTimes(1)
})

it('aborts a retired read and never offers stale confirmation', async () => {
  let resolve
  const client = { readContext: vi.fn(() => new Promise(done => { resolve = done })) }
  const confirm = vi.fn()
  const view = render(<X07DrawingRoleReview {...scope} projectClient={client} onConfirm={confirm} />)
  await act(async () => {})
  view.rerender(<X07DrawingRoleReview {...scope} isCurrent={() => false} projectClient={client} onConfirm={confirm} />)
  expect(client.readContext.mock.calls[0][0].signal.aborted).toBe(true)
  await act(async () => { resolve(reply) })
  expect(screen.queryByRole('button', { name: 'Confirm roles' })).toBeNull()
  expect(confirm).not.toHaveBeenCalled()
})

it('aborts pending confirmation on authority replacement and ignores its success', async () => {
  let finish
  const confirm = vi.fn(() => new Promise(resolve => { finish = resolve }))
  const client = { readContext: vi.fn().mockResolvedValue(reply) }
  const view = render(<X07DrawingRoleReview {...scope} projectClient={client} onConfirm={confirm} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Confirm roles' }))
  await act(async () => {})
  view.rerender(<X07DrawingRoleReview {...scope} authorityKey="new-authority" projectClient={client} onConfirm={confirm} />)
  expect(confirm.mock.calls[0][1].signal.aborted).toBe(true)
  await act(async () => { finish({ ok: true }) })
  expect(screen.queryByText('Roles confirmed.')).toBeNull()
  expect(await screen.findByRole('button', { name: 'Confirm roles' })).toBeTruthy()
})
