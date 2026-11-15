import React from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import ModuleSetPicker from './ModuleSetPicker.jsx'

afterEach(cleanup)
const data = { sets: [
  { set_id: 'first', layer: 'Design', block: 'Panel A', count: 12 },
  { set_id: 'second', layer: 'Alternate', block: 'Panel B', count: 9 },
] }

it('lists competing sets and submits only an explicit pick', () => {
  const onSelect = vi.fn()
  render(<ModuleSetPicker data={data} onSelect={onSelect} />)
  const button = screen.getByRole('button', { name: 'Count selected set' })
  expect(button.disabled).toBe(true)
  expect(screen.getByRole('option', { name: 'Design / Panel A: 12 candidates' })).toBeTruthy()
  fireEvent.change(screen.getByLabelText('Design set'), { target: { value: 'second' } })
  fireEvent.click(button)
  expect(onSelect).toHaveBeenCalledExactlyOnceWith(['second'])
})

it('cannot submit while a run is active or the drawing binding is stale', () => {
  const onSelect = vi.fn()
  const view = render(<ModuleSetPicker data={data} onSelect={onSelect} running />)
  expect(screen.getByRole('group').disabled).toBe(true)
  view.rerender(<ModuleSetPicker data={data} />)
  expect(screen.getByRole('group').disabled).toBe(true)
  expect(screen.getByRole('button').disabled).toBe(true)
  expect(onSelect).not.toHaveBeenCalled()
})

it('does not reuse a choice absent from a refreshed listing', () => {
  const onSelect = vi.fn()
  const view = render(<ModuleSetPicker data={data} onSelect={onSelect} />)
  fireEvent.change(screen.getByLabelText('Design set'), { target: { value: 'first' } })
  view.rerender(<ModuleSetPicker data={{ sets: [data.sets[1]] }} onSelect={onSelect} />)
  expect(screen.getByRole('button').disabled).toBe(true)
  expect(screen.getByLabelText('Design set').value).toBe('')
})

it('submits a design spanning two layers as one recorded selection', () => {
  const onSelect = vi.fn()
  render(<ModuleSetPicker data={data} onSelect={onSelect} />)
  const select = screen.getByLabelText('Design set')
  Array.from(select.options).forEach((option) => { option.selected = true })
  fireEvent.change(select)
  fireEvent.click(screen.getByRole('button'))
  expect(onSelect).toHaveBeenCalledExactlyOnceWith(['first', 'second'])
})

it('keeps only current choices across selection and running transitions', () => {
  const onSelect = vi.fn()
  const view = render(<ModuleSetPicker data={data} onSelect={onSelect} />)
  function choose(ids) {
    const select = screen.getByLabelText('Design set')
    Array.from(select.options).forEach((option) => { option.selected = ids.includes(option.value) })
    fireEvent.change(select)
  }
  choose(['first', 'second'])
  fireEvent.click(screen.getByRole('button'))
  expect(onSelect).toHaveBeenCalledExactlyOnceWith(['first', 'second'])
  view.rerender(<ModuleSetPicker data={data} onSelect={onSelect} running />)
  expect(screen.getByRole('group').disabled).toBe(true)
  expect(screen.getByRole('button').disabled).toBe(true)
  fireEvent.click(screen.getByRole('button'))
  expect(onSelect).toHaveBeenCalledTimes(1)
  view.rerender(<ModuleSetPicker data={data} onSelect={onSelect} />)
  choose([])
  expect(screen.getByRole('button').disabled).toBe(true)
  fireEvent.click(screen.getByRole('button'))
  expect(onSelect).toHaveBeenCalledTimes(1)
  choose(['second'])
  fireEvent.click(screen.getByRole('button'))
  expect(onSelect).toHaveBeenLastCalledWith(['second'])
  expect(onSelect).toHaveBeenCalledTimes(2)
})
