import React from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import SolarToolForm from './SolarToolForm.jsx'

afterEach(cleanup)

const tool = {
  name: 'solar-extra', label: 'Solar inputs', capabilities: ['drawing.write'],
  solar: {
    schema: 'leaf.solar-tool-view.v1', name: 'solar-extra', family: 'stringing',
    wave: 1, order: 10, entitlement: 'run_write', interaction: { mode: 'form' },
  },
  params: { type: 'object', properties: {
    drawing_id: { type: 'string', default: 'must-not-submit' },
    expected_rev: { type: 'integer', default: 1 },
    cancel: { type: 'boolean', default: false },
    changes: { type: 'object', default: {} },
    initialize: { type: ['object', 'null'], default: null },
  } },
}

it('SolarToolForm renders only the visible keys', () => {
  const { container } = render(<SolarToolForm tool={tool} onSubmit={vi.fn()} onClose={vi.fn()} />)
  expect(screen.getByRole('region', { name: 'Solar inputs parameters' }).id).toBe('solar-tool-form')
  expect(screen.getByRole('heading', { name: 'Solar inputs' })).toBeTruthy()
  for (const label of ['Expected rev', 'Cancel', 'Changes', 'Initialize']) {
    expect(screen.getByLabelText(label)).toBeTruthy()
  }
  expect(container.querySelectorAll('input')).toHaveLength(4)
  expect(screen.queryByLabelText('Drawing id')).toBeNull()
  expect(screen.getByRole('button', { name: 'Review & run' }).disabled).toBe(false)
})

it('SolarToolForm submits defaults merged with edits once and closes', () => {
  const onSubmit = vi.fn()
  const onClose = vi.fn()
  render(<SolarToolForm tool={tool} onSubmit={onSubmit} onClose={onClose} />)
  fireEvent.change(screen.getByLabelText('Expected rev'), { target: { value: '3' } })
  fireEvent.click(screen.getByRole('button', { name: 'Review & run' }))
  expect(onSubmit).toHaveBeenCalledTimes(1)
  expect(onSubmit).toHaveBeenCalledWith(tool, { expected_rev: 3, cancel: false, changes: {}, initialize: null })
  expect(onClose).toHaveBeenCalledTimes(1)
  expect(onSubmit.mock.invocationCallOrder[0]).toBeLessThan(onClose.mock.invocationCallOrder[0])
})

it('SolarToolForm Escape and Cancel close without a run', () => {
  const onSubmit = vi.fn()
  const onClose = vi.fn()
  const onParentKeyDown = vi.fn()
  render(<div onKeyDown={onParentKeyDown}><SolarToolForm tool={tool} onSubmit={onSubmit} onClose={onClose} /></div>)
  fireEvent.keyDown(screen.getByLabelText('Expected rev'), { key: 'Escape' })
  expect(onClose).toHaveBeenCalledTimes(1)
  expect(onSubmit).not.toHaveBeenCalled()
  expect(onParentKeyDown).not.toHaveBeenCalled()
  onClose.mockClear()
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
  expect(onClose).toHaveBeenCalledTimes(1)
  expect(onSubmit).not.toHaveBeenCalled()
})
