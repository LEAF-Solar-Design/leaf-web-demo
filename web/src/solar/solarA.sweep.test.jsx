import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import SolarCivilPanel from './SolarCivilPanel.jsx'
import SolarPhysicalReadPanel from './SolarPhysicalReadPanel.jsx'
import SolarTrackerRowsPanel from './SolarTrackerRowsPanel.jsx'
import { civilReason } from './solarCivilClient.js'

afterEach(cleanup)
const civilClient = () => ({
  getCivil: vi.fn().mockResolvedValue({ ok: true, value: {
    schema: 'leaf.solar-civil-view-response.v1', stored: false, head: null,
    preview: null, standing: null, grade_pads: 0,
  } }),
  runCivilOperation: vi.fn(),
})

it('S1-112 civil explains a missing checkout', async () => {
  render(<SolarCivilPanel drawingId="solar" client={civilClient()} disabled
    disabledReason="Take the drawing checkout before running civil operations." />)
  expect(screen.getByText('Take the drawing checkout before running civil operations.')).toBeInTheDocument()
  expect(screen.queryByText('A run is in progress, so wait for it to finish.')).toBeNull()
  await waitFor(() => expect(screen.getByRole('region').dataset.state).toBe('ready'))
})

it('S1-112 physical reads explains a failed drawing refresh', async () => {
  render(<SolarPhysicalReadPanel drawingId="solar" disabled
    disabledReason="The drawing could not be refreshed. Refresh the drawing before continuing." />)
  expect(screen.getByText('The drawing could not be refreshed. Refresh the drawing before continuing.')).toBeInTheDocument()
  await screen.findByRole('alert')
})

it('S1-119 read failures name the read instead of publishing', async () => {
  render(<SolarPhysicalReadPanel drawingId="solar" terrainClient={{ getTerrain: vi.fn().mockResolvedValue({ ok: false }) }} />)
  expect(await screen.findByRole('alert')).toHaveTextContent('The current physical state could not be read. Refresh it before running this read.')
})

it('S1-125 no fitting frames asks for geometry changes', () => {
  expect(civilReason('FRAMES_PILES_NO_FRAMES_FIT')).toContain('Enlarge the boundary')
  expect(civilReason('FRAMES_PILES_NO_FRAMES_FIT')).not.toContain('refresh')
})

it('S1-125 structured fields include accessible examples', async () => {
  render(<SolarCivilPanel drawingId="solar" client={civilClient()} />)
  expect(screen.getByLabelText('Boundary')).toHaveAccessibleDescription(/JSON example:.*100/)
  expect(screen.getByLabelText('Preset')).toHaveAccessibleDescription(/JSON example:.*Name/)
  fireEvent.change(screen.getByLabelText('Operation'), { target: { value: 'piling-generate' } })
  expect(screen.getByLabelText('Pile template')).toHaveAccessibleDescription(/JSON example:.*Stations/)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Run' })).not.toBeDisabled())
})

it('S1-125 JSON syntax failures differ from invalid geometry', async () => {
  render(<SolarCivilPanel drawingId="solar" client={civilClient()} />)
  const run = screen.getByRole('button', { name: 'Run' })
  await waitFor(() => expect(run).not.toBeDisabled())
  fireEvent.change(screen.getByLabelText('Boundary'), { target: { value: '[' } })
  fireEvent.click(run)
  expect(screen.getByRole('alert')).toHaveTextContent('Enter valid JSON for boundary.')
  fireEvent.change(screen.getByLabelText('Boundary'), { target: { value: '[]' } })
  fireEvent.change(screen.getByLabelText('Preset'), { target: { value: '{"Name":"Full"}' } })
  fireEvent.click(run)
  expect(screen.getByRole('alert')).toHaveTextContent('at least three coordinate pairs')
})

it('S1-127 civil read preserves expired session guidance', async () => {
  const client = civilClient()
  client.getCivil.mockResolvedValue({ ok: false, code: 'UNAUTHENTICATED' })
  render(<SolarCivilPanel drawingId="solar" client={client} />)
  expect(await screen.findByRole('alert')).toHaveTextContent('Sign in again')
  expect(screen.getByRole('button', { name: 'Refresh physical state' })).not.toBeDisabled()
})

it('S1-127 tracker read preserves plan guidance', async () => {
  render(<SolarTrackerRowsPanel drawingId="solar" checkoutHeld
    client={{ createTrackerRows: vi.fn() }} readIntake={vi.fn()}
    terrainClient={{ getTerrain: vi.fn().mockResolvedValue({ ok: false, code: 'ENTITLEMENT_REQUIRED' }) }} />)
  await waitFor(() => expect(screen.getByTestId('solar-tracker-rows-status')).toHaveTextContent('workspace plan does not include'))
})

it('S1-127 civil read gives timeout recovery guidance', async () => {
  const client = civilClient()
  client.getCivil.mockResolvedValue({ ok: false, code: 'TERRAIN_CLIENT_TIMEOUT' })
  render(<SolarCivilPanel drawingId="solar" client={client} />)
  expect(await screen.findByRole('alert')).toHaveTextContent('did not answer in time')
  fireEvent.click(screen.getByRole('button', { name: 'Refresh physical state' }))
  await waitFor(() => expect(client.getCivil).toHaveBeenCalledTimes(2))
})
