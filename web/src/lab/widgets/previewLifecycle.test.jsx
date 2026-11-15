import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import WidgetPreview from './WidgetPreview.jsx'
import { createWidgetDoc } from './useWidgetEditor.js'
import { createTextElement } from './widgetDoc.js'

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers() })

it('retired mounts release observers and cannot paint or schedule work in a fresh preview', async () => {
  vi.useFakeTimers()
  vi.setSystemTime(100000)
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
  const added = vi.spyOn(document, 'addEventListener'), removed = vi.spyOn(document, 'removeEventListener')
  const pending = []
  const client = { resolveValues: vi.fn(() => new Promise(resolve => { pending.push(resolve) })) }
  const doc = createWidgetDoc()
  doc.formats.la.els = [{ ...createTextElement('total'), text: 'Awaiting fresh value',
    min_renderer: '1.0.0', binding: { source: 'graph.output', doc_id: 'graph', output: 'total',
      min_renderer: '1.0.0', scale: 'identity', tone: 'ink' } }]
  for (let index = 0; index < 3; index++) {
    const view = render(<WidgetPreview docId={`retired-${index}`} doc={doc} client={client} />)
    expect(client.resolveValues).toHaveBeenCalledTimes(index + 1)
    expect(client.resolveValues.mock.calls[index][2].signal.aborted).toBe(false)
    view.unmount()
    expect(client.resolveValues.mock.calls[index][2].signal.aborted).toBe(true)
    expect(vi.getTimerCount()).toBe(0)
  }
  const fresh = render(<WidgetPreview docId="fresh" doc={doc} client={client} />)
  expect(client.resolveValues).toHaveBeenCalledTimes(4)
  await act(async () => {
    pending.slice(0, 3).forEach((resolve, index) => resolve({ values: [{ status: 'resolved', value: `RETIRED ${index}` }] }))
  })
  const element = screen.getByRole('region', { name: 'Live widget preview' }).querySelector('[data-element-id="total"]')
  expect(element).not.toHaveTextContent('RETIRED')
  expect(element).not.toHaveAttribute('data-freshness', 'live')
  await act(async () => pending[3]({ values: [{ status: 'resolved', value: 'Fresh result' }] }))
  expect(element).toHaveTextContent('Fresh result')
  expect(element).toHaveAttribute('data-freshness', 'live')
  fresh.unmount()
  expect(client.resolveValues.mock.calls[3][2].signal.aborted).toBe(true)
  expect(vi.getTimerCount()).toBe(0)
  const registrations = added.mock.calls.filter(([name]) => name === 'visibilitychange')
  const removals = removed.mock.calls.filter(([name]) => name === 'visibilitychange')
  expect(registrations).toHaveLength(4)
  expect(removals).toHaveLength(4)
  for (const [, listener] of registrations) expect(removals.some(([, removedListener]) => removedListener === listener)).toBe(true)
  await act(async () => { await vi.advanceTimersByTimeAsync(20000) })
  expect(client.resolveValues).toHaveBeenCalledTimes(4)
  expect(vi.getTimerCount()).toBe(0)
})
