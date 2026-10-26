// @vitest-environment jsdom
import React from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import RoutePanel from './RoutePanel.jsx'
import ResultPanel from './ResultPanel.jsx'
import { ProductSurfaceFrame } from './ProductSurfaceTabs.jsx'
import LocalCadConnect from '../localCad/LocalCadConnect.jsx'
import { productSurfaceStates } from '../site/productSurfaces.js'
import { humanizeError, MSG_NETWORK, MSG_SERVICE } from '../errorHumanize.js'
import { scanSource } from '../../scripts/check_user_facing_copy.mjs'

afterEach(() => { cleanup(); vi.restoreAllMocks() })

const tools = [
  { name: 'delete-panel', capabilities: ['drawing.write'], params: { properties: {} } },
  { name: 'count-panels', capabilities: ['drawing.read'], params: { properties: {} } },
]
const route = {
  lane: 'run', tool: 'delete-panel', confidence: 0.99,
  runIntent: { tool: 'delete-panel', params: { handle: '7FA3', layer: 'Roof' } },
}
function mountRoute(nextRoute = route, extra = {}) {
  const clock = vi.spyOn(performance, 'now').mockReturnValue(0)
  const onConfirmIntent = vi.fn(), onPickAlternative = vi.fn(), onPromptKey = vi.fn()
  render(<>
    <textarea className="bar-field" role="combobox" aria-label="Command bar" aria-expanded="false" onKeyDown={onPromptKey} />
    <RoutePanel route={nextRoute} tools={tools} running={false} writeLocked={false}
      onConfirmIntent={onConfirmIntent} onPickAlternative={onPickAlternative}
      onOpenAuthor={vi.fn()} onDismiss={vi.fn()} {...extra} />
  </>)
  clock.mockReturnValue(500)
  return { onConfirmIntent, onPickAlternative, onPromptKey, clock }
}
function browserFrame() {
  return render(<ProductSurfaceFrame activeSurface="browser"
    states={productSurfaceStates({ sessionActive: true })} />)
}

it('S1-73 describes a demo run without implementation labels', () => {
  render(<ResultPanel running={false} result={{ ok: true, result: {}, cost: null, timing_ms: 10 }} />)
  expect(screen.getByText('no cloud cost')).toBeTruthy()
  expect(screen.queryByText(/mock/i)).toBeNull()
})

it('S1-207 describes shared features in everyday words', () => {
  browserFrame()
  const list = screen.getByRole('heading', { name: 'Shared everywhere' }).nextElementSibling
  expect(within(list).getByText('Run history')).toBeTruthy()
  expect(within(list).getByText('Long-running tasks')).toBeTruthy()
  expect(within(list).getByText('Run a tool')).toBeTruthy()
  expect(list.textContent).not.toMatch(/receipts|marathons|one-shot execution/)
})

it('S1-207 explains project setup without internal layout names', () => {
  browserFrame()
  const note = screen.getByTestId('browser-composition-note')
  expect(note.textContent).toContain('Create a workspace project')
  expect(note.textContent).not.toMatch(/project rail|shared identity/)
})

it('S1-210 summarizes run parameters without JSON or object identifiers', () => {
  mountRoute()
  expect(screen.getByText(/layer Roof/)).toBeTruthy()
  expect(document.body.textContent).not.toMatch(/params \{|"handle"|7FA3/)
})

it('S1-210 labels drawing edits without capability codes', () => {
  mountRoute()
  expect(screen.getByText('Edits drawing')).toBeTruthy()
  expect(screen.queryByText('drawing.write')).toBeNull()
})

it('S1-70 confirms with Enter from the focused command bar', () => {
  const callbacks = mountRoute()
  const bar = screen.getByRole('combobox')
  bar.focus()
  fireEvent.keyDown(bar, { key: 'Enter' })
  expect(callbacks.onConfirmIntent).toHaveBeenCalledWith(route.runIntent, tools[0], route.runIntent.params)
  expect(document.activeElement).toBe(bar)
})

it('S1-70 selects a resolver row with command bar arrows before prompt history handles it', () => {
  const callbacks = mountRoute({ lane: 'run', tool: null, confidence: 0.1 })
  const bar = screen.getByRole('combobox')
  bar.focus()
  fireEvent.keyDown(bar, { key: 'ArrowDown' })
  expect(screen.getAllByRole('option')[1].getAttribute('aria-selected')).toBe('true')
  expect(callbacks.onPromptKey).not.toHaveBeenCalled()
  fireEvent.keyDown(bar, { key: 'Enter' })
  expect(callbacks.onPickAlternative).toHaveBeenCalledWith('count-panels')
  expect(document.activeElement).toBe(bar)
})

it('S1-14 humanizes bridge status failures and offers a retry', () => {
  const retry = vi.fn()
  render(<LocalCadConnect status="unreachable" error="Bridge HTTP 503" onConnect={retry} />)
  expect(document.body.textContent).toContain(MSG_SERVICE)
  expect(document.body.textContent).not.toContain('Bridge HTTP 503')
  fireEvent.click(screen.getByRole('button', { name: 'Retry AutoCAD' }))
  expect(retry).toHaveBeenCalledOnce()
})

it('S1-14 replaces invalid bridge response text', () => {
  render(<LocalCadConnect status="waiting" error="Invalid bridge response" />)
  expect(document.body.textContent).toContain(MSG_SERVICE)
  expect(document.body.textContent).not.toContain('Invalid bridge response')
})

it('S1-14 replaces bridge timeout text', () => {
  render(<LocalCadConnect status="unreachable" error="Bridge request timed out" />)
  expect(document.body.textContent).toContain(MSG_NETWORK)
  expect(document.body.textContent).not.toContain('Bridge request timed out')
})

it('S1-37 explains a startup timeout without its millisecond budget', () => {
  expect(humanizeError(new Error('The service did not respond within 5000 ms.'))).toBe(MSG_NETWORK)
})

it('S1-37 hides the sample data filename in a startup failure', () => {
  expect(humanizeError('failed to load sample.intake.json')).toBe(MSG_SERVICE)
})

it('S1-208 scans nested template copy for dashes', () => {
  const hits = scanSource('const title = `Saved ${ok ? `Ready ${n} — local fallback` : "waiting"}`;', 'src/X.jsx', { strict: true })
  expect(hits.map((hit) => hit.marker)).toContain('em dash')
})

it('S1-208 scans fallback literals inside template expressions', () => {
  expect(scanSource('const title = `Ready ${value || "—"}`;', 'src/X.jsx', { strict: true }).map((hit) => hit.marker)).toContain('em dash')
})

it('S1-208 rejects the plural placeholders in visible text', () => {
  expect(scanSource('<p>These placeholders will be replaced</p>', 'src/X.jsx').map((hit) => hit.marker)).toContain('placeholder')
})
