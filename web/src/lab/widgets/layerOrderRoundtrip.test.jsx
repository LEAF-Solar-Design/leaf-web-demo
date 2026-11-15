import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import WidgetsView from './WidgetsView.jsx'
import { createWidgetDoc } from './useWidgetEditor.js'
import { createTextElement } from './widgetDoc.js'

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
const response = graph => ({ ok: true, status: 200, json: async () => graph })

it('roundtrips overlapping layer order through history, Save, Reload and the actual Blob', async () => {
  const initial = createWidgetDoc()
  initial.formats.la.els = ['a', 'b'].map(id => ({ ...createTextElement(id), min_renderer: '1.0.0',
    preset: 'text', text: `Layer ${id}`, x: 14, y: 14, w: 120, h: 24 }))
  let saved
  const fetch = vi.fn().mockResolvedValueOnce(response({ graph: initial, kind: 'widget', revision: 1 }))
    .mockImplementationOnce(async (_, options) => {
      const body = JSON.parse(options.body)
      expect(body.expected_head).toBe(1)
      saved = body.graph
      return response({ revision: 2 })
    }).mockImplementationOnce(async () => response({ graph: saved, kind: 'widget', revision: 2 }))
  vi.stubGlobal('fetch', fetch)
  const createObjectURL = vi.fn(() => 'blob:layer-proof')
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL = createObjectURL
    static revokeObjectURL = vi.fn()
  })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  const { container } = render(<WidgetsView />)
  fireEvent.change(screen.getByLabelText('Document ID'), { target: { value: 'layer-proof' } })
  const click = name => fireEvent.click(screen.getByRole('button', { name, exact: true }))
  const layers = () => [...container.querySelectorAll('[data-layer-id]')].map(el => el.dataset.layerId)
  const paintOrder = () => [...container.querySelectorAll('.widget-interaction [data-element-id]')].map(el => el.dataset.elementId)
  click('Load')
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved revision 1.'))
  expect(layers()).toEqual(['a', 'b'])
  click('Move b earlier')
  expect(layers()).toEqual(['b', 'a'])
  expect(paintOrder()).toEqual(['b', 'a'])
  click('Undo'); expect(layers()).toEqual(['a', 'b'])
  click('Redo'); expect(layers()).toEqual(['b', 'a'])
  click('Save')
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved revision 2.'))
  expect(saved.formats.la.els).toEqual([initial.formats.la.els[1], initial.formats.la.els[0]])
  click('Move b later'); expect(layers()).toEqual(['a', 'b'])
  click('Load')
  await waitFor(() => expect(layers()).toEqual(['b', 'a']))
  expect(paintOrder()).toEqual(['b', 'a'])
  expect(screen.getByRole('button', { name: 'Undo', exact: true })).toBeDisabled()
  click('Download widget definition')
  expect(createObjectURL).toHaveBeenCalledTimes(1)
  const exported = await new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(JSON.parse(reader.result))
    reader.onerror = () => reject(reader.error)
    reader.readAsText(createObjectURL.mock.calls[0][0])
  })
  expect(exported).toEqual(saved)
  expect(fetch).toHaveBeenCalledTimes(3)
})
