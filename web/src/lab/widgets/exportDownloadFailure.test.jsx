import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import WidgetsView from './WidgetsView.jsx'

const errors = []
function observe(error) { errors.push(error.error?.message ?? error.message); error.preventDefault() }
afterEach(async () => { window.removeEventListener('error', observe); errors.length = 0; cleanup(); await new Promise(resolve => setTimeout(resolve, 0)); vi.restoreAllMocks(); vi.unstubAllGlobals() })
const click = name => fireEvent.click(screen.getByRole('button', { name, exact: true }))

it.each(['url', 'trigger'])('retains draft/head and explicitly recovers after download %s failure', async fault => {
  window.addEventListener('error', observe)
  const fetch = vi.fn().mockResolvedValueOnce({ ok: true, json: async () => ({ revision: 1 }) })
    .mockResolvedValueOnce({ ok: true, json: async () => ({ revision: 2 }) })
  vi.stubGlobal('fetch', fetch)
  let sequence = 0, filename
  const created = [], revoke = vi.fn()
  const create = vi.fn(value => { const url = `blob:export-${++sequence}`; created.push(url); return url })
  if (fault === 'url') create.mockImplementationOnce(() => { throw new Error('Synthetic URL failure') })
  vi.stubGlobal('URL', class extends URL { static createObjectURL = create; static revokeObjectURL = revoke })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementationOnce(function () {
    if (fault === 'trigger') throw new Error('Synthetic download failure')
    filename = this.download
  }).mockImplementation(function () { filename = this.download })
  render(<WidgetsView />)
  fireEvent.change(screen.getByLabelText('Document ID'), { target: { value: 'export-proof' } })
  click('New'); click('Add text'); click('Save')
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved revision 1.'))
  fireEvent.change(screen.getByLabelText('Text'), { target: { value: 'Local draft retained' } })
  click('Download widget definition')
  expect(errors).toEqual([])
  expect(await screen.findByRole('alert')).toHaveTextContent('Unable to download the widget definition. Your edits are kept.')
  expect(screen.getByRole('status')).toHaveTextContent('Local edits.')
  expect(screen.getByLabelText('Text')).toHaveValue('Local draft retained')
  expect(screen.getByRole('button', { name: 'Download widget definition' })).toBeEnabled()
  expect(document.querySelector('a[download]')).toBeNull()
  await waitFor(() => expect(revoke).toHaveBeenCalledTimes(created.length))
  expect(fetch).toHaveBeenCalledTimes(1)
  click('Download widget definition')
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect(filename).toBe('widget-export-proof.json')
  const actual = await new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(JSON.parse(reader.result))
    reader.onerror = () => reject(reader.error)
    reader.readAsText(create.mock.calls.at(-1)[0])
  })
  expect(actual.formats.la.els[0].text).toBe('Local draft retained')
  expect(document.querySelector('a[download]')).toBeNull()
  await waitFor(() => expect(revoke.mock.calls.map(call => call[0])).toEqual(created))
  expect(fetch).toHaveBeenCalledTimes(1)
  click('Save')
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved revision 2.'))
  expect(JSON.parse(fetch.mock.calls[1][1].body).expected_head).toBe(1)
  expect(errors).toEqual([])
})
