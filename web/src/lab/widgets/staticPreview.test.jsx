import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import WidgetPreview from './WidgetPreview.jsx'
import { createWidgetDoc } from './useWidgetEditor.js'
import { createTextElement } from './widgetDoc.js'
import { createHostProvider } from '../host/hostProvider.js'

function documentWith(binding) {
  const doc = createWidgetDoc()
  doc.formats.la.els = [{ ...createTextElement('summary'), min_renderer: '1.0.0', text: 'Static sample', ...(binding ? { binding } : {}) }]
  return doc
}
const binding = (source, values) => ({ source, min_renderer: '1.0.0', scale: 'identity', tone: 'ink', ...values })
const literal = documentWith()
beforeEach(() => {
  vi.useFakeTimers(); vi.setSystemTime(100000)
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers() })

it('keeps a literal preview idle for thirty seconds without clocks or requests', async () => {
  const now = vi.fn(() => Date.now()), client = { resolveValues: vi.fn() }
  render(<WidgetPreview docId="summary" doc={literal} now={now} client={client} />)
  const initialTimers = vi.getTimerCount()
  await act(async () => { await vi.advanceTimersByTimeAsync(30000) })
  console.log(JSON.stringify({ literalPreview: { seconds: 30, initialTimers, clockReads: now.mock.calls.length, remainingTimers: vi.getTimerCount() } }))
  expect(initialTimers).toBe(0)
  expect(vi.getTimerCount()).toBe(0)
  expect(now).toHaveBeenCalledTimes(1)
  expect(client.resolveValues).not.toHaveBeenCalled()
  expect(screen.getByText('Static sample', { exact: true })).toBeInTheDocument()
})

it('starts host freshness on a literal-to-host change and stops after returning to literals', async () => {
  const host = createHostProvider({ framed: true })
  host.announce([{ source: 'fixture', data: { openCount: '3' }, as_of: Date.now() }])
  const view = render(<WidgetPreview docId="summary" doc={literal} host={host} />)
  view.rerender(<WidgetPreview docId="summary" doc={documentWith(binding('kashbot.board', { field: 'openCount' }))} host={host} />)
  const element = () => screen.getByRole('region', { name: 'Live widget preview' }).querySelector('[data-element-id="summary"]')
  expect(element()).toHaveAttribute('data-freshness', 'live')
  expect(vi.getTimerCount()).toBe(1)
  await act(async () => { await vi.advanceTimersByTimeAsync(65000) })
  expect(element()).toHaveAttribute('data-freshness', 'stale')
  view.rerender(<WidgetPreview docId="summary" doc={literal} host={host} />)
  expect(vi.getTimerCount()).toBe(0)
})

it('preserves graph polling and clears its timer on a return to literal content', async () => {
  const client = { resolveValues: vi.fn(async () => ({ values: [{ status: 'resolved', value: '3' }] })) }
  const view = render(<WidgetPreview docId="summary" doc={documentWith(binding('graph.output', { doc_id: 'graph', output: 'total' }))} client={client} />)
  await act(async () => {})
  expect(client.resolveValues).toHaveBeenCalledTimes(1)
  await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
  expect(client.resolveValues).toHaveBeenCalledTimes(2)
  view.rerender(<WidgetPreview docId="summary" doc={literal} client={client} />)
  expect(vi.getTimerCount()).toBe(0)
  await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
  expect(client.resolveValues).toHaveBeenCalledTimes(2)
})
