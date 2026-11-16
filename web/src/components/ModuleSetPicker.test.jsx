import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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

it.each(['resolve', 'reject'])('ignores old %s after returning to A with a new request', async (terminal) => {
  let oldResolve, oldReject, newResolve
  const old = new Promise((resolve, reject) => { oldResolve = resolve; oldReject = reject })
  const current = new Promise((resolve) => { newResolve = resolve })
  const onSelect = vi.fn().mockReturnValueOnce(old).mockReturnValueOnce(current)
  const view = render(<ModuleSetPicker drawingKey="A" data={data} onSelect={onSelect} />)
  fireEvent.click(first())
  fireEvent.click(submit())
  view.rerender(<ModuleSetPicker drawingKey="B" data={data} onSelect={onSelect} />)
  view.rerender(<ModuleSetPicker drawingKey="A" data={data} onSelect={onSelect} />)
  expect(first().checked).toBe(false)
  fireEvent.click(second())
  fireEvent.click(submit())
  await act(async () => {
    if (terminal === 'resolve') oldResolve()
    else oldReject(new Error('old private detail'))
    await old.catch(() => {})
  })
  expect(submit().disabled).toBe(true)
  expect(screen.getByText('Counting modules.')).toBeTruthy()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.queryByText(/^Counted /)).toBeNull()
  expect(second().checked).toBe(true)
  fireEvent.click(submit())
  expect(onSelect).toHaveBeenCalledTimes(2)
  await act(async () => { newResolve(); await current })
  expect(submit().disabled).toBe(false)
  expect(onSelect.mock.calls).toEqual([[['first']], [['second']]])
  view.rerender(<ModuleSetPicker drawingKey="A" data={{ ...data, selected_sets: [data.sets[1]], count: 9 }} onSelect={onSelect} />)
  expect(screen.getByText('Counted 9 modules on Alternate / Panel B.')).toBeTruthy()
})

it.each(['getter throws', 'reject first', 'resolve first'])('settles custom thenables once: %s', async (mode) => {
  const unhandled = vi.fn()
  window.addEventListener('unhandledrejection', unhandled)
  const thenable = mode === 'getter throws'
    ? Object.defineProperty({}, 'then', { get() { throw new Error('private getter') } })
    : { then(resolve, reject) {
      if (mode === 'reject first') reject(new Error('private rejection'))
      resolve(); reject(new Error('late private rejection')); resolve()
    } }
  const onSelect = vi.fn().mockReturnValueOnce(thenable).mockReturnValueOnce(Promise.resolve())
  try {
    render(<ModuleSetPicker drawingKey="A" data={data} onSelect={onSelect} />)
    fireEvent.click(first())
    fireEvent.click(submit())
    await waitFor(() => expect(submit().disabled).toBe(false))
    if (mode === 'resolve first') expect(screen.queryByRole('alert')).toBeNull()
    else expect(screen.getByRole('alert').textContent).toBe('Unable to start module count. Try counting the selected set again.')
    expect(first().checked).toBe(true)
    expect(screen.queryByText(/private/)).toBeNull()
    expect(screen.queryByText(/^Counted /)).toBeNull()
    fireEvent.click(submit())
    await waitFor(() => expect(submit().disabled).toBe(false))
    expect(screen.queryByRole('alert')).toBeNull()
    expect(onSelect).toHaveBeenCalledTimes(2)
    expect(unhandled).not.toHaveBeenCalled()
  } finally { window.removeEventListener('unhandledrejection', unhandled) }
})

it('does not restore a removed pending choice or claim stale success when its promise settles', async () => {
  let settle
  const pending = new Promise((resolve) => { settle = resolve })
  const onSelect = vi.fn().mockReturnValueOnce(pending).mockResolvedValueOnce(undefined)
  const completed = { ...data, selected_sets: [data.sets[0]], count: 12 }
  const view = render(<ModuleSetPicker data={completed} onSelect={onSelect} />)
  fireEvent.click(first())
  fireEvent.click(submit())
  const remaining = { ...completed, sets: [data.sets[1]] }
  view.rerender(<ModuleSetPicker data={remaining} onSelect={onSelect} />)
  await act(async () => { settle(); await pending })
  expect(second().checked).toBe(false)
  expect(submit().disabled).toBe(true)
  expect(screen.queryByText(/^Counted /)).toBeNull()
  expect(onSelect).toHaveBeenCalledExactlyOnceWith(['first'])
  fireEvent.click(second())
  fireEvent.click(submit())
  await waitFor(() => expect(submit().disabled).toBe(false))
  expect(onSelect).toHaveBeenCalledTimes(2)
  expect(onSelect).toHaveBeenLastCalledWith(['second'])
  view.rerender(<ModuleSetPicker data={{ ...remaining, selected_sets: [data.sets[1]], count: 9 }} onSelect={onSelect} />)
  expect(screen.getByText('Counted 9 modules on Alternate / Panel B.')).toBeTruthy()
})

it('catches promise rejection and retries once per activation with choices intact', async () => {
  const onSelect = vi.fn().mockRejectedValueOnce(new Error('private async detail')).mockResolvedValueOnce(undefined)
  const unhandled = vi.fn()
  window.addEventListener('unhandledrejection', unhandled)
  try {
    render(<ModuleSetPicker data={data} onSelect={onSelect} />)
    fireEvent.click(second())
    fireEvent.click(submit())
    expect(submit().disabled).toBe(true)
    await screen.findByRole('alert')
    expect(second().checked).toBe(true)
    expect(onSelect).toHaveBeenCalledExactlyOnceWith(['second'])
    expect(screen.queryByText(/private async detail/)).toBeNull()
    fireEvent.click(submit())
    await waitFor(() => expect(submit().disabled).toBe(false))
    expect(onSelect).toHaveBeenCalledTimes(2)
    expect(onSelect).toHaveBeenLastCalledWith(['second'])
    expect(second().checked).toBe(true)
    expect(screen.queryByRole('alert')).toBeNull()
    expect(unhandled).not.toHaveBeenCalled()
  } finally {
    window.removeEventListener('unhandledrejection', unhandled)
  }
})

it('does not show a completed summary as the count of newly edited choices', () => {
  const onSelect = vi.fn()
  const view = render(<ModuleSetPicker data={{ ...data, selected_sets: [data.sets[0]], count: 12 }} onSelect={onSelect} />)
  expect(screen.getByText(/^Counted 12/)).toBeTruthy()
  fireEvent.click(second())
  expect(screen.queryByText(/^Counted /)).toBeNull()
  expect(screen.getByText('Choices changed. Count the selected set to update this result.')).toBeTruthy()
  fireEvent.click(submit())
  expect(onSelect).toHaveBeenCalledExactlyOnceWith(['second'])
  expect(screen.queryByText(/^Counted /)).toBeNull()
  view.rerender(<ModuleSetPicker data={{ ...data, selected_sets: [data.sets[1]], count: 9 }} onSelect={onSelect} />)
  expect(screen.getByText('Counted 9 modules on Alternate / Panel B.')).toBeTruthy()
})

it('associates lone-surrogate opaque IDs safely without aliasing a replacement character', () => {
  const ids = ['\ud800', '\udc00', '\ufffd']
  const onSelect = vi.fn()
  const view = render(<ModuleSetPicker data={{ sets: ids.map((set_id) => ({ ...data.sets[0], set_id })) }} onSelect={onSelect} />)
  const inputs = screen.getAllByRole('checkbox')
  const labels = [...view.container.querySelectorAll('label')]
  expect(new Set(inputs.map((input) => input.id)).size).toBe(3)
  labels.forEach((label, index) => expect(label.control).toBe(inputs[index]))
  fireEvent.click(labels[0])
  fireEvent.click(labels[1])
  expect(inputs[2].checked).toBe(false)
  fireEvent.click(submit())
  expect(onSelect).toHaveBeenCalledExactlyOnceWith(ids.slice(0, 2))
})

it.each(['below largest', 'layer mismatch', 'block mismatch'])('rejects impossible or mislabeled summary %s', (kind) => {
  const selected_sets = data.sets.map((set) => ({ ...set }))
  if (kind === 'layer mismatch') selected_sets[0].layer = 'Misleading layer'
  if (kind === 'block mismatch') selected_sets[0].block = 'Misleading block'
  const count = kind === 'below largest' ? 11 : 12
  render(<ModuleSetPicker data={{ ...data, selected_sets, count }} onSelect={vi.fn()} />)
  expect(screen.getByText(/Count summary unavailable/)).toBeTruthy()
  expect(screen.queryByText(/^Counted /)).toBeNull()
})

it.each(['per-set mismatch', 'total exceeds sum'])('refuses inconsistent summary %s', (kind) => {
  const selected_sets = kind === 'per-set mismatch' ? [{ ...data.sets[0], count: 11 }] : data.sets
  const count = kind === 'per-set mismatch' ? 11 : 22
  render(<ModuleSetPicker data={{ ...data, selected_sets, count }} onSelect={vi.fn()} />)
  expect(screen.getByText(/Count summary unavailable/)).toBeTruthy()
  expect(screen.queryByText(/^Counted /)).toBeNull()
})

it('allows a physical-union total smaller than overlapping candidate sums', () => {
  render(<ModuleSetPicker data={{ ...data, selected_sets: data.sets, count: 12 }} onSelect={vi.fn()} />)
  expect(screen.getByText('Counted 12 modules on Design / Panel A, Alternate / Panel B.')).toBeTruthy()
})

it('shows actionable synchronous callback failure and preserves choices for retry', () => {
  const onSelect = vi.fn().mockImplementationOnce(() => { throw new Error('private internal detail') }).mockImplementationOnce(() => {})
  render(<ModuleSetPicker data={data} onSelect={onSelect} />)
  fireEvent.click(second())
  fireEvent.click(submit())
  expect(onSelect).toHaveBeenCalledExactlyOnceWith(['second'])
  expect(second().checked).toBe(true)
  expect(screen.getByRole('alert').textContent).toBe('Unable to start module count. Try counting the selected set again.')
  expect(screen.queryByText(/private internal detail/)).toBeNull()
  fireEvent.click(submit())
  expect(onSelect).toHaveBeenCalledTimes(2)
  expect(onSelect).toHaveBeenLastCalledWith(['second'])
  expect(screen.queryByRole('alert')).toBeNull()
  expect(second().checked).toBe(true)
})

it('announces running, unavailable summary and completed status without moving checkbox focus', () => {
  const onSelect = vi.fn()
  const view = render(<ModuleSetPicker data={data} onSelect={onSelect} />)
  const focused = first()
  focused.focus()
  fireEvent.click(focused)
  view.rerender(<ModuleSetPicker data={data} onSelect={onSelect} running />)
  expect(screen.getByRole('status').getAttribute('aria-live')).toBe('polite')
  expect(screen.getByRole('status').textContent).toContain('Counting modules.')
  expect(document.activeElement).toBe(focused)
  fireEvent.click(submit())
  expect(onSelect).not.toHaveBeenCalled()
  view.rerender(<ModuleSetPicker data={{ ...data, selected_sets: [data.sets[0]], count: -1 }} onSelect={onSelect} />)
  expect(screen.getByRole('status').textContent).toContain('Count summary unavailable')
  expect(document.activeElement).toBe(focused)
  view.rerender(<ModuleSetPicker data={{ ...data, selected_sets: [data.sets[0]], count: 12 }} onSelect={onSelect} />)
  expect(screen.getByRole('status').textContent).toContain('Counted 12 modules')
  expect(document.activeElement).toBe(focused)
  fireEvent.click(submit())
  expect(onSelect).toHaveBeenCalledExactlyOnceWith(['first'])
})

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
