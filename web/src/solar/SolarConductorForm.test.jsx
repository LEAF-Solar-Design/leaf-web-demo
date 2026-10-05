import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import declaration from '../../../server/solar_tools/solar_string_conductors.json'
import SolarConductorForm from './SolarConductorForm.jsx'

afterEach(() => { cleanup(); vi.useRealTimers() })
const row = declaration.record
const fixture = () => ({ version: 3, intake: { solar_design_graph: { rev: 7, strings: [
  { id: 'T1', circuit_tag: 'A1', wire_gauge: '' }, { id: 'T2', circuit_tag: 'A2', wire_gauge: '8 AWG' },
] } } })
const four = () => ({ version: 3, intake: { solar_design_graph: { rev: 7, strings: ['1', '2', '3', '4'].map((n) => (
  { id: `T${n}`, circuit_tag: `A${n}`, wire_gauge: '' })) } } })
const expected = { operation: 'set-conductors', expected_rev: 7, assignments: [
  { string_ref: 'T1', wire_gauge: '10 AWG' }, { string_ref: 'T2', wire_gauge: '10 AWG' },
] }
const apply = () => screen.getByRole('button', { name: 'Apply to selected strings' })
const choose = () => {
  fireEvent.click(screen.getByRole('button', { name: 'Select all' }))
  fireEvent.change(screen.getByLabelText('Conductor'), { target: { value: '10 AWG' } })
}
const stringRow = (tag) => screen.getByLabelText(`Select ${tag}`)
const stringRows = () => [...document.querySelectorAll('tr[data-string-id]')]
const isSelected = (item) => item.getAttribute('aria-selected') === 'true'
const selectedIds = () => stringRows().filter(isSelected).map((item) => item.dataset.stringId)
// jsdom may lack PointerEvent; React reads pointerType off whatever native event arrives.
function pointer(node, type, { pointerType = 'touch', ...init } = {}) {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, clientX: 0, clientY: 0, ...init })
  Object.defineProperty(event, 'pointerType', { value: pointerType })
  fireEvent(node, event)
}
function tap(node) {
  pointer(node, 'pointerdown'); pointer(node, 'pointerup'); fireEvent.click(node)
}
async function setup(overrides = {}) {
  const props = { row, drawingId: 'd1', drawingVersion: 3, projectId: null,
    readIntake: vi.fn(async () => fixture()), onSubmit: vi.fn(), onClose: vi.fn(), ...overrides }
  const view = render(<SolarConductorForm {...props} />)
  if (props.projectId === null && props.drawingId) await waitFor(() => expect(props.readIntake).toHaveBeenCalled())
  await act(async () => {})
  return { ...view, props }
}

describe('SolarConductorForm', () => {
  it('CF1 displays stored choices with no implicit selection or conductor', async () => {
    await setup()
    const table = screen.getByRole('table', { name: 'String conductors' })
    expect(within(table).getAllByRole('row').map((item) => item.textContent)).toEqual([
      'Circuit tagCurrent conductor', 'A1Not set', 'A28 AWG',
    ])
    expect(stringRows()).toHaveLength(2)
    expect(stringRows().every((item) => !isSelected(item))).toBe(true)
    expect(screen.getByLabelText('Conductor').value).toBe('')
    expect(screen.getByRole('option', { name: 'Choose a conductor' }).selected).toBe(true)
    expect(apply().disabled).toBe(true)
    expect(screen.getByRole('status').textContent).toBe('Select at least one string.')
    expect(screen.getByText('This records conductor choices. It does not check ampacity or voltage drop.')).toBeTruthy()
  })

  it('CF2 applies the selected conductor to both strings once', async () => {
    const { props } = await setup()
    choose()
    fireEvent.click(apply())
    expect(props.onSubmit).toHaveBeenCalledTimes(1)
    expect(props.onSubmit).toHaveBeenCalledWith(row, expected)
  })

  it('CF3 reverse Ctrl or Cmd clicks still submit assignments in graph order', async () => {
    const { props } = await setup()
    fireEvent.click(stringRow('A2'), { ctrlKey: true })
    fireEvent.click(stringRow('A1'), { metaKey: true })
    fireEvent.change(screen.getByLabelText('Conductor'), { target: { value: '10 AWG' } })
    fireEvent.click(apply())
    expect(props.onSubmit).toHaveBeenCalledWith(row, expected)
  })

  it('CF4 requires an explicit conductor after selecting a string', async () => {
    const { props } = await setup()
    fireEvent.click(stringRow('A1'))
    expect(screen.getByRole('status').textContent).toBe('Choose a conductor for the selected strings.')
    expect(apply().disabled).toBe(true)
    fireEvent.click(apply())
    expect(props.onSubmit).not.toHaveBeenCalled()
  })

  it('CF5 locks synchronously before a parent render and releases on dismissal', async () => {
    const { props, rerender } = await setup()
    choose()
    const button = apply()
    act(() => { button.click(); button.click() })
    expect(props.onSubmit).toHaveBeenCalledTimes(1)
    expect(apply().disabled).toBe(false)
    expect(screen.queryByText('Review and confirm this change.')).toBeNull()
    rerender(<SolarConductorForm {...props} status="pending" />)
    expect(apply().disabled).toBe(true)
    expect(screen.getByText('This step is running. Wait for it to finish.')).toBeTruthy()
    rerender(<SolarConductorForm {...props} status={null} />)
    expect(apply().disabled).toBe(false)
    expect(props.readIntake).toHaveBeenCalledTimes(1)
  })

  it('CF6 failure retains the inputs and Retry stages one equal request', async () => {
    const { props, rerender } = await setup()
    choose()
    fireEvent.click(apply())
    rerender(<SolarConductorForm {...props} status="pending" />)
    rerender(<SolarConductorForm {...props} status="failed" failureCode="STALE_GRAPH_REVISION" />)
    expect(stringRows().every(isSelected)).toBe(true)
    expect(screen.getByLabelText('Conductor').value).toBe('10 AWG')
    expect(screen.getByRole('alert').textContent).toBe('This run failed: STALE_GRAPH_REVISION. Your inputs are kept.')
    const retry = screen.getByRole('button', { name: 'Retry' })
    act(() => { retry.click(); retry.click() })
    expect(props.onSubmit).toHaveBeenCalledTimes(2)
    expect(props.onSubmit.mock.calls[1]).toEqual(props.onSubmit.mock.calls[0])
    expect(retry.disabled).toBe(false)
    expect(apply().disabled).toBe(false)
    rerender(<SolarConductorForm {...props} status="pending" />)
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
    expect(apply().disabled).toBe(true)
  })

  it('CF16 identical ribbon-host props release the submit guard without a status prop', async () => {
    const { props, rerender } = await setup()
    choose()
    const button = apply()
    act(() => { button.click(); button.click() })
    expect(props.onSubmit).toHaveBeenCalledTimes(1)
    rerender(<SolarConductorForm {...props} />)
    expect(apply().disabled).toBe(false)
    expect(stringRows().every((item) => isSelected(item) && !item.hasAttribute('aria-disabled'))).toBe(true)
    expect(screen.queryByText('Review and confirm this change.')).toBeNull()
    fireEvent.click(apply())
    expect(props.onSubmit).toHaveBeenCalledTimes(2)
    expect(props.onSubmit.mock.calls[1]).toEqual([row, expected])
    expect(props.onSubmit.mock.calls[1]).toEqual(props.onSubmit.mock.calls[0])
    expect(props.readIntake).toHaveBeenCalledTimes(1)
  })

  it('CF17 consecutive finished props release the guard after another submission', async () => {
    const { props, rerender } = await setup()
    choose()
    fireEvent.click(apply())
    rerender(<SolarConductorForm {...props} status="finished" />)
    await waitFor(() => expect(props.readIntake).toHaveBeenCalledTimes(2))
    await act(async () => {})
    expect(stringRows().every((item) => !isSelected(item))).toBe(true)
    choose()
    fireEvent.click(apply())
    expect(props.onSubmit).toHaveBeenCalledTimes(2)
    rerender(<SolarConductorForm {...props} status="finished" />)
    expect(apply().disabled).toBe(false)
    expect(stringRows().every((item) => isSelected(item) && !item.hasAttribute('aria-disabled'))).toBe(true)
    expect(screen.queryByText('Review and confirm this change.')).toBeNull()
    expect(screen.getByText('Conductor choices applied.')).toBeTruthy()
    fireEvent.click(apply())
    expect(props.onSubmit).toHaveBeenCalledTimes(3)
    expect(props.onSubmit.mock.calls[2]).toEqual(props.onSubmit.mock.calls[1])
    expect(props.readIntake).toHaveBeenCalledTimes(2)
  })

  it.each(['STALE_GRAPH_REVISION', null])('CF18 failure after a version change asks for new inputs (code: %s)', async (failureCode) => {
    const { props, rerender } = await setup()
    choose()
    fireEvent.click(apply())
    expect(props.onSubmit).toHaveBeenCalledTimes(1)
    rerender(<SolarConductorForm {...props} status="pending" />)
    let resolveUpdated
    props.readIntake.mockImplementationOnce(() => new Promise((resolve) => { resolveUpdated = resolve }))
    rerender(<SolarConductorForm {...props} drawingVersion={4} status="pending" />)
    await waitFor(() => expect(props.readIntake).toHaveBeenLastCalledWith('d1', 4))
    await act(async () => { resolveUpdated({ ...fixture(), version: 4 }) })
    expect(stringRows()).toHaveLength(2)
    expect(stringRows().every((item) => !isSelected(item))).toBe(true)
    expect(screen.getByLabelText('Conductor').value).toBe('')
    rerender(<SolarConductorForm {...props} drawingVersion={4} status="failed" failureCode={failureCode} />)
    expect(screen.getByRole('alert').textContent).toBe(failureCode
      ? 'This run failed: STALE_GRAPH_REVISION. Choose the strings and the conductor again.'
      : 'This run failed. Choose the strings and the conductor again.')
    expect(screen.queryByText(/Your inputs are kept\./)).toBeNull()
    expect(apply().disabled).toBe(true)
    fireEvent.click(apply())
    expect(props.onSubmit).toHaveBeenCalledTimes(1)
    fireEvent.click(stringRow('A1'))
    expect(apply().disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Conductor'), { target: { value: '10 AWG' } })
    expect(apply().disabled).toBe(false)
  })

  it('CF7 completion refreshes the persisted revision and resets the draft', async () => {
    const { props, rerender } = await setup()
    choose()
    fireEvent.click(apply())
    const updated = fixture()
    updated.intake.solar_design_graph.rev = 8
    updated.intake.solar_design_graph.strings.forEach((item) => { item.wire_gauge = '10 AWG' })
    props.readIntake.mockResolvedValue(updated)
    rerender(<SolarConductorForm {...props} status="finished" />)
    await waitFor(() => expect(within(screen.getByRole('table')).getAllByText('10 AWG')).toHaveLength(2))
    expect(props.readIntake).toHaveBeenCalledTimes(2)
    expect(screen.getByText('Conductor choices applied.')).toBeTruthy()
    expect(stringRows().every((item) => !isSelected(item))).toBe(true)
    expect(screen.getByLabelText('Conductor').value).toBe('')
    choose()
    fireEvent.click(apply())
    expect(props.onSubmit.mock.calls[1][1]).toEqual({ ...expected, expected_rev: 8 })
  })

  it('CF8 refuses project scope and absent drawings without reading intake', async () => {
    for (const override of [{ projectId: 'p1' }, { drawingId: null }]) {
      const { props } = await setup(override)
      expect(screen.getByText('This form supports standalone drawings only.')).toBeTruthy()
      expect(apply().disabled).toBe(true)
      expect(stringRows()).toHaveLength(0)
      expect(screen.queryAllByRole('checkbox')).toHaveLength(0)
      expect(screen.getByLabelText('Conductor').disabled).toBe(true)
      expect(props.readIntake).not.toHaveBeenCalled()
      cleanup()
    }
  })

  it('CF9 invalid graph envelopes produce no string rows', async () => {
    const mismatch = { ...fixture(), version: 4 }
    const missing = fixture()
    delete missing.intake.solar_design_graph.strings
    const duplicate = fixture()
    duplicate.intake.solar_design_graph.strings[1].id = 'T1'
    const oversized = fixture()
    oversized.intake.solar_design_graph.strings = Array.from({ length: 4097 }, (_, i) => ({ id: String(i), circuit_tag: '', wire_gauge: '' }))
    for (const value of [mismatch, missing, duplicate, oversized]) {
      await setup({ readIntake: vi.fn(async () => value) })
      expect(screen.getByText('Conductor choices are unavailable for this drawing.')).toBeTruthy()
      expect(stringRows()).toHaveLength(0)
      expect(apply().disabled).toBe(true)
      cleanup()
    }
  })

  it('CF10 missing or duplicate gauge enums never fall back to a built-in list', async () => {
    for (const options of [undefined, ['10 AWG', '10 AWG']]) {
      const invalid = { ...row, params: { properties: { assignments: { items: { properties: { wire_gauge: { enum: options } } } } } } }
      await setup({ row: invalid })
      expect(screen.getByText('Conductor choices are unavailable for this drawing.')).toBeTruthy()
      expect(screen.getAllByRole('option')).toHaveLength(1)
      expect(apply().disabled).toBe(true)
      cleanup()
    }
  })

  it('CF9 ignores late responses after drawing or version changes', async () => {
    for (const override of [{ drawingId: 'd2' }, { drawingVersion: 4 }]) {
      let resolveOld
      const readIntake = vi.fn().mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve }))
        .mockResolvedValue({ version: override.drawingVersion ?? 3, intake: { solar_design_graph: { rev: 8, strings: [] } } })
      const { props, rerender } = await setup({ readIntake })
      rerender(<SolarConductorForm {...props} {...override} />)
      await waitFor(() => expect(readIntake).toHaveBeenCalledTimes(2))
      await act(async () => { resolveOld(fixture()) })
      expect(stringRows()).toHaveLength(0)
      expect(apply().disabled).toBe(true)
      cleanup()
    }
  })

  it('CF19 has no checkbox column: rows carry the selection and take keyboard focus', async () => {
    const { container } = await setup()
    expect(container.querySelectorAll('input[type="checkbox"]')).toHaveLength(0)
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0)
    expect(screen.queryByRole('columnheader', { name: 'Select' })).toBeNull()
    for (const item of stringRows()) {
      expect(item.tabIndex).toBe(0)
      expect(item).toHaveAttribute('aria-selected', 'false')
    }
    expect(screen.getByRole('table', { name: 'String conductors' })).toHaveAccessibleDescription(/Shift-click selects a range/)
    fireEvent.click(stringRow('A2'))
    expect(stringRow('A2')).toHaveAttribute('aria-selected', 'true')
    expect(stringRow('A2')).toHaveTextContent('A2 (selected)8 AWG')
  })

  it('CF20 a plain click selects one; Ctrl or Cmd adds or removes; Shift spans from the anchor in graph order', async () => {
    const { props } = await setup({ readIntake: vi.fn(async () => four()) })
    fireEvent.click(stringRow('A1'))
    fireEvent.click(stringRow('A3'), { shiftKey: true })
    expect(selectedIds()).toEqual(['T1', 'T2', 'T3'])
    fireEvent.click(stringRow('A4'))
    expect(selectedIds()).toEqual(['T4'])
    fireEvent.click(stringRow('A2'), { shiftKey: true })
    expect(selectedIds()).toEqual(['T2', 'T3', 'T4'])
    // The anchor stays put across Shift clicks.
    fireEvent.click(stringRow('A3'), { shiftKey: true })
    expect(selectedIds()).toEqual(['T3', 'T4'])
    fireEvent.click(stringRow('A1'), { ctrlKey: true })
    expect(selectedIds()).toEqual(['T1', 'T3', 'T4'])
    fireEvent.click(stringRow('A3'), { metaKey: true })
    expect(selectedIds()).toEqual(['T1', 'T4'])
    // Ctrl or Cmd with Shift adds the span; the last toggled row is the anchor.
    fireEvent.click(stringRow('A1'), { shiftKey: true, ctrlKey: true })
    expect(selectedIds()).toEqual(['T1', 'T2', 'T3', 'T4'])
    fireEvent.change(screen.getByLabelText('Conductor'), { target: { value: '10 AWG' } })
    fireEvent.click(apply())
    expect(props.onSubmit.mock.calls[0][1].assignments.map((item) => item.string_ref)).toEqual(['T1', 'T2', 'T3', 'T4'])
  })

  it('CF21 X toggles the focused row, Enter selects it alone, arrows move focus, typing never toggles', async () => {
    await setup({ readIntake: vi.fn(async () => four()) })
    const first = stringRow('A1')
    first.focus()
    expect(fireEvent.keyDown(first, { key: 'x' })).toBe(false)
    expect(selectedIds()).toEqual(['T1'])
    fireEvent.keyDown(stringRow('A1'), { key: 'ArrowDown' })
    expect(stringRow('A2')).toHaveFocus()
    fireEvent.keyDown(stringRow('A2'), { key: 'X', shiftKey: true })
    expect(selectedIds()).toEqual(['T1', 'T2'])
    fireEvent.keyDown(stringRow('A1'), { key: 'x' })
    expect(selectedIds()).toEqual(['T2'])
    for (const chord of [{ ctrlKey: true }, { metaKey: true }, { altKey: true }, { repeat: true }]) {
      expect(fireEvent.keyDown(stringRow('A3'), { key: 'x', ...chord })).toBe(true)
    }
    expect(selectedIds()).toEqual(['T2'])
    fireEvent.keyDown(stringRow('A2'), { key: 'ArrowUp' })
    expect(stringRow('A1')).toHaveFocus()
    // X made A1 the anchor even though it took A1 out.
    fireEvent.keyDown(stringRow('A4'), { key: 'Enter', shiftKey: true })
    expect(selectedIds()).toEqual(['T1', 'T2', 'T3', 'T4'])
    fireEvent.keyDown(stringRow('A3'), { key: 'Enter' })
    expect(selectedIds()).toEqual(['T3'])
    fireEvent.keyDown(stringRow('A4'), { key: ' ' })
    expect(selectedIds()).toEqual(['T4'])
    const gauge = screen.getByLabelText('Conductor')
    gauge.focus()
    expect(fireEvent.keyDown(gauge, { key: 'x' })).toBe(true)
    expect(selectedIds()).toEqual(['T4'])
  })

  it('CF22 a 500 ms touch press enters selection, then taps toggle until the selection is empty', async () => {
    const { props, rerender } = await setup({ readIntake: vi.fn(async () => four()) })
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    pointer(stringRow('A2'), 'pointerdown')
    act(() => { vi.advanceTimersByTime(499) })
    expect(selectedIds()).toEqual([])
    act(() => { vi.advanceTimersByTime(1) })
    expect(selectedIds()).toEqual(['T2'])
    // The release that ends the press is not a second tap.
    pointer(stringRow('A2'), 'pointerup'); fireEvent.click(stringRow('A2'))
    expect(selectedIds()).toEqual(['T2'])
    tap(stringRow('A4'))
    expect(selectedIds()).toEqual(['T2', 'T4'])
    tap(stringRow('A2'))
    tap(stringRow('A4'))
    expect(selectedIds()).toEqual([])
    // Empty again: a plain tap selects that row alone.
    tap(stringRow('A1'))
    tap(stringRow('A3'))
    expect(selectedIds()).toEqual(['T3'])
    // A mouse press never starts one, and a drift past the slop cancels one.
    pointer(stringRow('A1'), 'pointerdown', { pointerType: 'mouse' })
    act(() => { vi.advanceTimersByTime(600) })
    pointer(stringRow('A1'), 'pointerdown')
    pointer(stringRow('A1'), 'pointermove', { clientX: 0, clientY: 20 })
    act(() => { vi.advanceTimersByTime(600) })
    expect(selectedIds()).toEqual(['T3'])
    // A running step ignores presses.
    rerender(<SolarConductorForm {...props} status="pending" />)
    pointer(stringRow('A1'), 'pointerdown')
    act(() => { vi.advanceTimersByTime(600) })
    fireEvent.click(stringRow('A1'), { ctrlKey: true })
    expect(selectedIds()).toEqual(['T3'])
  })
})
