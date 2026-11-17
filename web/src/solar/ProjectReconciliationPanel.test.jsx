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
function fixture({ strict = false, subscribe } = {}) {
  const h = { snapshot: { binding, authorityKey: 'a' }, current: true, listeners: new Set() }
  h.client = { readContext: vi.fn(async () => response()), getSnapshot: () => h.snapshot,
    subscribe: (listener) => { h.listeners.add(listener); return () => h.listeners.delete(listener) },
    uploadSolarEdgePdf: vi.fn(), submitRun: vi.fn() }
  if (subscribe) h.client.subscribe = subscribe
  h.props = { solarBinding: binding, authorityKey: 'a', projectClient: h.client, isCurrent: () => h.current }
  const element = () => strict ? <React.StrictMode><ProjectReconciliationPanel {...h.props} /></React.StrictMode>
    : <ProjectReconciliationPanel {...h.props} />
  h.view = render(element())
  h.update = (props) => { h.props = { ...h.props, ...props }; h.view.rerender(element()) }
  h.emit = () => act(() => { for (const listener of h.listeners) listener() })
  return h
}
const read = () => fireEvent.click(screen.getByRole('button', { name: 'Read saved design' }))
const fill = (values = ['1', '0', '1', '1', '0.4']) => screen.getAllByRole('textbox').forEach((input, index) => {
  fireEvent.change(input, { target: { value: values[index] } })
})
const loaded = () => screen.findByText('Saved design revision 9.')
const blobText = blob => new Promise(resolve => {
  const reader = new FileReader()
  reader.onload = () => resolve(reader.result)
  reader.readAsText(blob)
})

it('contains a synchronous read throw and releases the request for a clean retry', async () => {
  const h = fixture()
  h.client.readContext.mockImplementationOnce(() => { throw new Error('private synchronous failure') })
  read()
  expect((await screen.findByRole('alert')).textContent).toMatch(/could not be read.*Try reading/)
  expect(screen.queryByText(/private synchronous/)).toBeNull()
  expect(screen.getByRole('button', { name: 'Read saved design' }).disabled).toBe(false)
  read(); await loaded()
  expect(h.client.readContext).toHaveBeenCalledTimes(2)
  expect(screen.queryByRole('alert')).toBeNull()
})

it('does not steal an entered report field focus when export fails', async () => {
  fixture(); read(); await loaded(); fill()
  const input = screen.getAllByRole('textbox')[0]
  input.focus()
  fireEvent.change(input, { target: { value: '012' } })
  vi.stubGlobal('URL', { createObjectURL: () => { throw new Error('private export') } })
  fireEvent.click(screen.getByRole('button', { name: 'Download CSV' }))
  expect(screen.getByRole('alert').textContent).toMatch(/Try Download CSV again/)
  expect(document.activeElement).toBe(input)
  expect(input.value).toBe('012')
})

it('exports UTF-8 CSV with the exact safe filename, MIME and accepted five rows', async () => {
  fixture(); read(); await loaded(); fill(['01', '00', '01', '01', '0.400'])
  const createObjectURL = vi.fn(() => 'blob:metadata')
  vi.stubGlobal('URL', { createObjectURL, revokeObjectURL: vi.fn() })
  let anchor
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function () { anchor = this })
  fireEvent.click(screen.getByRole('button', { name: 'Download CSV' }))
  const blob = createObjectURL.mock.calls[0][0]
  expect(blob.type).toBe('text/csv;charset=utf-8')
  expect(anchor.download).toBe('engineering-reconciliation.csv')
  expect(anchor.href).toBe('blob:metadata')
  expect(await blobText(blob)).toBe('metric,studio,oracle,delta,verdict\r\n' +
    '"panels","1","01","0","PASS"\r\n"optimizers","","00","","UNREADABLE"\r\n' +
    '"inverters","1","01","0","PASS"\r\n"strings","1","01","0","PASS"\r\n' +
    '"kw_dc","0.4","0.400","0","PASS"\r\n')
})

it.each(['valid', 'invalid', 'pending'])('Enter in every report input does not start read or download (%s)', async state => {
  const h = fixture()
  read(); await loaded(); fill()
  if (state === 'invalid') fill(['bad', 'bad', 'bad', 'bad', 'bad'])
  if (state === 'pending') {
    h.client.readContext.mockImplementationOnce(() => new Promise(() => {}))
    read()
  }
  const calls = h.client.readContext.mock.calls.length
  const createObjectURL = vi.fn(() => 'blob:explicit')
  vi.stubGlobal('URL', { createObjectURL, revokeObjectURL: vi.fn() })
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  for (const input of screen.getAllByRole('textbox')) {
    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter' })
    fireEvent.keyUp(input, { key: 'Enter', code: 'Enter' })
  }
  expect(h.client.readContext).toHaveBeenCalledTimes(calls)
  expect(createObjectURL).not.toHaveBeenCalled()
  expect(click).not.toHaveBeenCalled()
  if (state === 'valid') {
    fireEvent.click(screen.getByRole('button', { name: 'Download CSV' }))
    expect(click).toHaveBeenCalledOnce()
  }
})

const reportValues = ['01', '00', '01', '01', '0.400']
const totalsResponse = (revision = 0, values = null, canWrite = true) => ({ ok: true, status: 200,
  data: { revision, canWrite, record: values === null ? null :
    reconciliation.createEngineeringTotalsRecord(binding, 'a'.repeat(64), values) } })
const saveTotals = () => fireEvent.click(screen.getByRole('button', { name: 'Save engineering totals' }))
const reloadTotals = () => fireEvent.click(screen.getByRole('button', { name: 'Reload saved engineering totals' }))
const inputValues = () => screen.getAllByRole('textbox').map(input => input.value)
function canonicalReports(h, file = { response: totalsResponse() }) {
  h.client.loadEngineeringTotals = vi.fn(async () => structuredClone(file.response))
  h.client.saveEngineeringTotals = vi.fn(async ({ drafts, expectedRevision }) => {
    if (file.response.data.revision !== expectedRevision) return { ok: false, status: 409 }
    file.response = totalsResponse(expectedRevision + 1, drafts)
    return structuredClone(file.response)
  })
  return file
}
const reportsLoaded = () => screen.findByText(/No saved engineering totals|Saved engineering totals loaded/)

function sharedPanels() {
  const listeners = new Set()
  const h = { snapshot: { binding, authorityKey: 'a' } }
  h.client = { getSnapshot: () => h.snapshot,
    readContext: vi.fn(async () => response()),
    subscribe: vi.fn(listener => { listeners.add(listener); return () => listeners.delete(listener) }) }
  const file = canonicalReports(h)
  const props = { solarBinding: binding, authorityKey: 'a', projectClient: h.client, isCurrent: () => true }
  const panel = name => {
    const view = render(<section aria-label={name}><ProjectReconciliationPanel {...props} /></section>)
    const query = within(screen.getByRole('region', { name }))
    return { view, query, update: extra => view.rerender(<section aria-label={name}><ProjectReconciliationPanel {...props} {...extra} /></section>),
      click: label => fireEvent.click(query.getByRole('button', { name: label })),
      fill: values => query.getAllByRole('textbox').forEach((input, i) => fireEvent.change(input, { target: { value: values[i] } })),
      values: () => query.getAllByRole('textbox').map(input => input.value),
      loaded: () => query.findByText(/No saved engineering totals|Saved engineering totals loaded/) }
  }
  return { h, file, listeners, a: panel('Client A'), b: panel('Client B') }
}

function replacementClient(h, revision, values) {
  const fresh = { client: { ...h.client, readContext: vi.fn(async () => response()),
    subscribe: vi.fn(() => () => {}) } }
  canonicalReports(fresh, { response: totalsResponse(revision, values) })
  h.update({ projectClient: fresh.client })
  return fresh.client
}

it.each(['save', 'load'])('requires a fresh same-scope client read after replacing a pending %s client', async operation => {
  const h = fixture(); canonicalReports(h, { response: totalsResponse(1, reportValues) })
  read(); await reportsLoaded(); fill(reportValues)
  let settle
  const oldClient = h.client, method = operation === 'save' ? oldClient.saveEngineeringTotals : oldClient.loadEngineeringTotals
  method.mockReturnValueOnce(new Promise(resolve => { settle = resolve }))
  if (operation === 'save') saveTotals(); else reloadTotals()
  const old = method.mock.calls.at(-1)[0], freshValues = ['3', '0', '1', '1', '0.6']
  const client = replacementClient(h, 7, freshValues)
  expect(old.signal.aborted).toBe(true)
  expect(screen.getByRole('button', { name: 'Save engineering totals' }).disabled).toBe(true)
  expect(client.loadEngineeringTotals).not.toHaveBeenCalled()
  read(); await reportsLoaded()
  await act(async () => settle(totalsResponse(operation === 'save' ? 2 : 99, reportValues)))
  expect(inputValues()).toEqual(freshValues)
  const createObjectURL = vi.fn(() => 'blob:client-replaced')
  vi.stubGlobal('URL', { createObjectURL, revokeObjectURL: vi.fn() })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  fireEvent.click(screen.getByRole('button', { name: 'Download CSV' }))
  expect(await blobText(createObjectURL.mock.calls[0][0])).toContain('"panels","1","3","-2","FAIL"')
  saveTotals(); await screen.findByText('Engineering totals saved.')
  expect(client.saveEngineeringTotals.mock.calls[0][0]).toMatchObject({ expectedRevision: 7, drafts: freshValues })
})

it.each([0, 1, 2, 3, 4])('preserves invalid field %i entered during Save and repairs against the accepted baseline', async index => {
  const h = fixture(); canonicalReports(h); read(); await reportsLoaded(); fill(reportValues)
  let settle; h.client.saveEngineeringTotals.mockReturnValueOnce(new Promise(resolve => { settle = resolve }))
  saveTotals(); const input = screen.getAllByRole('textbox')[index]
  fireEvent.change(input, { target: { value: 'bad' } })
  await act(async () => settle(totalsResponse(1, reportValues)))
  expect(input.value).toBe('bad'); expect(input.getAttribute('aria-invalid')).toBe('true')
  expect(document.getElementById(input.getAttribute('aria-describedby').split(' ')[0]).textContent).toMatch(/whole number|nonnegative decimal/)
  expect(screen.getByRole('button', { name: 'Save engineering totals' }).disabled).toBe(true)
  expect(screen.getByText('Submitted engineering totals saved. Newer edits remain unsaved.')).toBeTruthy()
  fireEvent.change(input, { target: { value: reportValues[index] } })
  h.client.saveEngineeringTotals.mockResolvedValueOnce(totalsResponse(2, reportValues))
  saveTotals(); await screen.findByText('Engineering totals saved.')
  expect(h.client.saveEngineeringTotals.mock.calls.at(-1)[0]).toMatchObject({ expectedRevision: 1, drafts: reportValues })
})

it.each(['save', 'load'])('expires only one shared-client request while a sibling %s remains pending', async operation => {
  const { h, a, b } = sharedPanels()
  a.click('Read saved design'); b.click('Read saved design'); await a.loaded(); await b.loaded()
  let settleA, settleB
  h.client.loadEngineeringTotals.mockReturnValueOnce(new Promise(resolve => { settleA = resolve }))
  vi.useFakeTimers(); a.click('Reload saved engineering totals')
  const expired = h.client.loadEngineeringTotals.mock.calls.at(-1)[0]
  act(() => vi.advanceTimersByTime(5000))
  const method = operation === 'save' ? h.client.saveEngineeringTotals : h.client.loadEngineeringTotals
  method.mockReturnValueOnce(new Promise(resolve => { settleB = resolve }))
  b.fill(reportValues); b.click(operation === 'save' ? 'Save engineering totals' : 'Reload saved engineering totals')
  const active = method.mock.calls.at(-1)[0]
  act(() => vi.advanceTimersByTime(RECONCILIATION_READ_TIMEOUT_MS - 5000)); vi.useRealTimers()
  expect(expired.signal.aborted).toBe(true); expect(active.signal.aborted).toBe(false)
  expect(a.query.getByText(/Engineering totals could not be loaded/)).toBeTruthy()
  await act(async () => settleA(totalsResponse(99, ['9', '9', '9', '9', '9'])))
  expect(b.values()).toEqual(reportValues)
  expect(b.query.getByText(operation === 'save' ? 'Saving engineering totals.' : 'Loading engineering totals.')).toBeTruthy()
  await act(async () => settleB(totalsResponse(1, reportValues)))
  h.client.saveEngineeringTotals.mockResolvedValueOnce(totalsResponse(2, reportValues))
  b.click('Save engineering totals'); await b.query.findByText('Engineering totals saved.')
  expect(h.client.saveEngineeringTotals.mock.calls.at(-1)[0].expectedRevision).toBe(1)
})

it('revokes both shared-client pending observations and requires fresh authorized reads before reuse', async () => {
  const { h, listeners, a, b } = sharedPanels()
  a.click('Read saved design'); b.click('Read saved design'); await a.loaded(); await b.loaded()
  a.fill(reportValues); b.fill(reportValues)
  let settleA, settleB
  h.client.saveEngineeringTotals.mockReturnValueOnce(new Promise(resolve => { settleA = resolve }))
  h.client.loadEngineeringTotals.mockReturnValueOnce(new Promise(resolve => { settleB = resolve }))
  a.click('Save engineering totals'); b.click('Reload saved engineering totals')
  const sa = h.client.saveEngineeringTotals.mock.calls.at(-1)[0].signal
  const sb = h.client.loadEngineeringTotals.mock.calls.at(-1)[0].signal
  h.snapshot = { binding, authorityKey: 'b' }
  act(() => { for (const listener of listeners) listener() })
  expect(sa.aborted).toBe(true); expect(sb.aborted).toBe(true)
  for (const panel of [a, b]) expect(panel.query.getByRole('button', { name: 'Save engineering totals' }).disabled).toBe(true)
  a.update({ authorityKey: 'b' }); b.update({ authorityKey: 'b' })
  const fresh = ['3', '0', '1', '1', '0.6']
  h.client.loadEngineeringTotals.mockResolvedValue(totalsResponse(7, fresh))
  for (const panel of [a, b]) {
    expect(panel.query.getByRole('button', { name: 'Save engineering totals' }).disabled).toBe(true)
    panel.click('Read saved design'); await panel.loaded()
  }
  await act(async () => { settleA(totalsResponse(1, reportValues)); settleB(totalsResponse(99, reportValues)) })
  expect(a.values()).toEqual(fresh); expect(b.values()).toEqual(fresh)
  h.client.saveEngineeringTotals.mockResolvedValue(totalsResponse(8, fresh))
  for (const panel of [a, b]) { panel.click('Save engineering totals'); await panel.query.findByText('Engineering totals saved.') }
  expect(h.client.saveEngineeringTotals.mock.calls.slice(-2).map(([x]) => x.expectedRevision)).toEqual([7, 7])
})

it('keeps independent CAS baselines and losing drafts when two panels share one client', async () => {
  const { h, file, a, b } = sharedPanels()
  a.click('Read saved design'); b.click('Read saved design'); await a.loaded(); await b.loaded()
  const av = ['1', '0', '1', '1', '0.4'], bv = ['2', '0', '1', '1', '0.5']
  a.fill(av); b.fill(bv); a.click('Save engineering totals'); await a.query.findByText('Engineering totals saved.')
  const first = structuredClone(file.response)
  b.click('Save engineering totals'); await b.query.findByText(/Engineering totals changed/)
  expect(file.response).toEqual(first); expect(a.values()).toEqual(av); expect(b.values()).toEqual(bv)
  b.click('Reload saved engineering totals'); await b.loaded(); expect(b.values()).toEqual(av)
  b.fill(bv); b.click('Save engineering totals'); await b.query.findByText('Engineering totals saved.')
  const second = structuredClone(file.response)
  a.click('Save engineering totals'); await a.query.findByText(/Engineering totals changed/)
  expect(file.response).toEqual(second); expect(a.values()).toEqual(av); expect(b.values()).toEqual(bv)
  expect(h.client.saveEngineeringTotals.mock.calls.map(([x]) => x.expectedRevision)).toEqual([0, 0, 1, 1])
})

it.each(['save', 'load'])('unmounting shared-client panel during %s leaves its survivor request and subscription intact', async operation => {
  const { h, listeners, a, b } = sharedPanels()
  a.click('Read saved design'); b.click('Read saved design'); await a.loaded(); await b.loaded()
  let settleA, settleB
  const method = operation === 'save' ? h.client.saveEngineeringTotals : h.client.loadEngineeringTotals
  method.mockReturnValueOnce(new Promise(resolve => { settleA = resolve }))
  a.fill(reportValues); a.click(operation === 'save' ? 'Save engineering totals' : 'Reload saved engineering totals')
  const old = method.mock.calls.at(-1)[0]
  const survivor = ['2', '0', '1', '1', '0.5']; b.fill(survivor)
  h.client.saveEngineeringTotals.mockReturnValueOnce(new Promise(resolve => { settleB = resolve }))
  b.click('Save engineering totals'); const active = h.client.saveEngineeringTotals.mock.calls.at(-1)[0]
  expect(listeners.size).toBe(2); a.view.unmount()
  expect(old.signal.aborted).toBe(true); expect(active.signal.aborted).toBe(false); expect(listeners.size).toBe(1)
  await act(async () => settleA(totalsResponse(1, reportValues)))
  expect(b.values()).toEqual(survivor); expect(b.query.getByText('Saving engineering totals.')).toBeTruthy()
  await act(async () => settleB(totalsResponse(1, survivor)))
  expect(b.query.getByText('Engineering totals saved.')).toBeTruthy()
  h.client.saveEngineeringTotals.mockResolvedValueOnce(totalsResponse(2, survivor))
  b.click('Save engineering totals'); await b.query.findByText('Engineering totals saved.')
  expect(h.client.saveEngineeringTotals.mock.calls.at(-1)[0].expectedRevision).toBe(1)
  expect(listeners.size).toBe(1)
})

it.each([0, 1, 2, 3, 4])('rejects substituted acknowledged report value %i despite valid receipt metadata', async index => {
  const h = fixture(); canonicalReports(h); read(); await reportsLoaded(); fill(reportValues)
  const altered = [...reportValues]; altered[index] = index === 4 ? '0.5' : '2'
  h.client.saveEngineeringTotals.mockResolvedValueOnce(totalsResponse(1, altered))
  saveTotals(); await screen.findByText(/Engineering totals could not be saved/)
  expect(screen.queryByText('Engineering totals saved.')).toBeNull(); expect(inputValues()).toEqual(reportValues)
  h.client.saveEngineeringTotals.mockResolvedValueOnce(totalsResponse(1, reportValues))
  saveTotals(); await screen.findByText('Engineering totals saved.')
  expect(h.client.saveEngineeringTotals.mock.calls.map(([x]) => x.expectedRevision)).toEqual([0, 0])
})

it('isolates accepted Save receipt aliases from drafts, CSV and the next CAS baseline', async () => {
  const h = fixture(); canonicalReports(h); read(); await reportsLoaded(); fill(reportValues)
  const receipt = totalsResponse(1, reportValues)
  h.client.saveEngineeringTotals.mockResolvedValueOnce(receipt)
  saveTotals(); await screen.findByText('Engineering totals saved.')
  receipt.data.record.values.fill('9'); receipt.data.record.binding.project_id = '99999999-9999-4999-8999-999999999999'
  receipt.data.revision = 99; receipt.data.canWrite = false
  expect(inputValues()).toEqual(reportValues)
  const createObjectURL = vi.fn(() => 'blob:save-alias')
  vi.stubGlobal('URL', { createObjectURL, revokeObjectURL: vi.fn() })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  fireEvent.click(screen.getByRole('button', { name: 'Download CSV' }))
  expect(await blobText(createObjectURL.mock.calls[0][0])).toContain('"panels","1","01","0","PASS"')
  h.client.saveEngineeringTotals.mockResolvedValueOnce(totalsResponse(2, reportValues))
  saveTotals(); await screen.findByText('Engineering totals saved.')
  expect(h.client.saveEngineeringTotals.mock.calls.at(-1)[0]).toMatchObject({ expectedRevision: 1, drafts: reportValues })
})

it.each([0, 1, 2, 3, 4])('retains conservative dirty feedback after pending field %i is edited and restored exactly', async index => {
  const h = fixture(); canonicalReports(h); read(); await reportsLoaded(); fill(reportValues)
  let settle; h.client.saveEngineeringTotals.mockReturnValueOnce(new Promise(resolve => { settle = resolve }))
  saveTotals(); const input = screen.getAllByRole('textbox')[index]
  fireEvent.change(input, { target: { value: index === 4 ? '0.5' : '2' } })
  fireEvent.change(input, { target: { value: reportValues[index] } })
  await act(async () => settle(totalsResponse(1, reportValues)))
  expect(inputValues()).toEqual(reportValues)
  expect(screen.getByText('Submitted engineering totals saved. Newer edits remain unsaved.')).toBeTruthy()
  h.client.saveEngineeringTotals.mockResolvedValueOnce(totalsResponse(2, reportValues))
  saveTotals(); await screen.findByText('Engineering totals saved.')
  expect(h.client.saveEngineeringTotals.mock.calls.at(-1)[0]).toMatchObject({ expectedRevision: 1, drafts: reportValues })
})

it('contains a synchronous Reload throw, retains dirty values and releases its guard', async () => {
  const h = fixture(); canonicalReports(h, { response: totalsResponse(4, reportValues) })
  read(); await reportsLoaded()
  const dirty = ['2', '0', '1', '1', '0.5']; fill(dirty)
  h.client.loadEngineeringTotals.mockImplementationOnce(() => { throw new Error('PRIVATE_SYNC_RELOAD') })
  reloadTotals()
  expect(screen.getByText(/Engineering totals could not be loaded/)).toBeTruthy()
  expect(screen.queryByText(/PRIVATE_SYNC_RELOAD/)).toBeNull()
  expect(inputValues()).toEqual(dirty)
  expect(screen.getByRole('button', { name: 'Reload saved engineering totals' }).disabled).toBe(false)
  const fresh = ['3', '0', '1', '1', '0.6']
  h.client.loadEngineeringTotals.mockResolvedValueOnce(totalsResponse(5, fresh))
  reloadTotals(); await reportsLoaded()
  expect(inputValues()).toEqual(fresh)
  h.client.saveEngineeringTotals.mockResolvedValueOnce(totalsResponse(6, fresh))
  saveTotals(); await screen.findByText('Engineering totals saved.')
  expect(h.client.saveEngineeringTotals.mock.calls[0][0].expectedRevision).toBe(5)
})

it('keeps the accepted Reload retry values, CSV and CAS baseline after old timeout completion', async () => {
  const h = fixture(); canonicalReports(h, { response: totalsResponse(1, reportValues) })
  read(); await reportsLoaded()
  let settle
  h.client.loadEngineeringTotals.mockReturnValueOnce(new Promise(resolve => { settle = resolve }))
  vi.useFakeTimers(); reloadTotals()
  const old = h.client.loadEngineeringTotals.mock.calls[1][0]
  act(() => vi.advanceTimersByTime(RECONCILIATION_READ_TIMEOUT_MS)); vi.useRealTimers()
  expect(old.signal.aborted).toBe(true)
  const fresh = ['3', '0', '1', '1', '0.6']
  h.client.loadEngineeringTotals.mockResolvedValueOnce(totalsResponse(3, fresh))
  reloadTotals(); await reportsLoaded()
  await act(async () => settle(totalsResponse(99, ['9', '9', '9', '9', '9'])))
  expect(inputValues()).toEqual(fresh)
  expect(screen.getByText('Saved engineering totals loaded.')).toBeTruthy()
  const createObjectURL = vi.fn(() => 'blob:retry'); vi.stubGlobal('URL', { createObjectURL, revokeObjectURL: vi.fn() })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  fireEvent.click(screen.getByRole('button', { name: 'Download CSV' }))
  const text = await blobText(createObjectURL.mock.calls[0][0])
  expect(text.includes('"panels","1","3","-2","FAIL"')).toBe(true)
  expect(text.includes('99')).toBe(false)
  h.client.saveEngineeringTotals.mockResolvedValueOnce(totalsResponse(4, fresh))
  saveTotals(); await screen.findByText('Engineering totals saved.')
  expect(h.client.saveEngineeringTotals.mock.calls[0][0].expectedRevision).toBe(3)
})

it.each([0, 1, 2, 3, 4])('preserves field %i edited during a deferred Reload with explicit conflict', async index => {
  const h = fixture(); canonicalReports(h, { response: totalsResponse(1, reportValues) })
  read(); await reportsLoaded(); fill(reportValues)
  let settle
  h.client.loadEngineeringTotals.mockReturnValueOnce(new Promise(resolve => { settle = resolve }))
  reloadTotals()
  const input = screen.getAllByRole('textbox')[index]
  const changed = [...reportValues]; changed[index] = index === 4 ? '1.2' : '2'
  expect(input.disabled).toBe(false)
  fireEvent.change(input, { target: { value: changed[index] } })
  const newer = ['3', '0', '1', '1', '0.6']
  await act(async () => settle(totalsResponse(2, newer)))
  expect(inputValues()).toEqual(changed)
  expect(screen.getByText(/Engineering totals changed/)).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Save engineering totals' }).disabled).toBe(true)
  h.client.loadEngineeringTotals.mockResolvedValueOnce(totalsResponse(2, newer))
  reloadTotals(); await reportsLoaded()
  expect(inputValues()).toEqual(newer)
})

it.each(['before-read', 'after-read'])('rejects an A-B-A old report GET that settles %s', async order => {
  const h = fixture({ strict: true }); canonicalReports(h, { response: totalsResponse(1, reportValues) })
  read(); await reportsLoaded()
  let settle
  h.client.loadEngineeringTotals.mockReturnValueOnce(new Promise(resolve => { settle = resolve }))
  reloadTotals()
  const old = h.client.loadEngineeringTotals.mock.calls[1][0]
  const b = { ...binding, drawing_id: '55555555-5555-4555-8555-555555555555' }
  h.snapshot = { binding: b, authorityKey: 'b' }; h.update({ solarBinding: b, authorityKey: 'b' })
  h.snapshot = { binding, authorityKey: 'a' }; h.update({ solarBinding: binding, authorityKey: 'a' })
  expect(old.signal.aborted).toBe(true)
  const late = () => act(async () => settle(totalsResponse(99, ['9', '9', '9', '9', '9'])))
  if (order === 'before-read') await late()
  const fresh = ['3', '0', '1', '1', '0.6']
  h.client.loadEngineeringTotals.mockResolvedValueOnce(totalsResponse(3, fresh))
  read(); await reportsLoaded()
  if (order === 'after-read') await late()
  expect(inputValues()).toEqual(fresh)
  h.client.saveEngineeringTotals.mockResolvedValueOnce(totalsResponse(4, fresh))
  saveTotals(); await screen.findByText('Engineering totals saved.')
  expect(h.client.saveEngineeringTotals.mock.calls[0][0].expectedRevision).toBe(3)
})

it('aborts explicit Reload and clears its timer on unmount before a fresh panel accepts data', async () => {
  const h = fixture(); canonicalReports(h)
  read(); await reportsLoaded()
  let settle
  h.client.loadEngineeringTotals.mockReturnValueOnce(new Promise(resolve => { settle = resolve }))
  vi.useFakeTimers(); reloadTotals()
  const old = h.client.loadEngineeringTotals.mock.calls[1][0]
  expect(vi.getTimerCount()).toBe(1)
  h.view.unmount(); expect(old.signal.aborted).toBe(true); expect(vi.getTimerCount()).toBe(0)
  vi.useRealTimers()
  const fresh = fixture(); canonicalReports(fresh, { response: totalsResponse(3, reportValues) })
  read(); await reportsLoaded(); fill(['4', '0', '1', '1', '0.7'])
  await act(async () => settle(totalsResponse(99, ['9', '9', '9', '9', '9'])))
  expect(inputValues()).toEqual(['4', '0', '1', '1', '0.7'])
  saveTotals(); await screen.findByText('Engineering totals saved.')
  expect(fresh.client.saveEngineeringTotals.mock.calls[0][0].expectedRevision).toBe(3)
})

it('guards two Reload activations before React commits and releases after rejection', async () => {
  const h = fixture(); canonicalReports(h)
  read(); await reportsLoaded(); fill(reportValues)
  let reject
  h.client.loadEngineeringTotals.mockImplementationOnce(() => new Promise((_yes, no) => { reject = no }))
  const button = screen.getByRole('button', { name: 'Reload saved engineering totals' })
  act(() => { button.click(); button.click() })
  expect(h.client.loadEngineeringTotals).toHaveBeenCalledTimes(2)
  await act(async () => reject(new Error('private report GET failure')))
  expect(inputValues()).toEqual(reportValues)
  expect(screen.getByText(/Engineering totals could not be loaded/)).toBeTruthy()
  expect(button.disabled).toBe(false)
  reloadTotals(); await reportsLoaded()
  expect(h.client.loadEngineeringTotals).toHaveBeenCalledTimes(3)
})

it.each(['completion-first', 'timeout-first'])('settles the Reload deadline coherently (%s)', async order => {
  const h = fixture(); canonicalReports(h, { response: totalsResponse(1, reportValues) })
  read(); await reportsLoaded()
  const newer = ['2', '0', '1', '1', '0.5']
  let settle
  h.client.loadEngineeringTotals.mockReturnValueOnce(new Promise(resolve => { settle = resolve }))
  vi.useFakeTimers()
  const complete = () => settle(totalsResponse(2, newer))
  if (order === 'completion-first') setTimeout(complete, RECONCILIATION_READ_TIMEOUT_MS)
  reloadTotals()
  if (order === 'timeout-first') setTimeout(complete, RECONCILIATION_READ_TIMEOUT_MS)
  const pending = h.client.loadEngineeringTotals.mock.calls[1][0]
  await act(async () => { await vi.advanceTimersByTimeAsync(RECONCILIATION_READ_TIMEOUT_MS) })
  expect(vi.getTimerCount()).toBe(0)
  const revision = order === 'completion-first' ? 2 : 1
  expect(pending.signal.aborted).toBe(order === 'timeout-first')
  expect(inputValues()).toEqual(order === 'completion-first' ? newer : reportValues)
  expect(screen.queryByText(/Engineering totals could not be loaded/) !== null).toBe(order === 'timeout-first')
  vi.useRealTimers()
  const drafts = ['3', '0', '1', '1', '0.6']; fill(drafts)
  h.client.saveEngineeringTotals.mockResolvedValueOnce(totalsResponse(revision + 1, drafts))
  saveTotals(); await screen.findByText('Engineering totals saved.')
  expect(h.client.saveEngineeringTotals.mock.calls[0][0].expectedRevision).toBe(revision)
})

it('retains dirty comparisons through read-only report loads and recovers on writable reload', async () => {
  const h = fixture(); canonicalReports(h, { response: totalsResponse(1, reportValues) })
  read(); await reportsLoaded()
  const dirty = ['2', '0', '1', '1', '0.5']; fill(dirty)
  h.client.loadEngineeringTotals.mockResolvedValueOnce(totalsResponse(1, reportValues, false))
  read(); await screen.findByText(/Engineering totals are read-only/)
  expect(inputValues()).toEqual(dirty)
  expect(screen.getByRole('button', { name: 'Save engineering totals' }).disabled).toBe(true)
  const createObjectURL = vi.fn(() => 'blob:read-only'); vi.stubGlobal('URL', { createObjectURL, revokeObjectURL: vi.fn() })
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  fireEvent.click(screen.getByRole('button', { name: 'Download CSV' })); expect(click).toHaveBeenCalledOnce()
  expect((await blobText(createObjectURL.mock.calls[0][0])).includes('"panels","1","2","-1","FAIL"')).toBe(true)
  h.client.loadEngineeringTotals.mockResolvedValueOnce(totalsResponse(1, dirty, true))
  reloadTotals(); await reportsLoaded()
  expect(inputValues()).toEqual(dirty)
  expect(screen.getByRole('button', { name: 'Save engineering totals' }).disabled).toBe(false)
})

it('retains dirty values when an absent record is explicitly reloaded after conflict', async () => {
  const h = fixture(); canonicalReports(h, { response: totalsResponse(2, reportValues) })
  read(); await reportsLoaded()
  const dirty = ['4', '0', '1', '1', '0.7']; fill(dirty)
  h.client.saveEngineeringTotals.mockResolvedValueOnce({ ok: false, status: 409 })
  saveTotals(); await screen.findByText(/Engineering totals changed/)
  h.client.loadEngineeringTotals.mockResolvedValueOnce(totalsResponse(0, null))
  reloadTotals(); await reportsLoaded()
  expect(inputValues()).toEqual(dirty)
  h.client.saveEngineeringTotals.mockResolvedValueOnce(totalsResponse(1, dirty))
  saveTotals(); await screen.findByText('Engineering totals saved.')
  expect(h.client.saveEngineeringTotals.mock.calls[1][0]).toMatchObject({ expectedRevision: 0, drafts: dirty })
})

it('owns accepted loaded report values against later producer mutation', async () => {
  const h = fixture(); canonicalReports(h)
  const returned = totalsResponse(4, reportValues)
  h.client.loadEngineeringTotals.mockResolvedValueOnce(returned)
  read(); await reportsLoaded()
  returned.data.record.values.fill('999')
  returned.data.record.binding.project_id = '99999999-9999-4999-8999-999999999999'
  returned.data.revision = 99
  expect(inputValues()).toEqual(reportValues)
  const createObjectURL = vi.fn(() => 'blob:owned'); vi.stubGlobal('URL', { createObjectURL, revokeObjectURL: vi.fn() })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  fireEvent.click(screen.getByRole('button', { name: 'Download CSV' }))
  expect((await blobText(createObjectURL.mock.calls[0][0])).includes('999')).toBe(false)
  h.client.saveEngineeringTotals.mockResolvedValueOnce(totalsResponse(5, reportValues))
  saveTotals(); await screen.findByText('Engineering totals saved.')
  expect(h.client.saveEngineeringTotals.mock.calls[0][0]).toMatchObject({ expectedRevision: 4, drafts: reportValues })
})

it.each(['before-read', 'after-read'])('rejects the old A Save after A-B-A when receipt settles %s', async order => {
  const h = fixture({ strict: true }); canonicalReports(h)
  read(); await reportsLoaded(); fill(reportValues)
  let settle
  h.client.saveEngineeringTotals.mockImplementationOnce(() => new Promise(resolve => { settle = resolve }))
  saveTotals()
  const old = h.client.saveEngineeringTotals.mock.calls[0][0]
  const b = { ...binding, drawing_id: '55555555-5555-4555-8555-555555555555' }
  h.snapshot = { binding: b, authorityKey: 'b' }; h.update({ solarBinding: b, authorityKey: 'b' })
  h.snapshot = { binding, authorityKey: 'a' }; h.update({ solarBinding: binding, authorityKey: 'a' })
  expect(old.signal.aborted).toBe(true)
  fill(['2', '0', '1', '1', '0.5']); saveTotals()
  expect(h.client.saveEngineeringTotals).toHaveBeenCalledOnce()
  const late = () => act(async () => settle(totalsResponse(99, reportValues)))
  if (order === 'before-read') await late()
  read(); await reportsLoaded()
  if (order === 'after-read') await late()
  expect(inputValues()).toEqual(['2', '0', '1', '1', '0.5'])
  expect(screen.queryByText('Engineering totals saved.')).toBeNull()
  saveTotals(); await screen.findByText('Engineering totals saved.')
  expect(h.client.saveEngineeringTotals.mock.calls[1][0]).toMatchObject({ expectedRevision: 0, drafts: ['2', '0', '1', '1', '0.5'] })
})

it('aborts a save and clears its timer on unmount without touching a remounted panel', async () => {
  const old = fixture(); canonicalReports(old)
  read(); await reportsLoaded(); fill(reportValues)
  let settle
  old.client.saveEngineeringTotals.mockImplementationOnce(() => new Promise(resolve => { settle = resolve }))
  vi.useFakeTimers(); saveTotals()
  const signal = old.client.saveEngineeringTotals.mock.calls[0][0].signal
  expect(vi.getTimerCount()).toBe(1)
  old.view.unmount()
  expect(signal.aborted).toBe(true); expect(vi.getTimerCount()).toBe(0)
  vi.useRealTimers()
  const fresh = fixture(); canonicalReports(fresh)
  read(); await reportsLoaded(); fill(['3', '0', '1', '1', '0.7'])
  await act(async () => settle(totalsResponse(77, reportValues)))
  expect(inputValues()).toEqual(['3', '0', '1', '1', '0.7'])
  expect(screen.queryByText('Engineering totals saved.')).toBeNull()
  saveTotals(); await screen.findByText('Engineering totals saved.')
  expect(fresh.client.saveEngineeringTotals.mock.calls[0][0].expectedRevision).toBe(0)
})

it.each(['completion-first', 'timeout-first'])('settles an exact Save deadline coherently (%s)', async order => {
  const h = fixture(); canonicalReports(h)
  read(); await reportsLoaded(); fill(reportValues)
  let settle
  const completed = new Promise(resolve => { settle = resolve })
  h.client.saveEngineeringTotals.mockReturnValueOnce(completed)
  vi.useFakeTimers()
  const complete = () => settle(totalsResponse(1, reportValues))
  if (order === 'completion-first') setTimeout(complete, RECONCILIATION_READ_TIMEOUT_MS)
  saveTotals()
  if (order === 'timeout-first') setTimeout(complete, RECONCILIATION_READ_TIMEOUT_MS)
  const first = h.client.saveEngineeringTotals.mock.calls[0][0]
  await act(async () => { await vi.advanceTimersByTimeAsync(RECONCILIATION_READ_TIMEOUT_MS) })
  expect(vi.getTimerCount()).toBe(0)
  expect(inputValues()).toEqual(reportValues)
  if (order === 'completion-first') {
    expect(first.signal.aborted).toBe(false)
    expect(screen.getByText('Engineering totals saved.')).toBeTruthy()
    expect(screen.queryByText(/could not be saved/)).toBeNull()
  } else {
    expect(first.signal.aborted).toBe(true)
    expect(screen.getByText(/Engineering totals could not be saved/)).toBeTruthy()
    expect(screen.queryByText('Engineering totals saved.')).toBeNull()
  }
  vi.useRealTimers()
  const revision = order === 'completion-first' ? 1 : 0
  fill(['4', '0', '1', '1', '0.8'])
  h.client.saveEngineeringTotals.mockResolvedValueOnce(totalsResponse(revision + 1, ['4', '0', '1', '1', '0.8']))
  saveTotals(); await screen.findByText('Engineering totals saved.')
  expect(h.client.saveEngineeringTotals.mock.calls[1][0].expectedRevision).toBe(revision)
})

it.each(['organization_id', 'project_id', 'drawing_id', 'input_version_id', 'hash', 'revision-negative', 'revision-zero', 'revision-jump', 'revision-bool', 'revision-fraction'])('rejects Save receipt %s without advancing CAS or losing dirty drafts', async field => {
  const h = fixture(); canonicalReports(h)
  read(); await reportsLoaded(); fill(reportValues)
  const bad = totalsResponse(1, reportValues)
  if (field.endsWith('_id')) bad.data.record.binding[field] = '99999999-9999-4999-8999-999999999999'
  else if (field === 'hash') bad.data.record.graph_sha256 = 'b'.repeat(64)
  else bad.data.revision = { 'revision-negative': -1, 'revision-zero': 0, 'revision-jump': 2, 'revision-bool': true, 'revision-fraction': 1.5 }[field]
  h.client.saveEngineeringTotals.mockResolvedValueOnce(bad)
  saveTotals(); await screen.findByText(/Engineering totals could not be saved/)
  expect(screen.queryByText('Engineering totals saved.')).toBeNull()
  expect(inputValues()).toEqual(reportValues)
  const first = h.client.saveEngineeringTotals.mock.calls[0][0]
  saveTotals(); await screen.findByText('Engineering totals saved.')
  const retry = h.client.saveEngineeringTotals.mock.calls[1][0]
  expect(retry.expectedRevision).toBe(0)
  expect(retry.idempotencyKey).toBe(first.idempotencyKey)
})

it('accepts a newer explicit Read during Save and ignores the old receipt afterward', async () => {
  const h = fixture(); canonicalReports(h)
  read(); await reportsLoaded(); fill(reportValues)
  let settle
  h.client.saveEngineeringTotals.mockImplementationOnce(() => new Promise(resolve => { settle = resolve }))
  saveTotals()
  const old = h.client.saveEngineeringTotals.mock.calls[0][0]
  const fresh = response(); fresh.data.intake.solar_design_graph.rev = 10; fresh.data.intake.solar_design_graph_sha256 = 'b'.repeat(64)
  h.client.readContext.mockResolvedValueOnce(fresh)
  const button = screen.getByRole('button', { name: 'Read saved design' })
  expect(button.disabled).toBe(false); read()
  await screen.findByText('Saved design revision 10.')
  await reportsLoaded()
  expect(old.signal.aborted).toBe(true)
  expect(inputValues()).toEqual(reportValues)
  await act(async () => settle(totalsResponse(1, reportValues)))
  expect(screen.getByText('Saved design revision 10.')).toBeTruthy()
  expect(screen.queryByText('Engineering totals saved.')).toBeNull()
  const newResult = totalsResponse(1, reportValues); newResult.data.record.graph_sha256 = 'b'.repeat(64)
  h.client.saveEngineeringTotals.mockResolvedValueOnce(newResult)
  saveTotals(); await screen.findByText('Engineering totals saved.')
  expect(h.client.saveEngineeringTotals.mock.calls[1][0]).toMatchObject({ graphSha256: 'b'.repeat(64), expectedRevision: 0 })
})

it('guards two save activations before React commits and releases the guard after rejection', async () => {
  const h = fixture(); canonicalReports(h)
  read(); await reportsLoaded(); fill(reportValues)
  let reject
  h.client.saveEngineeringTotals.mockImplementationOnce(() => new Promise((_yes, no) => { reject = no }))
  const button = screen.getByRole('button', { name: 'Save engineering totals' })
  act(() => { button.click(); button.click() })
  expect(h.client.saveEngineeringTotals).toHaveBeenCalledOnce()
  await act(async () => reject(new Error('private rejected save')))
  expect(screen.getByText(/Engineering totals could not be saved/)).toBeTruthy()
  expect(button.disabled).toBe(false)
  expect(inputValues()).toEqual(reportValues)
  saveTotals(); await screen.findByText('Engineering totals saved.')
  expect(h.client.saveEngineeringTotals).toHaveBeenCalledTimes(2)
})

it('ignores a timed-out save after a changed retry and keeps the retry CAS baseline', async () => {
  const h = fixture(); canonicalReports(h)
  read(); await reportsLoaded(); fill(reportValues)
  let settle
  h.client.saveEngineeringTotals.mockImplementationOnce(() => new Promise(resolve => { settle = resolve }))
  vi.useFakeTimers(); saveTotals()
  const first = h.client.saveEngineeringTotals.mock.calls[0][0]
  act(() => vi.advanceTimersByTime(RECONCILIATION_READ_TIMEOUT_MS)); vi.useRealTimers()
  expect(first.signal.aborted).toBe(true)
  const fresh = ['2', '0', '1', '1', '0.5']
  fill(fresh); saveTotals(); await screen.findByText('Engineering totals saved.')
  await act(async () => settle(totalsResponse(9, reportValues)))
  expect(inputValues()).toEqual(fresh)
  expect(screen.getByText('Engineering totals saved.')).toBeTruthy()
  fill(['3', '0', '1', '1', '0.6']); saveTotals(); await screen.findByText('Engineering totals saved.')
  expect(h.client.saveEngineeringTotals.mock.calls[2][0].expectedRevision).toBe(1)
})

it.each([0, 1, 2, 3, 4])('acknowledges submitted values while preserving a newer dirty edit in field %i', async index => {
  const h = fixture(); canonicalReports(h)
  read(); await reportsLoaded(); fill(reportValues)
  let settle
  h.client.saveEngineeringTotals.mockImplementationOnce(() => new Promise(resolve => { settle = resolve }))
  saveTotals()
  const submitted = h.client.saveEngineeringTotals.mock.calls[0][0]
  const newer = [...reportValues]; newer[index] = index === 4 ? '1.2' : '2'
  const input = screen.getAllByRole('textbox')[index]
  expect(input.disabled).toBe(false)
  fireEvent.change(input, { target: { value: newer[index] } })
  await act(async () => settle(totalsResponse(1, submitted.drafts)))
  expect(submitted.drafts).toEqual(reportValues)
  expect(inputValues()).toEqual(newer)
  expect(screen.getByText(/Newer edits remain unsaved/)).toBeTruthy()
  h.client.saveEngineeringTotals.mockResolvedValueOnce(totalsResponse(2, newer))
  saveTotals(); await screen.findByText('Engineering totals saved.')
  expect(h.client.saveEngineeringTotals.mock.calls[1][0]).toMatchObject({ drafts: newer, expectedRevision: 1 })
})

it('requires a fresh authorized read after rejecting a revoked save receipt', async () => {
  const h = fixture(); canonicalReports(h)
  read(); await reportsLoaded(); fill(reportValues)
  let settle
  h.client.saveEngineeringTotals.mockImplementationOnce(() => new Promise(resolve => { settle = resolve }))
  saveTotals()
  const signal = h.client.saveEngineeringTotals.mock.calls[0][0].signal
  h.current = false; h.emit()
  await act(async () => settle(totalsResponse(99, reportValues)))
  expect(signal.aborted).toBe(true)
  expect(screen.queryByText('Engineering totals saved.')).toBeNull()
  h.current = true; h.snapshot = { binding, authorityKey: 'renewed' }; h.update({ authorityKey: 'renewed' })
  fill(reportValues); saveTotals()
  expect(h.client.saveEngineeringTotals).toHaveBeenCalledOnce()
  read(); await reportsLoaded(); saveTotals(); await screen.findByText('Engineering totals saved.')
  expect(h.client.readContext).toHaveBeenCalledTimes(2)
  expect(h.client.saveEngineeringTotals.mock.calls[1][0].expectedRevision).toBe(0)
})

it('contains a synchronous save throw, retains dirty inputs and retries explicitly', async () => {
  const h = fixture(); canonicalReports(h)
  read(); await reportsLoaded(); fill(reportValues)
  h.client.saveEngineeringTotals.mockImplementationOnce(() => { throw new Error('private synchronous save') })
  saveTotals()
  expect(screen.getByText(/Engineering totals could not be saved/)).toBeTruthy()
  expect(screen.queryByText(/private synchronous/)).toBeNull()
  expect(inputValues()).toEqual(reportValues)
  expect(screen.getByRole('button', { name: 'Save engineering totals' }).disabled).toBe(false)
  saveTotals(); await screen.findByText('Engineering totals saved.')
  expect(h.client.saveEngineeringTotals).toHaveBeenCalledTimes(2)
})

it.each(['', '0.000'])('explicitly persists exact report strings including DC %j across a new mount', async dc => {
  const h = fixture(), file = canonicalReports(h), values = [...reportValues.slice(0, 4), dc]
  expect(h.client.loadEngineeringTotals).not.toHaveBeenCalled()
  fill(values)
  expect(screen.getByRole('button', { name: 'Save engineering totals' }).disabled).toBe(true)
  read(); await reportsLoaded()
  expect(inputValues()).toEqual(values)
  expect(h.client.saveEngineeringTotals).not.toHaveBeenCalled()
  saveTotals(); await screen.findByText('Engineering totals saved.')
  const request = h.client.saveEngineeringTotals.mock.calls[0][0]
  expect(request).toMatchObject({ graphSha256: 'a'.repeat(64), drafts: values, expectedRevision: 0 })
  expect(typeof request.idempotencyKey).toBe('string')
  expect(request.idempotencyKey.length).toBeGreaterThan(0)
  expect(file.response.data.record.values).toEqual(values)
  expect(h.client.uploadSolarEdgePdf).not.toHaveBeenCalled()
  expect(h.client.submitRun).not.toHaveBeenCalled()
  h.view.unmount()
  const next = fixture(); canonicalReports(next, file)
  expect(inputValues()).toEqual(['', '', '', '', ''])
  read(); await reportsLoaded()
  expect(inputValues()).toEqual(values)
  expect(screen.getByText('Saved design revision 9.')).toBeTruthy()
  expect(screen.getByText(/not independently verified/)).toBeTruthy()
  expect(next.client.readContext).toHaveBeenCalledOnce()
  expect(next.client.loadEngineeringTotals).toHaveBeenCalledWith(expect.objectContaining({ graphSha256: 'a'.repeat(64) }))
})

it('retains initial entered drafts and late edits while establishing the first known baseline', async () => {
  const h = fixture(); canonicalReports(h)
  let settle
  h.client.loadEngineeringTotals.mockImplementationOnce(() => new Promise(resolve => { settle = resolve }))
  fill(reportValues); read(); await loaded()
  expect(screen.getByRole('button', { name: 'Save engineering totals' }).disabled).toBe(true)
  fill(['2', '0', '1', '1', ''])
  await act(async () => settle(totalsResponse(3, reportValues)))
  expect(inputValues()).toEqual(['2', '0', '1', '1', ''])
  expect(screen.getByText(/Saved engineering totals exist/)).toBeTruthy()
  saveTotals()
  await waitFor(() => expect(h.client.saveEngineeringTotals).toHaveBeenCalledOnce())
  expect(h.client.saveEngineeringTotals.mock.calls[0][0].expectedRevision).toBe(3)
})

it('keeps the dirty CAS baseline when a refresh finds a newer revision and requires explicit reload', async () => {
  const h = fixture(), file = canonicalReports(h, { response: totalsResponse(1, reportValues) })
  read(); await reportsLoaded(); fill(['2', '0', '1', '1', ''])
  file.response = totalsResponse(2, ['3', '0', '1', '1', ''])
  read(); await screen.findByText(/Engineering totals changed/)
  expect(inputValues()[0]).toBe('2')
  expect(screen.getByRole('button', { name: 'Save engineering totals' }).disabled).toBe(true)
  saveTotals(); expect(h.client.saveEngineeringTotals).not.toHaveBeenCalled()
  reloadTotals(); await reportsLoaded()
  expect(inputValues()[0]).toBe('3')
  fill(['4', '0', '1', '1', ''])
  saveTotals(); await screen.findByText('Engineering totals saved.')
  expect(h.client.saveEngineeringTotals.mock.calls[0][0].expectedRevision).toBe(2)
})

it('handles 409 without advancing or retrying the draft until explicit reload', async () => {
  const h = fixture(), file = canonicalReports(h)
  read(); await reportsLoaded(); fill(reportValues)
  file.response = totalsResponse(1, ['9', '0', '1', '1', ''])
  const canonical = structuredClone(file.response)
  saveTotals(); await screen.findByText(/Engineering totals changed/)
  expect(file.response).toEqual(canonical)
  expect(inputValues()).toEqual(reportValues)
  saveTotals(); expect(h.client.saveEngineeringTotals).toHaveBeenCalledOnce()
  expect(screen.getByRole('button', { name: 'Download CSV' }).disabled).toBe(false)
  reloadTotals(); await reportsLoaded()
  expect(inputValues()[0]).toBe('9')
  fill(reportValues); saveTotals(); await screen.findByText('Engineering totals saved.')
  expect(h.client.saveEngineeringTotals.mock.calls[1][0].expectedRevision).toBe(1)
})

it.each(['transport', 'timeout'])('retries an unchanged %s save with the same immutable intent', async failure => {
  const h = fixture(); canonicalReports(h)
  read(); await reportsLoaded(); fill(reportValues)
  let settle
  if (failure === 'timeout') {
    vi.useFakeTimers()
    h.client.saveEngineeringTotals.mockImplementationOnce(() => new Promise(resolve => { settle = resolve }))
  } else h.client.saveEngineeringTotals.mockRejectedValueOnce(new Error('private storage error'))
  saveTotals()
  if (failure === 'timeout') {
    expect(screen.getAllByRole('textbox').every(input => !input.disabled)).toBe(true)
    expect(screen.getByRole('button', { name: 'Read saved design' }).disabled).toBe(false)
    act(() => vi.advanceTimersByTime(RECONCILIATION_READ_TIMEOUT_MS))
    expect(h.client.saveEngineeringTotals.mock.calls[0][0].signal.aborted).toBe(true)
    vi.useRealTimers()
  }
  await screen.findByText(/Engineering totals could not be saved/)
  const first = h.client.saveEngineeringTotals.mock.calls[0][0]
  expect(Object.isFrozen(first.drafts)).toBe(true)
  expect(inputValues()).toEqual(reportValues)
  saveTotals(); await screen.findByText('Engineering totals saved.')
  const second = h.client.saveEngineeringTotals.mock.calls[1][0]
  expect(second.idempotencyKey).toBe(first.idempotencyKey)
  expect(second.drafts).toEqual(first.drafts)
  expect(second.expectedRevision).toBe(first.expectedRevision)
  if (settle) await act(async () => settle(totalsResponse(88, ['8', '0', '1', '1', ''])))
  expect(inputValues()).toEqual(reportValues)
  expect(screen.getByText('Engineering totals saved.')).toBeTruthy()
  h.client.saveEngineeringTotals.mockRejectedValueOnce(new Error('private failure'))
  fill(['2', '0', '1', '1', '']); saveTotals()
  await screen.findByText(/Engineering totals could not be saved/)
  expect(h.client.saveEngineeringTotals.mock.calls[2][0].idempotencyKey).not.toBe(first.idempotencyKey)
  fill(['3', '0', '1', '1', '']); saveTotals(); await screen.findByText('Engineering totals saved.')
  expect(h.client.saveEngineeringTotals.mock.calls[3][0].idempotencyKey)
    .not.toBe(h.client.saveEngineeringTotals.mock.calls[2][0].idempotencyKey)
  expect(screen.queryByText(/private/)).toBeNull()
})

it.each([false, true])('isolates a pending save across A-B-A scope changes (StrictMode %s)', async strict => {
  const h = fixture({ strict }); canonicalReports(h)
  read(); await reportsLoaded(); fill(reportValues)
  let settle
  h.client.saveEngineeringTotals.mockImplementationOnce(() => new Promise(resolve => { settle = resolve }))
  saveTotals()
  saveTotals(); reloadTotals()
  expect(h.client.saveEngineeringTotals).toHaveBeenCalledOnce()
  expect(h.client.readContext).toHaveBeenCalledOnce()
  expect(h.client.loadEngineeringTotals).toHaveBeenCalledOnce()
  const signal = h.client.saveEngineeringTotals.mock.calls[0][0].signal
  const other = { ...binding, drawing_id: '55555555-5555-4555-8555-555555555555' }
  h.snapshot = { binding: other, authorityKey: 'b' }
  h.update({ solarBinding: other, authorityKey: 'b' })
  h.snapshot = { binding, authorityKey: 'a' }
  h.update({ solarBinding: binding, authorityKey: 'a' })
  expect(signal.aborted).toBe(true)
  read(); await reportsLoaded()
  await act(async () => settle(totalsResponse(99, reportValues)))
  expect(inputValues()).toEqual(['', '', '', '', ''])
  expect(screen.queryByText('Engineering totals saved.')).toBeNull()
  expect(screen.getByRole('button', { name: 'Save engineering totals' }).disabled).toBe(true)
})

it.each(['observer', 'silent', 'unmount'])('rejects a pending save after %s authority revocation', async mode => {
  const h = fixture({ strict: true }); canonicalReports(h)
  read(); await reportsLoaded(); fill(reportValues)
  let settle
  h.client.saveEngineeringTotals.mockImplementationOnce(() => new Promise(resolve => { settle = resolve }))
  saveTotals()
  const signal = h.client.saveEngineeringTotals.mock.calls[0][0].signal
  if (mode === 'unmount') h.view.unmount()
  else { h.current = false; if (mode === 'observer') h.emit() }
  await act(async () => settle(totalsResponse(1, reportValues)))
  expect(screen.queryByText('Engineering totals saved.')).toBeNull()
  if (mode !== 'silent') expect(signal.aborted).toBe(true)
  if (mode === 'observer') expect(inputValues()).toEqual(['', '', '', '', ''])
})

it('aborts a separate report load across StrictMode A-B-A and ignores late saved values', async () => {
  const h = fixture({ strict: true }); canonicalReports(h)
  let settle
  h.client.loadEngineeringTotals.mockImplementationOnce(() => new Promise(resolve => { settle = resolve }))
  read(); await loaded()
  const signal = h.client.loadEngineeringTotals.mock.calls[0][0].signal
  h.snapshot = { binding, authorityKey: 'b' }; h.update({ authorityKey: 'b' })
  h.snapshot = { binding, authorityKey: 'a' }; h.update({ authorityKey: 'a' })
  expect(signal.aborted).toBe(true)
  read(); await reportsLoaded(); fill(reportValues)
  await act(async () => settle(totalsResponse(77, ['7', '0', '1', '1', ''])))
  expect(inputValues()).toEqual(reportValues)
  saveTotals(); await screen.findByText('Engineering totals saved.')
  expect(h.client.saveEngineeringTotals.mock.calls[0][0].expectedRevision).toBe(0)
})

it('loads read-only reports and never enables save', async () => {
  const h = fixture(); canonicalReports(h, { response: totalsResponse(4, reportValues, false) })
  read(); await screen.findByText(/Engineering totals are read-only/)
  expect(inputValues()).toEqual(reportValues)
  fill(['2', '0', '1', '1', ''])
  expect(screen.getByRole('button', { name: 'Save engineering totals' }).disabled).toBe(true)
  expect(screen.getByRole('button', { name: 'Download CSV' }).disabled).toBe(false)
  saveTotals(); expect(h.client.saveEngineeringTotals).not.toHaveBeenCalled()
})

it('preserves edits made during explicit reload and keeps a stale baseline blocked', async () => {
  const h = fixture(); canonicalReports(h, { response: totalsResponse(1, reportValues) })
  read(); await reportsLoaded(); fill(['2', '0', '1', '1', ''])
  let settle
  h.client.loadEngineeringTotals.mockImplementationOnce(() => new Promise(resolve => { settle = resolve }))
  reloadTotals()
  fill(['4', '0', '1', '1', ''])
  await act(async () => settle(totalsResponse(2, ['3', '0', '1', '1', ''])))
  expect(inputValues()[0]).toBe('4')
  expect(screen.getByRole('button', { name: 'Save engineering totals' }).disabled).toBe(true)
  expect(screen.getByText(/Engineering totals changed/)).toBeTruthy()
})

it('times out a report load without erasing comparison and ignores its late result after retry', async () => {
  const h = fixture(); canonicalReports(h)
  let settle
  h.client.loadEngineeringTotals.mockImplementationOnce(() => new Promise(resolve => { settle = resolve }))
  vi.useFakeTimers()
  read(); await act(async () => {})
  fill(reportValues)
  const signal = h.client.loadEngineeringTotals.mock.calls[0][0].signal
  act(() => vi.advanceTimersByTime(RECONCILIATION_READ_TIMEOUT_MS))
  vi.useRealTimers()
  expect(signal.aborted).toBe(true)
  expect(screen.getByText('Saved design revision 9.')).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Download CSV' }).disabled).toBe(false)
  reloadTotals(); await reportsLoaded()
  await act(async () => settle(totalsResponse(88, ['8', '0', '1', '1', ''])))
  expect(inputValues()).toEqual(reportValues)
  saveTotals(); await screen.findByText('Engineering totals saved.')
  expect(h.client.saveEngineeringTotals.mock.calls[0][0].expectedRevision).toBe(0)
})

it.each(['transport', 'values', 'binding', 'hash', 'provenance', 'revision'])('preserves valid comparison and CSV after %s report storage failure', async kind => {
  const h = fixture(); canonicalReports(h)
  const bad = totalsResponse(1, reportValues)
  if (kind === 'transport') h.client.loadEngineeringTotals.mockRejectedValueOnce(new Error('private storage failure'))
  else {
    if (kind === 'values') bad.data.record.values[0] = '1e0'
    if (kind === 'binding') bad.data.record.binding.project_id = binding.drawing_id
    if (kind === 'hash') bad.data.record.graph_sha256 = 'b'.repeat(64)
    if (kind === 'provenance') bad.data.record.provenance = 'verified'
    if (kind === 'revision') bad.data.revision = -1
    h.client.loadEngineeringTotals.mockResolvedValueOnce(bad)
  }
  fill(reportValues); read(); await screen.findByText(/Engineering totals could not be loaded/)
  expect(screen.getByText('Saved design revision 9.')).toBeTruthy()
  expect(inputValues()).toEqual(reportValues)
  expect(screen.getByRole('button', { name: 'Download CSV' }).disabled).toBe(false)
  expect(screen.getByRole('button', { name: 'Save engineering totals' }).disabled).toBe(true)
  const createObjectURL = vi.fn(() => 'blob:storage-failure')
  vi.stubGlobal('URL', { createObjectURL, revokeObjectURL: vi.fn() })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  fireEvent.click(screen.getByRole('button', { name: 'Download CSV' }))
  expect(await blobText(createObjectURL.mock.calls[0][0])).toContain('"panels","1","01","0","PASS"')
  expect(screen.queryByText(/private storage failure/)).toBeNull()
})

it.each(['binding', 'authority'])('rejects synchronous subscribe revocation of %s and owns each disposer', async revoked => {
  const h = fixture()
  read(); await loaded(); fill()
  const oldCallbacks = [], oldDispose = vi.fn(), freshDispose = vi.fn()
  const replacement = { ...h.client, readContext: vi.fn(async () => response()),
    subscribe: vi.fn(callback => {
      oldCallbacks.push(callback)
      h.snapshot = revoked === 'binding'
        ? { binding: { ...binding, drawing_id: '55555555-5555-4555-8555-555555555555' }, authorityKey: 'a' }
        : { binding, authorityKey: 'revoked' }
      callback() // Revocation arrives before subscribe returns its disposer.
      return oldDispose
    }) }
  const createObjectURL = vi.fn(() => 'blob:fresh'), revokeObjectURL = vi.fn()
  vi.stubGlobal('URL', { createObjectURL, revokeObjectURL })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  h.update({ projectClient: replacement })
  expect(replacement.subscribe).toHaveBeenCalledOnce()
  expect(oldDispose).not.toHaveBeenCalled()
  expect(h.listeners.size).toBe(0)
  expect(screen.getByText(/selected drawing scope is stale or unavailable/)).toBeTruthy()
  expect(screen.queryByText(/Saved design revision|^PASS$/)).toBeNull()
  expect(screen.getAllByRole('textbox').every(input => input.disabled && input.value === '')).toBe(true)
  expect(screen.getByRole('button', { name: 'Read saved design' }).disabled).toBe(true)
  expect(screen.getByRole('button', { name: 'Download CSV' }).disabled).toBe(true)
  read(); fireEvent.click(screen.getByRole('button', { name: 'Download CSV' }))
  expect(replacement.readContext).not.toHaveBeenCalled()
  expect(createObjectURL).not.toHaveBeenCalled()
  let settleFresh
  const fresh = { ...h.client,
    readContext: vi.fn(() => new Promise(resolve => { settleFresh = resolve })),
    subscribe: vi.fn(() => freshDispose) }
  h.snapshot = { binding, authorityKey: 'fresh' }
  h.update({ projectClient: fresh, authorityKey: 'fresh' })
  expect(oldDispose).toHaveBeenCalledOnce()
  expect(freshDispose).not.toHaveBeenCalled()
  read()
  const signal = fresh.readContext.mock.calls[0][0].signal
  act(() => oldCallbacks.forEach(callback => callback()))
  expect(signal.aborted).toBe(false)
  expect(screen.getByText('Reading saved design.')).toBeTruthy()
  await act(async () => settleFresh(response()))
  fill()
  const accepted = screen.getByRole('table').textContent
  act(() => oldCallbacks.forEach(callback => callback()))
  expect(screen.getByRole('table').textContent).toBe(accepted)
  expect(screen.getAllByText('PASS')).toHaveLength(4)
  fireEvent.click(screen.getByRole('button', { name: 'Download CSV' }))
  expect(createObjectURL).toHaveBeenCalledOnce()
  expect(revokeObjectURL).toHaveBeenCalledWith('blob:fresh')
  expect(fresh.readContext).toHaveBeenCalledOnce()
  expect(replacement.readContext).not.toHaveBeenCalled()
  h.view.unmount()
  expect(oldDispose).toHaveBeenCalledOnce()
  expect(freshDispose).toHaveBeenCalledOnce()
  act(() => oldCallbacks.forEach(callback => callback()))
  expect(oldDispose).toHaveBeenCalledOnce()
  expect(freshDispose).toHaveBeenCalledOnce()
})

it.each([
  ['remove', true, false, false], ['revoke', false, true, false], ['both', true, true, false],
  ['click and remove', true, false, true], ['click and revoke', false, true, true],
  ['click and both', true, true, true],
])('contains %s cleanup failures and retries with unchanged CSV bytes', async (_name, failRemove, failRevoke, failClick) => {
  const derive = vi.spyOn(reconciliation, 'savedReconciliationValues'), h = fixture()
  const drafts = ['01', '00', '01', '01', '0.400']
  read(); await loaded(); fill(drafts)
  const table = screen.getByRole('table').textContent, derivations = derive.mock.calls.length
  const events = [], anchors = [], removedAnchors = [], connectedAtClick = []
  let failing = true
  const createObjectURL = vi.fn(() => `blob:cleanup-${createObjectURL.mock.calls.length}`)
  const revokeObjectURL = vi.fn(url => {
    events.push(['revoke', url])
    if (failing && failRevoke) throw new Error('private revoke failure')
  })
  vi.stubGlobal('URL', { createObjectURL, revokeObjectURL })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function () {
    anchors.push(this)
    connectedAtClick.push(this.isConnected)
    if (failing && failClick) throw new Error('private click failure')
  })
  const nativeRemove = HTMLAnchorElement.prototype.remove
  const remove = vi.spyOn(HTMLAnchorElement.prototype, 'remove').mockImplementation(function () {
    removedAnchors.push(this)
    events.push(['remove', this.getAttribute('href')])
    if (failing && failRemove) throw new Error('private remove failure')
    nativeRemove.call(this)
  })
  const download = screen.getByRole('button', { name: 'Download CSV' })
  const expected = 'metric,studio,oracle,delta,verdict\r\n' +
    '"panels","1","01","0","PASS"\r\n"optimizers","","00","","UNREADABLE"\r\n' +
    '"inverters","1","01","0","PASS"\r\n"strings","1","01","0","PASS"\r\n' +
    '"kw_dc","0.4","0.400","0","PASS"\r\n'
  for (let attempt = 1; attempt <= 2; attempt++) {
    fireEvent.click(download)
    expect(events).toEqual(Array.from({ length: attempt }, (_, i) => [
      ['remove', `blob:cleanup-${i + 1}`], ['revoke', `blob:cleanup-${i + 1}`],
    ]).flat())
    expect(remove).toHaveBeenCalledTimes(attempt)
    expect(removedAnchors[attempt - 1]).toBe(anchors[attempt - 1])
    expect(revokeObjectURL).toHaveBeenCalledTimes(attempt)
    expect(await blobText(createObjectURL.mock.calls[attempt - 1][0])).toBe(expected)
    expect(screen.getByRole('table').textContent).toBe(table)
    expect(screen.getAllByRole('textbox').map(input => input.value)).toEqual(drafts)
    expect(download.disabled).toBe(false)
    if (failing && failClick) expect(screen.getByRole('alert').textContent).toMatch(/Try Download CSV again/)
    else expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByText(/private .* failure/)).toBeNull()
    failing = false
  }
  expect(new Set(anchors).size).toBe(2)
  expect(connectedAtClick).toEqual([false, false])
  expect(anchors.every(anchor => !anchor.isConnected)).toBe(true)
  expect(h.client.readContext).toHaveBeenCalledOnce()
  expect(derive).toHaveBeenCalledTimes(derivations)
})

it.each([0, 1, 2, 3, 4])('clears report %s after export and restores exact cached rows and lexemes', async index => {
  const derive = vi.spyOn(reconciliation, 'savedReconciliationValues'), h = fixture()
  const drafts = ['01', '00', '01', '01', '0.400']
  const createObjectURL = vi.fn(() => 'blob:report'), revokeObjectURL = vi.fn()
  vi.stubGlobal('URL', { createObjectURL, revokeObjectURL })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  read(); await loaded(); fill(drafts)
  expect(derive).toHaveBeenCalledOnce()
  const table = screen.getByRole('table').textContent
  const inputs = screen.getAllByRole('textbox')
  const guidance = inputs.map(input => input.getAttribute('aria-describedby'))
  const download = screen.getByRole('button', { name: 'Download CSV' })
  fireEvent.click(download)
  const baseline = await blobText(createObjectURL.mock.calls[0][0])
  expect(baseline).toBe('metric,studio,oracle,delta,verdict\r\n' +
    '"panels","1","01","0","PASS"\r\n"optimizers","","00","","UNREADABLE"\r\n' +
    '"inverters","1","01","0","PASS"\r\n"strings","1","01","0","PASS"\r\n' +
    '"kw_dc","0.4","0.400","0","PASS"\r\n')
  fireEvent.change(inputs[index], { target: { value: '' } })
  expect(download.disabled).toBe(index !== 4)
  fireEvent.click(download)
  expect(createObjectURL).toHaveBeenCalledTimes(index === 4 ? 2 : 1)
  if (index === 4) {
    const fresh = await blobText(createObjectURL.mock.calls[1][0])
    expect(fresh).toBe(baseline.replace('"kw_dc","0.4","0.400","0","PASS"',
      '"kw_dc","0.4","","","UNREADABLE"'))
  }
  expect(inputs.map(input => input.value)).toEqual(drafts.map((value, i) => i === index ? '' : value))
  expect(inputs.map(input => input.getAttribute('aria-describedby'))).toEqual(guidance)
  fireEvent.change(inputs[index], { target: { value: drafts[index] } })
  expect(screen.getByRole('table').textContent).toBe(table)
  expect(inputs.map(input => input.value)).toEqual(drafts)
  fireEvent.click(download)
  expect(await blobText(createObjectURL.mock.calls.at(-1)[0])).toBe(baseline)
  expect(revokeObjectURL).toHaveBeenCalledTimes(createObjectURL.mock.calls.length)
  expect(h.client.readContext).toHaveBeenCalledOnce()
  expect(derive).toHaveBeenCalledOnce()
})

it.each(['completion-first', 'timeout-first'])('settles an equal-deadline race coherently: %s', async order => {
  vi.useFakeTimers()
  const h = fixture()
  let resolveOld
  const old = new Promise(resolve => { resolveOld = resolve })
  const late = response(); late.data.intake.solar_design_graph.rev = 77
  const complete = () => resolveOld(late)
  // The product installs its timeout before calling the client. Register the
  // completion on either side of read(), at the exact same clock deadline.
  if (order === 'completion-first') setTimeout(complete, RECONCILIATION_READ_TIMEOUT_MS)
  h.client.readContext.mockReturnValueOnce(old)
  act(() => { read(); read() })
  if (order === 'timeout-first') setTimeout(complete, RECONCILIATION_READ_TIMEOUT_MS)
  const signal = h.client.readContext.mock.calls[0][0].signal
  expect(h.client.readContext).toHaveBeenCalledOnce()
  await act(async () => { await vi.advanceTimersByTimeAsync(RECONCILIATION_READ_TIMEOUT_MS) })
  if (order === 'completion-first') {
    expect(screen.getByText('Saved design revision 77.')).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(signal.aborted).toBe(false)
  } else {
    expect(screen.getByRole('alert').textContent).toBe('Saved design read timed out. Try reading it again.')
    expect(screen.queryByText(/Saved design revision/)).toBeNull()
    expect(signal.aborted).toBe(true)
    expect(screen.getByRole('button', { name: 'Download CSV' }).disabled).toBe(true)
  }
  expect(screen.getByRole('button', { name: 'Read saved design' }).disabled).toBe(false)
  expect(vi.getTimerCount()).toBe(0)
  await act(async () => { read(); read() })
  expect(screen.getByText('Saved design revision 9.')).toBeTruthy()
  expect(screen.queryByText('Saved design revision 77.')).toBeNull()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(h.client.readContext).toHaveBeenCalledTimes(2)
  expect(vi.getTimerCount()).toBe(0)
  h.view.unmount()
  expect(vi.getTimerCount()).toBe(0)
})

it.each(['binding', 'client'].flatMap(change => ['before', 'after'].map(phase => [change, phase])))(
  'rejects the first A read after A-to-B-to-A reuses %s scope inputs and settles %s the fresh read', async (change, phase) => {
  const h = fixture(), original = { ...h.props }
  let settleOld
  h.client.readContext.mockImplementationOnce(() => new Promise(resolve => { settleOld = resolve }))
  fill(['77', '77', '77', '77', '77'])
  read()
  const oldSignal = h.client.readContext.mock.calls[0][0].signal
  const bBinding = change === 'binding' ? { ...binding, drawing_id: '55555555-5555-4555-8555-555555555555' } : binding
  const bResponse = response()
  bResponse.data.context = { ...bBinding }
  bResponse.data.intake.solar_design_graph.rev = 20
  const bClient = change === 'client' ? { ...h.client, readContext: vi.fn(async () => bResponse) } : h.client
  if (change === 'binding') h.client.readContext.mockResolvedValueOnce(bResponse)
  h.snapshot = { binding: bBinding, authorityKey: 'a' }
  h.update({ solarBinding: bBinding, projectClient: bClient })
  expect(oldSignal.aborted).toBe(true)
  read(); await screen.findByText('Saved design revision 20.')
  fill(['20', '20', '20', '20', '20'])
  h.snapshot = { binding, authorityKey: 'a' }
  h.update(original)
  expect(screen.queryByText(/Saved design revision/)).toBeNull()
  expect(screen.getAllByRole('textbox').map(input => input.value)).toEqual(['', '', '', '', ''])
  expect(h.client.readContext).toHaveBeenCalledTimes(change === 'binding' ? 2 : 1)
  const createObjectURL = vi.fn(() => 'blob:returned-a'), revokeObjectURL = vi.fn()
  vi.stubGlobal('URL', { createObjectURL, revokeObjectURL })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  const download = () => fireEvent.click(screen.getByRole('button', { name: 'Download CSV' }))
  download(); expect(createObjectURL).not.toHaveBeenCalled()
  const settleFirstA = async () => act(async () => {
    const old = response(); old.data.intake.solar_design_graph.rev = 77
    settleOld(old)
  })
  if (phase === 'before') {
    await settleFirstA()
    expect(screen.queryByText(/Saved design revision|PASS/)).toBeNull()
    expect(screen.getAllByRole('textbox').map(input => input.value)).toEqual(['', '', '', '', ''])
    expect(screen.getByRole('button', { name: 'Download CSV' }).disabled).toBe(true)
    expect(h.client.readContext).toHaveBeenCalledTimes(change === 'binding' ? 2 : 1)
    download(); expect(createObjectURL).not.toHaveBeenCalled()
  }
  const fresh = response()
  fresh.data.intake.solar_design_graph.rev = 10
  fresh.data.intake.solar_design_graph.frames[0].module_power_watts = 800
  h.client.readContext.mockResolvedValueOnce(fresh)
  act(() => { read(); read() })
  await screen.findByText('Saved design revision 10.')
  expect(screen.getByRole('button', { name: 'Download CSV' }).disabled).toBe(true)
  fill(['1', '0', '1', '1', '0.8'])
  const accepted = screen.getByRole('table').textContent
  if (phase === 'after') await settleFirstA()
  expect(screen.queryByText('Saved design revision 77.')).toBeNull()
  expect(screen.getByText('Saved design revision 10.')).toBeTruthy()
  expect(screen.getByRole('table').textContent).toBe(accepted)
  expect(h.client.readContext).toHaveBeenCalledTimes(change === 'binding' ? 3 : 2)
  if (change === 'client') expect(bClient.readContext).toHaveBeenCalledOnce()
  expect(oldSignal.aborted).toBe(true)
  download()
  expect(await blobText(createObjectURL.mock.calls[0][0])).toBe(
    'metric,studio,oracle,delta,verdict\r\n"panels","1","1","0","PASS"\r\n' +
    '"optimizers","","0","","UNREADABLE"\r\n"inverters","1","1","0","PASS"\r\n' +
    '"strings","1","1","0","PASS"\r\n"kw_dc","0.8","0.8","0","PASS"\r\n')
  expect(createObjectURL).toHaveBeenCalledOnce()
  expect(revokeObjectURL.mock.calls).toEqual([['blob:returned-a']])
})

it.each(['silent', 'observer', 'throw'])('rejects synchronous authority revocation during cloning (%s) and recovers', async mode => {
  const h = fixture(), clone = globalThis.structuredClone
  const createObjectURL = vi.fn(() => 'blob:authorized'), revokeObjectURL = vi.fn()
  vi.stubGlobal('URL', { createObjectURL, revokeObjectURL })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  vi.stubGlobal('structuredClone', vi.fn(value => {
    h.current = false
    if (mode !== 'silent') for (const listener of h.listeners) listener()
    if (mode === 'throw') throw new Error('private clone exception token=synthetic-secret')
    return clone(value)
  }))
  fill(); read()
  await act(async () => {})
  expect(h.client.readContext.mock.calls[0][0].signal.aborted).toBe(mode !== 'silent')
  expect(screen.queryByText(/Saved design revision|private clone|synthetic-secret/)).toBeNull()
  expect(screen.getByRole('button', { name: 'Download CSV' }).disabled).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: 'Download CSV' }))
  expect(createObjectURL).not.toHaveBeenCalled()
  vi.stubGlobal('structuredClone', clone)
  h.current = true
  h.snapshot = { binding, authorityKey: 'fresh' }
  h.update({ authorityKey: 'fresh' })
  expect(screen.getAllByRole('textbox').map(input => input.value)).toEqual(['', '', '', '', ''])
  read(); await loaded()
  expect(screen.getByRole('button', { name: 'Download CSV' }).disabled).toBe(true)
  fill()
  fireEvent.click(screen.getByRole('button', { name: 'Download CSV' }))
  expect(createObjectURL).toHaveBeenCalledOnce()
  expect(await blobText(createObjectURL.mock.calls[0][0])).toContain('"kw_dc","0.4","0.4","0","PASS"')
  expect(h.client.readContext).toHaveBeenCalledTimes(2)
})

it('alternates export success and failure from one cached snapshot with exactly-once URL cleanup', async () => {
  const derive = vi.spyOn(reconciliation, 'savedReconciliationValues'), h = fixture()
  read(); await loaded(); fill()
  const derivations = derive.mock.calls.length
  const createObjectURL = vi.fn(() => `blob:attempt-${createObjectURL.mock.calls.length}`), revokeObjectURL = vi.fn()
  vi.stubGlobal('URL', { createObjectURL, revokeObjectURL })
  const anchors = [], successful = []
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function () {
    anchors.push(this)
    if (anchors.length % 2 === 0) throw new Error('private export failure')
    successful.push(createObjectURL.mock.calls.at(-1)[0])
  })
  for (let attempt = 1; attempt <= 5; attempt++) {
    fireEvent.click(screen.getByRole('button', { name: 'Download CSV' }))
    expect(revokeObjectURL.mock.calls).toEqual(Array.from({ length: attempt }, (_, i) => [`blob:attempt-${i + 1}`]))
    if (attempt % 2 === 0) expect(screen.getByRole('alert').textContent).toBe('CSV could not be downloaded. Try Download CSV again.')
    else expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByRole('button', { name: 'Download CSV' }).disabled).toBe(false)
  }
  expect(new Set(anchors).size).toBe(5)
  const bytes = await Promise.all(successful.map(blobText))
  expect(bytes).toHaveLength(3)
  expect(bytes.every(value => value === bytes[0])).toBe(true)
  expect(bytes[0]).toContain('"kw_dc","0.4","0.4","0","PASS"')
  expect(h.client.readContext).toHaveBeenCalledOnce()
  expect(derive).toHaveBeenCalledTimes(derivations)
  expect(screen.queryByText(/private export failure/)).toBeNull()
})

const metadataCases = [
  ...['organization_id', 'project_id', 'drawing_id', 'input_version_id'].flatMap(key =>
    ['mismatch', 'missing', 'number', 'null', 'object'].map(type => [`${key} ${type}`, value => {
      if (type === 'missing') delete value.data.context[key]
      else value.data.context[key] = { mismatch: '99999999-9999-4999-8999-999999999999', number: 1,
        null: null, object: { injected: 'private metadata' } }[type]
    }])),
  ...[undefined, null, '9', -1, 1.5, Number.MAX_SAFE_INTEGER + 1, NaN, Infinity].map(rev =>
    [`revision ${String(rev)}`, value => { value.data.intake.solar_design_graph.rev = rev }]),
  ...[undefined, null, 123, {}, '', 'a'.repeat(63), 'a'.repeat(65), 'g'.repeat(64), 'private metadata'].map(sha =>
    [`digest ${String(sha)}`, value => { value.data.intake.solar_design_graph_sha256 = sha }]),
]
it.each(metadataCases)('rejects %s metadata and recovers exact rows and drafts on a clean retry', async (_name, mutate) => {
  const h = fixture(), drafts = ['01', '00', '01', '01', '0.400']
  read(); await loaded(); fill(drafts)
  const baseline = screen.getByRole('table').textContent
  const bad = response(); mutate(bad)
  h.client.readContext.mockResolvedValueOnce(bad)
  read()
  expect((await screen.findByRole('alert')).textContent).toBe(
    'Saved design unavailable: the response, drawing identity or graph is invalid. Try reading it again.')
  expect(screen.queryByText(/Saved design revision/)).toBeNull()
  expect(screen.queryByText(/private metadata/)).toBeNull()
  expect(screen.queryByText('PASS')).toBeNull()
  expect(screen.getByRole('button', { name: 'Download CSV' }).disabled).toBe(true)
  expect(screen.getByRole('button', { name: 'Read saved design' }).disabled).toBe(false)
  expect(screen.getAllByRole('textbox').map(input => input.value)).toEqual(drafts)
  read(); await loaded()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.getByRole('table').textContent).toBe(baseline)
  expect(screen.getAllByRole('textbox').map(input => input.value)).toEqual(drafts)
  expect(screen.getByRole('button', { name: 'Download CSV' }).disabled).toBe(false)
})

it('isolates throwing disposers during StrictMode replacement and final pending unmount', async () => {
  const callbacks = [], live = new Set(), disposers = []
  const subscribe = listener => {
    callbacks.push(listener); live.add(listener)
    const dispose = vi.fn(() => { live.delete(listener); throw new Error('private disposer injection') })
    disposers.push(dispose)
    return dispose
  }
  const h = fixture({ strict: true, subscribe })
  expect(live.size).toBe(1)
  let resolveOld, resolveNew, resolveFinal
  h.client.readContext.mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
  read()
  const oldSignal = h.client.readContext.mock.calls[0][0].signal
  const oldCallbacks = [...callbacks]
  const replacement = { ...h.client, subscribe,
    readContext: vi.fn(() => new Promise(resolve => { resolveNew = resolve })) }
  h.update({ projectClient: replacement })
  expect(oldSignal.aborted).toBe(true)
  expect(live.size).toBe(1)
  read()
  const newSignal = replacement.readContext.mock.calls[0][0].signal
  await act(async () => {
    oldCallbacks.forEach(callback => callback())
    const old = response(); old.data.intake.solar_design_graph.rev = 77
    resolveOld(old)
  })
  expect(newSignal.aborted).toBe(false)
  expect(screen.getByText('Reading saved design.')).toBeTruthy()
  expect(screen.queryByText('Saved design revision 77.')).toBeNull()
  read(); expect(replacement.readContext).toHaveBeenCalledTimes(1)
  await act(async () => resolveNew(response()))
  fill()
  const createObjectURL = vi.fn(() => 'blob:replacement'), revokeObjectURL = vi.fn()
  vi.stubGlobal('URL', { createObjectURL, revokeObjectURL })
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  fireEvent.click(screen.getByRole('button', { name: 'Download CSV' }))
  expect(click).toHaveBeenCalledOnce()
  expect(revokeObjectURL).toHaveBeenCalledWith('blob:replacement')
  expect(screen.getAllByText('PASS')).toHaveLength(4)
  expect(screen.queryByText(/private disposer injection/)).toBeNull()
  replacement.readContext.mockImplementationOnce(() => new Promise(resolve => { resolveFinal = resolve }))
  read()
  const finalSignal = replacement.readContext.mock.calls[1][0].signal
  expect(() => h.view.unmount()).not.toThrow()
  expect(finalSignal.aborted).toBe(true)
  expect(live.size).toBe(0)
  disposers.forEach(dispose => expect(dispose).toHaveBeenCalledOnce())
  await act(async () => { callbacks.forEach(callback => callback()); resolveFinal(response()) })
  expect(screen.queryByRole('table')).toBeNull()
  expect(createObjectURL).toHaveBeenCalledOnce()
})

it.each(['resolve', 'reject'])('preserves drafts when a deferred read settles by %s while busy', async outcome => {
  const h = fixture(), drafts = ['01', '0', '1', '1', '0.400']
  fill(drafts)
  let resolve, reject
  h.client.readContext.mockImplementationOnce(() => new Promise((yes, no) => { resolve = yes; reject = no }))
  const createObjectURL = vi.fn(() => 'blob:busy'), revokeObjectURL = vi.fn()
  vi.stubGlobal('URL', { createObjectURL, revokeObjectURL })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  const download = () => fireEvent.click(screen.getByRole('button', { name: 'Download CSV' }))
  const assertDisabled = () => {
    expect(screen.getByRole('button', { name: 'Read saved design' }).disabled).toBe(true)
    expect(screen.getByRole('button', { name: 'Download CSV' }).disabled).toBe(true)
    expect(screen.getAllByRole('textbox').every(input => input.disabled)).toBe(true)
    fill(['9', '9', '9', '9', '9']); read(); download()
    expect(screen.getAllByRole('textbox').map(input => input.value)).toEqual(drafts)
    expect(h.client.readContext).toHaveBeenCalledTimes(1)
    expect(createObjectURL).not.toHaveBeenCalled()
  }
  read()
  const signal = h.client.readContext.mock.calls[0][0].signal
  h.update({ busy: true })
  assertDisabled()
  await act(async () => {
    if (outcome === 'resolve') resolve(response())
    else reject(new Error('private busy failure: token=synthetic-secret'))
  })
  assertDisabled()
  expect(signal.aborted).toBe(false)
  expect(screen.queryByText(/private busy failure|synthetic-secret/)).toBeNull()
  h.update({ busy: false })
  expect(screen.getAllByRole('textbox').every(input => !input.disabled)).toBe(true)
  expect(screen.getAllByRole('textbox').map(input => input.value)).toEqual(drafts)
  expect(screen.getByRole('button', { name: 'Read saved design' }).disabled).toBe(false)
  expect(h.client.readContext).toHaveBeenCalledTimes(1)
  if (outcome === 'reject') {
    expect(screen.getByRole('alert').textContent).toBe('Saved design could not be read. Try reading it again.')
    expect(screen.getByRole('button', { name: 'Download CSV' }).disabled).toBe(true)
    expect(screen.queryByText('PASS')).toBeNull()
    read(); await loaded()
    expect(h.client.readContext).toHaveBeenCalledTimes(2)
  } else expect(screen.getByText('Saved design revision 9.')).toBeTruthy()
  expect(screen.getAllByText('PASS')).toHaveLength(4)
  expect(screen.getByRole('button', { name: 'Download CSV' }).disabled).toBe(false)
  download()
  expect(createObjectURL).toHaveBeenCalledOnce()
  expect(revokeObjectURL).toHaveBeenCalledWith('blob:busy')
  const csv = await new Promise(resolveCsv => {
    const reader = new FileReader(); reader.onload = () => resolveCsv(reader.result)
    reader.readAsText(createObjectURL.mock.calls[0][0])
  })
  expect(csv).toContain('"panels","1","01","0","PASS"')
  expect(csv).toContain('"kw_dc","0.4","0.400","0","PASS"')
})

it('guards reentrant reads before React commits and releases ownership after success', async () => {
  const h = fixture()
  let resolve
  h.client.readContext.mockImplementationOnce(() => {
    read()
    return new Promise(yes => { resolve = yes })
  })
  act(() => { read(); read() })
  expect(h.client.readContext).toHaveBeenCalledTimes(1)
  await act(async () => resolve(response()))
  read(); await loaded()
  expect(h.client.readContext).toHaveBeenCalledTimes(2)
})

it.each(['timeout', 'failure'])('retries after %s and protects the new request from late old completion', async (failure) => {
  vi.useFakeTimers()
  const h = fixture(); fill()
  let resolveOld, rejectOld, resolveNew
  h.client.readContext.mockImplementationOnce(() => new Promise((yes, no) => { resolveOld = yes; rejectOld = no }))
  read()
  const oldSignal = h.client.readContext.mock.calls[0][0].signal
  if (failure === 'timeout') act(() => vi.advanceTimersByTime(RECONCILIATION_READ_TIMEOUT_MS))
  else await act(async () => rejectOld(new Error('private failure')))
  expect(screen.getByRole('alert').textContent).toMatch(/Try reading it again/)
  expect(oldSignal.aborted).toBe(failure === 'timeout')
  h.client.readContext.mockImplementationOnce(() => new Promise(yes => { resolveNew = yes }))
  read()
  await act(async () => resolveOld(response()))
  expect(screen.queryByText('Saved design revision 9.')).toBeNull()
  read()
  expect(h.client.readContext).toHaveBeenCalledTimes(2)
  await act(async () => resolveNew(response()))
  expect(screen.getAllByText('PASS')).toHaveLength(4)
  expect(screen.getAllByRole('textbox').map(input => input.value)).toEqual(['1', '0', '1', '1', '0.4'])
})

it('keeps one live StrictMode observer and aborts the final pending read on unmount', async () => {
  const h = fixture({ strict: true })
  expect(h.listeners.size).toBe(1)
  expect(screen.getByRole('button', { name: 'Read saved design' }).disabled).toBe(false)
  read(); await loaded(); fill()
  expect(screen.getByRole('button', { name: 'Download CSV' }).disabled).toBe(false)
  let resolve
  h.client.readContext.mockImplementationOnce(() => new Promise(yes => { resolve = yes }))
  read()
  const signal = h.client.readContext.mock.calls[1][0].signal
  h.view.unmount()
  expect(signal.aborted).toBe(true)
  expect(h.listeners.size).toBe(0)
  await act(async () => resolve(response()))
  expect(screen.queryByText('Saved design revision 9.')).toBeNull()
})

it.each(['throw', 'nonfunction'])('renders subscription %s failure closed and recovers with a replacement client', async (failure) => {
  const invalid = { dispose: vi.fn() }
  const subscribe = () => {
    if (failure === 'throw') throw new Error('private observer details')
    return invalid
  }
  const h = fixture({ subscribe })
  expect(screen.getByText(/saved design changes could not be observed/)).toBeTruthy()
  expect(screen.queryByText(/private observer details/)).toBeNull()
  expect(screen.getByRole('button', { name: 'Read saved design' }).disabled).toBe(true)
  expect(screen.getByRole('button', { name: 'Download CSV' }).disabled).toBe(true)
  expect(screen.getAllByRole('textbox').every(input => input.disabled)).toBe(true)
  read()
  expect(h.client.readContext).not.toHaveBeenCalled()
  const replacement = { ...h.client, subscribe: listener => {
    h.listeners.add(listener)
    return () => h.listeners.delete(listener)
  } }
  h.update({ projectClient: replacement })
  expect(h.listeners.size).toBe(1)
  read(); await loaded(); fill()
  expect(screen.getByRole('button', { name: 'Download CSV' }).disabled).toBe(false)
  expect(invalid.dispose).not.toHaveBeenCalled()
})

it('fails closed when a replacement subscription fails after an accepted read', async () => {
  const h = fixture(); read(); await loaded(); fill()
  h.update({ projectClient: { ...h.client, subscribe: () => { throw new Error('private replacement') } } })
  expect(h.listeners.size).toBe(0)
  expect(screen.getByText(/saved design changes could not be observed/)).toBeTruthy()
  expect(screen.queryByText(/private replacement/)).toBeNull()
  expect(screen.queryByText('PASS')).toBeNull()
  expect(screen.getByRole('button', { name: 'Read saved design' }).disabled).toBe(true)
  expect(screen.getByRole('button', { name: 'Download CSV' }).disabled).toBe(true)
})

it('ignores an old observer and pending completion after a new scope starts reading', async () => {
  const h = fixture({ strict: true })
  const oldObserver = [...h.listeners][0]
  let resolveOld, resolveNew
  h.client.readContext.mockImplementationOnce(() => new Promise(yes => { resolveOld = yes }))
  read()
  const oldSignal = h.client.readContext.mock.calls[0][0].signal
  const replacement = { ...h.client, readContext: vi.fn(() => new Promise(yes => { resolveNew = yes })) }
  h.update({ projectClient: replacement })
  expect(oldSignal.aborted).toBe(true)
  expect(h.listeners.size).toBe(1)
  read()
  const newSignal = replacement.readContext.mock.calls[0][0].signal
  act(() => oldObserver())
  await act(async () => resolveOld(response()))
  expect(newSignal.aborted).toBe(false)
  expect(screen.queryByText('Saved design revision 9.')).toBeNull()
  await act(async () => resolveNew(response()))
  fill()
  expect(screen.getAllByText('PASS')).toHaveLength(4)
  h.view.unmount()
  expect(h.listeners.size).toBe(0)
})

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

it.each(['\u0661', '\uff11', '1,0', '1\u200b', '=1+1', '+SUM(1)', '@SUM(1)', '-1+2'])(
  'rejects pasted lexeme %s in every report field and repairs only that field', async token => {
    fixture(); read(); await loaded()
    const drafts = ['01', '00', '001', '01', '0.400']
    fill(drafts)
    const inputs = screen.getAllByRole('textbox')
    const guidance = inputs.map(input => [input.id, input.getAttribute('aria-describedby')])
    const download = screen.getByRole('button', { name: 'Download CSV' })
    const createObjectURL = vi.fn(() => 'blob:lexemes')
    vi.stubGlobal('URL', { createObjectURL, revokeObjectURL: vi.fn() })
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    for (const [index, input] of inputs.entries()) {
      // A paste delivers the whole lexeme in one input change, without key filtering.
      fireEvent.paste(input, { clipboardData: { getData: () => token } })
      fireEvent.change(input, { target: { value: token } })
      expect(download.disabled).toBe(true)
      fireEvent.click(download)
      expect(createObjectURL).not.toHaveBeenCalled()
      inputs.forEach((field, sibling) => {
        expect(field.value).toBe(sibling === index ? token : drafts[sibling])
        expect(field.getAttribute('aria-invalid')).toBe(sibling === index ? 'true' : 'false')
        expect([field.id, field.getAttribute('aria-describedby')]).toEqual(guidance[sibling])
        field.getAttribute('aria-describedby').split(' ').forEach(id => expect(document.getElementById(id)?.textContent).toBeTruthy())
      })
      fireEvent.change(input, { target: { value: drafts[index] } })
      expect(input.getAttribute('aria-invalid')).toBe('false')
      expect(download.disabled).toBe(false)
      expect(inputs.map(field => field.value)).toEqual(drafts)
    }
    fireEvent.click(download)
    const csv = await new Promise(resolve => {
      const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.readAsText(createObjectURL.mock.calls[0][0])
    })
    expect(csv).toBe('metric,studio,oracle,delta,verdict\r\n"panels","1","01","0","PASS"\r\n' +
      '"optimizers","","00","","UNREADABLE"\r\n"inverters","1","001","0","PASS"\r\n' +
      '"strings","1","01","0","PASS"\r\n"kw_dc","0.4","0.400","0","PASS"\r\n')
  })

it('revokes authority during URL allocation and requires fresh evidence and drafts in the replacement scope', async () => {
  const h = fixture(); read(); await loaded(); fill()
  const revokeObjectURL = vi.fn(() => { throw new Error('private authority cleanup token=synthetic-secret') })
  const createObjectURL = vi.fn(() => {
    h.snapshot = { binding, authorityKey: 'b' }
    return 'blob:revoked'
  })
  vi.stubGlobal('URL', { createObjectURL, revokeObjectURL })
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  fireEvent.click(screen.getByRole('button', { name: 'Download CSV' }))
  expect(click).not.toHaveBeenCalled()
  expect(revokeObjectURL.mock.calls).toEqual([['blob:revoked']])
  h.emit()
  expect(screen.queryByText(/private authority|synthetic-secret/)).toBeNull()
  expect(screen.queryByText('PASS')).toBeNull()
  const replacement = { ...h.client, readContext: vi.fn(async () => response()) }
  h.update({ authorityKey: 'b', projectClient: replacement })
  const download = screen.getByRole('button', { name: 'Download CSV' })
  expect(replacement.readContext).not.toHaveBeenCalled()
  expect(screen.getAllByRole('textbox').map(input => input.value)).toEqual(['', '', '', '', ''])
  expect(download.disabled).toBe(true)
  read(); await loaded()
  expect(download.disabled).toBe(true)
  expect(screen.queryByText('PASS')).toBeNull()
  fill(['01', '0', '1', '1', '0.400'])
  createObjectURL.mockImplementationOnce(() => 'blob:fresh')
  revokeObjectURL.mockImplementation(() => {})
  fireEvent.click(download)
  expect(click).toHaveBeenCalledOnce()
  expect(revokeObjectURL.mock.calls).toEqual([['blob:revoked'], ['blob:fresh']])
  const csv = await new Promise(resolve => {
    const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.readAsText(createObjectURL.mock.calls[1][0])
  })
  expect(csv).toContain('"panels","1","01","0","PASS"')
  expect(csv).toContain('"kw_dc","0.4","0.400","0","PASS"')
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
  expect((await screen.findByRole('alert')).textContent).toBe('Saved design could not be read. Try reading it again.')
  expect(screen.queryByText(/private snapshot/)).toBeNull()
  expect(screen.queryByText('PASS')).toBeNull()
  expect(screen.queryByText('Saved design revision 9.')).toBeNull()
  expect(screen.getByRole('button', { name: 'Download CSV' }).disabled).toBe(true)
  vi.stubGlobal('structuredClone', clone)
  read(); await loaded()
  expect(screen.getAllByText('PASS')).toHaveLength(4)
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.getByRole('button', { name: 'Download CSV' }).disabled).toBe(false)
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
