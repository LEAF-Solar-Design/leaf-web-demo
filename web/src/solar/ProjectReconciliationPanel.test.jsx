import React from 'react'
import { act, cleanup, fireEvent, render, screen, within, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import ProjectReconciliationPanel, { RECONCILIATION_READ_TIMEOUT_MS } from './ProjectReconciliationPanel.jsx'
import * as reconciliation from './projectReconciliation.js'

afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
const binding = { kind: 'project', organization_id: '11111111-1111-4111-8111-111111111111',
  project_id: '22222222-2222-4222-8222-222222222222', drawing_id: '33333333-3333-4333-8333-333333333333',
  input_version_id: '44444444-4444-4444-8444-444444444444' }
const id = (kind) => `leaf:${kind}:00000000-0000-4000-8000-000000000001`
const response = () => ({ ok: true, data: { context: { ...binding }, intake: {
  solar_design_graph_sha256: 'a'.repeat(64), solar_design_graph: { rev: 9,
    frames: [{ id: id('frame'), panel_refs: [id('panel')], module_power_watts: 400 }],
    panels: [{ id: id('panel') }], strings: [{ id: id('string'), module_count: 1, ordered_panel_refs: [id('panel')] }],
    inverters: [{ id: id('inverter'), equipment_type: 'string_inverter' }],
  },
} } })
function fixture() {
  const h = { snapshot: { binding, authorityKey: 'a' }, current: true, listeners: new Set() }
  h.client = { readContext: vi.fn(async () => response()), getSnapshot: () => h.snapshot,
    subscribe: (listener) => { h.listeners.add(listener); return () => h.listeners.delete(listener) },
    uploadSolarEdgePdf: vi.fn(), submitRun: vi.fn() }
  h.props = { solarBinding: binding, authorityKey: 'a', projectClient: h.client, isCurrent: () => h.current }
  h.view = render(<ProjectReconciliationPanel {...h.props} />)
  h.update = (props) => { h.props = { ...h.props, ...props }; h.view.rerender(<ProjectReconciliationPanel {...h.props} />) }
  h.emit = () => act(() => { for (const listener of h.listeners) listener() })
  return h
}
const read = () => fireEvent.click(screen.getByRole('button', { name: 'Read saved design' }))
const fill = (values = ['1', '0', '1', '1', '0.4']) => screen.getAllByRole('textbox').forEach((input, index) => {
  fireEvent.change(input, { target: { value: values[index] } })
})
const loaded = () => screen.findByText('Saved design revision 9.')

it.each(['blob', 'url', 'anchor', 'click'])('recovers from %s download failure without losing evidence or drafts', async (failure) => {
  fixture(); read(); await loaded(); fill()
  const before = screen.getByRole('table').textContent
  const createObjectURL = vi.fn(() => 'blob:retry'), revokeObjectURL = vi.fn()
  vi.stubGlobal('URL', { createObjectURL, revokeObjectURL })
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  const secret = () => { throw new Error('private download details') }
  const NativeBlob = globalThis.Blob
  if (failure === 'blob') vi.stubGlobal('Blob', vi.fn(secret))
  if (failure === 'url') createObjectURL.mockImplementationOnce(secret)
  if (failure === 'click') click.mockImplementationOnce(secret)
  const createElement = document.createElement.bind(document)
  if (failure === 'anchor') vi.spyOn(document, 'createElement').mockImplementation((tag, ...args) => {
    if (tag === 'a') return secret()
    return createElement(tag, ...args)
  })
  fireEvent.click(screen.getByRole('button', { name: 'Download CSV' }))
  expect(screen.getByRole('alert').textContent).toMatch(/Try Download CSV again/)
  expect(screen.queryByText(/private download details/)).toBeNull()
  expect(screen.getByRole('table').textContent).toBe(before)
  expect(screen.getAllByRole('textbox').map(input => input.value)).toEqual(['1', '0', '1', '1', '0.4'])
  expect(revokeObjectURL).toHaveBeenCalledTimes(['anchor', 'click'].includes(failure) ? 1 : 0)
  vi.stubGlobal('Blob', NativeBlob)
  if (failure === 'anchor') document.createElement.mockRestore()
  fireEvent.click(screen.getByRole('button', { name: 'Download CSV' }))
  expect(screen.queryByRole('alert')).toBeNull()
  expect(revokeObjectURL).toHaveBeenLastCalledWith('blob:retry')
  expect(click).toHaveBeenCalled()
})

it('isolates URL cleanup errors and releases allocations even when scope changes during export', async () => {
  const h = fixture(); read(); await loaded(); fill()
  const revokeObjectURL = vi.fn(() => { throw new Error('private cleanup') })
  const createObjectURL = vi.fn(() => 'blob:cleanup')
  vi.stubGlobal('URL', { createObjectURL, revokeObjectURL })
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  fireEvent.click(screen.getByRole('button', { name: 'Download CSV' }))
  expect(screen.getAllByText('PASS')).toHaveLength(4)
  createObjectURL.mockImplementationOnce(() => { h.current = false; return 'blob:stale' })
  fireEvent.click(screen.getByRole('button', { name: 'Download CSV' }))
  expect(click).toHaveBeenCalledOnce()
  expect(revokeObjectURL).toHaveBeenLastCalledWith('blob:stale')
})

it.each(['resolve', 'reject'])('times out and ignores a late %s after a successful retry', async (outcome) => {
  vi.useFakeTimers()
  const h = fixture(); fill()
  let resolve, reject
  h.client.readContext.mockImplementationOnce(() => new Promise((yes, no) => { resolve = yes; reject = no }))
  read()
  const signal = h.client.readContext.mock.calls[0][0].signal
  await act(async () => { vi.advanceTimersByTime(RECONCILIATION_READ_TIMEOUT_MS) })
  expect(signal.aborted).toBe(true)
  expect(screen.getByRole('alert').textContent).toMatch(/timed out.*Try reading/)
  expect(screen.getByRole('button', { name: 'Read saved design' }).disabled).toBe(false)
  await act(async () => { read() })
  expect(screen.getByText('Saved design revision 9.')).toBeTruthy()
  expect(vi.getTimerCount()).toBe(0)
  await act(async () => { outcome === 'resolve' ? resolve(response()) : reject(new Error('private late rejection')) })
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.getAllByText('PASS')).toHaveLength(4)
})

it.each(['invalidation', 'unmount'])('clears stalled timers on %s', async (action) => {
  vi.useFakeTimers()
  const h = fixture()
  let resolve
  h.client.readContext.mockImplementationOnce(() => new Promise(yes => { resolve = yes }))
  read()
  if (action === 'unmount') h.view.unmount()
  else { h.current = false; h.emit() }
  expect(vi.getTimerCount()).toBe(0)
  await act(async () => { resolve(response()); vi.advanceTimersByTime(RECONCILIATION_READ_TIMEOUT_MS) })
  expect(screen.queryByText('Saved design revision 9.')).toBeNull()
})

it('keeps stable unique input guidance for invalid nonblank values', () => {
  fixture()
  const inputs = screen.getAllByRole('textbox')
  const identities = inputs.map(input => [input.id, input.getAttribute('aria-describedby')])
  expect(new Set(inputs.map(input => input.id)).size).toBe(5)
  fill(['1e0', '-1', '1.1', 'NaN', 'Infinity'])
  inputs.forEach((input, index) => {
    expect(input.getAttribute('aria-invalid')).toBe('true')
    expect([input.id, input.getAttribute('aria-describedby')]).toEqual(identities[index])
    input.getAttribute('aria-describedby').split(' ').forEach(id => expect(document.getElementById(id)?.textContent).toBeTruthy())
  })
})

it('keeps focused retry controls and announces read and download errors assertively', async () => {
  const h = fixture()
  h.client.readContext.mockRejectedValueOnce(new Error('private read'))
  const button = screen.getByRole('button', { name: 'Read saved design' })
  button.focus()
  fireEvent.click(button, { detail: 0 })
  expect((await screen.findByRole('alert')).textContent).toMatch(/Try reading it again/)
  expect(document.activeElement).toBe(button)
  fireEvent.click(button, { detail: 0 }); await loaded(); fill()
  const download = screen.getByRole('button', { name: 'Download CSV' })
  download.focus()
  const createObjectURL = vi.fn(() => 'blob:keyboard')
  createObjectURL.mockImplementationOnce(() => { throw new Error('private export') })
  vi.stubGlobal('URL', { createObjectURL, revokeObjectURL: vi.fn() })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  fireEvent.click(download, { detail: 0 })
  expect(screen.getByRole('alert').textContent).toMatch(/Try Download CSV again/)
  expect(document.activeElement).toBe(download)
  fireEvent.click(download, { detail: 0 })
  expect(screen.queryByRole('alert')).toBeNull()
  expect(document.activeElement).toBe(download)
})

it('exports blank unknown DC as unreadable while requiring all four count reports', async () => {
  fixture(); read(); await loaded(); fill(['1', '0', '1', '1', ''])
  const createObjectURL = vi.fn(() => 'blob:blank')
  vi.stubGlobal('URL', { createObjectURL, revokeObjectURL: vi.fn() })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  const download = screen.getByRole('button', { name: 'Download CSV' })
  expect(download.disabled).toBe(false)
  fireEvent.click(download)
  const csv = await new Promise(resolve => {
    const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.readAsText(createObjectURL.mock.calls[0][0])
  })
  expect(csv).toContain('"kw_dc","0.4","","","UNREADABLE"')
  fill(['1', '0', '1', '1', 'invalid']); expect(download.disabled).toBe(true)
  for (let index = 0; index < 4; index++) {
    const values = ['1', '0', '1', '1', '']; values[index] = ''; fill(values)
    expect(download.disabled).toBe(true)
  }
})

it('owns nested evidence and derives once per accepted read, never on edits or export', async () => {
  const derive = vi.spyOn(reconciliation, 'savedReconciliationValues')
  const h = fixture(), original = response()
  h.client.readContext.mockResolvedValue(original)
  read(); await loaded(); fill()
  expect(derive).toHaveBeenCalledTimes(1)
  const before = screen.getByRole('table').textContent
  const graph = original.data.intake.solar_design_graph
  graph.panels[0].id = 'mutated'
  graph.frames[0].module_power_watts = 999
  graph.frames[0].panel_refs[0] = 'mutated'
  graph.strings[0].ordered_panel_refs[0] = 'mutated'
  graph.strings[0].module_count = 2
  graph.inverters[0].equipment_type = 'combiner_box'
  fill(['2', '0', '1', '1', '0.4']); fill()
  expect(screen.getByRole('table').textContent).toBe(before)
  const createObjectURL = vi.fn(() => 'blob:owned')
  vi.stubGlobal('URL', { createObjectURL, revokeObjectURL: vi.fn() })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  fireEvent.click(screen.getByRole('button', { name: 'Download CSV' }))
  const csv = await new Promise(resolve => {
    const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.readAsText(createObjectURL.mock.calls[0][0])
  })
  expect(csv).toContain('"kw_dc","0.4","0.4","0","PASS"')
  expect(derive).toHaveBeenCalledTimes(1)
  await act(async () => { read() })
  expect(derive).toHaveBeenCalledTimes(2)
  expect(screen.getByRole('table').textContent).not.toBe(before)
  h.current = false; h.emit()
  expect(derive).toHaveBeenCalledTimes(2)
  expect(screen.queryByText('PASS')).toBeNull()
})

it('treats snapshot failure as a retryable read failure', async () => {
  const clone = globalThis.structuredClone
  vi.stubGlobal('structuredClone', vi.fn(() => { throw new Error('private snapshot') }))
  fixture(); fill(); read()
  await screen.findByRole('alert')
  expect(screen.queryByText(/private snapshot/)).toBeNull()
  expect(screen.queryByText('PASS')).toBeNull()
  vi.stubGlobal('structuredClone', clone)
  read(); await loaded()
  expect(screen.getAllByText('PASS')).toHaveLength(4)
})

it('reads only on demand, shows five editable rows and exports precisely the current comparison', async () => {
  const h = fixture()
  expect(h.client.readContext).not.toHaveBeenCalled()
  expect(screen.getAllByRole('textbox').map(input => input.value)).toEqual(['', '', '', '', ''])
  expect(screen.getAllByRole('row').slice(1).map(row => within(row).getByRole('rowheader').textContent))
    .toEqual(['Panels', 'Optimizers', 'Inverters', 'Strings', 'kW DC'])
  read(); await loaded()
  expect(h.client.readContext).toHaveBeenCalledWith({ signal: expect.any(AbortSignal) })
  fill(['1', '0', '2', '1', '0.400000000000000001'])
  expect(screen.getAllByText('PASS')).toHaveLength(2)
  expect(screen.getAllByText('FAIL')).toHaveLength(2)
  expect(screen.getByText(/no explicit measured optimizer/)).toBeTruthy()
  const createObjectURL = vi.fn(() => 'blob:comparison'), revokeObjectURL = vi.fn()
  vi.stubGlobal('URL', { createObjectURL, revokeObjectURL })
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  fireEvent.click(screen.getByRole('button', { name: 'Download CSV' }))
  const blob = createObjectURL.mock.calls[0][0]
  const csv = await new Promise((resolve) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.readAsText(blob) })
  expect(csv).toBe('metric,studio,oracle,delta,verdict\r\n"panels","1","1","0","PASS"\r\n' +
    '"optimizers","","0","","UNREADABLE"\r\n"inverters","1","2","-1","FAIL"\r\n' +
    '"strings","1","1","0","PASS"\r\n"kw_dc","0.4","0.400000000000000001","-0.000000000000000001","FAIL"\r\n')
  expect(click).toHaveBeenCalledOnce()
  expect(revokeObjectURL).toHaveBeenCalledWith('blob:comparison')
  expect(h.client.uploadSolarEdgePdf).not.toHaveBeenCalled()
  expect(h.client.submitRun).not.toHaveBeenCalled()
})

it('clears saved values during reread and catches read failures without fake zeros', async () => {
  const h = fixture(); read(); await loaded(); fill()
  let reject
  h.client.readContext.mockImplementationOnce(() => new Promise((resolve, no) => { reject = no }))
  read()
  expect(screen.queryByText('PASS')).toBeNull()
  expect(screen.getByRole('button', { name: 'Download CSV' }).disabled).toBe(true)
  expect(screen.getByRole('button', { name: 'Read saved design' }).disabled).toBe(true)
  await act(async () => reject(new Error('private details')))
  expect(screen.getByText(/Saved design could not be read/)).toBeTruthy()
  expect(screen.queryByText(/private details/)).toBeNull()
})

it.each(['authority', 'binding', 'predicate'])('invalidates %s immediately and aborts/ignores late reads', async (change) => {
  const h = fixture(); fill()
  let resolve
  h.client.readContext.mockImplementationOnce(() => new Promise(yes => { resolve = yes }))
  read()
  const signal = h.client.readContext.mock.calls[0][0].signal
  if (change === 'authority') h.snapshot = { ...h.snapshot, authorityKey: 'b' }
  if (change === 'binding') h.snapshot = { ...h.snapshot, binding: { ...binding, input_version_id: binding.project_id } }
  if (change === 'predicate') h.current = false
  h.emit()
  expect(signal.aborted).toBe(true)
  await act(async () => resolve(response()))
  expect(screen.queryByText('Saved design revision 9.')).toBeNull()
  expect(screen.getAllByRole('textbox').every(input => input.value === '')).toBe(true)
  expect(screen.getByRole('button', { name: 'Download CSV' }).disabled).toBe(true)
})

it('hides saved values and resets drafts when the scope key changes', async () => {
  const h = fixture(); read(); await loaded(); fill()
  h.snapshot = { binding, authorityKey: 'b' }
  h.update({ authorityKey: 'b' })
  expect(screen.queryByText('Saved design revision 9.')).toBeNull()
  expect(screen.queryByText('PASS')).toBeNull()
  expect(screen.getAllByRole('textbox').every(input => input.value === '')).toBe(true)
})

it('disables an untouched panel when its subscription reports invalidation', () => {
  const h = fixture()
  h.current = false
  h.emit()
  expect(screen.getByRole('button', { name: 'Read saved design' }).disabled).toBe(true)
})

it('aborts a pending read on binding replacement and never shows its late result', async () => {
  const h = fixture()
  let resolve
  h.client.readContext.mockImplementationOnce(() => new Promise(yes => { resolve = yes }))
  read()
  const signal = h.client.readContext.mock.calls[0][0].signal
  const next = { ...binding, input_version_id: binding.project_id }
  h.snapshot = { binding: next, authorityKey: 'a' }
  h.update({ solarBinding: next })
  expect(signal.aborted).toBe(true)
  await act(async () => resolve(response()))
  expect(screen.queryByText('Saved design revision 9.')).toBeNull()
  expect(screen.getByRole('button', { name: 'Download CSV' }).disabled).toBe(true)
})

it('checks current snapshot again at export even without a notification', async () => {
  const h = fixture(); read(); await loaded(); fill()
  const createObjectURL = vi.fn()
  vi.stubGlobal('URL', { createObjectURL })
  h.snapshot = { binding, authorityKey: 'other' }
  fireEvent.click(screen.getByRole('button', { name: 'Download CSV' }))
  expect(createObjectURL).not.toHaveBeenCalled()
})

it('aborts on unmount and removes its subscription', async () => {
  const h = fixture()
  h.client.readContext.mockImplementationOnce(() => new Promise(() => {}))
  read()
  const signal = h.client.readContext.mock.calls[0][0].signal
  h.view.unmount()
  expect(signal.aborted).toBe(true)
  expect(h.listeners.size).toBe(0)
})

it('rejects mismatched identity and malformed graphs and disables export for invalid reports or busy state', async () => {
  const h = fixture()
  const wrong = response(); wrong.data.context.project_id = binding.drawing_id
  h.client.readContext.mockResolvedValueOnce(wrong)
  read(); await screen.findByText(/response, drawing identity or graph is invalid/)
  const malformed = response(); delete malformed.data.intake.solar_design_graph.panels
  h.client.readContext.mockResolvedValueOnce(malformed)
  read(); await waitFor(() => expect(h.client.readContext).toHaveBeenCalledTimes(2))
  await screen.findByText(/response, drawing identity or graph is invalid/)
  read(); await loaded(); fill()
  expect(screen.getByRole('button', { name: 'Download CSV' }).disabled).toBe(false)
  fireEvent.change(screen.getByLabelText('Panels engineering report/BOM'), { target: { value: '1e0' } })
  expect(screen.getByRole('button', { name: 'Download CSV' }).disabled).toBe(true)
  fill(); h.update({ busy: true })
  expect(screen.getByRole('button', { name: 'Download CSV' }).disabled).toBe(true)
  expect(screen.getByRole('button', { name: 'Read saved design' }).disabled).toBe(true)
})
