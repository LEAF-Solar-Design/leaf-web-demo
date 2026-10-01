import React from 'react'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import declaration from '../../../server/solar_tools/solar_design_presets.json'
import snapshot from '../../../docs/parity/evidence/ground/generate/profile-settings.json'
import SolarPresetForm from './SolarPresetForm.jsx'
import { presetFormSpec, presetDefaults, SOLAR_PRESET_REASONS, SOLAR_PRESET_NOTES } from './solarPresetModel.js'

afterEach(cleanup)

const tool = {
  name: 'solar-design-presets', label: 'Design presets', capabilities: ['drawing.write'],
  solar: { schema: 'leaf.solar-tool-view.v1', name: 'solar-design-presets', family: 'settings',
    wave: 2, order: 12, entitlement: 'run_write', interaction: { mode: 'form' } },
  params: declaration.record.params,
}
const keys = Object.keys(declaration.record.params.properties.current_settings.properties)
const SNAP = Object.fromEntries(keys.map((key) => [key, snapshot[key]]))
const row = (n, name, value) => ({ id: { entity_id: n }, type: 'report', quantity: 1, unit: 'each', name, value })
const LIST_TWO = { schema: 'leaf.solar-design-presets.v1', active_prefix: 'B', current_settings: { ...SNAP }, rows: [],
  list_rows: [row('report-profile-1', 'profile-1', 'A Alpha'), row('report-profile-2', 'profile-2', 'B Beta'), row('report-profiles', 'profiles', 2)] }
const LIST_EMPTY = { schema: LIST_TWO.schema, active_prefix: null, current_settings: null, rows: [], list_rows: [row('report-profiles', 'profiles', 0)] }
const LIST_257 = { schema: LIST_TWO.schema, active_prefix: 'A', current_settings: { ...SNAP, StringLayer: 'x'.repeat(257) }, rows: [],
  list_rows: [row('report-profile-1', 'profile-1', 'A Alpha'), row('report-profiles', 'profiles', 1)] }
const type = (label, value) => {
  const el = screen.getByLabelText(label)
  fireEvent.change(el, { target: { value } })
  fireEvent.blur(el)
}
const button = () => screen.getByRole('button', { name: 'Review & run' })
const supply = () => fireEvent.click(screen.getByLabelText('Supply the settings'))
const action = (value) => type('Action', value)
const reason = (key) => expect(screen.getByTestId('solar-preset-reason').textContent).toBe(SOLAR_PRESET_REASONS[key])
function open(props = {}) {
  const onSubmit = vi.fn(), onClose = vi.fn()
  return { ...render(<SolarPresetForm tool={tool} onSubmit={onSubmit} onClose={onClose} {...props} />), onSubmit, onClose }
}

it('PF1 opens typed controls with drawing settings selected', () => {
  const { container } = open()
  expect(screen.getByRole('region', { name: 'Design presets parameters' }).id).toBe('solar-tool-form')
  expect(screen.getByRole('heading', { name: 'Design presets' })).toBeTruthy()
  expect(screen.getByLabelText('Graph revision').value).toBe('')
  const select = screen.getByLabelText('Action')
  expect(select.tagName).toBe('SELECT')
  expect(select.value).toBe('Create')
  expect(within(select).getAllByRole('option').map((option) => option.value)).toEqual(['Create', 'Swap', 'Delete'])
  expect(screen.getByLabelText('Preset name')).toBeTruthy()
  expect(screen.queryByLabelText('Preset prefix')).toBeNull()
  expect(screen.getByLabelText("Use the drawing's current settings").checked).toBe(true)
  expect(screen.queryByLabelText('String layer')).toBeNull()
  expect([...container.querySelectorAll('input')].some((input) => input.value === '{}')).toBe(false)
  reason('revision_invalid')
  expect(button().disabled).toBe(true)
})

it('PF2 submits Create with settings omitted then closes', () => {
  const { onSubmit, onClose } = open()
  type('Graph revision', '0')
  type('Preset name', '  Alpha ')
  fireEvent.click(button())
  expect(onSubmit).toHaveBeenCalledTimes(1)
  expect(onSubmit).toHaveBeenCalledWith(tool, { expected_rev: 0, subcommand: 'Create', name: 'Alpha' })
  expect(Object.hasOwn(onSubmit.mock.calls[0][1], 'current_settings')).toBe(false)
  expect(onClose).toHaveBeenCalledTimes(1)
  expect(onSubmit.mock.invocationCallOrder[0]).toBeLessThan(onClose.mock.invocationCallOrder[0])
})

it('PF3 refuses a complete default object and explains layer syncing', () => {
  const { container, onSubmit, onClose } = open()
  supply()
  expect(screen.getByText(SOLAR_PRESET_NOTES['supply_sync'])).toBeTruthy()
  const fields = container.querySelector('.solar-preset-fields')
  expect(fields.querySelectorAll('input')).toHaveLength(60)
  expect(fields.querySelectorAll('input[type="text"]')).toHaveLength(53)
  expect(fields.querySelectorAll('input[type="checkbox"]')).toHaveLength(7)
  expect(screen.getByText(SOLAR_PRESET_NOTES['layer_blank'])).toBeTruthy()
  type('Graph revision', '0')
  type('Preset name', 'Alpha')
  reason('settings_all_default')
  expect(button().disabled).toBe(true)
  fireEvent.click(button())
  expect(onSubmit).not.toHaveBeenCalled()
  expect(onClose).not.toHaveBeenCalled()
  type('String layer', 'Strings A')
  type('Homerun layer', 'Homeruns')
  type('Panel group layer', 'Groups')
  expect(screen.queryByText(SOLAR_PRESET_NOTES['layer_blank'])).toBeNull()
  expect(button().disabled).toBe(false)
  fireEvent.click(button())
  expect(onSubmit).toHaveBeenCalledTimes(1)
  expect(onSubmit.mock.calls[0][1].current_settings).toEqual({ ...presetDefaults(presetFormSpec(tool.params)), StringLayer: 'Strings A', HomeRunLayer: 'Homeruns', PanelGroupLayer: 'Groups' })
  expect(Object.keys(onSubmit.mock.calls[0][1].current_settings)).toEqual(keys)
})

it('PF4 displays the List and prefills supplied settings', () => {
  const { onSubmit } = open({ listing: LIST_TWO, revision: 2 })
  expect(screen.getByText('A Alpha')).toBeTruthy()
  expect(screen.getByText('B Beta (active)')).toBeTruthy()
  expect(screen.getByLabelText('Graph revision').value).toBe('2')
  supply()
  for (const [label, value] of [['Vmp', '44.64'], ['Max panel gap', '120'], ['MPPT count', '12'], ['String layer', 'String']]) expect(screen.getByLabelText(label).value).toBe(value)
  expect(screen.getByLabelText('Use L2 collectors').checked).toBe(true)
  type('Preset name', 'Gamma')
  fireEvent.click(button())
  expect(onSubmit).toHaveBeenCalledTimes(1)
  expect(onSubmit).toHaveBeenCalledWith(tool, { expected_rev: 2, subcommand: 'Create', name: 'Gamma', current_settings: SNAP })
})

it('PF5 preserves an overlong layer and blocks drawing and supplied commits until corrected', () => {
  const { onSubmit } = open({ listing: LIST_257, revision: 1 })
  const unfit = screen.getByTestId('solar-preset-unfit')
  expect(unfit.textContent).toContain('String layer on this drawing has 257 characters, and a preset stores at most 256.')
  expect(unfit.querySelector('code').textContent).toBe('x'.repeat(257))
  reason('name_invalid')
  type('Preset name', 'Beta')
  reason('drawing_settings_unfit')
  expect(button().disabled).toBe(true)
  action('Swap')
  type('Preset prefix', 'A')
  reason('drawing_settings_unfit')
  expect(button().disabled).toBe(true)
  action('Create')
  expect(screen.getByLabelText('Preset name').value).toBe('Beta')
  supply()
  const layer = screen.getByLabelText('String layer')
  expect(layer.value).toHaveLength(257)
  expect(layer.hasAttribute('maxlength')).toBe(false)
  fireEvent.blur(layer)
  expect(layer.getAttribute('aria-invalid')).toBe('true')
  expect(document.getElementById(layer.getAttribute('aria-describedby')).textContent).toBe('This value has 257 characters, and a preset stores at most 256.')
  reason('settings_invalid')
  type('String layer', 'x'.repeat(256))
  fireEvent.click(button())
  expect(onSubmit).toHaveBeenCalledTimes(1)
  expect(onSubmit.mock.calls[0][1].current_settings).toEqual({ ...SNAP, StringLayer: 'x'.repeat(256) })
})

it('PF6 requires supplied settings for an empty drawing', () => {
  open({ listing: LIST_EMPTY })
  expect(screen.getByText('No presets on this drawing yet.')).toBeTruthy()
  type('Graph revision', '0')
  type('Preset name', 'Alpha')
  reason('settings_required')
  expect(button().disabled).toBe(true)
})

it('PF7 submits Swap and Delete with a valid prefix only', () => {
  const first = open()
  action('Swap')
  expect(screen.queryByLabelText('Preset name')).toBeNull()
  expect(screen.getByLabelText('Preset prefix')).toBeTruthy()
  type('Graph revision', '2')
  type('Preset prefix', 'AB')
  reason('prefix_invalid')
  expect(button().disabled).toBe(true)
  type('Preset prefix', 'a')
  fireEvent.click(button())
  expect(first.onSubmit).toHaveBeenCalledWith(tool, { expected_rev: 2, subcommand: 'Swap', prefix: 'a' })
  first.unmount()
  const second = open()
  action('Delete')
  type('Graph revision', '3')
  type('Preset prefix', 'B')
  fireEvent.click(button())
  expect(second.onSubmit).toHaveBeenCalledWith(tool, { expected_rev: 3, subcommand: 'Delete', prefix: 'B' })
})

it('PF8 shows invalid field messages after blur', () => {
  const { onSubmit } = open()
  type('Graph revision', '0')
  type('Preset name', 'Alpha')
  supply()
  const mppt = screen.getByLabelText('MPPT count')
  fireEvent.change(mppt, { target: { value: '2.5' } })
  expect(mppt.hasAttribute('aria-invalid')).toBe(false)
  expect(screen.queryByText('Enter a whole number.')).toBeNull()
  fireEvent.blur(mppt)
  expect(mppt.getAttribute('aria-invalid')).toBe('true')
  expect(document.getElementById(mppt.getAttribute('aria-describedby')).textContent).toBe('Enter a whole number.')
  type('Vmp', 'abc')
  expect(screen.getByText('Enter a number.')).toBeTruthy()
  type('Max panel gap', '2000000')
  expect(screen.getByText('Enter a value from -1000000 to 1000000.')).toBeTruthy()
  reason('settings_invalid')
  expect(button().disabled).toBe(true)
  fireEvent.click(button())
  expect(onSubmit).not.toHaveBeenCalled()
})

it('PF9 handles Escape and locks repeated keyboard submits', () => {
  const onSubmit = vi.fn(), onClose = vi.fn(), parent = vi.fn()
  const first = render(<div onKeyDown={parent}><SolarPresetForm tool={tool} onSubmit={onSubmit} onClose={onClose} /></div>)
  fireEvent.keyDown(screen.getByLabelText('Preset name'), { key: 'Escape' })
  expect(onClose).toHaveBeenCalledTimes(1)
  expect(onSubmit).not.toHaveBeenCalled()
  expect(parent).not.toHaveBeenCalled()
  first.unmount()
  const second = open()
  expect(screen.getByLabelText('Action').tagName).toBe('SELECT')
  for (const radio of screen.getAllByRole('radio')) expect(radio.type).toBe('radio')
  type('Graph revision', '0')
  type('Preset name', 'Alpha')
  fireEvent.submit(second.container.querySelector('form'))
  fireEvent.submit(second.container.querySelector('form'))
  expect(second.onSubmit).toHaveBeenCalledTimes(1)
  expect(second.onClose).toHaveBeenCalledTimes(1)
})

it('PF10 keeps an unsupported declaration closed', () => {
  const params = structuredClone(tool.params)
  delete params.properties.current_settings
  const { container, onClose } = open({ tool: { ...tool, params } })
  reason('preset_declaration_unsupported')
  expect(screen.queryByRole('button', { name: 'Review & run' })).toBeNull()
  expect(container.querySelectorAll('input')).toHaveLength(0)
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
  expect(onClose).toHaveBeenCalledTimes(1)
})

it('PF11 prefills only a valid numeric revision prop', () => {
  for (const revision of [7, -1, 1.5, '7', 2147483648]) {
    const instance = open({ revision })
    expect(screen.getByLabelText('Graph revision').value).toBe(revision === 7 ? '7' : '')
    instance.unmount()
  }
})

it('PF12 names an unreadable List but permits ordinary drawing mode', () => {
  const { onSubmit } = open({ listing: { schema: 'x' } })
  expect(screen.getByTestId('solar-preset-listing').textContent).toBe(SOLAR_PRESET_REASONS['listing_unreadable'])
  expect(screen.queryByTestId('solar-preset-list')).toBeNull()
  type('Graph revision', '0')
  type('Preset name', 'Alpha')
  fireEvent.click(button())
  expect(onSubmit).toHaveBeenCalledWith(tool, { expected_rev: 0, subcommand: 'Create', name: 'Alpha' })
})

it('PF13 never submits from mounting, mode changes or field edits', () => {
  const { onSubmit, rerender } = open({ listing: LIST_TWO, revision: 2 })
  supply()
  type('String layer', 'User layer')
  action('Swap')
  type('Preset prefix', 'A')
  fireEvent.click(screen.getByLabelText("Use the drawing's current settings"))
  action('Create')
  type('Preset name', 'Gamma')
  type('Graph revision', '2')
  supply()
  expect(button().disabled).toBe(false)
  rerender(<SolarPresetForm tool={tool} onSubmit={onSubmit} onClose={vi.fn()} listing={LIST_257} revision={2} />)
  expect(screen.getByLabelText('String layer').value).toBe('User layer')
  expect(onSubmit).not.toHaveBeenCalled()
})

it('PF14 ignores a direct submit of an invalid form', () => {
  const { container, onSubmit, onClose } = open()
  type('Graph revision', '0')
  type('Preset name', 'Alpha')
  supply()
  type('MPPT count', '2.5')
  reason('settings_invalid')
  fireEvent.submit(container.querySelector('form'))
  expect(onSubmit).not.toHaveBeenCalled()
  expect(onClose).not.toHaveBeenCalled()
})

it('PF15 shows field messages on a submit attempt before any blur', () => {
  const { container, onSubmit } = open()
  type('Graph revision', '0')
  type('Preset name', 'Alpha')
  supply()
  const mppt = screen.getByLabelText('MPPT count')
  fireEvent.change(mppt, { target: { value: '2.5' } })
  expect(mppt.hasAttribute('aria-invalid')).toBe(false)
  expect(screen.queryByText('Enter a whole number.')).toBeNull()
  fireEvent.submit(container.querySelector('form'))
  expect(mppt.getAttribute('aria-invalid')).toBe('true')
  expect(document.getElementById(mppt.getAttribute('aria-describedby')).textContent).toBe('Enter a whole number.')
  expect(onSubmit).not.toHaveBeenCalled()
})

it('PF16 turns the browser validation off on the form', () => {
  const { container } = open()
  const element = container.querySelector('form')
  expect(element.noValidate).toBe(true)
  expect(element.hasAttribute('novalidate')).toBe(true)
})

it('PF17 reports an out-of-bounds numeric List value as unfit in drawing mode', () => {
  const listing = { ...LIST_TWO, current_settings: { ...SNAP, Vmp: 1000001 } }
  const { onSubmit } = open({ listing, revision: 2 })
  type('Preset name', 'Gamma')
  fireEvent.click(screen.getByLabelText("Use the drawing's current settings"))
  fireEvent.click(button())
  reason('drawing_settings_unfit')
  expect(button().disabled).toBe(true)
  expect(onSubmit).not.toHaveBeenCalled()
})

it('PF18 gives the integer field a numeric keyboard', () => {
  open()
  supply()
  expect(screen.getByLabelText('MPPT count').getAttribute('inputmode')).toBe('numeric')
  expect(screen.getByLabelText('Vmp').getAttribute('inputmode')).toBe('decimal')
})
