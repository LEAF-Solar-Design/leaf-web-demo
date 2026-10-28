import { expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { SEED_SETTINGS } from './solarSettingsModel.js'
import workerUnavailable from './__fixtures__/workerUnavailable.json'
import { PROJECT_SOLAR_GRAPH_TOOLS, PROJECT_SOLAR_GRAPH_REASONS, projectSolarGraphReason,
  parseProjectSolarInsertionPoint, deriveProjectSolarGraphFormState, buildProjectSolarGraphRequest,
  exactProjectSolarGraphPreview, projectSolarGraphFeedback, panelGroupAngleKey, proposePanelGroups,
  validatePanelGroups, panelHandleValid } from './solarProjectGraphFormModel.js'

const binding = { kind: 'project', organization_id: '11111111-1111-1111-1111-111111111111',
  project_id: '22222222-2222-2222-2222-222222222222', drawing_id: '33333333-3333-3333-3333-333333333333',
  input_version_id: '44444444-4444-4444-4444-444444444444' }
const digest = 'sha256:' + 'd'.repeat(64)
function context() {
  return { context: { ...binding, head_version_id: binding.input_version_id, is_head: true, intake_sha256: 'a'.repeat(64) },
    intake: { solar_design_graph: { rev: 6, settings: { ...SEED_SETTINGS },
      project: { name: 'Field', zip_code: '', latitude: null, longitude: null } }, solar_design_graph_sha256: 'b'.repeat(64) } }
}
const derive = (contextView = context(), value = binding) => deriveProjectSolarGraphFormState({ binding: value, contextView })
const build = (changes = {}) => {
  const state = derive()
  return buildProjectSolarGraphRequest({ tool: 'solar-homeruns', binding, state, currentState: state,
    catalogDigest: digest, idempotencyKey: 'graph-intent', ...changes })
}
function row(changes = {}, flags = {}) {
  return { name: 'solar-homeruns', catalog_digest: digest, availability: {
    admission_checked: true, admission: { status_code: 200, error: null }, entitled: true, engine_ready: true,
    implemented: true, input_ready: true, runnable: true, refusal_reasons: [], ...flags }, ...changes }
}
const preview = (rows = [row()], changes = {}) => exactProjectSolarGraphPreview({ tool: 'solar-homeruns',
  catalogDigest: digest, outcome: { ok: true, data: { families: [{ capabilities: rows }] } }, ...changes })

it('G17 graph feedback distinguishes refusal from uncertainty', () => {
  const reason = 'SIP_R4_WORKER_UNAVAILABLE'
  const wire = { ok: false, status: 503, reason, envelope: workerUnavailable }
  const tool = 'solar-panels-from-drawing'
  const text = 'No worker was available to start Import panels from drawing. Try again later: Refresh preview, then Review & run.'
  for (const submission of [wire, { ok: false, status: 503, reason: reason.toLowerCase(), envelope: null },
    { ok: false, status: 409, reason }]) {
    expect(projectSolarGraphFeedback({ tool, submission })).toEqual({ state: 'refused',
      reasonCode: reason.toLowerCase(), text, jobId: null, outputVersionId: null })
  }
  let reads = 0
  const accessor = () => { reads += 1; return reason }
  for (const submission of [
    ...[500, 502, '503'].map(status => ({ ...wire, status })),
    { ...wire, reason: 'SIP_R1_STORE_UNAVAILABLE' }, { ...wire, reason: reason + '_EXTRA' },
    { ok: false, status: 503, envelope: null },
    { ...wire, envelope: { ...workerUnavailable, ok: true } },
    { ...wire, data: { status: 'submitted', job_id: binding.input_version_id } },
    { ...wire, envelope: { ...workerUnavailable, error: { reason_code: 'OTHER' } } },
    Object.defineProperty({ ...wire }, 'reason', { get: accessor }),
    { ...wire, envelope: Object.defineProperty({ ...workerUnavailable }, 'error', { get: accessor }) },
    { ...wire, envelope: { error: Object.defineProperty({}, 'reason_code', { get: accessor }) } },
  ]) {
    expect(projectSolarGraphFeedback({ tool, submission })).toMatchObject({ state: 'outcome_unknown',
      reasonCode: null, jobId: null, outputVersionId: null })
  }
  expect(reads).toBe(0)
  // Preserve ordinary bounded-code refusal handling when no HTTP status was
  // supplied, but it cannot gain the guarded no-worker admission message.
  expect(projectSolarGraphFeedback({ tool, submission: { ...wire, status: undefined } }).text).not.toBe(text)
  expect(projectSolarGraphFeedback({ tool, completion: { state: 'refused', reasonCode: reason,
    jobId: null, outputVersionId: null } }).state).toBe('outcome_unknown')
  expect(projectSolarGraphFeedback({ tool, submission: { ok: true, status: 202,
    data: { status: 'submitted', job_id: binding.input_version_id } } }).state).toBe('submitted')
  for (const code of ['SIP_R4_WORKER_UNAVAILABLE', 'sip_r4_worker_unavailable']) {
    const denied = row({}, { runnable: false, input_ready: false,
      admission: { status_code: 503, error: workerUnavailable.error }, refusal_reasons: [code] })
    expect(preview([denied])).toEqual({ accepted: false, reasonCode: reason.toLowerCase() })
    for (const refusal_reasons of [[code, 'OTHER'], ['SIP_R1_STORE_UNAVAILABLE'], [null]]) {
      expect(preview([{ ...denied, availability: { ...denied.availability, refusal_reasons } }]))
        .toEqual({ accepted: false, reasonCode: null })
    }
    expect(preview([denied, row({}, { runnable: false, refusal_reasons: ['OTHER'] })]))
      .toEqual({ accepted: false, reasonCode: null })
    expect(preview([{ ...denied, availability: { ...denied.availability,
      admission: { status_code: 503, error: { reason_code: 'bad code' } } } }]))
      .toEqual({ accepted: false, reasonCode: null })
    expect(preview([{ ...denied, availability: { ...denied.availability,
      admission: Object.defineProperty({}, 'error', { get: accessor }) } }]))
      .toEqual({ accepted: false, reasonCode: null })
  }
  expect(preview([], { outcome: { ...wire, reason: 'SIP_R1_STORE_UNAVAILABLE' } }))
    .toEqual({ accepted: false, reasonCode: null })
  expect(reads).toBe(0)
})

const panel = (id, angle = 0, changes = {}) => ({ id, angle, frame_ref: null,
  provenance: { source_handle: id }, ...changes })
const groupGraph = () => ({ panels: [panel('b'), panel('a'), panel('d', 90), panel('c', 90)], frames: [] })
const groupNames = [{ key: '0.0', name: ' Group 1 ' }, { key: '90.0', name: 'Group 2' }]
const groupNumbers = { module_width_along_row: '2', module_height_across_row: '1', alignment_tolerance: '0.01' }

it('W1C-26b accepts exactly the specified Python hexadecimal handle syntax', () => {
  for (const handle of ['1A', '1a', ' 1a ', '+1a', '-1a', '0x1A', '0X1a', '0x_1a', '1_a']) {
    expect(panelHandleValid(handle)).toBe(true)
  }
  for (const handle of ['', ' ', '_1a', '1a_', '1__a', '0x', 'g1', '1a1g', '0x1a_', '１', null, 12]) {
    expect(panelHandleValid(handle)).toBe(false)
  }
})
it('W1C-26b excludes invalid handles from buckets, singletons and the ungrouped count', () => {
  const graph = groupGraph()
  graph.panels.push(panel('bad', 0, { provenance: { source_handle: 'g1' } }),
    panel('bad-single', 45, { provenance: { source_handle: '_1a' } }))
  expect(proposePanelGroups(graph)).toEqual({ ...proposePanelGroups(groupGraph()), invalid_handles: 2 })
})
it('W1C-26b refuses handles repeated within or across proposals and passes the refusal through validation', () => {
  for (const duplicateId of ['b', 'c']) {
    const graph = groupGraph()
    graph.panels = graph.panels.map((entry) => ({ ...entry, provenance: {
      source_handle: entry.id === 'a' ? 'A1' : entry.id === duplicateId ? 'a1' : entry.id,
    } }))
    expect(proposePanelGroups(graph).refused).toBe('ambiguous_panel_handle')
    expect(validatePanelGroups(graph, groupNames, groupNumbers))
      .toEqual({ ok: false, reason: 'ambiguous_panel_handle' })
  }
})
it('W1C-26b compares the original handles in proposals without numeric normalization or singleton collisions', () => {
  const graph = { panels: [panel('a', 0, { provenance: { source_handle: '1a' } }),
    panel('b', 0, { provenance: { source_handle: '0x1a' } }),
    panel('c', 90, { provenance: { source_handle: '1A' } })] }
  expect(proposePanelGroups(graph).refused).toBe(null)
})

it('W1C-26 mirrors the server angle conversion, folding and exact binary rounding', () => {
  // math.radians -> r_to_d snaps -> fmod -> fold -> Decimal(float) HALF_UP.
  // 179.95 and the folded -0.05 are binary values below the .95 midpoint:
  // both round to 179.9. 359.96 folds to 179.96 and rounds to 180.0 (no second fold).
  for (const [angle, expected] of [[0, '0.0'], [179.95, '179.9'], [180, '0.0'],
    [-0.05, '179.9'], [359.96, '180.0'], [90.0005, '90.0'], [NaN, null], [Infinity, null]]) {
    expect(panelGroupAngleKey(angle)).toBe(expected)
  }
})
it('W1C-26 proposes sorted angle buckets and reports singletons without mutating panels', () => {
  const graph = groupGraph()
  graph.panels.reverse()
  graph.panels.push(panel('single', 45, { provenance: { source_handle: 'e' } }), panel('framed', 0, { frame_ref: 'frame' }),
    panel('missing', 0, { provenance: {} }), panel(42), panel('nan', NaN, { provenance: { source_handle: 'f' } }))
  const before = graph.panels.slice()
  expect(proposePanelGroups(graph)).toEqual({ proposals: [
    { key: '0.0', angle_key: '0.0', panel_refs: ['a', 'b'], count: 2 },
    { key: '90.0', angle_key: '90.0', panel_refs: ['c', 'd'], count: 2 },
  ], singletons: 1, ungrouped: 5, invalid_handles: 1, refused: null })
  expect(graph.panels).toEqual(before)
})
it('W1C-26 refuses limits without truncating', () => {
  const many = { panels: Array.from({ length: 129 }, (_, index) =>
    [panel(`${index}a`, index), panel(`${index}b`, index)]).flat() }
  many.panels[1].provenance.source_handle = many.panels[0].provenance.source_handle
  expect(proposePanelGroups(many).refused).toBe('too_many_groups')
  expect(proposePanelGroups(many).proposals).toHaveLength(129)
  expect(validatePanelGroups(many, [], groupNumbers).reason).toBe('too_many_groups')
  const large = { panels: Array.from({ length: 4097 }, (_, index) => panel(String(index))) }
  large.panels[1].provenance.source_handle = large.panels[0].provenance.source_handle
  expect(proposePanelGroups(large).refused).toBe('group_too_large')
  expect(proposePanelGroups(large).proposals[0].panel_refs).toHaveLength(4097)
  expect(validatePanelGroups(large, [], groupNumbers).reason).toBe('group_too_large')
})
it('W1C-26 validates names, numeric bounds and exact proposal coverage', () => {
  const graph = groupGraph()
  const check = (groups = groupNames, numbers = groupNumbers) => validatePanelGroups(graph, groups, numbers)
  expect(check().value[0]).toEqual({ name: 'Group 1', panel_refs: ['a', 'b'],
    module_width_along_row: 2, module_height_across_row: 1, alignment_tolerance: 0.01 })
  expect(validatePanelGroups({ panels: [] }, [], groupNumbers).reason).toBe('no_panel_groups')
  for (const names of [[], groupNames.slice().reverse(), [{ ...groupNames[0], key: 'missing' }, groupNames[1]]]) {
    expect(check(names).reason).toBe('panel_groups_stale')
  }
  for (const name of ['', ' ', 'x'.repeat(256), 'bad[name', null]) {
    expect(check([{ key: '0.0', name }, groupNames[1]]).reason).toBe('group_name_invalid')
  }
  expect(check([{ key: '0.0', name: 'group 2' }, groupNames[1]]).reason).toBe('group_name_collision')
  graph.frames.push({ name: 'GROUP 1' })
  expect(check().reason).toBe('group_name_collision')
  graph.frames = [{ name: 'Straße' }]
  expect(check([{ key: '0.0', name: 'STRASSE' }, groupNames[1]]).reason).toBe('group_name_collision')
  graph.frames = [{ name: 'ẞ' }]
  expect(check([{ key: '0.0', name: 'ss' }, groupNames[1]]).reason).toBe('group_name_collision')
  graph.frames = []
  for (const key of Object.keys(groupNumbers)) {
    for (const value of ['', ' ', 'NaN', 'Infinity', '0', '-1', '1000001', '2x', null]) {
      expect(check(groupNames, { ...groupNumbers, [key]: value }).reason).toBe('group_numbers_invalid')
    }
    expect(check(groupNames, { ...groupNumbers, [key]: '1000000' }).ok).toBe(true)
  }
})
it('W1C-26 builds exactly the panel-groups request and leaves panel import unchanged', () => {
  const groups = validatePanelGroups(groupGraph(), groupNames, groupNumbers).value
  expect(build({ tool: 'solar-panel-groups', groups })).toEqual({ ok: true, request: {
    tool: 'solar-panel-groups', params: { drawing_id: binding.drawing_id, expected_rev: 6, groups },
    catalogDigest: digest, idempotencyKey: 'graph-intent' } })
  expect(build({ tool: 'solar-panel-groups' }).reason).toBe('request_invalid')
  expect(build({ tool: 'solar-panels-from-drawing', groups }).request.params)
    .toEqual({ drawing_id: binding.drawing_id, expected_rev: 6 })
})

it('R13C-01 builds panel import', () => {
  expect(build({ tool: 'solar-panels-from-drawing' })).toEqual({ ok: true, request: {
    tool: 'solar-panels-from-drawing', params: { drawing_id: binding.drawing_id, expected_rev: 6 },
    catalogDigest: digest, idempotencyKey: 'graph-intent' } })
})
it('R13C-02 builds homeruns', () => {
  expect(build().request.params).toEqual({ drawing_id: binding.drawing_id, expected_rev: 6 })
  expect(build().request.tool).toBe('solar-homeruns')
})
it('R13C-03 builds feeders', () => {
  expect(build({ tool: 'solar-feeders' }).request.params).toEqual({ drawing_id: binding.drawing_id, expected_rev: 6 })
  expect(build({ tool: 'solar-feeders' }).request.tool).toBe('solar-feeders')
})
it('R13C-04 builds schedule', () => {
  expect(build({ tool: 'solar-schedule', insertionPointText: '1, -2.5, 3' }).request.params)
    .toEqual({ drawing_id: binding.drawing_id, expected_rev: 6, insertion_point: [1, -2.5, 3] })
})
it('R13C-05 rejects invalid bindings', () => {
  const accessor = { ...binding }
  Object.defineProperty(accessor, 'drawing_id', { get() { throw new Error('Must not read') } })
  for (const value of [null, {}, { ...binding, input_version_id: 'AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA' },
    { ...binding, extra: true }, accessor, { ...binding, input_version_id: 4 }]) {
    expect(derive(context(), value)).toEqual({ mode: 'refused', reason: 'binding_invalid' })
    expect(build({ binding: value })).toEqual({ ok: false, reason: 'binding_invalid' })
  }
})
it('R13C-06 validates exact context', () => {
  const foreign = context(); foreign.context.drawing_id = binding.input_version_id
  const old = context(); old.context.head_version_id = binding.drawing_id; old.context.is_head = false
  const inconsistent = context(); inconsistent.context.head_version_id = binding.drawing_id
  const noDigest = context(); delete noDigest.context.intake_sha256
  const graphless = context(); graphless.intake = {}
  const partial = context(); delete partial.intake.solar_design_graph_sha256
  for (const [value, reason] of [[null, 'context_unreadable'], [foreign, 'context_unreadable'],
    [inconsistent, 'context_unreadable'], [old, 'not_current_head'], [noDigest, 'version_digest_unavailable'],
    [graphless, 'graphless'], [partial, 'graph_unreadable']]) expect(derive(value)).toEqual({ mode: 'refused', reason })
  for (const rev of [-1, 1.5, '6', Number.MAX_SAFE_INTEGER + 1]) {
    const value = context(); value.intake.solar_design_graph.rev = rev
    expect(derive(value)).toEqual({ mode: 'refused', reason: 'graph_unreadable' })
  }
  const zero = context(); zero.intake.solar_design_graph.rev = 0
  expect(derive(zero)).toEqual({ mode: 'edit', binding, intakeSha256: 'a'.repeat(64), rev: 0 })
  const badSettings = context(); badSettings.intake.solar_design_graph.settings.use_l2_collectors = 1
  expect(derive(badSettings).reason).toBe('graph_unreadable')
})
it('R13C-07 rejects stale revisions', () => {
  expect(build({ currentState: { ...derive(), rev: 7 } })).toEqual({ ok: false, reason: 'stale_graph_revision' })
  expect(build({ currentState: { ...derive(), intakeSha256: 'c'.repeat(64) } }))
    .toEqual({ ok: false, reason: 'scope_stale' })
  for (const field of ['state', 'currentState']) expect(build({ [field]: undefined })).toEqual({ ok: false, reason: 'scope_stale' })
})
it('R13C-08 rejects invalid insertion text', () => {
  for (const text of ['', '1', '1,', ',2', '1,2,3,4', '[1,2]', '1e2,3', '0xff,2', 'Infinity,2', 'NaN,2',
    '1000000000001,2', '1.,2', '1'.repeat(49) + ',2', ' '.repeat(161), null, 12]) {
    expect(parseProjectSolarInsertionPoint(text)).toEqual({ ok: false, reason: 'insertion_invalid' })
  }
})
it('R13C-09 accepts bounded insertion text', () => {
  for (const [text, point] of [['0,0', [0, 0, 0]], ['.5, -2', [0.5, -2, 0]],
    ['+1000000000000,-1000000000000,0', [1e12, -1e12, 0]], ['-0,+0,-0.0', [0, 0, 0]]]) {
    expect(parseProjectSolarInsertionPoint(text)).toEqual({ ok: true, point })
    expect(Object.is(parseProjectSolarInsertionPoint(text).point[2], -0)).toBe(false)
  }
})
it('R13C-10 closes the request surface', () => {
  expect(PROJECT_SOLAR_GRAPH_TOOLS).toEqual(['solar-panels-from-drawing', 'solar-panel-groups', 'solar-homeruns', 'solar-feeders', 'solar-schedule'])
  expect(Object.isFrozen(PROJECT_SOLAR_GRAPH_TOOLS)).toBe(true)
  expect(build({ tool: 'solar-settings' })).toEqual({ ok: false, reason: 'tool_unsupported' })
  expect(build({ insertionPointText: '1,2' })).toEqual({ ok: false, reason: 'request_invalid' })
  const state = Object.freeze(derive())
  expect(build({ state, currentState: state, params: { cancel: true }, graph: {}, sourceIntake: {}, expected_rev: 100 })
    .request.params).toEqual({ drawing_id: binding.drawing_id, expected_rev: 6 })
  expect(state.rev).toBe(6)
})
it('R13C-11 requires digest and request key', () => {
  for (const catalogDigest of [undefined, '', 'd'.repeat(64), 'sha256:' + 'D'.repeat(64)]) {
    expect(build({ catalogDigest })).toEqual({ ok: false, reason: 'catalog_digest_required' })
  }
  for (const idempotencyKey of [undefined, '', ' ', 'k'.repeat(257), 'é'.repeat(129), 'lco1.' + 'c'.repeat(64)]) {
    expect(build({ idempotencyKey })).toEqual({ ok: false, reason: 'idempotency_key_required' })
  }
  expect(build({ idempotencyKey: 'é'.repeat(128) }).ok).toBe(true)
})
it('R13C-12 requires exact preview admission', () => {
  expect(preview()).toEqual({ accepted: true, reasonCode: null })
  for (const rows of [[], [row({ name: 'solar-feeders' })], [row({ catalog_digest: 'sha256:' + 'c'.repeat(64) })],
    [row(), row({}, { runnable: false })], [row({}, { admission_checked: false })],
    [row({}, { admission: { status_code: 200 } })], [row({}, { refusal_reasons: null })]]) {
    expect(preview(rows).accepted).toBe(false)
  }
  for (const flag of ['entitled', 'engine_ready', 'implemented', 'input_ready', 'runnable']) {
    expect(preview([row({}, { [flag]: 1 })]).accepted).toBe(false)
  }
  expect(preview(Array.from({ length: 1025 }, () => row())).accepted).toBe(false)
  expect(preview([], { outcome: { ok: true, data: { families: Array.from({ length: 65 }, () => ({ capabilities: [row()] })) } } }).accepted).toBe(false)
  expect(preview([], { outcome: { ok: true, data: { families: [{ capabilities: null }] } } }).accepted).toBe(false)
})
it('R13C-13 preserves preview refusal codes', () => {
  const refused = (code) => row({}, { admission: { status_code: 409, error: { reason_code: code } },
    runnable: false, refusal_reasons: ['OTHER_CODE'] })
  expect(preview([refused('EQUIPMENT_ASSIGNMENT_REQUIRED')])).toEqual({ accepted: false, reasonCode: 'EQUIPMENT_ASSIGNMENT_REQUIRED' })
  expect(preview([refused('CODE_ONE'), refused('CODE_TWO')])).toEqual({ accepted: false, reasonCode: null })
  expect(preview([], { outcome: { ok: false, reason: 'SIP_R7_TRANSPORT_FAILED' } }))
    .toEqual({ accepted: false, reasonCode: 'SIP_R7_TRANSPORT_FAILED' })
  expect(preview([refused('bad code')]).reasonCode).toBe('OTHER_CODE')
  expect(preview([row({}, { runnable: false, refusal_reasons: ['a'.repeat(129)] })]).reasonCode).toBe(null)
})
it('R13C-14 freezes local and status sentences', () => {
  const expected = {
    binding_invalid: 'Select a valid project drawing version to use Solar.',
    context_unreadable: 'This project drawing version could not be read. Reload it and try again.',
    graph_unreadable: 'The Solar design stored with this drawing could not be read.',
    graphless: 'Start a Solar design in settings before using this tool.',
    not_current_head: 'Open the newest drawing version before using this tool.',
    version_digest_unavailable: 'This drawing version has no recorded content digest. Reload it before continuing.',
    scope_stale: 'The drawing or session changed. Refresh before continuing.',
    stale_graph_revision: 'The Solar graph revision changed. Refresh before continuing.',
    catalog_digest_required: 'Refresh the Solar tools before preparing this request.',
    idempotency_key_required: 'This Solar request could not be identified. Review it again.',
    request_invalid: 'This Solar request could not be prepared. Review the inputs.',
    insertion_invalid: 'Enter two or three comma-separated coordinates, each between -1000000000000 and 1000000000000.',
    no_panel_groups: 'No panel angles have at least two ungrouped panels.',
    too_many_groups: 'There are more than 128 proposed groups. No groups have been omitted.',
    group_too_large: 'A proposed group has more than 4096 panels. No panels have been omitted.',
    ambiguous_panel_handle: 'Two panels share a drawing handle, so they cannot be grouped. Re-import the panels.',
    panel_groups_stale: 'The proposed panel groups changed. Refresh the preview before continuing.',
    group_name_invalid: 'Enter a group name of 1 to 255 characters without [.',
    group_name_collision: 'Group names must be unique, including existing panel groups.',
    group_numbers_invalid: 'Enter all three dimensions and tolerances as finite numbers greater than 0 and at most 1000000.',
    tool_unsupported: 'This tool has no project form in this view.',
    submit_unavailable: 'Project Solar submission is unavailable in this view.',
    run_in_progress: 'A Solar run is in progress. Wait for it to finish.',
    preview_required: 'Check this request before submitting it.',
    preview_refused: 'This Solar request could not be checked. Refresh before continuing.',
    request_refused: 'The server refused this Solar request. Review the reported code.',
    equipment_assignment_required: 'This tool requires valid equipment assignments. Review the electrical reasons on the inverter.',
    homeruns_equipment_required: 'Homeruns require valid equipment assignments. Review the electrical reasons on the inverter.',
    equipment_voltage_unknown: 'The inverter has no verified string voltage.',
    equipment_power_unknown: 'The inverter has no verified module power.',
    valid_settings_required: 'Feeders require valid L2 collector settings.',
    string_collectors_required: 'Feeders require string collectors.',
    routing_topology_required: 'This tool requires valid routing topology.',
    schedule_topology_required: 'The schedule requires routing topology.',
    complete_routing_required: 'The schedule requires complete current homeruns.',
    solar_output_not_current: 'Electrical output is unavailable because this Solar design is not current.',
    project_unavailable: 'Project Solar runs are unavailable in this view.',
    cancelled: 'The Solar request was cancelled without publishing a drawing version.',
    context_loading: 'Reading the selected project drawing version.',
    submitted: 'The Solar request was submitted.', pending: 'The Solar run is still in progress.',
    succeeded: 'The Solar run published a drawing version.', failed: 'The Solar run did not publish a drawing version.',
    unreadable: 'This Solar run finished, but its output could not be read.',
    outcome_unknown: 'The Solar run outcome is unknown. Check its job before submitting again.',
    aborted: 'Observation stopped. The server job may still be running.',
  }
  expect(PROJECT_SOLAR_GRAPH_REASONS).toEqual(expected)
  expect(Object.isFrozen(PROJECT_SOLAR_GRAPH_REASONS)).toBe(true)
  for (const [code, sentence] of Object.entries(expected)) {
    expect(sentence.length).toBeGreaterThanOrEqual(12)
    if (['no_panel_groups', 'too_many_groups', 'group_too_large', 'ambiguous_panel_handle',
      'panel_groups_stale', 'group_name_invalid', 'group_name_collision', 'group_numbers_invalid'].includes(code)) {
      expect(projectSolarGraphReason(code, 'solar-panel-groups')).toBe(sentence)
      expect(projectSolarGraphReason(code, 'solar-feeders')).toBe(expected.request_refused)
      expect(projectSolarGraphReason(code, 'solar-panels-from-drawing')).toBe(expected.request_refused)
    } else {
      expect(projectSolarGraphReason(code, 'solar-feeders')).toBe(sentence)
    }
  }
  for (const [code, key] of [['SIP_R7_BINDING_INVALID', 'binding_invalid'], ['SIP_R7_SCOPE_STALE', 'scope_stale'],
    ['SIP_R7_REQUEST_INVALID', 'request_invalid'], ['SIP_R13B_RUN_IN_PROGRESS', 'run_in_progress'],
    ['SIP_R13B_OUTCOME_UNKNOWN', 'outcome_unknown'], ['SIP_R7_ABORTED', 'aborted'],
    ['GRAPH_COMMIT_CANCELLED', 'cancelled'], ['project_execution_disabled', 'project_unavailable'],
    ['project_adapter_unavailable', 'project_unavailable']]) expect(projectSolarGraphReason(code)).toBe(expected[key])
  expect(projectSolarGraphReason(null)).toBe(null)
  expect(projectSolarGraphReason(undefined)).toBe(null)
  expect(projectSolarGraphReason('bad code')).toBe(expected.request_refused)
  expect(projectSolarGraphFeedback({ submission: null, completion: null }).state).toBe('outcome_unknown')
  const feedback = projectSolarGraphFeedback({ submission: { ok: false, status: 409, reason: 'SUCCEEDED' } })
  expect(feedback.state).toBe('refused')
  expect(feedback.reasonCode).toBe('SUCCEEDED')
  expect(feedback.text).toBe(expected.request_refused)
  const source = readFileSync(resolve(process.cwd(), 'src/solar/solarProjectGraphFormModel.js'), 'utf8')
  const start = source.indexOf('export function projectSolarGraphReason')
  const end = source.indexOf('\nexport function', start + 1)
  const selector = source.slice(start, end)
  expect(selector).not.toContain('PROJECT_SOLAR_GRAPH_REASONS[')
  // The absent-code guard returns no sentence, as the public contract requires.
  expect(selector).toContain('if (code == null) return null')
  const sentenceSelector = selector.replace('if (code == null) return null', '')
  const returns = [...sentenceSelector.matchAll(new RegExp('\\breturn\\s+([^\\n]+)', 'g'))]
  expect(returns.length).toBeGreaterThan(0)
  for (const [, expression] of returns) {
    expect(expression).toMatch(new RegExp('^PROJECT_SOLAR_GRAPH_REASONS\\.[A-Za-z_][A-Za-z0-9_]*$'))
  }
})
it('R13C-42 keeps the form independent of runtime controllers and storage', async () => {
  const modulePath = pathToFileURL(resolve(process.cwd(), 'scripts/check_user_facing_copy.mjs')).href
  const { scanSource } = await import(/* @vite-ignore */ modulePath)
  for (const file of ['solarProjectGraphFormModel.js', 'ProjectSolarGraphForm.jsx']) {
    const source = readFileSync(resolve(process.cwd(), 'src/solar/' + file), 'utf8')
    expect(source).not.toMatch(new RegExp('import[^\\n]*(?:App\\.jsx|solarProjectAppModel|useProjectSolarScope|createProjectSolarRunController)'))
    expect(source).not.toMatch(new RegExp('localStorage|sessionStorage'))
    expect(scanSource(source, 'src/solar/' + file, { strict: true })).toEqual([])
  }
})
it('R13C-46 contains hostile preview and feedback values', () => {
  const hostile = () => { throw new Error('Hostile getter') }
  const outcome = Object.defineProperty({}, 'ok', { get: hostile })
  const data = Object.defineProperty({}, 'families', { get: hostile })
  for (const value of [outcome, { ok: true, data }]) {
    expect(exactProjectSolarGraphPreview({ tool: 'solar-homeruns', catalogDigest: digest, outcome: value }))
      .toEqual({ accepted: false, reasonCode: null })
  }
  const expected = { state: 'outcome_unknown', reasonCode: null, text: PROJECT_SOLAR_GRAPH_REASONS.outcome_unknown,
    jobId: null, outputVersionId: null }
  expect(projectSolarGraphFeedback({ tool: 'solar-homeruns', submission: outcome })).toEqual(expected)
  expect(projectSolarGraphFeedback(Object.defineProperty({}, 'tool', { get: hostile }))).toEqual(expected)
  for (const key of ['tool', 'catalogDigest', 'outcome']) {
    expect(exactProjectSolarGraphPreview(Object.defineProperty({}, key, { get: hostile })))
      .toEqual({ accepted: false, reasonCode: null })
  }
  expect(exactProjectSolarGraphPreview(null)).toEqual({ accepted: false, reasonCode: null })
})
it('R13C-48 reads each builder state field once', () => {
  const currentState = derive()
  let reads = 0
  const proto = { mode: 'edit', binding, intakeSha256: currentState.intakeSha256,
    get rev() { reads += 1; return reads === 1 ? 6 : { injected: true } } }
  const state = Object.create(proto)
  const result = build({ state, currentState })
  expect(result.ok).toBe(true)
  expect(result.request.params.expected_rev).toBe(6)
  expect(typeof result.request.params.expected_rev).toBe('number')
  expect(reads).toBe(1)
  let currentReads = 0
  const counted = Object.create({ ...currentState, get rev() {
    currentReads += 1
    return currentReads === 1 ? 6 : { injected: true }
  } })
  const second = build({ state: currentState, currentState: counted })
  expect(second.ok).toBe(true)
  expect(second.request.params.expected_rev).toBe(6)
  expect(currentReads).toBe(1)
  const throwing = Object.create({ ...currentState,
    get rev() { throw new Error('Hostile revision') } })
  expect(build({ state: throwing, currentState })).toEqual({ ok: false, reason: 'request_invalid' })
})
