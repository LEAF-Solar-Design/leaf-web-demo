import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import X07RoleConfirmation from './X07RoleConfirmation.jsx'
afterEach(cleanup)
const row = block => ({ block, layer: 'MODULES', count: 3, insertion_bounds: [0, 0, 2, 2] })
const proposal = { module: { candidates: [row('Panel A'), row('Panel B')], first_choice: row('Panel A') } }

it('saves the chosen competitor exactly once', async () => {
  let finish
  const confirm = vi.fn(() => new Promise(done => { finish = done }))
  render(<X07RoleConfirmation proposal={proposal} onConfirm={confirm} />)
  fireEvent.click(screen.getByRole('radio', { name: /Panel B/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
  fireEvent.click(screen.getByRole('button', { name: 'Saving roles…' }))
  await act(async () => {})
  expect(confirm).toHaveBeenCalledTimes(1)
  expect(confirm.mock.calls[0][0].block).toBe('Panel B')
  await act(async () => { finish({ ok: true }) })
  expect(screen.getByText('Roles confirmed.')).toBeTruthy()
})

it('keeps the choice after a retryable failure', async () => {
  const confirm = vi.fn().mockRejectedValueOnce(new Error('Try again')).mockResolvedValueOnce({ ok: true })
  render(<X07RoleConfirmation proposal={proposal} onConfirm={confirm} />)
  fireEvent.click(screen.getByRole('radio', { name: /Panel B/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Try again')
  expect(screen.getByRole('radio', { name: /Panel B/ })).toBeChecked()
  fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
  expect(await screen.findByText('Roles confirmed.')).toBeTruthy()
})

it('cancels without confirming', () => {
  const confirm = vi.fn(), cancel = vi.fn()
  render(<X07RoleConfirmation proposal={proposal} onConfirm={confirm} onCancel={cancel} />)
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
  expect(cancel).toHaveBeenCalledTimes(1)
  expect(confirm).not.toHaveBeenCalled()
})

it('bounds candidate work during paging across ten thousand sets', async () => {
  let reads = 0
  const candidates = Array.from({ length: 10000 }, (_, index) => ({
    get block() { reads++; return `Panel ${index}` }, layer: 'MODULES', count: index + 1, insertion_bounds: [0, 0, 2, 2],
  }))
  const confirm = vi.fn().mockResolvedValue({ ok: true })
  const large = { module: { candidates, first_choice: candidates[9999] } }
  render(<X07RoleConfirmation proposal={large} onConfirm={confirm} />)
  expect(screen.getAllByRole('radio')).toHaveLength(10)
  const initialReads = reads
  fireEvent.click(screen.getByRole('button', { name: 'Previous choices' }))
  const pageReads = reads - initialReads
  expect(initialReads).toBeGreaterThan(10000)
  expect(pageReads).toBeLessThan(300)
  expect(screen.getAllByRole('radio')).toHaveLength(10)
  expect(screen.getByText(/Selected module set:/)).toHaveTextContent('Panel 9999')
  fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
  expect(await screen.findByText('Roles confirmed.')).toBeTruthy()
  expect(confirm).toHaveBeenCalledTimes(1)
  expect(confirm.mock.calls[0][0].block).toBe('Panel 9999')
  console.info(JSON.stringify({ synthetic_sets: 10000, initial_block_reads: initialReads, paging_block_reads: pageReads }))
}, 15000)
