import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import X07CopyPicker from './X07CopyPicker.jsx'
afterEach(cleanup)
const copies = [0, 10].map(x => ({ block: 'Panel', layer: 'MODULES', design_window: [x, 0, x + 2, 2], oracle_count: 3 }))
it('picks the exact window once and displays independent counts', async () => {
  const pick = vi.fn().mockResolvedValue(undefined)
  render(<X07CopyPicker copies={copies} onPick={pick} />)
  expect(screen.getAllByRole('cell', { name: '3' })).toHaveLength(2)
  fireEvent.click(screen.getByRole('button', { name: 'Use copy 2' }))
  expect(await screen.findByText('Design copy selected.')).toBeTruthy()
  expect(pick).toHaveBeenCalledTimes(1)
  expect(pick.mock.calls[0][0].design_window).toEqual([10, 0, 12, 2])
})
it('refuses empty or invalid choices and supports cancellation', () => {
  const cancel = vi.fn()
  const view = render(<X07CopyPicker copies={[]} onCancel={cancel} />)
  expect(screen.getByText('No design copies are available.')).toBeTruthy()
  view.rerender(<X07CopyPicker copies={[{ ...copies[0], design_window: [0, 0, Infinity, 2] }]} onCancel={cancel} />)
  expect(screen.getByRole('alert')).toBeTruthy()
  expect(screen.queryByRole('button', { name: /Use copy/ })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
  expect(cancel).toHaveBeenCalledTimes(1)
})
