import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import WidgetPreview from './WidgetPreview.jsx'
import { createWidgetDoc } from './useWidgetEditor.js'
import { createTextElement } from './widgetDoc.js'

beforeEach(() => {
  vi.useFakeTimers(); vi.setSystemTime(100000)
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers() })
const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const result = value => ({ values: [{ status: 'resolved', value }] })
function docFor(docId) {
  const doc = createWidgetDoc()
  doc.formats.la.els = [{ ...createTextElement('value'), min_renderer: '1.0.0',
    binding: { source: 'graph.output', doc_id: docId, output: 'total', min_renderer: '1.0.0', scale: 'identity', tone: 'ink' } }]
  return doc
}
const value = () => screen.getByRole('region', { name: 'Live widget preview' }).querySelector('[data-element-id="value"]')

it.each(['success', 'failure'])('ignores prior binding late %s within one mounted preview', async outcome => {
  const old = deferred(), current = deferred()
  const client = { resolveValues: vi.fn().mockReturnValueOnce(old.promise).mockReturnValueOnce(current.promise).mockResolvedValue(result('Next B')) }
  const a = docFor('graph-a'), b = docFor('graph-b')
  const { rerender, unmount } = render(<WidgetPreview docId="widget" doc={a} client={client} />)
  expect(client.resolveValues).toHaveBeenCalledTimes(1)
  const oldSignal = client.resolveValues.mock.calls[0][2].signal
  rerender(<WidgetPreview docId="widget" doc={b} client={client} />)
  expect(oldSignal.aborted).toBe(true)
  expect(client.resolveValues.mock.calls[1][1]).toEqual([b.formats.la.els[0].binding])
  await act(async () => { current.resolve(result('Current B')) })
  expect(value()).toHaveTextContent('Current B')
  expect(value()).toHaveAttribute('data-freshness', 'live')
  expect(vi.getTimerCount()).toBe(1)
  await act(async () => {
    if (outcome === 'success') old.resolve(result('Obsolete A'))
    else old.reject(new Error('Obsolete A failure'))
  })
  expect(value()).toHaveTextContent('Current B')
  expect(value()).not.toHaveTextContent('Obsolete A')
  expect(value()).toHaveAttribute('data-freshness', 'live')
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect(vi.getTimerCount()).toBe(1)
  await act(async () => { vi.advanceTimersByTime(4999) })
  expect(client.resolveValues).toHaveBeenCalledTimes(2)
  await act(async () => { vi.advanceTimersByTime(1) })
  expect(client.resolveValues).toHaveBeenCalledTimes(3)
  expect(client.resolveValues.mock.calls[2][1]).toEqual([b.formats.la.els[0].binding])
  expect(value()).toHaveTextContent('Next B')
  unmount()
  expect(vi.getTimerCount()).toBe(0)
})
