import React from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
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
