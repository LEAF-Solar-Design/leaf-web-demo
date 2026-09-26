// @vitest-environment jsdom
import { useState } from 'react'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import PromptBox from './components/PromptBox.jsx'
import { rowsForIdentity } from './components/ElementContextMenu.jsx'
import { digest, flushNow, setTourStep } from './telemetry.js'
import { setUsageConsent } from './lib/telemetryConsent.js'

vi.mock('./converse.js', () => ({ ensureSession: vi.fn(), postMessage: vi.fn() }))
vi.mock('./useAnnotations.js', () => ({
  useAnnotations: () => ({
    annotation: null, busy: false, error: null, confirmation: null,
    preview: vi.fn(), accept: vi.fn(), reject: vi.fn(), retry: vi.fn(), undo: vi.fn(),
  }),
}))

const noop = () => {}
let fetchMock

function jsonResponse(body, ok = true, status = 200) {
  return Promise.resolve({ ok, status, json: () => Promise.resolve(body), clone: function () { return this } })
}

function Composer({ initialValue = '', ...props }) {
  const [value, setValue] = useState(initialValue)
  return <PromptBox value={value} onChange={setValue} onDispatch={noop} projectName="cat-panels" {...props} />
}

function openScope(container, label) {
  fireEvent.click(container.querySelector('.bar-scope'))
  fireEvent.click(screen.getByText(label))
}

function typeQuery(container, value) {
  fireEvent.change(container.querySelector('.bar-field'), { target: { value } })
}

function postedBodies() {
  return fetchMock.mock.calls
    .filter(([url, init]) => String(url).includes('/api/telemetry') && init?.method === 'POST')
    .map(([, init]) => JSON.parse(init.body))
}

function postedEvents(name) {
  return postedBodies().flatMap((body) => body.events).filter((event) => event.event_name === name)
}

function searchRequests() {
  return fetchMock.mock.calls.filter(([url]) => String(url).includes('/api/search'))
}

function expectEvent(name, labels, hashInputs = {}) {
  const events = postedEvents(name)
  expect(events).toHaveLength(1)
  expect(events[0].event_name).toMatch(/^[a-z0-9_]+\.[a-z0-9_]+$/)
  expect(Object.keys(events[0].labels).sort()).toEqual(Object.keys(labels).sort())
  expect(events[0].labels).toEqual(labels)
  for (const [key, input] of Object.entries(hashInputs)) {
    expect(events[0].labels[key]).toMatch(/^\d{16}$/)
    expect(events[0].labels[key]).toBe(digest(input))
  }
}

function selectMenuAction() {
  const onFit = vi.fn(() => 'fitted')
  const rows = rowsForIdentity({ kind: 'tool', id: 'fit' }, {
    hasDrawing: true, onFit, drawingId: 'private-drawing-id',
  })
  expect(rows).toHaveLength(1)
  expect(rows[0].disabled).toBe(false)
  expect(rows[0].onSelect()).toBe('fitted')
  expect(onFit).toHaveBeenCalledTimes(1)
}

beforeEach(() => {
  vi.useFakeTimers()
  localStorage.clear()
  sessionStorage.clear()
  setUsageConsent(false)
  fetchMock = vi.fn((url) => {
    const u = String(url)
    if (u.includes('/api/telemetry')) return Promise.resolve({ ok: true, status: 202 })
    if (u.includes('/api/drawings/')) {
      return jsonResponse({ drawing_id: 'demo', head: 2, latest: 2, versions: [
        { v: 1, parent: null, tool: 'drawing.ingest', note: 'first' },
        { v: 2, parent: 1, tool: 'drawing.write', note: 'panel move' },
      ] })
    }
    if (u.includes('/api/operator/sessions')) {
      return jsonResponse({ sessions: [{ session_id: 'opsess-1', profile: 'default', environment: 'staging', status: 'idle' }] })
    }
    if (u.includes('/api/search')) {
      return jsonResponse({ results: [{ kind: 'tool', id: 'tool:panel-cut', label: 'panel-cut', description: 'cuts panels' }] })
    }
    return jsonResponse(null, false, 404)
  })
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(async () => {
  cleanup()
  flushNow()
  await Promise.resolve()
  setUsageConsent(false)
  setTourStep(null)
  localStorage.clear()
  sessionStorage.clear()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

it('usage emitters row1 without consent a palette pick, a find query and a menu action post nothing', async () => {
  setUsageConsent(false)
  const onSelect = vi.fn()
  const { container } = render(<Composer initialValue="fit" paletteActions={[
    { id: 'fit', label: 'Fit drawing to viewport', icon: 'fit', disabled: false, reason: '', onSelect },
  ]} />)
  openScope(container, 'act')
  fireEvent.click(screen.getByText('Fit drawing to viewport'))
  expect(onSelect).toHaveBeenCalledTimes(1)
  openScope(container, 'find')
  typeQuery(container, 'private unconsented search')
  await act(async () => { await vi.advanceTimersByTimeAsync(150) })
  expect(searchRequests()).toHaveLength(1)
  expect(screen.getByText('panel-cut')).toBeTruthy()
  selectMenuAction()
  flushNow()
  for (const name of ['palette.pick', 'find.query', 'context_menu.action']) {
    expect(postedEvents(name)).toHaveLength(0)
  }
  setUsageConsent(true)
  selectMenuAction()
  flushNow()
  expectEvent('context_menu.action', { action_id: 'fit', element_kind: 'tool' })
})

it('usage emitters row2 a palette pick posts exactly one palette.pick with digests and no typed text', async () => {
  setUsageConsent(true)
  const query = 'private palette needle'
  const toolName = `${query} tool`
  const rowId = `tool:${toolName}`
  const { container } = render(<Composer tools={[{ name: toolName, description: 'private row description' }]} />)
  openScope(container, 'act')
  typeQuery(container, `  ${query}  `)
  fireEvent.click(screen.getByText(toolName))
  await act(async () => { await Promise.resolve() })
  flushNow()
  expectEvent('palette.pick', {
    scope: 'act', row_kind: 'tool', query_hash: digest(query), row_hash: digest(rowId),
  }, { query_hash: query, row_hash: rowId })
  const wire = JSON.stringify(postedBodies())
  expect(wire).not.toContain(query)
  expect(wire).not.toContain(toolName)
  expect(wire).not.toContain(rowId)
  expect(wire).not.toContain('private row description')
})

it('usage emitters row3 a find query posts exactly one find.query per debounced search with a digest only', async () => {
  setUsageConsent(true)
  const query = 'private final search needle'
  const superseded = 'private unfinished search'
  const { container } = render(<Composer />)
  openScope(container, 'find')
  typeQuery(container, superseded)
  await act(async () => { await vi.advanceTimersByTimeAsync(100) })
  expect(searchRequests()).toHaveLength(0)
  typeQuery(container, `  ${query}  `)
  await act(async () => { await vi.advanceTimersByTimeAsync(149) })
  expect(searchRequests()).toHaveLength(0)
  await act(async () => { await vi.advanceTimersByTimeAsync(1) })
  expect(searchRequests()).toHaveLength(1)
  expect(screen.getByText('panel-cut')).toBeTruthy()
  flushNow()
  expectEvent('find.query', { query_hash: digest(query) }, { query_hash: query })
  const wire = JSON.stringify(postedBodies())
  expect(wire).not.toContain(query)
  expect(wire).not.toContain(superseded)

  const secondQuery = 'private second search needle'
  typeQuery(container, `  ${secondQuery}  `)
  await act(async () => { await vi.advanceTimersByTimeAsync(149) })
  expect(searchRequests()).toHaveLength(1)
  await act(async () => { await vi.advanceTimersByTimeAsync(1) })
  expect(searchRequests()).toHaveLength(2)
  flushNow()
  const events = postedEvents('find.query')
  expect(events).toHaveLength(2)
  expect(events.map((event) => event.labels.query_hash)).toEqual([digest(query), digest(secondQuery)])
  for (const event of events) {
    expect(event.event_name).toMatch(/^[a-z0-9_]+\.[a-z0-9_]+$/)
    expect(Object.keys(event.labels)).toEqual(['query_hash'])
    expect(event.labels.query_hash).toMatch(/^\d{16}$/)
  }
  const allWire = JSON.stringify(postedBodies())
  expect(allWire).not.toContain(query)
  expect(allWire).not.toContain(superseded)
  expect(allWire).not.toContain(secondQuery)
})

it('usage emitters row4 a context-menu action posts exactly one context_menu.action with the action id and element kind', () => {
  setUsageConsent(true)
  const query = 'private draft beside the menu'
  const { container } = render(<Composer />)
  typeQuery(container, query)
  selectMenuAction()
  flushNow()
  expectEvent('context_menu.action', { action_id: 'fit', element_kind: 'tool' })
  const wire = JSON.stringify(postedBodies())
  expect(wire).not.toContain(query)
  expect(wire).not.toContain('private-drawing-id')
  expect(wire).not.toContain('tool:fit')
})

it('usage emitters row5 during a tour the only extra label is the tour step', async () => {
  setUsageConsent(true)
  setTourStep('request')
  try {
    selectMenuAction()
    flushNow()
    expectEvent('context_menu.action', {
      action_id: 'fit', element_kind: 'tool', tour_step: 'request',
    })

    const query = 'private tour palette needle'
    const toolName = `${query} tool`
    const rowId = `tool:${toolName}`
    const { container } = render(<Composer tools={[{ name: toolName, description: 'private tour row description' }]} />)
    openScope(container, 'act')
    typeQuery(container, `  ${query}  `)
    fireEvent.click(screen.getByText(toolName))
    await act(async () => { await Promise.resolve() })
    flushNow()
    expectEvent('palette.pick', {
      scope: 'act', row_kind: 'tool', query_hash: digest(query), row_hash: digest(rowId), tour_step: 'request',
    }, { query_hash: query, row_hash: rowId })
    const wire = JSON.stringify(postedBodies())
    expect(wire).not.toContain(query)
    expect(wire).not.toContain(toolName)
    expect(wire).not.toContain(rowId)
    expect(wire).not.toContain('private tour row description')
    expect(wire).not.toContain('private-drawing-id')
    expect(wire).not.toContain('tool:fit')
  } finally {
    setTourStep(null)
  }
})

it('usage emitters row6 a palette pick of an action row posts exactly one palette.pick with the action labels', async () => {
  setUsageConsent(true)
  const query = 'drawing to'
  const label = 'Fit drawing to viewport'
  const onSelect = vi.fn()
  const { container } = render(<Composer paletteActions={[
    { id: 'fit', label, icon: 'fit', disabled: false, reason: '', onSelect },
  ]} />)
  // PromptBox projects paletteActions only in act; find uses search-result rows.
  const pickedScope = 'act'
  openScope(container, pickedScope)
  typeQuery(container, `  ${query}  `)
  fireEvent.click(screen.getByText(label))
  expect(onSelect).toHaveBeenCalledTimes(1)
  await act(async () => { await Promise.resolve() })
  flushNow()
  expectEvent('palette.pick', {
    scope: 'act', row_kind: 'action', query_hash: digest(query), action_id: 'fit',
  }, { query_hash: query })
  expect(postedEvents('palette.pick')[0].labels.scope).toBe(pickedScope)
  const wire = JSON.stringify(postedBodies())
  expect(wire).not.toContain(query)
  expect(wire).not.toContain(label)
  expect(wire).not.toContain('tool:fit')
})

it('usage emitters row7 a published tool in the palette never sends its name', async () => {
  setUsageConsent(true)
  const name = 'private_customer_roof'
  const rowId = `authored:${name}`
  const onSelect = vi.fn()
  const { container } = render(<Composer paletteActions={[
    { id: rowId, label: name, icon: 'fit', disabled: false, reason: '', onSelect },
  ]} />)
  openScope(container, 'act')
  typeQuery(container, `  ${name}  `)
  fireEvent.click(screen.getByRole('option', { name: (label) => label.includes(name) }))
  expect(onSelect).toHaveBeenCalledTimes(1)
  await act(async () => { await Promise.resolve() })
  flushNow()
  expectEvent('palette.pick', {
    scope: 'act', row_kind: 'action', query_hash: digest(name),
    action_id: 'unregistered', row_hash: digest(rowId),
  }, { query_hash: name, row_hash: rowId })
  const wire = JSON.stringify(postedBodies())
  expect(wire).not.toContain(name)
  expect(wire).not.toContain('authored:')
})
