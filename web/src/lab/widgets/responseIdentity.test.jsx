import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import WidgetsView from './WidgetsView.jsx'
import { loadWidget, saveWidget } from './widgetsClient.js'
import { createWidgetDoc } from './useWidgetEditor.js'
import { createTextElement } from './widgetDoc.js'

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
const response = body => ({ ok: true, status: 200, json: async () => body })
const detail = 'The document store returned a mismatched document identity.'
const body = method => ({ revision: 2, ...(method === 'load' ? { graph: createWidgetDoc(), kind: 'widget' } : {}) })
const call = (method, value) => {
  const options = { fetch: vi.fn(async () => response(value)) }
  return method === 'load' ? loadWidget('identity-proof', options) : saveWidget('identity-proof', createWidgetDoc(), 1, options)
}

it.each(['load', 'save'])('rejects an explicit wrong document identity in a successful %s reply', async method => {
  expect(await call(method, { ...body(method), doc_id: 'another-document' })).toMatchObject({ ok: false, status: 502, detail })
})

it.each([['load', false], ['load', true], ['save', false], ['save', true]])('preserves %s compatibility when identity is matching or omitted (%s)', async (method, included) => {
  expect(await call(method, { ...body(method), ...(included ? { doc_id: 'identity-proof' } : {}) })).toMatchObject({ ok: true, revision: 2 })
})

it.each(['Load', 'Save'])('keeps local draft and prior head after %s returns another artifact identity', async operation => {
  const foreign = createWidgetDoc()
  foreign.formats.la.els = [{ ...createTextElement('text1'), min_renderer: '1.0.0', preset: 'text', text: 'Other document data' }]
  const fetch = vi.fn().mockResolvedValueOnce(response({ revision: 1, doc_id: 'identity-proof' }))
    .mockResolvedValueOnce(response({ revision: 9, doc_id: 'another-document', graph: foreign, kind: 'widget' }))
    .mockResolvedValueOnce(response({ revision: 2, doc_id: 'identity-proof' }))
  vi.stubGlobal('fetch', fetch)
  render(<WidgetsView />)
  fireEvent.change(screen.getByLabelText('Document ID'), { target: { value: 'identity-proof' } })
  const click = name => fireEvent.click(screen.getByRole('button', { name, exact: true }))
  click('New'); click('Add text'); click('Save')
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved revision 1.'))
  fireEvent.change(screen.getByLabelText('Text'), { target: { value: 'Keep local draft' } })
  click(operation)
  expect(await screen.findByRole('alert')).toHaveTextContent(detail)
  expect(screen.getByLabelText('Text')).toHaveValue('Keep local draft')
  expect(screen.getByRole('status')).toHaveTextContent('request failed')
  expect(fetch).toHaveBeenCalledTimes(2)
  click('Save')
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved revision 2.'))
  const sent = JSON.parse(fetch.mock.calls[2][1].body)
  expect(sent.expected_head).toBe(1)
  expect(sent.graph.formats.la.els[0].text).toBe('Keep local draft')
  expect(fetch).toHaveBeenCalledTimes(3)
})
