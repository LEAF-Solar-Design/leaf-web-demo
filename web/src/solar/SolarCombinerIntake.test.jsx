import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import SolarCombinerIntake from './SolarCombinerIntake.jsx'
import { COMBINER_INTAKE_MAX_BYTES, COMBINER_INTAKE_REASONS } from './solarCombinerIntakeClient.js'

afterEach(() => { cleanup(); vi.restoreAllMocks() })
const value = { drawing_id: 'a', project_id: 'p', created: true, version: 2, parent_version: 1,
  graph_rev: 7, graph_sha256: 'a'.repeat(64), bound: { l2_inverters: 2, strings: 4 },
  panel_groups: 1, outline_vertices: 4, intake_sha256: 'b'.repeat(64),
  combiner_intake_sha256: 'c'.repeat(64), panel_groups_sha256: 'd'.repeat(64) }
const success = { ok: true, status: 200, value }
const importButton = () => screen.getByRole('button', { name: 'Import' })
const runButton = () => screen.getByRole('button', { name: 'Run placement' })
const fileInput = () => screen.getByLabelText('Combiner intake file')
const hardware = { model: ' Combiner ', max_dc_voltage: '480', max_ac_power_kw: '10' }
const labels = { model: 'Model', max_dc_voltage: 'DC voltage', max_ac_power_kw: 'AC power' }
function typeHardware(values = hardware) {
  for (const [field, text] of Object.entries(values)) fireEvent.change(screen.getByLabelText(labels[field]), { target: { value: text } })
}
function expectDraft(file) {
  expect(fileInput().files[0]).toBe(file)
  for (const [field, text] of Object.entries(hardware)) expect(screen.getByLabelText(labels[field]).value).toBe(text)
}
function setup(overrides = {}) {
  const props = { drawingId: 'a', projectId: 'p', client: { importCombinerIntake: vi.fn().mockResolvedValue(success) },
    onStored: vi.fn(), onRunPlacement: vi.fn(), ...overrides }
  const view = render(<SolarCombinerIntake {...props} />)
  const file = new File(['{}'], 'intake.json', { type: 'application/json' })
  fireEvent.change(fileInput(), { target: { files: [file] } })
  return { ...view, props, file }
}
function deferred() {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}
async function importAndWait() {
  fireEvent.click(importButton())
  await waitFor(() => expect(importButton().disabled).toBe(false))
}

describe('SolarCombinerIntake', () => {
  it('CI1 stores once and offers placement only after a successful import', async () => {
    const pending = deferred()
    const { props, file } = setup({ client: { importCombinerIntake: vi.fn(() => pending.promise) } })
    typeHardware()
    expect(runButton().disabled).toBe(true)
    fireEvent.click(importButton())
    expect(runButton().disabled).toBe(true)
    expect(props.onStored).not.toHaveBeenCalled()
    expect(props.client.importCombinerIntake).toHaveBeenCalledWith({ drawingId: 'a', projectId: 'p', file, signal: expect.any(AbortSignal) })
    await act(async () => { pending.resolve(success) })
    expect(props.onStored).toHaveBeenCalledTimes(1)
    expect(props.onStored).toHaveBeenCalledWith(value)
    expect(runButton().disabled).toBe(false)
    expect(fileInput().accept).toBe('.json,application/json')
  })

  it('CI2 disables placement with an import-first reason', () => {
    const { props } = setup()
    typeHardware()
    expect(runButton().disabled).toBe(true)
    expect(screen.getByText('Import the combiner intake for this drawing first')).toBeTruthy()
    fireEvent.click(runButton())
    expect(props.onRunPlacement).not.toHaveBeenCalled()
  })

  it('CI3 displays every client refusal sentence and retains the file and hardware', async () => {
    const { props, file } = setup()
    typeHardware()
    for (const [code, sentence] of Object.entries(COMBINER_INTAKE_REASONS)) {
      props.client.importCombinerIntake.mockResolvedValueOnce({ ok: false, status: 400, code, retryable: false })
      await importAndWait()
      expect(screen.getByRole('status').textContent).toBe(sentence)
      expectDraft(file)
      expect(runButton().disabled).toBe(true)
    }
    expect(props.onStored).not.toHaveBeenCalled()
  })

  it('CI4 discards drawing A success after switching to B without a new import', async () => {
    const pending = deferred()
    const { props, rerender } = setup({ client: { importCombinerIntake: vi.fn(() => pending.promise) } })
    typeHardware()
    fireEvent.click(importButton())
    const signal = props.client.importCombinerIntake.mock.calls[0][0].signal
    rerender(<SolarCombinerIntake {...props} drawingId="b" />)
    expect(signal.aborted).toBe(true)
    await act(async () => { pending.resolve(success) })
    expect(props.onStored).not.toHaveBeenCalled()
    expect(runButton().disabled).toBe(true)
    expect(screen.getByRole('status').textContent).toBe('')
    expect(screen.getByText('Import the combiner intake for this drawing first')).toBeTruthy()
  })

  it('CI4 latest request wins even when an older request returns after switching back', async () => {
    const old = deferred()
    const latest = deferred()
    const client = { importCombinerIntake: vi.fn().mockReturnValueOnce(old.promise).mockReturnValueOnce(latest.promise) }
    const { props, rerender } = setup({ client })
    fireEvent.click(importButton())
    rerender(<SolarCombinerIntake {...props} drawingId="b" />)
    rerender(<SolarCombinerIntake {...props} drawingId="a" />)
    fireEvent.click(importButton())
    await act(async () => { latest.resolve({ ...success, value: { ...value, graph_rev: 8 } }) })
    await act(async () => { old.resolve(success) })
    expect(props.onStored).toHaveBeenCalledTimes(1)
    expect(props.onStored.mock.calls[0][0].graph_rev).toBe(8)
    typeHardware()
    fireEvent.click(runButton())
    expect(props.onRunPlacement.mock.calls[0][0].expected_rev).toBe(8)
  })

  it.each([
    ['success', success],
    ['refusal', { ok: false, status: null, code: 'COMBINER_CLIENT_ABORTED', retryable: false }],
  ])('CI15 discards cancelled drawing A %s after switching to B and back without a new import', async (_outcome, result) => {
    const pending = deferred()
    const { props, rerender } = setup({ client: { importCombinerIntake: vi.fn(() => pending.promise) } })
    typeHardware()
    fireEvent.click(importButton())
    const signal = props.client.importCombinerIntake.mock.calls[0][0].signal
    rerender(<SolarCombinerIntake {...props} drawingId="b" />)
    expect(signal.aborted).toBe(true)
    rerender(<SolarCombinerIntake {...props} drawingId="a" />)
    await act(async () => { pending.resolve(result) })
    expect(props.client.importCombinerIntake).toHaveBeenCalledTimes(1)
    expect(props.onStored).not.toHaveBeenCalled()
    expect(screen.getByRole('status').textContent).toBe('')
    expect(runButton().disabled).toBe(true)
    const reason = screen.getByText('Import the combiner intake for this drawing first')
    expect(runButton().getAttribute('aria-describedby')).toBe(reason.id)
    fireEvent.click(runButton())
    expect(props.onRunPlacement).not.toHaveBeenCalled()
  })

  it('CI5 locks the import synchronously against a double click', async () => {
    const pending = deferred()
    const { props } = setup({ client: { importCombinerIntake: vi.fn(() => pending.promise) } })
    const button = importButton()
    act(() => { button.click(); button.click() })
    expect(props.client.importCombinerIntake).toHaveBeenCalledTimes(1)
    expect(button.disabled).toBe(true)
    await act(async () => { pending.resolve(success) })
  })

  it('CI6 refuses an oversized Blob before calling the client', () => {
    const { props } = setup()
    const blob = new Blob([new Uint8Array(COMBINER_INTAKE_MAX_BYTES + 1)], { type: 'application/json' })
    fireEvent.change(fileInput(), { target: { files: [blob] } })
    fireEvent.click(importButton())
    expect(props.client.importCombinerIntake).not.toHaveBeenCalled()
    expect(screen.getByRole('status').textContent).toBe(COMBINER_INTAKE_REASONS.COMBINER_IMPORT_TOO_LARGE)
    expect(importButton().disabled).toBe(false)
  })

  it('CI10 hands over only the three hardware keys and the imported graph revision', async () => {
    const { props, rerender } = setup()
    await importAndWait()
    for (const label of Object.values(labels)) {
      const input = screen.getByLabelText(label)
      expect(input.getAttribute('aria-invalid')).toBe('true')
      expect(document.getElementById(input.getAttribute('aria-describedby')).textContent).toBeTruthy()
    }
    expect(runButton().disabled).toBe(true)
    typeHardware()
    expect(runButton().disabled).toBe(false)
    for (const label of Object.values(labels)) expect(screen.getByLabelText(label).getAttribute('aria-invalid')).toBe('false')
    fireEvent.click(runButton())
    expect(props.onRunPlacement).toHaveBeenCalledTimes(1)
    const params = props.onRunPlacement.mock.calls[0][0]
    expect(params).toEqual({ expected_rev: 7, hardware: { model: ' Combiner ', max_dc_voltage: 480, max_ac_power_kw: 10 } })
    expect(Object.keys(params).sort()).toEqual(['expected_rev', 'hardware'])
    expect(Object.keys(params.hardware).sort()).toEqual(['max_ac_power_kw', 'max_dc_voltage', 'model'])
    typeHardware({ max_dc_voltage: '0' })
    expect(runButton().disabled).toBe(true)
    fireEvent.click(runButton())
    expect(props.onRunPlacement).toHaveBeenCalledTimes(1)
    rerender(<SolarCombinerIntake {...props} drawingId="b" />)
    expect(runButton().disabled).toBe(true)
  })

  it('CI11 retries a retryable refusal with the same file and inputs', async () => {
    const { props, file } = setup()
    props.client.importCombinerIntake.mockResolvedValueOnce({ ok: false, status: 503, code: 'COMBINER_IMPORT_STORE_UNAVAILABLE', retryable: true })
    typeHardware()
    await importAndWait()
    expect(screen.getByRole('status').textContent).toContain('This import can be retried.')
    expectDraft(file)
    await importAndWait()
    expectDraft(file)
    expect(props.client.importCombinerIntake).toHaveBeenCalledTimes(2)
    expect(props.client.importCombinerIntake.mock.calls[1][0].file).toBe(file)
    expect(props.client.importCombinerIntake.mock.calls[1][0].signal).not.toBe(props.client.importCombinerIntake.mock.calls[0][0].signal)
    expect(props.onStored).toHaveBeenCalledTimes(1)
  })

  it('CI12 aborts on unmount and ignores the eventual response', async () => {
    const pending = deferred()
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { props, unmount } = setup({ client: { importCombinerIntake: vi.fn(() => pending.promise) } })
    fireEvent.click(importButton())
    const signal = props.client.importCombinerIntake.mock.calls[0][0].signal
    unmount()
    expect(signal.aborted).toBe(true)
    await act(async () => { pending.resolve(success) })
    expect(props.onStored).not.toHaveBeenCalled()
    expect(error).not.toHaveBeenCalled()
  })

  it('CI13 announces success then failure politely and restores Import focus', async () => {
    const { props } = setup()
    await importAndWait()
    expect(screen.getByRole('status').getAttribute('aria-live')).toBe('polite')
    expect(screen.getByRole('status').textContent).toBe('Combiner intake imported for this drawing.')
    expect(document.activeElement).toBe(importButton())
    props.client.importCombinerIntake.mockResolvedValueOnce({ ok: false, status: 400, code: 'COMBINER_IMPORT_JSON_INVALID', retryable: false })
    screen.getByLabelText('Model').focus()
    await importAndWait()
    expect(screen.getByRole('status').textContent).toBe(COMBINER_INTAKE_REASONS.COMBINER_IMPORT_JSON_INVALID)
    expect(document.activeElement).toBe(importButton())
  })
})
