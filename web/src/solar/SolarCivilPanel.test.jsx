import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import SolarCivilPanel from './SolarCivilPanel.jsx'
import { CIVIL_ROUTE_REASONS, civilReason } from './solarCivilClient.js'

// Synthetic fixtures use the closed server contracts; no fixture comes from another worktree.
const H = 'a'.repeat(64)
const H2 = 'b'.repeat(64)
const D = 'd'.repeat(64)
const clone = (v) => JSON.parse(JSON.stringify(v))
const headOf = (artifact = H, index = 0, parent = null, drawing = 'solar', project = 'p') => ({
  schema: 'leaf.solar-physical-head.v1', drawing_id: drawing, project_id: project, index, parent,
  state: { schema: 'leaf.solar-artifact-ref.v1', artifact_id: artifact, content_sha256: D,
    media_type: 'application/json', filename: 'physical-state.json', byte_length: 1024, source_version: 1,
    download: `/api/drawings/${drawing}/artifacts/${artifact}` },
})
const previewOf = (frames = 242, piles = 0, terrain = true) => ({
  schema: 'leaf.solar-frames-piles-preview.v1', maturity: 'preview', frames, piles,
  collision_markers: 0, range_markers: 0, terrain,
})
const standingOf = (frames = 242, piles = 0, stale = 0, terrain = true) => ({
  schema: 'leaf.solar-frames-piles-terrain-standing.v1', maturity: 'preview', grid_sha256: terrain ? D : null,
  frames: { state: frames === 0 ? 'absent' : stale ? 'stale' : 'current', checked: frames, stale },
  piles: { state: piles === 0 ? 'absent' : 'current', checked: piles, stale: 0 },
})
const viewOf = (head = headOf(), frames = 242, piles = 0) => ({
  schema: 'leaf.solar-civil-view-response.v1', stored: true, head, preview: previewOf(frames, piles),
  standing: standingOf(frames, piles), grade_pads: 0,
})
const absent = () => ({ schema: 'leaf.solar-civil-view-response.v1', stored: false, head: null, preview: null, standing: null, grade_pads: 0 })
const square = [[0, 0], [100, 0], [100, 100], [0, 100]]
const preset = { Name: 'Full', PileTemplateName: 'Full', Rows: 2, Columns: 1, Piling: { MinPileLengthM: 1, MaxPileLengthM: 4 } }
const template = { Name: 'Full', Stations: [{ Position: 0.5, Length: 2 }] }
const bodyOf = (operation = 'frame-generate', expected = H) => ({
  operation, expected_head: expected,
  ...(operation === 'frame-generate' ? { boundary: clone(square), preset: clone(preset), drawing_units: 'm' } : {}),
  ...(operation === 'piling-generate' ? { preset: clone(preset), pile_template: clone(template) } : {}),
  ...(operation === 'pile-length-range-check' ? { preset: clone(preset) } : {}),
  ...(operation === 'grade-pad' ? { boundary: clone(square), mode: 'Auto', value_du: null } : {}),
})
const resultOf = (operation = 'frame-generate', outcome = 'published', base = H) => ({
  schema: operation === 'grade-pad' ? 'leaf.solar-civil-operation.v1' : 'leaf.solar-frames-piles.v1',
  operation, maturity: 'preview', outcome, created: outcome === 'published',
  drawing_id: 'solar', project_id: 'p', base, units: { drawing_units: 'm', meters_per_unit: 1 },
  terrain: { present: true, sampled: ['frame-generate', 'piling-generate', 'grade-pad'].includes(operation), rows: 2, cols: 2, grid_sha256: D },
  summary: outcome === 'retry' ? null : {
    'frame-generate': { frames_added: 242, frames_off_terrain: 0, preset_name: 'Full' },
    'frame-collision-detect': { frames_checked: 242, collisions: 0 },
    'piling-generate': { native_frames: 242, piles: 1936, piles_replaced: 0, grid_piles: 1936, joint_piles: 0, station_piles: 0, short_trackers: 0, template_name: 'Full' },
    'pile-length-range-check': { total_piles: 1936, out_of_range: 0, min_m: 1, max_m: 4 },
    'grade-pad': { pads_added: 1, grade_pads: 1, mode: 'Auto', elevation_m: 0, label: 'Pad', total_cut_m3: 0, total_fill_m3: 0, net_m3: 0 },
  }[operation],
  preview: previewOf(242, operation === 'piling-generate' ? 1936 : 0),
  standing: standingOf(242, operation === 'piling-generate' ? 1936 : 0),
  head: outcome === 'unchanged' ? headOf(base) : headOf(H2, base === null ? 0 : 1, base),
})
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done }); return { promise, resolve } }

afterEach(() => { cleanup(); vi.restoreAllMocks() })
const success = (value) => ({ ok: true, status: 200, value })
const clientOf = (view = viewOf()) => ({ getCivil: vi.fn().mockResolvedValue(success(view)), runCivilOperation: vi.fn().mockResolvedValue(success(resultOf())) })
const props = (client, extra = {}) => ({ drawingId: 'solar', projectId: 'p', client, ...extra })
const ready = async () => { await waitFor(() => expect(screen.getByRole('button', { name: 'Run' })).not.toBeDisabled()) }
function edit() {
  fireEvent.change(screen.getByLabelText('Boundary'), { target: { value: JSON.stringify(square) } })
  fireEvent.change(screen.getByLabelText('Preset'), { target: { value: JSON.stringify(preset) } })
  fireEvent.change(screen.getByLabelText('Drawing units'), { target: { value: 'm' } })
}
const press = () => fireEvent.click(screen.getByRole('button', { name: 'Run' }))
it('PC15 no drawing', () => {
  const client = clientOf()
  render(<SolarCivilPanel client={client} />)
  expect(screen.getByText('Open a drawing to view terrain previews')).toBeInTheDocument()
  expect(client.getCivil).not.toHaveBeenCalled()
  expect(client.runCivilOperation).not.toHaveBeenCalled()
})
it('PC16 civil counts', async () => {
  const client = clientOf()
  render(<SolarCivilPanel {...props(client)} />)
  await ready()
  expect(screen.getAllByText('242', { selector: 'dd' }).length).toBeGreaterThan(0)
  expect(within(screen.getByRole('region', { name: 'Frames standing' })).getByText('0', { selector: 'dd' })).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('Operation'), { target: { value: 'piling-generate' } })
  fireEvent.change(screen.getByLabelText('Preset'), { target: { value: JSON.stringify(preset) } })
  fireEvent.change(screen.getByLabelText('Pile template'), { target: { value: JSON.stringify(template) } })
  client.runCivilOperation.mockResolvedValue(success(resultOf('piling-generate')))
  client.getCivil.mockResolvedValue(success(viewOf(headOf(H2, 1, H), 242, 1936)))
  press()
  await waitFor(() => expect(screen.getAllByText('1936', { selector: 'dd' }).length).toBeGreaterThan(0))
  expect(client.runCivilOperation.mock.calls[0][0].body).toEqual(bodyOf('piling-generate'))
})
it('PC36 piling summary rows clear after refresh and a head change', async () => {
  const client = clientOf()
  const v = render(<SolarCivilPanel {...props(client, { headSignal: 1 })} />)
  await ready()
  fireEvent.change(screen.getByLabelText('Operation'), { target: { value: 'piling-generate' } })
  fireEvent.change(screen.getByLabelText('Preset'), { target: { value: JSON.stringify(preset) } })
  fireEvent.change(screen.getByLabelText('Pile template'), { target: { value: JSON.stringify(template) } })
  client.runCivilOperation.mockResolvedValue(success(resultOf('piling-generate')))
  client.getCivil.mockResolvedValue(success(viewOf(headOf(H2, 1, H), 242, 1936)))
  press()
  await ready()
  expect(screen.getAllByText('Piles', { selector: 'dt' })).toHaveLength(2)
  expect(screen.getAllByText('1936', { selector: 'dd' }).length).toBeGreaterThan(0)
  fireEvent.click(screen.getByRole('button', { name: 'Refresh physical state' }))
  await ready()
  expect(client.getCivil).toHaveBeenCalledTimes(3)
  const changed = deferred()
  client.getCivil.mockReturnValue(changed.promise)
  v.rerender(<SolarCivilPanel {...props(client, { headSignal: 2 })} />)
  expect(client.getCivil).toHaveBeenCalledTimes(4)
  await act(async () => changed.resolve(success(viewOf(headOf('c'.repeat(64), 2, H2), 242, 0))))
  await ready()
  const piles = screen.getAllByText('Piles', { selector: 'dt' })
  expect(piles).toHaveLength(1)
  expect(piles[0].nextElementSibling).toHaveTextContent(/^0$/)
  expect(screen.queryAllByText('1936')).toHaveLength(0)
})
it('PC17 single flight', async () => {
  const pending = deferred()
  const client = clientOf(); client.runCivilOperation.mockReturnValue(pending.promise)
  render(<SolarCivilPanel {...props(client)} />); await ready(); edit()
  const run = screen.getByRole('button', { name: 'Run' })
  act(() => { run.click(); run.click() })
  expect(client.runCivilOperation).toHaveBeenCalledTimes(1)
  for (const control of [screen.getByLabelText('Operation'), screen.getByLabelText('Boundary'), screen.getByLabelText('Preset'), screen.getByLabelText('Drawing units'), run, screen.getByRole('button', { name: 'Refresh physical state' })]) expect(control).toBeDisabled()
  expect(screen.getByRole('status')).toHaveTextContent('A terrain operation is in progress, so wait for it to finish.')
  await act(async () => pending.resolve({ ok: false, code: 'FRAMES_PILES_NO_FRAMES_FIT' }))
})
it('PC18 retained refusal', async () => {
  for (const code of Object.keys(CIVIL_ROUTE_REASONS)) {
    const client = clientOf(); client.runCivilOperation.mockResolvedValue({ ok: false, code })
    const v = render(<SolarCivilPanel {...props(client)} />); await ready()
    fireEvent.change(screen.getByLabelText('Operation'), { target: { value: 'piling-generate' } })
    fireEvent.change(screen.getByLabelText('Pile template'), { target: { value: JSON.stringify(template) } })
    fireEvent.change(screen.getByLabelText('Operation'), { target: { value: 'grade-pad' } })
    fireEvent.change(screen.getByLabelText('Mode'), { target: { value: 'Manual' } })
    fireEvent.change(screen.getByLabelText('Value du'), { target: { value: '12.5' } })
    fireEvent.change(screen.getByLabelText('Operation'), { target: { value: 'frame-generate' } })
    edit(); press()
    await waitFor(() => expect(screen.getByRole('alert').textContent).toBe(civilReason(code)))
    expect(screen.getByLabelText('Boundary')).toHaveValue(JSON.stringify(square))
    expect(screen.getByLabelText('Preset')).toHaveValue(JSON.stringify(preset))
    expect(screen.getByLabelText('Drawing units')).toHaveValue('m')
    expect(client.runCivilOperation).toHaveBeenCalledTimes(1)
    await ready()
    fireEvent.change(screen.getByLabelText('Operation'), { target: { value: 'grade-pad' } })
    expect(screen.getByLabelText('Mode')).toHaveValue('Manual')
    expect(screen.getByLabelText('Value du')).toHaveValue('12.5')
    fireEvent.change(screen.getByLabelText('Operation'), { target: { value: 'piling-generate' } })
    expect(screen.getByLabelText('Pile template')).toHaveValue(JSON.stringify(template))
    v.unmount()
  }
})
it('PC19 stale civil head', async () => {
  for (const code of ['FRAMES_PILES_STALE_BASE', 'PHYSICAL_HEAD_CONFLICT']) {
    const client = clientOf(); client.runCivilOperation.mockResolvedValue({ ok: false, code })
    const v = render(<SolarCivilPanel {...props(client)} />); await ready(); edit()
    client.getCivil.mockResolvedValue(success(viewOf(headOf(H2, 1, H))))
    press()
    await waitFor(() => expect(client.getCivil).toHaveBeenCalledTimes(2))
    await ready()
    expect(client.runCivilOperation).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('alert').textContent).toBe(civilReason(code))
    press(); await waitFor(() => expect(client.runCivilOperation).toHaveBeenCalledTimes(2))
    expect(client.runCivilOperation.mock.calls[1][0].body.expected_head).toBe(H2)
    v.unmount()
  }
})
it('PC20 old scope', async () => {
  const pending = deferred(); const callback = vi.fn()
  const client = clientOf(); client.runCivilOperation.mockReturnValue(pending.promise)
  const v = render(<SolarCivilPanel {...props(client, { onPhysicalHeadChanged: callback })} />)
  await ready(); edit(); press()
  const signal = client.runCivilOperation.mock.calls[0][0].signal
  v.rerender(<SolarCivilPanel {...props(client, { drawingId: 'other', onPhysicalHeadChanged: callback })} />)
  v.rerender(<SolarCivilPanel {...props(client, { onPhysicalHeadChanged: callback })} />)
  await ready()
  const before = client.getCivil.mock.calls.length
  await act(async () => pending.resolve(success(resultOf())))
  expect(signal.aborted).toBe(true); expect(callback).not.toHaveBeenCalled()
  expect(client.getCivil).toHaveBeenCalledTimes(before)
  expect(screen.queryByText('Outcome: published')).not.toBeInTheDocument()
  v.rerender(<SolarCivilPanel {...props(client, { projectId: 'other' })} />)
  v.unmount()
  const delayed = deferred(); const late = clientOf(); late.getCivil.mockReturnValue(delayed.promise)
  const mount = render(<SolarCivilPanel {...props(late)} />)
  const readSignal = late.getCivil.mock.calls[0][0].signal
  mount.unmount()
  await act(async () => delayed.resolve(success(viewOf())))
  expect(readSignal.aborted).toBe(true); expect(late.getCivil).toHaveBeenCalledTimes(1)
  const inFlight = deferred(); const signaled = clientOf(); signaled.runCivilOperation.mockReturnValue(inFlight.promise)
  const signalMount = render(<SolarCivilPanel {...props(signaled, { headSignal: 1, onPhysicalHeadChanged: callback })} />)
  await ready(); edit(); press()
  signalMount.rerender(<SolarCivilPanel {...props(signaled, { headSignal: 2, onPhysicalHeadChanged: callback })} />)
  await waitFor(() => expect(signaled.getCivil).toHaveBeenCalledTimes(2))
  await act(async () => inFlight.resolve(success(resultOf())))
  expect(callback).not.toHaveBeenCalled()
  expect(signaled.getCivil).toHaveBeenCalledTimes(2)
  expect(screen.queryByText('Outcome: published')).not.toBeInTheDocument()
  signalMount.unmount()
})
it('PC21 uncertain mutation', async () => {
  for (const code of ['TERRAIN_CLIENT_TIMEOUT', 'TERRAIN_CLIENT_NETWORK', 'TERRAIN_CLIENT_ABORTED', 'TERRAIN_CLIENT_RESPONSE_INVALID']) {
    const client = clientOf(); client.runCivilOperation.mockResolvedValue({ ok: false, code })
    const v = render(<SolarCivilPanel {...props(client)} />); await ready(); edit(); press()
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('The publication outcome is unknown, so refresh before retrying the original request.'))
    expect(screen.getByRole('button', { name: 'Run' })).toBeDisabled()
    expect(client.getCivil).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: 'Refresh physical state' }))
    await ready()
    expect(client.getCivil).toHaveBeenCalledTimes(2)
    expect(screen.getByLabelText('Preset')).toHaveValue(JSON.stringify(preset))
    v.unmount()
  }
})
it('PC34 an uncertain run clears the previous outcome', async () => {
  const client = clientOf()
  client.runCivilOperation.mockResolvedValue(success(resultOf('frame-generate', 'unchanged')))
  render(<SolarCivilPanel {...props(client)} />)
  await ready(); edit(); press()
  await waitFor(() => expect(screen.getByText('Outcome: unchanged')).toBeInTheDocument())
  await ready()
  client.runCivilOperation.mockResolvedValue({ ok: false, code: 'TERRAIN_CLIENT_NETWORK' })
  press()
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('The publication outcome is unknown, so refresh before retrying the original request.'))
  expect(screen.queryByText('Outcome: unchanged')).not.toBeInTheDocument()
})
it('PC22 accessible outcome', async () => {
  const client = clientOf(); const callback = vi.fn()
  render(<SolarCivilPanel {...props(client, { onPhysicalHeadChanged: callback })} />)
  await ready()
  const run = screen.getByRole('button', { name: 'Run' })
  run.focus(); press()
  expect(screen.getByLabelText('Boundary')).toHaveAttribute('aria-invalid', 'true')
  expect(screen.getByLabelText('Boundary')).toHaveAttribute('aria-describedby', 'civil-field-error')
  edit(); run.focus(); press()
  await ready()
  expect(callback).toHaveBeenCalledTimes(1)
  expect(screen.getByRole('status')).toHaveAttribute('aria-live', 'polite')
  expect(screen.getByRole('status')).toHaveTextContent('published')
  expect(document.activeElement).toBe(run)
  const pending = deferred(); client.runCivilOperation.mockReturnValue(pending.promise)
  run.focus(); press()
  const other = document.createElement('button'); document.body.appendChild(other); other.focus()
  await act(async () => pending.resolve({ ok: false, code: 'FRAMES_PILES_NO_FRAMES' }))
  expect(document.activeElement).toBe(other); other.remove()
})
