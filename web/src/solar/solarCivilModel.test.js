// @vitest-environment node
import { expect, it } from 'vitest'
import { CIVIL_OPERATIONS, validateCivilBody, parseCivilDrafts, validateCivilView, validateCivilOperation, civilSummary } from './solarCivilModel.js'

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

it('PC8 request validation', () => {
  expect(CIVIL_OPERATIONS).toEqual(['frame-generate', 'frame-collision-detect', 'piling-generate', 'pile-length-range-check', 'grade-pad'])
  for (const op of CIVIL_OPERATIONS) {
    const body = bodyOf(op)
    const parsed = validateCivilBody(body)
    expect(parsed).toEqual({ ok: true, body })
    expect(parsed.body).not.toBe(body)
    for (const key of ['base', 'expected_rev', 'project_id', 'hidden']) expect(validateCivilBody({ ...body, [key]: H }).ok).toBe(false)
    const missing = clone(body); delete missing.expected_head
    expect(validateCivilBody(missing).code).toBe('TERRAIN_EXPECTED_HEAD_INVALID')
  }
})
it('PC9 boundary drafts', () => {
  const drafts = { operation: 'frame-generate', boundary: JSON.stringify(square), preset: JSON.stringify(preset), drawingUnits: 'm' }
  expect(parseCivilDrafts(drafts).fields.boundary).toEqual(square)
  for (const boundary of [JSON.stringify(Array.from({ length: 20001 }, () => [0, 0])), '[[true,0],[1,0],[0,1]]', '{"x":1,"x":2}', '[[0,0],[1e400,0],[0,1]]']) {
    expect(parseCivilDrafts({ ...drafts, boundary })).toMatchObject({ ok: false, field: 'boundary' })
  }
  expect(parseCivilDrafts({ operation: 'frame-collision-detect', boundary: 'invalid', preset: 'invalid' })).toEqual({ ok: true, fields: {} })
})
it('PC10 preset/template', () => {
  // Full pinned objects from docs/parity/evidence/ground/generate/intake.json, copied into this owned fixture.
  const pinnedPreset = {"Name":"TinyTest","ModuleLengthM":2.384,"ModuleWidthM":1.303,"ModuleThicknessM":0.033,"ModulePowerWp":715,"InverterTypeName":"","ColorIndex":7,"FramingType":"FixedTilt","Orientation":"Portrait","Rows":6,"Columns":1,"TiltDegrees":20,"HorizontalGapM":0.02,"VerticalGapM":0.02,"AxisAzimuthDeg":180,"RomMinDeg":-60,"RomMaxDeg":60,"HeightAtLowPoseM":0.8,"HeightAtHighPoseM":1.62,"MaxNsSlopePct":8.5,"MaxRowToRowEwSlopePct":10,"MaxAxialSlopePct":8.5,"MaxCrossAxisSlopePct":10,"MaxRowToRowSlopeDeg":4,"MaxSlopePercent":15,"TrackerPack":{"Segments":[{"Kind":"Modules","Count":1},{"Kind":"Modules","Count":1},{"Kind":"Modules","Count":1},{"Kind":"Modules","Count":1},{"Kind":"Modules","Count":1},{"Kind":"Modules","Count":1}],"JointGapWidthM":0.05,"MotorGapWidthM":0.3,"IsMotorGapEnabled":true,"PlacePilesAtJoints":true},"PileTemplateName":"Default","Piling":{"HorizontalPolesPerFrame":4,"VerticalPolesPerGroup":2,"PileDiameterM":0.15,"PileRevealM":0.5,"PileEmbedmentM":1.5,"PileDepthM":2,"MinPileLengthM":1,"MaxPileLengthM":6}}
  const pinnedTemplate = {"Name":"Default","AreEqualMargins":false,"ShouldPlacePilesAtJoints":false,"IsMirrorFromMiddle":false,"DistributionType":2,"HorizontalDistancesM":[1,1,1,1,1],"VerticalDistancesM":[1,1,1],"MiddleDistribution":0,"SelectedMiddlePole":false,"HorizontalPoleCount":4,"VerticalPoleCount":2,"PlacementMode":"Grid","StationAxis":"LocalX","ReverseStationStart":false,"Stations":[],"PileDiameterM":0,"PileRevealM":0,"PileEmbedmentM":0,"MinPileLengthM":0,"MaxPileLengthM":0,"RevealBucketBoundariesM":[0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336,0.9144000000000001,1.2192,1.524,1.8288000000000002,2.1336],"PilesPerFrame":8}
  expect(parseCivilDrafts({ operation: 'piling-generate', preset: JSON.stringify(pinnedPreset), pileTemplate: JSON.stringify(pinnedTemplate) }))
    .toEqual({ ok: true, fields: { preset: pinnedPreset, pile_template: pinnedTemplate } })
  const drafts = { operation: 'piling-generate', preset: JSON.stringify(preset), pileTemplate: JSON.stringify(template) }
  expect(parseCivilDrafts(drafts)).toEqual({ ok: true, fields: { preset, pile_template: template } })
  for (const text of ['{"Name":""}', '{"Name":1}', '{"Name":"x","name":"y"}', '{"Name":"x","PileTemplateName":"a","piletemplatename":"b"}', '{"Name":"x","nested":{"x":1,"x":2}}', JSON.stringify({ Name: 'x', data: 'a'.repeat(1000000) })]) {
    expect(parseCivilDrafts({ ...drafts, preset: text }).ok).toBe(false)
  }
  expect(validateCivilBody({ ...bodyOf('piling-generate'), pile_template: { Name: 'x'.repeat(257) } }).ok).toBe(false)
  expect(validateCivilBody({ ...bodyOf(), preset: { ...preset, data: String.fromCharCode(0xe9).repeat(200000) } }).ok).toBe(false)
})
it('PC11 grade drafts', () => {
  const drafts = { operation: 'grade-pad', boundary: JSON.stringify(square), mode: 'Auto', value: '' }
  expect(parseCivilDrafts(drafts).fields).toEqual({ boundary: square, mode: 'Auto', value_du: null })
  expect(parseCivilDrafts({ ...drafts, mode: 'Manual', value: '-12.5' }).fields.value_du).toBe(-12.5)
  for (const extra of [{ mode: 'Clearance', value: '-1' }, { mode: null }, { value: '1' }, { mode: 'Manual', value: 'Infinity' }, { mode: 'Manual', value: '1000000001' }]) expect(parseCivilDrafts({ ...drafts, ...extra }).ok).toBe(false)
})
it('PC12 absent civil view', () => {
  expect(validateCivilView(absent(), { drawingId: 'solar' })).toEqual(absent())
  for (const extra of [{ grade_pads: 1 }, { preview: previewOf() }, { extra: 1 }, { error: {} }, { degraded_mode: true }]) expect(validateCivilView({ ...absent(), ...extra }, { drawingId: 'solar' })).toBeNull()
})
it('PC13 result variants', () => {
  for (const operation of CIVIL_OPERATIONS) for (const outcome of ['published', 'retry', 'unchanged']) {
    const value = resultOf(operation, outcome)
    const parsed = validateCivilOperation(value, { drawingId: 'solar', projectId: 'p', body: bodyOf(operation) })
    expect(parsed).toEqual(value); expect(parsed).not.toBe(value)
    if (outcome === 'retry') expect(civilSummary(parsed).summary).toEqual([])
    for (const extra of [{ created: !value.created }, { base: H2 }, { drawing_id: 'other' }, { project_id: 'other' }, { extra: true }]) expect(validateCivilOperation({ ...value, ...extra }, { drawingId: 'solar', projectId: 'p', body: bodyOf(operation) })).toBeNull()
  }
  expect(validateCivilOperation(resultOf('frame-generate', 'published', null), { drawingId: 'solar', body: bodyOf('frame-generate', null) })).not.toBeNull()
})
it('PC14 standing', () => {
  for (const [frames, stale] of [[0, 0], [242, 0], [242, 2]]) {
    const value = viewOf(headOf(), frames)
    value.standing = standingOf(frames, 0, stale)
    expect(validateCivilView(value, { drawingId: 'solar' })).toEqual(value)
  }
  for (const e of [{ state: 'current', checked: 0, stale: 0 }, { state: 'stale', checked: 1, stale: 2 }, { state: 'current', checked: 242, stale: 1 }, { state: 'current', checked: 1.5, stale: 0 }]) {
    const value = viewOf(); value.standing.frames = e
    expect(validateCivilView(value, { drawingId: 'solar' })).toBeNull()
  }
})
