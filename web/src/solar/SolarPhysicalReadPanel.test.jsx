import React from 'react'
import { createHash } from 'node:crypto'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import SolarPhysicalReadPanel from './SolarPhysicalReadPanel.jsx'
import { createSolarImportClient } from './solarImportClient.js'
import { READ_RESULT_UNREADABLE, ARTIFACT_UNVERIFIED } from './solarReadResultModel.js'

const H = 'a'.repeat(64)
const H2 = 'b'.repeat(64)
const D = 'd'.repeat(64)
const clone = (v) => JSON.parse(JSON.stringify(v))
const headOf = (artifact = H, drawing = 'solar', project = 'p') => ({
  schema: 'leaf.solar-physical-head.v1', drawing_id: drawing, project_id: project, index: artifact === H ? 0 : 1, parent: artifact === H ? null : H,
  state: { schema: 'leaf.solar-artifact-ref.v1', artifact_id: artifact, content_sha256: D, media_type: 'application/json',
    filename: 'physical-state.json', byte_length: 1024, source_version: 1, download: `/api/drawings/${drawing}/artifacts/${artifact}` },
})
const terrainOf = (head = headOf()) => ({
  schema: 'leaf.solar-terrain-view-response.v1', stored: true, head,
  terrain: { schema: 'leaf.solar-terrain-view.v1', maturity: 'preview', drawing_id: head.drawing_id, project_id: head.project_id,
    frame: { coordinate_system: 'world', transform: 'identity', drawing_units: 'm', meters_per_unit: 1, crs: 'none',
      elevation_datum: 'unrecorded', horizontal: 'drawing-units', elevation: 'metres' },
    grid: { rows: 2, cols: 2, x_min: 0, x_max: 10, y_min: 0, y_max: 10, cell_x: 10, cell_y: 10, cell_x_m: 10, cell_y_m: 10,
      elevation_min_m: 0, elevation_max_m: 0, grid_sha256: D },
    mesh_faces: 0, slope_markers: 0, previews: {
      'terrain-mesh-render': { state: 'absent', record: null }, 'tracker-slope-violations': { state: 'absent', record: null },
    },
  },
})
const artifactOf = (filename = 'terrain.csv', byteLength = 138) => ({
  schema: 'leaf.solar-artifact-ref.v1', artifact_id: D, content_sha256: D, media_type: 'text/csv', filename,
  byte_length: byteLength, source_version: 1, download: `/api/drawings/solar/artifacts/${D}`,
})
const readOf = (tool = 'solar-physical-shade', format = 'terrain-csv', head = headOf(), version = 1) => ({
  ok: true, result: {
    schema_version: 'leaf.solar-graph-read.v1', adapter: 'local-graph-read', drawing_id: head.drawing_id, project_id: head.project_id,
    source_version: version, drawing_changed: false, tool, job_id: 'physical-test', output_sha256: D,
    output: tool === 'solar-physical-shade' ? {
      schema: 'leaf.solar-physical-shade.v1', maturity: 'preview', scope: 'cpu-terrain-native-frame-centres', head,
      sample_count: 15, mean_shade: 0, datum_shift_m: 1.5,
      units: { drawing_units: 'm', meters_per_unit: 1 },
      settings: { mode: 'defaults', target_clearance_m: 1.5, profile_selection: 'automatic' },
      profile: { name: 'full', angle_count: 468, ray_step_m: 1, max_ray_m: 400, estimated_samples: 2808000 },
      surface: { rows: 21, cols: 21, cells: 441 },
      frames: Array.from({ length: 15 }, (_, i) => ({ sample_index: i, frame_index: i, shade: 0 })), frames_omitted: 0,
    } : {
      head,
      summary: { schema: 'leaf.solar-physical-export.v1', maturity: 'preview',
        scope: format === 'terrain-csv' ? 'terrain-nodes' : 'cpu-terrain-native-frame-centres', head: clone(head), format,
        units: { drawing_units: 'm', meters_per_unit: 1 }, grid: { rows: 2, cols: 2, cells: 4 },
        settings: null, sample_count: null, profile: null, mean_shade: null, datum_shift_m: null },
      artifact: artifactOf(),
    },
  },
})
const bindingOf = (toolName = 'solar-physical-shade', format = 'terrain-csv') => ({
  toolName, drawingId: 'solar', projectId: 'p', drawingVersion: 1, head: headOf(), format,
})
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done }); return { promise, resolve } }

afterEach(() => { cleanup(); vi.restoreAllMocks() })
const terrainClientOf = () => ({ getTerrain: vi.fn().mockResolvedValue({ ok: true, value: terrainOf() }) })
const props = (terrainClient, runRead, extra = {}) => ({ drawingId: 'solar', projectId: 'p', drawingVersion: 1, terrainClient, runRead, ...extra })
const ready = async () => { await waitFor(() => expect(screen.getByRole('button', { name: 'Run' })).not.toBeDisabled()) }
const run = () => fireEvent.click(screen.getByRole('button', { name: 'Run' }))
const selectExport = (format = 'terrain-csv') => {
  fireEvent.change(screen.getByLabelText('Tool'), { target: { value: 'solar-physical-export' } })
  fireEvent.change(screen.getByLabelText('Format'), { target: { value: format } })
}
const waitResult = async () => { await waitFor(() => expect(screen.getByTestId('solar-read-result')).toBeInTheDocument()) }
function downloadOf(envelope, size = 138, filename = 'terrain.csv') {
  // Synthetic byte content at the server evidence's pinned sizes, downloaded through the real bounded transport.
  const bytes = new Uint8Array(size).fill(65)
  const digest = createHash('sha256').update(bytes).digest('hex')
  envelope.result.output.artifact = { ...artifactOf(filename, size), content_sha256: digest }
  const fetchImpl = vi.fn(async () => new Response(bytes, { headers: {
    'x-leaf-artifact-id': D, 'content-length': String(size), 'content-type': 'text/csv',
  } }))
  const transport = createSolarImportClient({
    fetchImpl, headers: () => ({ Authorization: 'Bearer fixture', 'X-Tenant-Id': 'fixture-tenant' }),
    sha256Hex: async (content) => createHash('sha256').update(content).digest('hex'),
  })
  return { bytes, fetchImpl, download: vi.fn(transport.downloadArtifact) }
}
it('PC26 automatic head', async () => {
  const terrain = terrainClientOf(); const runner = vi.fn().mockResolvedValue(readOf())
  const v = render(<SolarPhysicalReadPanel {...props(terrain, runner)} />)
  await ready()
  expect(terrain.getTerrain).toHaveBeenCalledTimes(1)
  expect(screen.queryByLabelText(new RegExp('head|hash|index|clearance|profile', 'i'))).not.toBeInTheDocument()
  expect(screen.getByText('Report CPU terrain shade at native frame centres in the current physical head. Uses default clearance and an automatic sample profile. Reads only. Excludes weather weighting and individual-module shading.')).toBeInTheDocument()
  expect(screen.getByText('Download one terrain or CPU terrain shade CSV from the requested physical head. Shade samples native frame centres using default clearance and automatic profiles. Excludes weather weighting and individual-module shading.')).toBeInTheDocument()
  run(); await waitResult()
  expect(runner).toHaveBeenCalledWith('solar-physical-shade', { drawing_id: 'solar' }, 'solar', { projectId: 'p', dwgVersion: 1 })
  expect(terrain.getTerrain).toHaveBeenCalledTimes(3)
  selectExport(); runner.mockResolvedValue(readOf('solar-physical-export')); run(); await waitResult()
  expect(runner.mock.calls[1]).toEqual(['solar-physical-export', { drawing_id: 'solar', expected_head: H, format: 'terrain-csv' }, 'solar', { projectId: 'p', dwgVersion: 1 }])
  v.unmount()
  const missing = terrainClientOf()
  render(<SolarPhysicalReadPanel terrainClient={missing} runRead={runner} />)
  expect(screen.getByText('Open a drawing to view terrain previews')).toBeInTheDocument()
  expect(missing.getTerrain).not.toHaveBeenCalled()
})
it('PC27 shade values', async () => {
  render(<SolarPhysicalReadPanel {...props(terrainClientOf(), vi.fn().mockResolvedValue(readOf()))} />)
  await ready(); run(); await waitResult()
  for (const [label, text] of [['Sample count', '15'], ['Mean shade', '0'], ['Datum shift m', '1.5']]) {
    const header = screen.getByRole('rowheader', { name: label })
    expect(within(header.closest('tr')).getByRole('cell')).toHaveTextContent(text)
  }
  const table = screen.getByRole('region', { name: 'Frames' })
  expect(within(table).getByRole('columnheader', { name: 'Sample index' })).toBeInTheDocument()
  expect(within(table).getAllByRole('row')).toHaveLength(16)
})
it('PC28 terrain download', async () => {
  const envelope = readOf('solar-physical-export')
  const transport = downloadOf(envelope)
  const save = vi.fn()
  render(<SolarPhysicalReadPanel {...props(terrainClientOf(), vi.fn().mockResolvedValue(envelope), { download: transport.download, save })} />)
  await ready(); selectExport(); run(); await waitResult()
  fireEvent.click(screen.getByRole('button', { name: 'Download terrain.csv (138 bytes)' }))
  await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
  expect(save).toHaveBeenCalledWith(transport.bytes, 'text/csv', 'terrain.csv')
  expect(transport.download).toHaveBeenCalledWith({ drawingId: 'solar', ref: envelope.result.output.artifact, signal: expect.any(AbortSignal) })
  expect(screen.getByTestId('solar-read-download-status')).toHaveTextContent('Downloaded terrain.csv.')
})
it('PC29 large download', async () => {
  const envelope = readOf('solar-physical-export', 'shade-per-panel')
  envelope.result.output.summary.sample_count = 1197
  envelope.result.output.summary.mean_shade = 0
  envelope.result.output.summary.datum_shift_m = 1.5
  const transport = downloadOf(envelope, 4816417, 'shade-per-panel.csv')
  const save = vi.fn()
  render(<SolarPhysicalReadPanel {...props(terrainClientOf(), vi.fn().mockResolvedValue(envelope), { download: transport.download, save })} />)
  await ready(); selectExport('shade-per-panel'); run(); await waitResult()
  fireEvent.click(screen.getByTestId('solar-read-download'))
  await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
  // Compare the saved bytes by length and digest: a deep equality over 4.8 MB costs about 30 s in vitest.
  const [saved, savedType, savedName] = save.mock.calls[0]
  expect([savedType, savedName]).toEqual(['text/csv', 'shade-per-panel.csv'])
  expect(saved.byteLength).toBe(transport.bytes.byteLength)
  expect(createHash('sha256').update(saved).digest('hex')).toBe(createHash('sha256').update(transport.bytes).digest('hex'))
  expect(transport.bytes.byteLength).toBe(4816417)
  expect(transport.fetchImpl.mock.calls[0][1].headers.Authorization).toBe('Bearer fixture')
  expect(transport.fetchImpl.mock.calls[0][0]).toBe(`/api/drawings/solar/artifacts/${D}`)
  expect(screen.getByTestId('solar-read-download-status')).toHaveTextContent('Downloaded shade-per-panel.csv.')
})
it('PC30 read movement', async () => {
  for (const movedRefusal of [true, false]) {
    const terrain = terrainClientOf()
    const runner = vi.fn().mockResolvedValue(movedRefusal ? { ok: false, error: { reason_code: 'PHYSICAL_EXPORT_HEAD_MOVED' } } : readOf('solar-physical-export'))
    const v = render(<SolarPhysicalReadPanel {...props(terrain, runner)} />)
    await ready(); selectExport()
    terrain.getTerrain.mockResolvedValueOnce({ ok: true, value: terrainOf() }).mockResolvedValue({ ok: true, value: terrainOf(headOf(H2)) })
    run()
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('The terrain changed, so review the refreshed preview before running again'))
    expect(terrain.getTerrain).toHaveBeenCalledTimes(3)
    expect(runner).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId('solar-read-result')).not.toBeInTheDocument()
    v.unmount()
  }
})
it('PC35 the final head read compares the whole head', async () => {
  for (const field of ['parent', 'content_sha256', 'source_version']) {
    // A nonnull parent is valid only above index 0; keep index 1 throughout that variant.
    const captured = field === 'parent' ? headOf(H2) : headOf()
    const changed = clone(captured)
    if (field === 'parent') changed.parent = D
    else if (field === 'content_sha256') changed.state.content_sha256 = H2
    else changed.state.source_version = 2
    const terrain = terrainClientOf()
    const runner = vi.fn().mockResolvedValue(readOf('solar-physical-export', 'terrain-csv', captured))
    const v = render(<SolarPhysicalReadPanel {...props(terrain, runner)} />)
    await ready(); selectExport()
    terrain.getTerrain.mockResolvedValueOnce({ ok: true, value: terrainOf(captured) })
      .mockResolvedValue({ ok: true, value: terrainOf(changed) })
    run()
    await waitFor(() => expect(screen.getByRole('alert').textContent).toBe('The terrain changed, so review the refreshed preview before running again'))
    expect(screen.queryByTestId('solar-read-result')).not.toBeInTheDocument()
    expect(runner).toHaveBeenCalledTimes(1)
    expect(terrain.getTerrain).toHaveBeenCalledTimes(3)
    v.unmount()
  }
})
it('PC31 read lifetime', async () => {
  const pending = deferred(); const terrain = terrainClientOf(); const runner = vi.fn().mockReturnValue(pending.promise)
  const v = render(<SolarPhysicalReadPanel {...props(terrain, runner)} />)
  await ready()
  const button = screen.getByRole('button', { name: 'Run' })
  act(() => { button.click(); button.click() })
  await waitFor(() => expect(runner).toHaveBeenCalledTimes(1))
  v.rerender(<SolarPhysicalReadPanel {...props(terrain, runner, { drawingVersion: 2 })} />)
  await ready()
  const before = terrain.getTerrain.mock.calls.length
  await act(async () => pending.resolve(readOf()))
  expect(terrain.getTerrain).toHaveBeenCalledTimes(before)
  expect(screen.queryByTestId('solar-read-result')).not.toBeInTheDocument()
  v.rerender(<SolarPhysicalReadPanel {...props(terrain, runner, { projectId: 'other' })} />)
  v.rerender(<SolarPhysicalReadPanel {...props(terrain, runner)} />)
  await ready()
  runner.mockResolvedValue(readOf('solar-physical-export'))
  selectExport(); run(); await waitResult()
  const downloadPending = deferred(); const download = vi.fn().mockReturnValue(downloadPending.promise); const save = vi.fn()
  v.rerender(<SolarPhysicalReadPanel {...props(terrain, runner, { download, save })} />)
  fireEvent.click(screen.getByTestId('solar-read-download'))
  const signal = download.mock.calls[0][0].signal
  v.rerender(<SolarPhysicalReadPanel {...props(terrain, runner, { drawingId: 'other', download, save })} />)
  expect(signal.aborted).toBe(true)
  await act(async () => downloadPending.resolve({ ok: true, value: { bytes: new Uint8Array(138), mediaType: 'text/csv', filename: 'terrain.csv' } }))
  expect(save).not.toHaveBeenCalled()
  v.unmount()
  const late = deferred(); const last = vi.fn().mockReturnValue(late.promise)
  const mount = render(<SolarPhysicalReadPanel {...props(terrain, last)} />)
  await ready(); run(); await waitFor(() => expect(last).toHaveBeenCalledTimes(1))
  const count = terrain.getTerrain.mock.calls.length
  mount.unmount(); await act(async () => late.resolve(readOf()))
  expect(terrain.getTerrain).toHaveBeenCalledTimes(count)
  const moved = deferred(); const signaledTerrain = terrainClientOf(); const signaledRun = vi.fn().mockReturnValue(moved.promise)
  const signalMount = render(<SolarPhysicalReadPanel {...props(signaledTerrain, signaledRun, { headSignal: 1 })} />)
  await ready(); run(); await waitFor(() => expect(signaledRun).toHaveBeenCalledTimes(1))
  signalMount.rerender(<SolarPhysicalReadPanel {...props(signaledTerrain, signaledRun, { headSignal: 2 })} />)
  await waitFor(() => expect(signaledTerrain.getTerrain).toHaveBeenCalledTimes(3))
  await act(async () => moved.resolve(readOf()))
  expect(signaledRun).toHaveBeenCalledTimes(1)
  expect(signaledTerrain.getTerrain).toHaveBeenCalledTimes(3)
  expect(screen.queryByTestId('solar-read-result')).not.toBeInTheDocument()
  signalMount.unmount()
})
it('PC32 inherited view', async () => {
  const envelope = readOf('solar-physical-export')
  envelope.result.output.summary.rows = [{ name: 'node', elevation_m: 0 }]
  envelope.result.output.artifact.download = 'https://unsafe.example/file.csv'
  const runner = vi.fn().mockResolvedValue(envelope)
  const v = render(<SolarPhysicalReadPanel {...props(terrainClientOf(), runner)} />)
  await ready(); selectExport(); run(); await waitResult()
  expect(within(screen.getByRole('region', { name: 'Rows' })).getByRole('cell', { name: 'node' })).toBeInTheDocument()
  expect(screen.getByText(ARTIFACT_UNVERIFIED)).toBeInTheDocument()
  expect(screen.queryByTestId('solar-read-download')).not.toBeInTheDocument()
  expect(screen.queryByRole('link')).not.toBeInTheDocument()
  v.unmount()
  const good = readOf('solar-physical-export'); const download = vi.fn().mockResolvedValue({ ok: false, code: 'ARTIFACT_NOT_FOUND', status: 404 })
  const next = render(<SolarPhysicalReadPanel {...props(terrainClientOf(), vi.fn().mockResolvedValue(good), { download })} />)
  await ready(); selectExport(); run(); await waitResult(); fireEvent.click(screen.getByTestId('solar-read-download'))
  await waitFor(() => expect(screen.getByTestId('solar-read-download-status')).toHaveTextContent('This file is no longer stored, so run the tool again to make a new one.'))
  next.unmount()
  render(<SolarPhysicalReadPanel {...props(terrainClientOf(), vi.fn().mockResolvedValue({ ...good, result: { ...good.result, output: null } }))} />)
  await ready(); run()
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(READ_RESULT_UNREADABLE))
})
