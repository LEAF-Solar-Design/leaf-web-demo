import React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import SolarFrameInspector from './SolarFrameInspector.jsx'

afterEach(cleanup)
const id = n => '00000000-0000-0000-0000-' + String(n).padStart(12, '0')
const scope = { organization_id: id(1), project_id: id(2), drawing_id: id(3), input_version_id: id(4) }
const frame = n => ({ handle: (n + 10).toString(16).toUpperCase(), row: n, column: 3, elevation: 1.25,
  vertices: [[0, 0], [4, 0], [4, 8], [0, 8]], preset: 'Synthetic', pile_template: 'Synthetic piles',
  pose: { tilt_degrees: 12, axis_azimuth_deg: 180, height_at_low_pose_m: 0.8, height_at_high_pose_m: 1.6 } })
const page = (offset = 0, total = 26) => ({ offset, total, page_size: 25, drawing_units: 'm',
  frames: Array.from({ length: Math.min(25, total - offset) }, (_, n) => frame(offset + n)) })
const read = () => fireEvent.click(screen.getByRole('button', { name: 'Read saved frame attributes' }))

describe('saved Solar frame inspector', () => {
  it('shows canonical frame identity, cells, units and preset pose for every page', async () => {
    const client = { inspectFrames: vi.fn(async (_, { offset }) => ({ ok: true, value: page(offset) })) }
    render(<SolarFrameInspector scope={scope} client={client} />)
    expect(client.inspectFrames).not.toHaveBeenCalled()
    read()
    await screen.findByText('Frames 1 to 25 of 26')
    expect(screen.getByLabelText('Generated frame').options).toHaveLength(25)
    expect(screen.getByText('Drawing elevation (metres)')).toBeTruthy()
    expect(screen.getByText('Preset tilt (degrees)')).toBeTruthy()
    expect(screen.getByText('Low pose height (metres)')).toBeTruthy()
    expect(screen.getByText('Synthetic piles')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Generated frame'), { target: { value: frame(24).handle } })
    expect(screen.getByLabelText('Generated frame').value).toBe(frame(24).handle)
    fireEvent.click(screen.getByRole('button', { name: 'Next frames' }))
    await screen.findByText('Frames 26 to 26 of 26')
    expect(screen.getByRole('button', { name: 'Next frames' }).disabled).toBe(true)
    expect(screen.getByLabelText('Generated frame').options).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: 'Previous frames' }))
    await screen.findByText('Frames 1 to 25 of 26')
    expect(client.inspectFrames.mock.calls.map(([, options]) => options.offset)).toEqual([0, 25, 0])
  })

  it('clears a prior snapshot when refresh fails', async () => {
    const client = { inspectFrames: vi.fn().mockResolvedValueOnce({ ok: true, value: page(0, 1) }).mockResolvedValue({ ok: false }) }
    render(<SolarFrameInspector scope={scope} client={client} />)
    read(); await screen.findByText('Synthetic piles')
    read(); await screen.findByRole('alert')
    expect(screen.queryByText('Synthetic piles')).toBeNull()
    expect(screen.queryByLabelText('Generated frame')).toBeNull()
  })

  it.each(['organization_id', 'project_id', 'drawing_id', 'input_version_id'])('discards a late response after %s changes', async field => {
    let resolve
    const client = { inspectFrames: vi.fn(() => new Promise(done => { resolve = done })) }
    const view = render(<SolarFrameInspector scope={scope} client={client} />)
    read()
    await waitFor(() => expect(client.inspectFrames).toHaveBeenCalledOnce())
    const signal = client.inspectFrames.mock.calls[0][1].signal
    view.rerender(<SolarFrameInspector scope={{ ...scope, [field]: id(99) }} client={client} />)
    expect(signal.aborted).toBe(true)
    await act(async () => resolve({ ok: true, value: page(0, 1) }))
    expect(screen.queryByText('Synthetic piles')).toBeNull()
    expect(screen.queryByLabelText('Generated frame')).toBeNull()
  })
})
