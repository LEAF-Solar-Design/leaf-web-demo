import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import SolarSizingForm from './SolarSizingForm.jsx'
import SolarStringComposer from './SolarStringComposer.jsx'
import SolarCombinerIntake from './SolarCombinerIntake.jsx'
import SolarConductorForm from './SolarConductorForm.jsx'
import ground from './__fixtures__/stringComposerGround.json'
import declaration from '../../../server/solar_tools/solar_string_conductors.json'

afterEach(cleanup)
const envelope = (version = 7) => ({ version, head: version, latest: version,
  intake: { solar_design_graph: structuredClone(ground) } })
const button = (name) => screen.getByRole('button', { name })
const addRange = () => {
  fireEvent.click(button('Select row 1 Group 1'))
  fireEvent.change(screen.getByLabelText('First slot'), { target: { value: '1' } })
  fireEvent.change(screen.getByLabelText('Last slot'), { target: { value: '3' } })
  fireEvent.click(button('Add range'))
}

it('S1-115 shows loading without claiming that sizing is unavailable', async () => {
  render(<SolarSizingForm row={{ name: 'solar-string-size' }} drawingId="d1" drawingVersion={7}
    readIntake={() => new Promise(() => {})} onSubmit={vi.fn()} />)
  await act(async () => {})
  expect(screen.getByRole('status').textContent).toBe('Loading the drawing.')
  expect(screen.queryByText('String sizing is unavailable for this drawing.')).toBeNull()
  expect(button('Review & run').disabled).toBe(true)
})

it('S1-115 retries a failed intake read without showing transport details', async () => {
  const readIntake = vi.fn().mockRejectedValueOnce(new Error('GET /api/intake -> 503'))
    .mockResolvedValueOnce(envelope())
  render(<SolarSizingForm row={{ name: 'solar-string-size' }} drawingId="d1" drawingVersion={7}
    readIntake={readIntake} onSubmit={vi.fn()} />)
  await screen.findByRole('alert')
  expect(screen.getByRole('alert').textContent).not.toMatch(/GET|api|503/)
  fireEvent.click(button('Retry loading'))
  await waitFor(() => expect(readIntake).toHaveBeenCalledTimes(2))
  await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
})

it('S1-116 reloads the latest drawing and discards the stale selection queue', async () => {
  const readIntake = vi.fn().mockResolvedValueOnce(envelope()).mockResolvedValue(envelope(8))
  const onSubmit = vi.fn()
  render(<SolarStringComposer row={{ name: 'solar-string-add' }} drawingId="d1" drawingVersion={7}
    readIntake={readIntake} onSubmit={onSubmit} />)
  await screen.findByRole('button', { name: 'Select row 1 Group 1' })
  addRange()
  fireEvent.click(button('Review & run'))
  await screen.findByRole('alert')
  expect(button('Review & run').disabled).toBe(true)
  fireEvent.click(button('Reload drawing'))
  await screen.findByRole('button', { name: 'Select row 1 Group 1' })
  expect(readIntake).toHaveBeenLastCalledWith('d1', 'head')
  expect(within(screen.getByRole('region', { name: 'Selection queue' })).queryAllByRole('listitem')).toHaveLength(0)
  expect(button('Review & run').disabled).toBe(true)
  addRange()
  fireEvent.click(button('Review & run'))
  await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
})

it('S1-116 offers reload after a failed freshness read', async () => {
  const readIntake = vi.fn().mockResolvedValueOnce(envelope()).mockRejectedValueOnce(new Error('Failed to fetch'))
    .mockResolvedValue(envelope())
  render(<SolarStringComposer row={{ name: 'solar-string-add' }} drawingId="d1" drawingVersion={7}
    readIntake={readIntake} onSubmit={vi.fn()} />)
  await screen.findByRole('button', { name: 'Select row 1 Group 1' })
  addRange()
  fireEvent.click(button('Review & run'))
  await screen.findByRole('alert')
  fireEvent.click(button('Reload drawing'))
  await screen.findByRole('button', { name: 'Select row 1 Group 1' })
  expect(screen.queryByRole('alert')).toBeNull()
  expect(button('Review & run').disabled).toBe(true)
})

it('S1-117 labels hardware units and waits for interaction before showing errors', () => {
  render(<SolarCombinerIntake drawingId="d1" client={{}} />)
  const model = screen.getByLabelText('Model')
  expect(model.getAttribute('aria-invalid')).toBe('false')
  expect(screen.getByLabelText('DC voltage (V)').getAttribute('aria-invalid')).toBe('false')
  expect(screen.getByLabelText('AC power (kW)').getAttribute('aria-invalid')).toBe('false')
  fireEvent.blur(model)
  expect(model.getAttribute('aria-invalid')).toBe('true')
  expect(document.getElementById(model.getAttribute('aria-describedby')).textContent).toContain('characters')
  expect(document.body.textContent).not.toContain('code points')
  fireEvent.change(model, { target: { value: 'Combiner' } })
  expect(model.getAttribute('aria-invalid')).toBe('false')
})

it('S1-120 exposes checked state and toggles each conductor string independently', async () => {
  const readIntake = vi.fn().mockResolvedValue({ version: 3, intake: { solar_design_graph: { rev: 7,
    strings: [{ id: 'T1', circuit_tag: 'A1', wire_gauge: '' }, { id: 'T2', circuit_tag: 'A2', wire_gauge: '' }] } } })
  render(<SolarConductorForm row={declaration.record} drawingId="d1" drawingVersion={3}
    readIntake={readIntake} onSubmit={vi.fn()} />)
  const first = await screen.findByRole('checkbox', { name: 'Include A1' })
  const second = screen.getByRole('checkbox', { name: 'Include A2' })
  expect(first.checked).toBe(false)
  fireEvent.click(first)
  fireEvent.click(second)
  expect(first.checked).toBe(true)
  expect(second.checked).toBe(true)
  fireEvent.click(first)
  expect(first.checked).toBe(false)
  expect(second.checked).toBe(true)
  expect(first.closest('tr').hasAttribute('aria-selected')).toBe(false)
})
