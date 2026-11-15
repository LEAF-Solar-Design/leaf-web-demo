import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import WidgetsView from './WidgetsView.jsx'

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it('preserves full Unicode through Undo/Redo after Save without rewinding the stored head', async () => {
  let revision = 0
  const writes = []
  vi.stubGlobal('fetch', vi.fn(async (url, options) => {
    writes.push(JSON.parse(options.body))
    return { ok: true, status: 200, json: async () => ({ revision: ++revision }) }
  }))
  render(<WidgetsView />)
  fireEvent.change(screen.getByLabelText('Document ID'), { target: { value: 'unicode-history' } })
  fireEvent.click(screen.getByRole('button', { name: 'New', exact: true }))
  fireEvent.click(screen.getByRole('button', { name: 'Add text', exact: true }))
  const original = screen.getByLabelText('Text').value
  fireEvent.click(screen.getByRole('button', { name: 'Save', exact: true }))
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved revision 1.'))
  fireEvent.click(screen.getByRole('button', { name: 'Edit full text', exact: true }))
  const text = '\u03b1\u{1F600}e\u0301\n'.repeat(12)
  fireEvent.change(screen.getByLabelText('Full text'), { target: { value: text } })
  fireEvent.click(screen.getByRole('button', { name: 'Save', exact: true }))
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved revision 2.'))
  fireEvent.click(screen.getByRole('button', { name: 'Undo', exact: true }))
  fireEvent.click(document.querySelector('button[data-layer-id]'))
  expect(screen.getByRole('textbox', { name: /^(Full text|Text)$/ })).toHaveValue(original)
  expect(screen.getByRole('status')).toHaveTextContent('Local edits.')
  fireEvent.click(screen.getByRole('button', { name: 'Redo', exact: true }))
  fireEvent.click(document.querySelector('button[data-layer-id]'))
  expect(screen.getByLabelText('Full text')).toHaveValue(text)
  expect(screen.getByRole('img', { name: 'Widget preview', exact: true }).querySelector('text').textContent).toBe(text)
  fireEvent.click(screen.getByRole('button', { name: 'Save', exact: true }))
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved revision 3.'))
  expect(writes.map(write => write.expected_head)).toEqual([null, 1, 2])
  expect(writes[1].graph.formats.la.els[0].text).toBe(text)
  expect(writes[2].graph).toEqual(writes[1].graph)
})
