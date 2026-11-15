import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import WidgetsView from './WidgetsView.jsx'

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
const response = body => ({ ok: true, status: 200, json: async () => body })

it.each([
  ['over-limit text', 'x'.repeat(513)],
  ['null text', null],
  ['object text', { value: 'not a string' }],
])('rejects loaded %s without replacing the draft, then recovers on explicit Load', async (_, invalidText) => {
  let saved
  const recovered = 'Recovered \u{1F600}e\u0301'
  const fetch = vi.fn()
    .mockImplementationOnce(async (url, options) => {
      saved = JSON.parse(options.body).graph
      return response({ revision: 1 })
    })
    .mockImplementationOnce(async () => {
      const graph = structuredClone(saved)
      graph.formats.la.els[0].text = invalidText
      return response({ graph, kind: 'widget', revision: 2 })
    })
    .mockImplementationOnce(async () => {
      const graph = structuredClone(saved)
      graph.formats.la.els[0].text = recovered
      return response({ graph, kind: 'widget', revision: 2 })
    })
  vi.stubGlobal('fetch', fetch)
  render(<WidgetsView />)
  fireEvent.change(screen.getByLabelText('Document ID'), { target: { value: 'invalid-text-proof' } })
  fireEvent.click(screen.getByRole('button', { name: 'New', exact: true }))
  fireEvent.click(screen.getByRole('button', { name: 'Add text', exact: true }))
  fireEvent.click(screen.getByRole('button', { name: 'Save', exact: true }))
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved revision 1.'))
  fireEvent.change(screen.getByLabelText('Text'), { target: { value: 'Keep local draft' } })
  fireEvent.click(screen.getByRole('button', { name: 'Load', exact: true }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Invalid text. (/graph)')
  expect(screen.getByRole('status')).toHaveTextContent('Invalid widget.')
  expect(screen.getByLabelText('Text')).toHaveValue('Keep local draft')
  expect(screen.getByRole('button', { name: 'Save', exact: true })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Download widget definition' })).toBeDisabled()
  expect(fetch).toHaveBeenCalledTimes(2)
  fireEvent.click(screen.getByRole('button', { name: 'Load', exact: true }))
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved revision 2.'))
  fireEvent.click(screen.getByRole('button', { name: `Text 1: ${recovered}`, exact: true }))
  expect(screen.getByLabelText('Text')).toHaveValue(recovered)
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Download widget definition' })).toBeEnabled()
  expect(fetch).toHaveBeenCalledTimes(3)
})
