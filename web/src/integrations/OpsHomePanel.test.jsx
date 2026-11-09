import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, renderHook, screen, within } from '@testing-library/react'
import OpsHomePanel from './OpsHomePanel.jsx'
import useHomeLights from './useHomeLights.js'

const surface = vi.hoisted(() => ({ frame: undefined }))
vi.mock('../site/SurfaceFrame.jsx', () => ({ useSurfaceFrame: () => surface.frame }))

const states = [
  { entity_id: 'light.desk', state: 'on', attributes: { friendly_name: 'Desk', brightness: 128, rgb_color: [12, 34, 56], token: 'private-value' } },
  { entity_id: 'light.hall', state: 'off', attributes: { friendly_name: 'Hall', brightness: 0 } },
  { entity_id: 'sensor.room', state: 'on', attributes: { friendly_name: 'Room sensor' } },
  { entity_id: 'light.bad id', state: 'on', attributes: { friendly_name: 'Malformed' } },
]
const success = (body = states) => ({ result: { status: 'ok', data: { status: 200, body_json: body } } })
let onCommand
let onGetCommand
let device
let visibility
const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve() })
const advance = (ms) => act(async () => { await vi.advanceTimersByTimeAsync(ms) })
function props(active = true) { return { deviceId: 'device-1', onCommand, onGetCommand, active } }
function hide(value) {
  act(() => { visibility = value; document.dispatchEvent(new Event('visibilitychange')) })
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-11-09T12:00:00Z'))
  visibility = 'visible'
  vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility)
  onCommand = vi.fn().mockResolvedValue({ command_id: 'read-1' })
  onGetCommand = vi.fn().mockResolvedValue(success())
  device = { device_id: 'device-1', label: 'Home mini', last_seen_at: new Date().toISOString(), status: 'active',
    packages: [{ package: 'homeassistant', version: '2026.11.0', status: 'running' }] }
  surface.frame = { devices: { devices: [device], onCommand, onGetCommand, onToggle: vi.fn() } }
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

it('renders nothing outside a surface frame', () => {
  surface.frame = undefined
  const { container } = render(<OpsHomePanel active />)
  expect(container).toBeEmptyDOMElement()
  expect(onCommand).not.toHaveBeenCalled()
})

it('opens Devices when no home device is linked', () => {
  surface.frame.devices.devices = []
  render(<OpsHomePanel active />)
  expect(screen.getByText('No home device linked')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Open Devices' }))
  expect(surface.frame.devices.onToggle).toHaveBeenCalledWith(true)
  expect(onCommand).not.toHaveBeenCalled()
})

it.each(['Offline', 'Paused', 'Revoked'])('shows the device label and %s without reading lights', (state) => {
  if (state === 'Offline') device.last_seen_at = '2026-11-01T00:00:00Z'
  else device.status = state.toLowerCase()
  render(<OpsHomePanel active />)
  expect(screen.getByText('Home mini')).toBeInTheDocument()
  expect(screen.getByText(state)).toBeInTheDocument()
  expect(onCommand).not.toHaveBeenCalled()
})

it.each([[], [{ package: 'homeassistant', status: 'stopped' }]])('shows Home Assistant setup state for packages %j', (packages) => {
  device.packages = packages
  render(<OpsHomePanel active />)
  expect(screen.getByText('Home Assistant is not set up')).toBeInTheDocument()
  expect(onCommand).not.toHaveBeenCalled()
})

it('renders the live header and only valid light tiles with brightness and colour', async () => {
  const { container } = render(<OpsHomePanel active />)
  await flush()
  expect(screen.getByRole('region', { name: 'Home' })).toBeInTheDocument()
  expect(screen.getByText('Online')).toBeInTheDocument()
  expect(screen.getByText('Home Assistant')).toBeInTheDocument()
  expect(screen.getByText('2026.11.0')).toBeInTheDocument()
  const tiles = within(screen.getByRole('list', { name: 'Lights' })).getAllByRole('listitem')
  expect(tiles).toHaveLength(2)
  expect(within(tiles[0]).getByText('Desk')).toBeInTheDocument()
  expect(within(tiles[0]).getByText('On')).toBeInTheDocument()
  expect(within(tiles[0]).getByText('50%')).toBeInTheDocument()
  expect(within(tiles[1]).getByText('Off')).toBeInTheDocument()
  expect(within(tiles[1]).getByText('0%')).toBeInTheDocument()
  expect(screen.getByTestId('light-swatch')).toHaveStyle({ background: 'rgb(12, 34, 56)' })
  expect(screen.getByText(`Updated ${new Date().toLocaleTimeString()}`)).toBeInTheDocument()
  expect(container.innerHTML).not.toMatch(/private-value|Room sensor|Malformed/)
  expect(onCommand).toHaveBeenCalledWith('device-1', 'http.proxy', { package: 'homeassistant', method: 'GET', path: '/api/states' })
  expect(onGetCommand).toHaveBeenCalledWith('device-1', 'read-1')
  expect(screen.queryByRole('region', { name: 'Home Assistant setup' })).not.toBeInTheDocument()
})

it('bounds projection and omits invalid brightness and colour values', async () => {
  onGetCommand.mockResolvedValue(success(Array.from({ length: 105 }, (_, index) => ({ entity_id: `light.bulb_${index}`, state: 'on',
    attributes: { brightness: 256, rgb_color: [1, 2, 3.5] } }))))
  const { result } = renderHook(() => useHomeLights(props()))
  await flush()
  expect(result.current.lights).toHaveLength(100)
  expect(result.current.lights[0]).toEqual({ entity_id: 'light.bulb_0', name: 'light.bulb_0', state: 'on', brightness_pct: null, rgb: null })
})

it('polls at 15 seconds after success and Refresh triggers one immediate read', async () => {
  render(<OpsHomePanel active />)
  await flush()
  expect(onCommand).toHaveBeenCalledTimes(1)
  await advance(14999)
  expect(onCommand).toHaveBeenCalledTimes(1)
  await advance(1)
  expect(onCommand).toHaveBeenCalledTimes(2)
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await flush()
  expect(onCommand).toHaveBeenCalledTimes(3)
  await advance(15000)
  expect(onCommand).toHaveBeenCalledTimes(4)
})

it('never overlaps reads while waiting for a queued command result', async () => {
  onGetCommand.mockResolvedValue({ result: null })
  const { result, unmount } = renderHook(() => useHomeLights(props()))
  await flush()
  act(() => { result.current.refresh(); result.current.refresh() })
  await advance(45000)
  expect(onCommand).toHaveBeenCalledTimes(1)
  onGetCommand.mockResolvedValue(success())
  await advance(1000)
  expect(result.current.status).toBe('ready')
  await advance(15000)
  expect(onCommand).toHaveBeenCalledTimes(2)
  unmount()
  const calls = onGetCommand.mock.calls.length
  await advance(60000)
  expect(onGetCommand).toHaveBeenCalledTimes(calls)
})

it('backs off to 30, 60, then at most 120 seconds and resets on success', async () => {
  onGetCommand.mockResolvedValue({ result: { status: 'error', error: { code: 'upstream_error' } } })
  const { result } = renderHook(() => useHomeLights(props()))
  await flush()
  expect(result.current.status).toBe('error')
  await advance(29999)
  expect(onCommand).toHaveBeenCalledTimes(1)
  await advance(1)
  expect(onCommand).toHaveBeenCalledTimes(2)
  await advance(60000)
  expect(onCommand).toHaveBeenCalledTimes(3)
  await advance(120000)
  expect(onCommand).toHaveBeenCalledTimes(4)
  onGetCommand.mockResolvedValue(success())
  await advance(120000)
  expect(result.current.status).toBe('ready')
  await advance(15000)
  expect(onCommand).toHaveBeenCalledTimes(6)
})

it('keeps the last lights and marks tiles stale after 60 seconds without success', async () => {
  render(<OpsHomePanel active />)
  await flush()
  onGetCommand.mockResolvedValue(success({ message: 'invalid body' }))
  await advance(60001)
  expect(screen.getByText('Could not update lights')).toBeInTheDocument()
  expect(screen.getAllByRole('listitem')).toHaveLength(2)
  for (const tile of screen.getAllByRole('listitem')) expect(tile).toHaveClass('stale')
  onGetCommand.mockResolvedValue(success())
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await flush()
  for (const tile of screen.getAllByRole('listitem')) expect(tile).not.toHaveClass('stale')
})

it('does not poll while inactive or hidden and resumes when visible', async () => {
  const { result, rerender } = renderHook(({ active }) => useHomeLights(props(active)), { initialProps: { active: false } })
  act(() => result.current.refresh())
  await advance(30000)
  expect(result.current.status).toBe('idle')
  expect(onCommand).not.toHaveBeenCalled()
  hide('hidden')
  rerender({ active: true })
  await advance(30000)
  expect(onCommand).not.toHaveBeenCalled()
  hide('visible')
  await flush()
  expect(onCommand).toHaveBeenCalledTimes(1)
  hide('hidden')
  await advance(60000)
  expect(onCommand).toHaveBeenCalledTimes(1)
  hide('visible')
  await flush()
  expect(onCommand).toHaveBeenCalledTimes(2)
  rerender({ active: false })
  await advance(60000)
  expect(onCommand).toHaveBeenCalledTimes(2)
})

it('waits for an outstanding transport before reading a new device and ignores its old result', async () => {
  let settle
  onCommand.mockImplementationOnce(() => new Promise((resolve) => { settle = resolve }))
  const { result, rerender } = renderHook(({ deviceId }) => useHomeLights({ ...props(), deviceId }), { initialProps: { deviceId: 'old-device' } })
  rerender({ deviceId: 'new-device' })
  expect(onCommand).toHaveBeenCalledTimes(1)
  await act(async () => settle({ command_id: 'old-read' }))
  await flush()
  expect(onCommand).toHaveBeenCalledTimes(2)
  expect(onCommand.mock.calls[1][0]).toBe('new-device')
  expect(onGetCommand).not.toHaveBeenCalledWith('old-device', 'old-read')
  expect(result.current.status).toBe('ready')
})

it('cancels result polling and discards late results on unmount', async () => {
  let settle
  onGetCommand.mockImplementation(() => new Promise((resolve) => { settle = resolve }))
  const { unmount } = renderHook(() => useHomeLights(props()))
  await flush()
  unmount()
  await act(async () => settle(success()))
  await advance(120000)
  expect(onCommand).toHaveBeenCalledTimes(1)
  expect(onGetCommand).toHaveBeenCalledTimes(1)
})
