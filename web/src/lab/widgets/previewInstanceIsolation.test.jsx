import { act, cleanup, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import WidgetPreview from './WidgetPreview.jsx'
import { createWidgetDoc } from './useWidgetEditor.js'
import { createTextElement } from './widgetDoc.js'

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers() })
const result = value => ({ values: [{ status: 'resolved', value }] })
function docFor(id) {
  const doc = createWidgetDoc()
  doc.formats.la.els = [{ ...createTextElement('value'), min_renderer: '1.0.0',
    binding: { source: 'graph.output', doc_id: id, output: 'total', min_renderer: '1.0.0', scale: 'identity', tone: 'ink' } }]
  return doc
}

it.each(['success', 'failure'])('retiring one preview with late %s leaves its mounted peer independent', async outcome => {
  vi.useFakeTimers(); vi.setSystemTime(100000)
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
  let resolveOld, rejectOld
  const old = new Promise((resolve, reject) => { resolveOld = resolve; rejectOld = reject })
  const client = { resolveValues: vi.fn((id) => id === 'widget-a' ? old : Promise.resolve(result('Peer B'))) }
  const a = render(<WidgetPreview docId="widget-a" doc={docFor('graph-a')} client={client} />)
  const b = render(<WidgetPreview docId="widget-b" doc={docFor('graph-b')} client={client} />)
  await act(async () => {})
  const value = () => b.container.querySelector('[data-element-id="value"]')
  expect(value()).toHaveTextContent('Peer B')
  expect(vi.getTimerCount()).toBe(2)
  const aSignal = client.resolveValues.mock.calls[0][2].signal
  const bSignal = client.resolveValues.mock.calls[1][2].signal
  a.unmount()
  expect(aSignal.aborted).toBe(true)
  expect(bSignal.aborted).toBe(false)
  expect(vi.getTimerCount()).toBe(1)
  await act(async () => {
    if (outcome === 'success') resolveOld(result('Retired A'))
    else rejectOld(new Error('Retired A failure'))
  })
  expect(value()).toHaveTextContent('Peer B')
  expect(value()).toHaveAttribute('data-freshness', 'live')
  expect(b.container.querySelector('[role="alert"]')).toBeNull()
  await act(async () => { await vi.advanceTimersByTimeAsync(4999) })
  expect(client.resolveValues).toHaveBeenCalledTimes(2)
  await act(async () => { await vi.advanceTimersByTimeAsync(1) })
  expect(client.resolveValues).toHaveBeenCalledTimes(3)
  expect(client.resolveValues.mock.calls[2][0]).toBe('widget-b')
  expect(client.resolveValues.mock.calls[2][1]).toEqual([docFor('graph-b').formats.la.els[0].binding])
  expect(value()).toHaveTextContent('Peer B')
  expect(vi.getTimerCount()).toBe(1)
  b.unmount()
  expect(vi.getTimerCount()).toBe(0)
  await act(async () => { await vi.advanceTimersByTimeAsync(20000) })
  expect(client.resolveValues).toHaveBeenCalledTimes(3)
})
