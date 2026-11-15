import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import WidgetsView from './WidgetsView.jsx'
import { createWidgetDoc } from './useWidgetEditor.js'
import { createTextElement } from './widgetDoc.js'

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
function documentWith(text, x) {
  const doc = createWidgetDoc()
  doc.formats.la.els = [{ ...createTextElement('text1'), preset: 'text', min_renderer: '1.0.0', text, x }]
  return doc
}
const click = name => fireEvent.click(screen.getByRole('button', { name, exact: true }))

it('cannot restore a prior document through Undo/Redo after loading and exporting another identity', async () => {
  const first = documentWith('A original', 20), second = documentWith('B only \u{1F600}e\u0301', 40)
  const fetch = vi.fn(async url => {
    const id = url.match(/\/docs\/(document-[ab])\/head$/)?.[1]
    expect(id).toBeTruthy()
    return { ok: true, status: 200, json: async () => ({ graph: id === 'document-a' ? first : second, kind: 'widget', revision: id === 'document-a' ? 1 : 2 }) }
  })
  vi.stubGlobal('fetch', fetch)
  let blob, filename
  const revoke = vi.fn()
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL(value) { blob = value; return 'blob:history-isolation' }
    static revokeObjectURL = revoke
  })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function () { filename = this.download })
  render(<WidgetsView />)
  fireEvent.change(screen.getByLabelText('Document ID'), { target: { value: 'document-a' } })
  click('Load')
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved revision 1.'))
  fireEvent.click(document.querySelector('button[data-layer-id]'))
  fireEvent.change(screen.getByLabelText('Text'), { target: { value: 'A unsaved draft' } })
  fireEvent.change(screen.getByLabelText('X'), { target: { value: '23' } })
  fireEvent.blur(screen.getByLabelText('X'))
  expect(screen.getByRole('button', { name: 'Undo', exact: true })).toBeEnabled()
  fireEvent.change(screen.getByLabelText('Document ID'), { target: { value: 'document-b' } })
  click('Load')
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved revision 2.'))
  expect(screen.getByRole('button', { name: 'Undo', exact: true })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Redo', exact: true })).toBeDisabled()
  fireEvent.keyDown(document.body, { key: 'z', ctrlKey: true })
  fireEvent.keyDown(document.body, { key: 'z', ctrlKey: true, shiftKey: true })
  fireEvent.click(document.querySelector('button[data-layer-id]'))
  expect(screen.getByLabelText('Text')).toHaveValue(second.formats.la.els[0].text)
  expect(screen.getByLabelText('X')).toHaveValue(40)
  expect(screen.getByRole('status')).toHaveTextContent('Saved revision 2.')
  click('Download widget definition')
  const actual = await new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(JSON.parse(reader.result))
    reader.onerror = () => reject(reader.error)
    reader.readAsText(blob)
  })
  expect(filename).toBe('widget-document-b.json')
  expect(actual).toEqual(second)
  expect(JSON.stringify(actual)).not.toContain('A unsaved draft')
  expect(fetch).toHaveBeenCalledTimes(2)
  await waitFor(() => expect(revoke).toHaveBeenCalledExactlyOnceWith('blob:history-isolation'))
})
