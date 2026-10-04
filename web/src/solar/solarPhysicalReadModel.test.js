// @vitest-environment node
import { expect, it } from 'vitest'
import { PHYSICAL_READ_TOOLS, PHYSICAL_EXPORT_FORMATS, buildPhysicalRead, physicalReadData, physicalReadReason } from './solarPhysicalReadModel.js'

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

it('PC23 shade parameters', () => {
  expect(PHYSICAL_READ_TOOLS).toEqual(['solar-physical-shade', 'solar-physical-export'])
  expect(buildPhysicalRead({ toolName: 'solar-physical-shade', drawingId: 'solar', head: headOf(), format: 'unused' })).toEqual({ ok: true, params: { drawing_id: 'solar' } })
  expect(buildPhysicalRead({ toolName: 'solar-physical-shade', drawingId: 'solar', head: null })).toEqual({ ok: false, reason: 'no_head' })
})
it('PC24 export parameters', () => {
  expect(PHYSICAL_EXPORT_FORMATS).toEqual(['terrain-csv', 'shade-azal-matrix', 'shade-sam', 'shade-per-panel'])
  for (const format of PHYSICAL_EXPORT_FORMATS) expect(buildPhysicalRead({ toolName: 'solar-physical-export', drawingId: 'solar', head: headOf(), format })).toEqual({ ok: true, params: { drawing_id: 'solar', expected_head: H, format } })
  for (const extra of [{ head: headOf(H, 'other') }, { format: 'unknown' }, { drawingId: '../solar' }]) expect(buildPhysicalRead({ toolName: 'solar-physical-export', drawingId: 'solar', head: headOf(), format: 'terrain-csv', ...extra }).ok).toBe(false)
})
it('PC37 outer tool matches the physical read binding when present', () => {
  for (const tool of PHYSICAL_READ_TOOLS) {
    const envelope = readOf(tool)
    const binding = bindingOf(tool)
    const other = PHYSICAL_READ_TOOLS.find((name) => name !== tool)
    expect(physicalReadData({ ...envelope, tool: other }, binding)).toEqual({ ok: false, reason: 'unreadable' })
    expect(physicalReadData({ ...envelope, tool }, binding)).toEqual({ ok: true, data: envelope.result })
    expect(physicalReadData(envelope, binding)).toEqual({ ok: true, data: envelope.result })
  }
})
it('PC25 result binding', () => {
  for (const tool of PHYSICAL_READ_TOOLS) {
    const envelope = readOf(tool); const binding = bindingOf(tool)
    expect(physicalReadData(envelope, binding)).toEqual({ ok: true, data: envelope.result })
    for (const extra of [{ drawing_id: 'other' }, { project_id: 'other' }, { source_version: 2 }, { tool: 'other' }, { drawing_changed: true }, { adapter: 'other' }]) expect(physicalReadData({ ...envelope, result: { ...envelope.result, ...extra } }, binding).ok).toBe(false)
    const moved = clone(envelope); moved.result.output.head = headOf(H2)
    expect(physicalReadData(moved, binding)).toEqual({ ok: false, reason: 'head_moved' })
  }
  const exported = readOf('solar-physical-export')
  exported.result.output.summary.head = headOf(H2)
  expect(physicalReadData(exported, bindingOf('solar-physical-export')).reason).toBe('head_moved')
  expect(physicalReadData(readOf('solar-physical-export'), bindingOf('solar-physical-export', 'shade-sam')).ok).toBe(false)
  expect(physicalReadData({ ok: false, error: { reason_code: 'PHYSICAL_EXPORT_HEAD_MOVED' } }, bindingOf()).reason).toBe('head_moved')
  expect(physicalReadReason('PHYSICAL_EXPORT_HEAD_MOVED')).toBe('The terrain changed, so review the refreshed preview before running again')
  for (const code of ['head_moved', 'UNKNOWN', 'PHYSICAL_SHADE_HEAD_REQUIRED']) expect(physicalReadReason(code)).toBe('The terrain request stopped')
})
