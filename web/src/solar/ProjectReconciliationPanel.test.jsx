import React from 'react'
import { act, cleanup, fireEvent, render, screen, within, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import ProjectReconciliationPanel from './ProjectReconciliationPanel.jsx'

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
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
