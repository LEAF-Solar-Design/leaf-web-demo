import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import X07CustomerProfileEditor from './X07CustomerProfileEditor.jsx'
afterEach(cleanup)
const record = (customer, version = 1) => ({ customer_key: customer, profile_id: 'profile-' + customer,
  version, roles: { module: { block: 'Panel', layer: 'MODULES' } } })
it('keeps edits on a stale save and displays the older project pin', async () => {
  const load = vi.fn().mockResolvedValueOnce(record('a')).mockResolvedValueOnce(record('a', 2))
  const save = vi.fn().mockRejectedValueOnce({ code: 'STALE_VERSION' })
    .mockImplementationOnce(async (_customer, roles) => ({ ...record('a', 3), roles }))
  render(<X07CustomerProfileEditor customerKey="a" loadProfile={load} saveProfile={save} applicationRecord={record('a')} />)
  fireEvent.change(await screen.findByLabelText('Module layer'), { target: { value: 'EDITED' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
  await screen.findByRole('alert')
  expect(screen.getByLabelText('Module layer')).toHaveValue('EDITED')
  fireEvent.click(screen.getByRole('button', { name: 'Reload version, keep edits' }))
  await screen.findByText('Profile version: 2')
  fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
  await screen.findByText('Profile saved.')
  expect(save.mock.calls.map(call => call[2].expected_version)).toEqual([1, 2])
  expect(screen.getByText('Project is pinned to version 1.')).toBeTruthy()
})
it('clears the previous customer and never displays its pinned record', async () => {
  const view = render(<X07CustomerProfileEditor customerKey="a" loadProfile={async () => record('a')} />)
  await screen.findByText('Profile id: profile-a')
  view.rerender(<X07CustomerProfileEditor customerKey="b" loadProfile={async () => record('b')} applicationRecord={record('a')} />)
  await screen.findByText('Profile id: profile-b')
  expect(screen.queryByText('Profile id: profile-a')).toBeNull()
  expect(screen.queryByText(/Project is pinned/)).toBeNull()
})
