import React from 'react'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import ModuleSetPicker from './ModuleSetPicker.jsx'

afterEach(cleanup)
const data = { sets: [
  { set_id: 'first', layer: 'Design', block: 'Panel A', count: 12 },
  { set_id: 'second', layer: 'Alternate', block: 'Panel B', count: 9 },
] }
const first = () => screen.getByRole('checkbox', { name: 'Design / Panel A: 12 candidates' })
const second = () => screen.getByRole('checkbox', { name: 'Alternate / Panel B: 9 candidates' })
const submit = () => screen.getByRole('button', { name: 'Count selected set' })

it('gives identical labels unique input IDs and independent explicit associations', () => {
  const onSelect = vi.fn()
  const sets = [{ ...data.sets[0], set_id: 'same-label-a' }, { ...data.sets[0], set_id: 'same-label-b' }]
  const view = render(<ModuleSetPicker data={{ sets }} onSelect={onSelect} />)
  const inputs = screen.getAllByRole('checkbox')
  const labels = [...view.container.querySelectorAll('label')]
  expect(inputs[0].id).not.toBe('')
  expect(inputs[1].id).not.toBe(inputs[0].id)
  labels.forEach((label, index) => expect(label.control).toBe(inputs[index]))
  fireEvent.click(labels[1])
  expect(inputs[0].checked).toBe(false)
  expect(inputs[1].checked).toBe(true)
  fireEvent.click(submit())
  expect(onSelect).toHaveBeenCalledExactlyOnceWith(['same-label-b'])
})

it('keeps explicit label IDs unique across simultaneous picker instances', () => {
  const onA = vi.fn(), onB = vi.fn()
  const view = render(<><ModuleSetPicker data={data} onSelect={onA} /><ModuleSetPicker data={data} onSelect={onB} /></>)
  const inputs = screen.getAllByRole('checkbox')
  const labels = [...view.container.querySelectorAll('label')]
  expect(new Set(inputs.map((input) => input.id)).size).toBe(4)
  inputs.forEach((input) => expect(input.id).not.toBe(''))
  labels.forEach((label, index) => expect(label.control).toBe(inputs[index]))
  fireEvent.click(labels[0])
  expect(inputs.map((input) => input.checked)).toEqual([true, false, false, false])
  fireEvent.click(labels[3])
  expect(inputs.map((input) => input.checked)).toEqual([true, false, false, true])
  const buttons = screen.getAllByRole('button', { name: 'Count selected set' })
  fireEvent.click(buttons[0])
  fireEvent.click(buttons[1])
  expect(onA).toHaveBeenCalledExactlyOnceWith(['first'])
  expect(onB).toHaveBeenCalledExactlyOnceWith(['second'])
})

it.each([
  { layer: {} }, { layer: 2 }, { block: {} }, { block: true },
  { count: -1 }, { count: 1.5 }, { count: NaN }, { count: Infinity }, { count: 9007199254740992 },
])('refuses malformed labels or candidate counts %# without selection', (fields) => {
  const onSelect = vi.fn()
  render(<ModuleSetPicker data={{ sets: [{ ...data.sets[0], ...fields }] }} onSelect={onSelect} />)
  expect(screen.getByText(/Module sets unavailable.*Run the module tool again/)).toBeTruthy()
  expect(screen.queryAllByRole('checkbox')).toHaveLength(0)
  fireEvent.click(submit())
  expect(onSelect).not.toHaveBeenCalled()
})

it.each(['duplicate', 'absent'])('refuses a summary with %s candidate IDs', (kind) => {
  const rows = kind === 'duplicate' ? [data.sets[0], data.sets[0]]
    : [{ ...data.sets[0], set_id: 'missing' }]
  render(<ModuleSetPicker data={{ ...data, selected_sets: rows, count: 12 }} onSelect={vi.fn()} />)
  expect(screen.getByText(/Count summary unavailable/)).toBeTruthy()
  expect(screen.queryByText(/^Counted /)).toBeNull()
})

it('keeps focus and choices attached to set IDs when rows reorder', () => {
  const onSelect = vi.fn()
  const view = render(<ModuleSetPicker data={data} onSelect={onSelect} />)
  const focused = first()
  focused.focus()
  fireEvent.click(focused)
  view.rerender(<ModuleSetPicker data={{ sets: [...data.sets].reverse() }} onSelect={onSelect} />)
  expect(document.activeElement).toBe(focused)
  expect(first()).toBe(focused)
  expect(first().checked).toBe(true)
  expect(second().checked).toBe(false)
  fireEvent.click(submit())
  expect(onSelect).toHaveBeenCalledExactlyOnceWith(['first'])
})

it('hides late completed drawing A counts after switching to drawing B', () => {
  const onSelect = vi.fn()
  const completedA = { ...data, selected_sets: [data.sets[0]], count: 12 }
  const view = render(<ModuleSetPicker drawingKey="A" data={completedA} onSelect={onSelect} />)
  expect(screen.getByText(/^Counted 12/)).toBeTruthy()
  fireEvent.click(first())
  view.rerender(<ModuleSetPicker drawingKey="B" data={data} onSelect={onSelect} />)
  expect(first().checked).toBe(false)
  // ResultPanel passes the old origin; App withholds the callback for that origin.
  view.rerender(<ModuleSetPicker drawingKey="A" data={completedA} />)
  expect(screen.queryByText(/^Counted /)).toBeNull()
  expect(screen.queryAllByRole('checkbox')).toHaveLength(0)
  expect(screen.queryByText(/12 candidates/)).toBeNull()
  expect(screen.queryByText('No module candidates were found.')).toBeNull()
  expect(submit().disabled).toBe(true)
  expect(screen.getByText('Run the module tool on the current drawing to choose a set.')).toBeTruthy()
  view.rerender(<ModuleSetPicker drawingKey="B" data={data} onSelect={onSelect} />)
  expect(first().checked).toBe(false)
  expect(onSelect).not.toHaveBeenCalled()
})

it.each([null, {}, 'bad', [null], [{ layer: {} }]].map((value) => [value]))('refuses malformed counted summary rows %#', (selected_sets) => {
  render(<ModuleSetPicker data={{ ...data, selected_sets, count: 2 }} onSelect={vi.fn()} />)
  expect(screen.getByText(/Count summary unavailable.*Run the module tool again/)).toBeTruthy()
  expect(screen.queryByText(/^Counted /)).toBeNull()
})

it.each([NaN, Infinity, -1, 1.5, 9007199254740992, '2', {}])('refuses invalid result count %#', (count) => {
  render(<ModuleSetPicker data={{ ...data, selected_sets: [data.sets[0]], count }} onSelect={vi.fn()} />)
  expect(screen.getByText(/Count summary unavailable/)).toBeTruthy()
  expect(screen.queryByText(/^Counted /)).toBeNull()
})

it('shows a valid counted summary and clears it on a listing result', () => {
  const view = render(<ModuleSetPicker data={{ ...data, selected_sets: [data.sets[0]], count: 12 }} onSelect={vi.fn()} />)
  expect(screen.getByText('Counted 12 modules on Design / Panel A.')).toBeTruthy()
  view.rerender(<ModuleSetPicker data={{ ...data, selected_sets: [], count: null }} onSelect={vi.fn()} />)
  expect(screen.queryByText(/^Counted /)).toBeNull()
  expect(screen.queryByText(/Count summary unavailable/)).toBeNull()
})

it('uses only the latest callback and requires a fresh choice after StrictMode remount', () => {
  const oldCallback = vi.fn()
  const latest = vi.fn()
  const view = render(<React.StrictMode><ModuleSetPicker data={data} drawingKey="same" onSelect={oldCallback} /></React.StrictMode>)
  fireEvent.click(second())
  view.rerender(<React.StrictMode><ModuleSetPicker data={data} drawingKey="same" onSelect={latest} /></React.StrictMode>)
  fireEvent.click(submit())
  expect(latest).toHaveBeenCalledExactlyOnceWith(['second'])
  expect(oldCallback).not.toHaveBeenCalled()
  view.unmount()
  render(<React.StrictMode><ModuleSetPicker data={data} drawingKey="same" onSelect={latest} /></React.StrictMode>)
  expect(second().checked).toBe(false)
  expect(submit().disabled).toBe(true)
  fireEvent.click(submit())
  expect(latest).toHaveBeenCalledTimes(1)
  fireEvent.click(first())
  fireEvent.click(submit())
  expect(latest).toHaveBeenCalledTimes(2)
  expect(latest).toHaveBeenLastCalledWith(['first'])
})

it('selects prototype-named IDs independently without inherited state', () => {
  const ids = ['__proto__', 'constructor', 'toString']
  const onSelect = vi.fn()
  render(<ModuleSetPicker data={{ sets: ids.map((id) => ({ set_id: id, layer: id, block: 'Panel', count: 1 })) }} onSelect={onSelect} />)
  const controls = screen.getAllByRole('checkbox')
  controls.forEach((control) => fireEvent.click(control))
  fireEvent.click(submit())
  expect(onSelect).toHaveBeenLastCalledWith(ids)
  fireEvent.click(controls[1])
  fireEvent.click(submit())
  expect(onSelect).toHaveBeenLastCalledWith(['__proto__', 'toString'])
  fireEvent.click(controls[0])
  fireEvent.click(submit())
  expect(onSelect).toHaveBeenLastCalledWith(['toString'])
  fireEvent.click(controls[2])
  expect(submit().disabled).toBe(true)
  fireEvent.click(submit())
  expect(onSelect).toHaveBeenCalledTimes(3)
})

it('records one selection without submitting its enclosing form', () => {
  const onSelect = vi.fn()
  const onSubmit = vi.fn((event) => event.preventDefault())
  render(<form onSubmit={onSubmit}>
    <ModuleSetPicker data={data} onSelect={onSelect} />
    <button type="submit">Save unrelated form</button>
  </form>)
  fireEvent.click(second())
  fireEvent.click(submit())
  expect(onSelect).toHaveBeenCalledExactlyOnceWith(['second'])
  expect(onSubmit).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Save unrelated form' }))
  expect(onSubmit).toHaveBeenCalledTimes(1)
  expect(onSelect).toHaveBeenCalledTimes(1)
})

it.each([
  [null],
  [{ layer: 'Missing ID', count: 1 }],
  [{ ...data.sets[0], set_id: '' }],
  [{ ...data.sets[0], set_id: '   ' }],
  [{ ...data.sets[0], set_id: 3 }],
  [data.sets[0], { ...data.sets[1], set_id: 'first' }],
])('refuses malformed candidate listing %# without partial selection', (...rows) => {
  const onSelect = vi.fn()
  const view = render(<ModuleSetPicker data={data} onSelect={onSelect} />)
  fireEvent.click(first())
  view.rerender(<ModuleSetPicker data={{ sets: rows }} onSelect={onSelect} />)
  expect(screen.getByText(/Module sets unavailable.*Run the module tool again/)).toBeTruthy()
  expect(screen.getByRole('group').disabled).toBe(true)
  expect(screen.queryAllByRole('checkbox')).toHaveLength(0)
  expect(submit().disabled).toBe(true)
  fireEvent.click(submit())
  expect(onSelect).not.toHaveBeenCalled()
  view.rerender(<ModuleSetPicker data={data} onSelect={onSelect} />)
  expect(first().checked).toBe(false)
  expect(submit().disabled).toBe(true)
})

it('lists competing sets and submits only an explicit pick', () => {
  const onSelect = vi.fn()
  render(<ModuleSetPicker data={data} onSelect={onSelect} />)
  expect(submit().disabled).toBe(true)
  fireEvent.click(second())
  fireEvent.click(submit())
  expect(onSelect).toHaveBeenCalledExactlyOnceWith(['second'])
})

it('cannot submit while a run is active or the drawing binding is stale', () => {
  const onSelect = vi.fn()
  const view = render(<ModuleSetPicker data={data} onSelect={onSelect} running />)
  expect(screen.getByRole('group').disabled).toBe(true)
  view.rerender(<ModuleSetPicker data={data} />)
  expect(screen.getByRole('group').disabled).toBe(true)
  expect(submit().disabled).toBe(true)
  expect(onSelect).not.toHaveBeenCalled()
})

it('does not reuse a choice absent from a refreshed listing', () => {
  const onSelect = vi.fn()
  const view = render(<ModuleSetPicker data={data} onSelect={onSelect} />)
  fireEvent.click(first())
  view.rerender(<ModuleSetPicker data={{ sets: [data.sets[1]] }} onSelect={onSelect} />)
  expect(submit().disabled).toBe(true)
  expect(second().checked).toBe(false)
})

it('submits a design spanning two layers as one recorded selection', () => {
  const onSelect = vi.fn()
  render(<ModuleSetPicker data={data} onSelect={onSelect} />)
  fireEvent.click(first())
  fireEvent.click(second())
  fireEvent.click(submit())
  expect(onSelect).toHaveBeenCalledExactlyOnceWith(['first', 'second'])
})

it('keeps only current choices across selection and running transitions', () => {
  const onSelect = vi.fn()
  const view = render(<ModuleSetPicker data={data} onSelect={onSelect} />)
  fireEvent.click(first())
  fireEvent.click(second())
  fireEvent.click(submit())
  view.rerender(<ModuleSetPicker data={data} onSelect={onSelect} running />)
  expect(screen.getByRole('group').disabled).toBe(true)
  fireEvent.click(submit())
  expect(onSelect).toHaveBeenCalledTimes(1)
  view.rerender(<ModuleSetPicker data={data} onSelect={onSelect} />)
  fireEvent.click(first())
  fireEvent.click(second())
  expect(submit().disabled).toBe(true)
  fireEvent.click(submit())
  expect(onSelect).toHaveBeenCalledTimes(1)
  fireEvent.click(second())
  fireEvent.click(submit())
  expect(onSelect).toHaveBeenLastCalledWith(['second'])
  expect(onSelect).toHaveBeenCalledTimes(2)
})

it('requires a fresh explicit choice for a different drawing even when set IDs repeat', () => {
  const onSelect = vi.fn()
  const view = render(<ModuleSetPicker drawingKey="drawing-a" data={data} onSelect={onSelect} />)
  fireEvent.click(first())
  const next = { sets: [{ ...data.sets[0], layer: 'New drawing' }] }
  view.rerender(<ModuleSetPicker drawingKey="drawing-b" data={next} onSelect={onSelect} />)
  const fresh = screen.getByRole('checkbox', { name: 'New drawing / Panel A: 12 candidates' })
  expect(fresh.checked).toBe(false)
  expect(submit().disabled).toBe(true)
  fireEvent.click(submit())
  expect(onSelect).not.toHaveBeenCalled()
  view.rerender(<ModuleSetPicker drawingKey="drawing-a" data={data} onSelect={onSelect} />)
  expect(first().checked).toBe(false)
  expect(submit().disabled).toBe(true)
  fireEvent.click(second())
  fireEvent.click(submit())
  expect(onSelect).toHaveBeenCalledExactlyOnceWith(['second'])
})

// JSDOM omits keyboard default activation. Simulate its native click default
// only when the key event is not prevented and the control is enabled.
// This is DOM activation simulation, not a real-browser keyboard proof.
function keyboardActivate(element, key) {
  element.focus()
  expect(document.activeElement).toBe(element)
  const allowed = fireEvent.keyDown(element, { key, code: key === ' ' ? 'Space' : 'Enter' })
  fireEvent.keyUp(element, { key, code: key === ' ' ? 'Space' : 'Enter' })
  if (allowed && !element.matches(':disabled')) fireEvent.click(element, { detail: 0 })
}

it('supports labeled native checkboxes and one keyboard-activated submit', () => {
  const onSelect = vi.fn()
  render(<ModuleSetPicker drawingKey="keyboard-drawing" data={data} onSelect={onSelect} />)
  expect(first().tabIndex).toBe(0)
  expect(second().tabIndex).toBe(0)
  keyboardActivate(first(), ' ')
  keyboardActivate(second(), ' ')
  expect(first().checked && second().checked).toBe(true)
  keyboardActivate(submit(), 'Enter')
  expect(onSelect).toHaveBeenCalledExactlyOnceWith(['first', 'second'])
})

it('refreshes labels and counts without changing the explicitly selected IDs', () => {
  const onSelect = vi.fn()
  const view = render(<ModuleSetPicker drawingKey="same-drawing" data={data} onSelect={onSelect} />)
  fireEvent.click(first())
  const refreshed = { sets: [
    { ...data.sets[0], layer: 'Updated roof', block: 'Updated panel', count: 14 },
    { ...data.sets[1], count: 7 },
  ] }
  view.rerender(<ModuleSetPicker drawingKey="same-drawing" data={refreshed} onSelect={onSelect} />)
  expect(screen.queryByRole('checkbox', { name: 'Design / Panel A: 12 candidates' })).toBeNull()
  expect(screen.getByRole('checkbox', { name: 'Updated roof / Updated panel: 14 candidates' }).checked).toBe(true)
  fireEvent.click(screen.getByRole('checkbox', { name: 'Alternate / Panel B: 7 candidates' }))
  fireEvent.click(submit())
  expect(onSelect).toHaveBeenCalledExactlyOnceWith(['first', 'second'])
})

it('never revives a vanished choice when its candidate later reappears', () => {
  const onSelect = vi.fn()
  const view = render(<ModuleSetPicker drawingKey="same-drawing" data={data} onSelect={onSelect} />)
  fireEvent.click(first())
  view.rerender(<ModuleSetPicker drawingKey="same-drawing" data={{ sets: [data.sets[1]] }} onSelect={onSelect} />)
  expect(submit().disabled).toBe(true)
  view.rerender(<ModuleSetPicker drawingKey="same-drawing" data={data} onSelect={onSelect} />)
  expect(first().checked).toBe(false)
  expect(submit().disabled).toBe(true)
  fireEvent.click(submit())
  expect(onSelect).not.toHaveBeenCalled()
  fireEvent.click(first())
  fireEvent.click(submit())
  expect(onSelect).toHaveBeenCalledExactlyOnceWith(['first'])
})

it('explains unavailable selection and accepts an explicit choice after callback restoration', () => {
  const onSelect = vi.fn()
  const view = render(<ModuleSetPicker drawingKey="callback-drawing" data={data} />)
  expect(screen.getByRole('group').disabled).toBe(true)
  expect(screen.getByText('Run the module tool on the current drawing to choose a set.')).toBeTruthy()
  fireEvent.click(first())
  fireEvent.click(submit())
  expect(onSelect).not.toHaveBeenCalled()
  view.rerender(<ModuleSetPicker drawingKey="callback-drawing" data={data} onSelect={onSelect} />)
  expect(screen.getByRole('group').disabled).toBe(false)
  expect(first().checked).toBe(false)
  fireEvent.click(second())
  fireEvent.click(submit())
  expect(onSelect).toHaveBeenCalledExactlyOnceWith(['second'])
})

it('isolates two mounted drawings during interleaved checkbox changes and submissions', () => {
  const onA = vi.fn(), onB = vi.fn()
  render(<>
    <div data-testid="picker-a"><ModuleSetPicker drawingKey="A" data={data} onSelect={onA} /></div>
    <div data-testid="picker-b"><ModuleSetPicker drawingKey="B" data={data} onSelect={onB} /></div>
  </>)
  const a = within(screen.getByTestId('picker-a')), b = within(screen.getByTestId('picker-b'))
  fireEvent.click(a.getAllByRole('checkbox')[0])
  expect(b.getAllByRole('checkbox').every((box) => !box.checked)).toBe(true)
  fireEvent.click(b.getAllByRole('checkbox')[1])
  fireEvent.click(a.getByRole('button'))
  expect(onA).toHaveBeenCalledExactlyOnceWith(['first'])
  expect(onB).not.toHaveBeenCalled()
  fireEvent.click(a.getAllByRole('checkbox')[0])
  fireEvent.click(b.getByRole('button'))
  expect(onB).toHaveBeenCalledExactlyOnceWith(['second'])
  expect(a.getByRole('button').disabled).toBe(true)
  expect(onA).toHaveBeenCalledTimes(1)
})
