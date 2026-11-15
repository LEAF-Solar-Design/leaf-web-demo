import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import WidgetsView from './WidgetsView.jsx'
import { claimLegacyWidget, projectWidgetId } from './widgetsClient.js'

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
const click = name => fireEvent.click(screen.getByRole('button', { name, exact: true }))

it.each(['SecurityError', 'QuotaExceededError'])('saves and manually reopens project data when optional storage throws %s', async kind => {
  const storage = {
    getItem: vi.fn(() => { if (kind === 'SecurityError') throw new DOMException('denied', kind); return null }),
    setItem: vi.fn(() => { throw new DOMException('not stored', kind) }),
  }
  vi.stubGlobal('localStorage', storage)
  const artifact = await projectWidgetId('storage-project', 'storage-proof')
  let stored
  const fetch = vi.fn(async (url, options) => {
    expect(url).toBe(`/api/lab/docs/${artifact}/${options.method === 'POST' ? 'save' : 'head'}`)
    if (options.method === 'POST') {
      const body = JSON.parse(options.body)
      expect(body.expected_head).toBeNull(); expect(body.kind).toBe('widget')
      stored = body.graph
    }
    return { ok: true, status: 200, json: async () => ({ graph: stored, kind: 'widget', revision: 1 }) }
  })
  vi.stubGlobal('fetch', fetch)
  const view = render(<WidgetsView projectId="storage-project" accountScope="synthetic-account" />)
  expect(fetch).not.toHaveBeenCalled()
  fireEvent.change(screen.getByLabelText('Document ID'), { target: { value: 'storage-proof' } })
  click('New'); click('Add text')
  fireEvent.change(screen.getByLabelText('Text'), { target: { value: 'Persist without local storage' } })
  click('Save')
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved revision 1.'))
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect(stored.formats.la.els[0].text).toBe('Persist without local storage')
  view.unmount()
  render(<WidgetsView projectId="storage-project" accountScope="synthetic-account" />)
  expect(screen.getByLabelText('Document ID')).toHaveValue('')
  expect(fetch).toHaveBeenCalledTimes(1)
  fireEvent.change(screen.getByLabelText('Document ID'), { target: { value: 'storage-proof' } })
  click('Load')
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved revision 1.'))
  fireEvent.click(document.querySelector('button[data-layer-id]'))
  expect(screen.getByLabelText('Text')).toHaveValue('Persist without local storage')
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect(storage.setItem).toHaveBeenCalledTimes(2)
  expect(fetch).toHaveBeenCalledTimes(2)
  expect(claimLegacyWidget('storage-project', 'legacy-proof', 'synthetic-account')).toBe(false)
  expect(fetch).toHaveBeenCalledTimes(2)
})
