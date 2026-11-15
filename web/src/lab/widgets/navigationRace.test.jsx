import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import WidgetsView from './WidgetsView.jsx'
import { createWidgetDoc } from './useWidgetEditor.js'
import { widgetProjectKey } from './widgetsClient.js'

const response = (body, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => body })
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it.each(['success', 'error'])('ignores an old project load %s while the new project has an unsaved draft', async outcome => {
  let finish
  const pending = new Promise(resolve => { finish = resolve })
  const fetch = vi.fn().mockImplementationOnce(() => pending).mockResolvedValueOnce(response({ revision: 1 }))
  vi.stubGlobal('fetch', fetch)
  const account = 'synthetic-navigation-race', a = `race-a-${outcome}`, b = `race-b-${outcome}`
  const view = render(<WidgetsView projectId={a} accountScope={account} />)
  fireEvent.change(screen.getByLabelText('Document ID'), { target: { value: 'old-project-widget' } })
  fireEvent.click(screen.getByRole('button', { name: 'Load', exact: true }))
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
  // Editing the document ID during a pending request is not a reachable UI path.
  expect(screen.getByLabelText('Document ID')).toBeDisabled()
  expect(screen.getByRole('button', { name: 'New', exact: true })).toBeDisabled()
  view.rerender(<WidgetsView projectId={b} accountScope={account} />)
  fireEvent.change(screen.getByLabelText('Document ID'), { target: { value: 'new-project-widget' } })
  fireEvent.click(screen.getByRole('button', { name: 'New', exact: true }))
  fireEvent.click(screen.getByRole('button', { name: 'Add text', exact: true }))
  fireEvent.change(screen.getByLabelText('Text'), { target: { value: 'Keep the new project draft' } })
  await act(async () => finish(outcome === 'success'
    ? response({ graph: createWidgetDoc(), kind: 'widget', revision: 7 })
    : response({ detail: 'old project refusal' }, 503)))
  expect(screen.getByLabelText('Document ID')).toHaveValue('new-project-widget')
  expect(screen.getByLabelText('Text')).toHaveValue('Keep the new project draft')
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect(fetch).toHaveBeenCalledTimes(1)
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Save', exact: true })))
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved revision 1.'))
  expect(JSON.parse(fetch.mock.calls[1][1].body).expected_head).toBeNull()
  expect(screen.getByLabelText('Text')).toHaveValue('Keep the new project draft')
  expect(localStorage.getItem(widgetProjectKey(a, account))).toBeNull()
  expect(localStorage.getItem(widgetProjectKey(b, account))).toBe('new-project-widget')
  expect(fetch).toHaveBeenCalledTimes(2)
})
