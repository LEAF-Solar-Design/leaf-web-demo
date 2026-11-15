import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import WidgetsView from './WidgetsView.jsx'

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
const response = (body, status = 200) => ({ ok: status === 200, status, json: async () => body })

it.each(['success', 'failure'])('sends one pending Save and restores explicit controls after %s', async outcome => {
  let finish
  const fetch = vi.fn().mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    .mockResolvedValue(response({ revision: outcome === 'success' ? 2 : 1 }))
  vi.stubGlobal('fetch', fetch)
  render(<WidgetsView />)
  fireEvent.change(screen.getByLabelText('Document ID'), { target: { value: 'save-reentry' } })
  fireEvent.click(screen.getByRole('button', { name: 'New', exact: true }))
  fireEvent.click(screen.getByRole('button', { name: 'Add text', exact: true }))
  fireEvent.change(screen.getByLabelText('Text'), { target: { value: 'Keep this draft' } })
  const save = screen.getByRole('button', { name: 'Save', exact: true })
  fireEvent.click(save)
  expect(screen.getByRole('status')).toHaveTextContent('Saving')
  expect(save).toBeDisabled()
  fireEvent.click(save); fireEvent.click(save)
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(screen.getByLabelText('Text')).toHaveValue('Keep this draft')
  await act(async () => { finish(outcome === 'success' ? response({ revision: 1 }) : response({ detail: 'Temporary store error' }, 503)) })
  await waitFor(() => expect(save).toBeEnabled())
  if (outcome === 'success') expect(screen.getByRole('status')).toHaveTextContent('Saved revision 1.')
  else expect(screen.getByRole('alert')).toHaveTextContent('Temporary store error')
  expect(screen.getByLabelText('Text')).toHaveValue('Keep this draft')
  expect(fetch).toHaveBeenCalledTimes(1)
  fireEvent.change(screen.getByLabelText('Text'), { target: { value: 'Explicit next draft' } })
  fireEvent.click(save)
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(`Saved revision ${outcome === 'success' ? 2 : 1}.`))
  expect(fetch).toHaveBeenCalledTimes(2)
  const next = JSON.parse(fetch.mock.calls[1][1].body)
  expect(next.expected_head).toBe(outcome === 'success' ? 1 : null)
  expect(next.graph.formats.la.els[0].text).toBe('Explicit next draft')
})
