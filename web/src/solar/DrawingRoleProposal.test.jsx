import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { StrictMode } from 'react'
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

it('refuses malformed rows and labels without crashing or exposing supplied objects', async () => {
  const invalid = [null, { ...row, block: { private: 'SENTINEL' } }, { ...row, count: Number.MAX_SAFE_INTEGER + 1 },
    { ...row, count: -1 }, { ...row, insertion_bounds: [0, 0, Infinity, 2] },
    { ...row, insertion_bounds: [3, 0, 1, 2] }, { ...row, insertion_bounds: [0, 0] }]
  for (const candidate of invalid) {
    const data = { ...reply.data.role_discovery, module: { ...reply.data.role_discovery.module, candidates: [candidate] } }
    const client = { readContext: vi.fn().mockResolvedValue({ ok: true, data: { role_discovery: data } }) }
    const view = render(<DrawingRoleProposal {...props} projectClient={client} />)
    expect(await screen.findByText('Drawing roles could not be read.')).toBeTruthy()
    expect(screen.queryByRole('table')).toBeNull()
    expect(screen.queryByText('SENTINEL')).toBeNull()
    expect(client.readContext).toHaveBeenCalledTimes(1)
    view.unmount()
  }
})

it('recovers from synchronous throws and rejection with one pending retry', async () => {
  let resolve
  const client = { readContext: vi.fn().mockImplementationOnce(() => { throw new Error('private detail') })
    .mockRejectedValueOnce(new Error('unavailable')).mockImplementationOnce(() => new Promise(done => { resolve = done })) }
  const view = render(<DrawingRoleProposal {...props} projectClient={client} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Retry reading roles' }))
  const retry = await screen.findByRole('button', { name: 'Retry reading roles' })
  fireEvent.click(retry)
  fireEvent.click(retry)
  await act(async () => {})
  expect(client.readContext).toHaveBeenCalledTimes(3)
  expect(screen.queryByRole('button', { name: 'Retry reading roles' })).toBeNull()
  await act(async () => { resolve(reply) })
  expect(await screen.findByRole('heading', { name: 'Drawing roles' })).toBeTruthy()
  expect(screen.queryByText('private detail')).toBeNull()
  view.unmount()
  expect(client.readContext.mock.calls[2][0].signal.aborted).toBe(true)
})

it('paginates locally with bounded rows and keeps the exact first choice', async () => {
  const candidates = Array.from({ length: 203 }, (_, index) => ({ ...row, block: `Panel ${index}`, count: index + 1 }))
  const data = { ...reply.data.role_discovery, module: { first_choice: candidates[0], candidates, competing: true } }
  const client = { readContext: vi.fn().mockResolvedValue({ ok: true, data: { role_discovery: data } }) }
  render(<DrawingRoleProposal {...props} projectClient={client} />)
  expect(await screen.findByText('203 module sets')).toBeTruthy()
  expect(screen.getAllByRole('row')).toHaveLength(11)
  const next = screen.getByRole('button', { name: 'Next module sets' })
  next.focus()
  expect(next).toHaveFocus()
  fireEvent.click(next)
  expect(screen.getByText('Page 2 of 21')).toBeTruthy()
  expect(screen.getByRole('cell', { name: 'Panel 10' })).toBeTruthy()
  expect(screen.getByRole('cell', { name: '11' })).toBeTruthy()
  expect(screen.getByText(/Module first choice:/).textContent).toContain('Panel 0')
  fireEvent.click(screen.getByRole('button', { name: 'Previous module sets' }))
  expect(screen.getByText('Page 1 of 21')).toBeTruthy()
  expect(client.readContext).toHaveBeenCalledTimes(1)
})

it('retires settled labels immediately when the client changes with the same binding', async () => {
  const old = { readContext: vi.fn().mockResolvedValue(reply) }
  let resolve
  const next = { readContext: vi.fn(() => new Promise(done => { resolve = done })) }
  const view = render(<DrawingRoleProposal {...props} projectClient={old} />)
  await screen.findByRole('heading', { name: 'Drawing roles' })
  view.rerender(<DrawingRoleProposal {...props} projectClient={next} />)
  expect(screen.queryByText(/Module first choice:/)).toBeNull()
  expect(old.readContext.mock.calls[0][0].signal.aborted).toBe(true)
  await act(async () => {})
  await act(async () => { resolve({ ok: false }) })
  expect(await screen.findByText('Drawing roles could not be read.')).toBeTruthy()
})

it('hides settled labels when authority retires and aborts the old read', async () => {
  let live = true
  const isCurrent = () => live
  const client = { readContext: vi.fn().mockResolvedValue(reply) }
  const view = render(<DrawingRoleProposal {...props} isCurrent={isCurrent} projectClient={client} />)
  await screen.findByRole('heading', { name: 'Drawing roles' })
  live = false
  view.rerender(<DrawingRoleProposal {...props} isCurrent={isCurrent} projectClient={client} />)
  expect(screen.queryByText(/Module first choice:/)).toBeNull()
  expect(client.readContext.mock.calls[0][0].signal.aborted).toBe(true)
})

it('StrictMode aborts obsolete requests and ignores manually resolved old promises', async () => {
  const pending = []
  const old = { readContext: vi.fn(({ signal }) => new Promise(resolve => { pending.push({ signal, resolve }) })) }
  const next = { readContext: vi.fn().mockResolvedValue({ ok: false }) }
  const view = render(<StrictMode><DrawingRoleProposal {...props} projectClient={old} /></StrictMode>)
  await act(async () => {})
  expect(pending.length).toBeGreaterThan(0)
  view.rerender(<StrictMode><DrawingRoleProposal {...props} authorityKey="next" projectClient={next} /></StrictMode>)
  expect(pending.every(item => item.signal.aborted)).toBe(true)
  await act(async () => { pending.forEach(item => item.resolve(reply)) })
  expect(await screen.findByText('Drawing roles could not be read.')).toBeTruthy()
  expect(screen.queryByText(/Module first choice:/)).toBeNull()
})
