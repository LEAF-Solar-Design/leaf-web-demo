import React from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import SolarToolForm from './SolarToolForm.jsx'
import declaration from '../../../server/solar_tools/solar_design_presets.json'
import listDeclaration from '../../../server/solar_tools/solar_design_presets_list.json'
import snapshot from '../../../docs/parity/evidence/ground/generate/profile-settings.json'

afterEach(cleanup)

const presetTool = {
  name: 'solar-design-presets', label: 'Design presets', capabilities: ['drawing.write'],
  solar: { schema: 'leaf.solar-tool-view.v1', name: 'solar-design-presets', family: 'settings',
    wave: 2, order: 12, entitlement: 'run_write', interaction: { mode: 'form' } },
  params: declaration.record.params,
}
const SNAP = Object.fromEntries(Object.keys(declaration.record.params.properties.current_settings.properties).map((key) => [key, snapshot[key]]))
const row = (n, name, value) => ({ id: { entity_id: n }, type: 'report', quantity: 1, unit: 'each', name, value })
const LIST_TWO = { schema: 'leaf.solar-design-presets.v1', active_prefix: 'B', current_settings: { ...SNAP }, rows: [],
  list_rows: [row('report-profile-1', 'profile-1', 'A Alpha'), row('report-profile-2', 'profile-2', 'B Beta'), row('report-profiles', 'profiles', 2)] }


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

it('ST4 routes design presets to the typed form', () => {
  const { container } = render(<SolarToolForm tool={presetTool} onSubmit={vi.fn()} onClose={vi.fn()} />)
  expect(screen.getByTestId('solar-preset-form')).toBeTruthy()
  expect(screen.getByRole('region', { name: 'Design presets parameters' }).id).toBe('solar-tool-form')
  expect([...container.querySelectorAll('input')].some((input) => input.value === '{}')).toBe(false)
})

it('ST5 forwards the optional listing and revision', () => {
  render(<SolarToolForm tool={presetTool} presetListing={LIST_TWO} presetRevision={2} onSubmit={vi.fn()} onClose={vi.fn()} />)
  expect(screen.getByText('B Beta (active)')).toBeTruthy()
  expect(screen.getByLabelText('Graph revision').value).toBe('2')
})

it('ST6 keeps the List tool on the generic form', () => {
  const listTool = { ...presetTool, name: 'solar-design-presets-list', capabilities: ['drawing.read'],
    params: listDeclaration.record.params,
    solar: { ...presetTool.solar, name: 'solar-design-presets-list', entitlement: 'run_read', order: 14 } }
  render(<SolarToolForm tool={listTool} onSubmit={vi.fn()} onClose={vi.fn()} />)
  expect(screen.queryByTestId('solar-preset-form')).toBeNull()
  expect(screen.getByRole('button', { name: 'Review & run' })).toBeTruthy()
})
