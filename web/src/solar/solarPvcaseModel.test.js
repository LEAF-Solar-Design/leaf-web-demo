import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, it } from 'vitest'
import { SOLAR_REFUSAL_REASONS, solarRunRefusal } from '../lib/ribbonClusters.js'
import { validatePvcaseSource, buildPvcaseParams, validatePvcaseCommit, validatePvcaseCommittedGraph, validatePvcaseExport, sourceSummary, commitSummary, solveSummary, pvcaseReason, pvcaseServerRefusalCode, PVCASE_REASONS } from './solarPvcaseModel.js'

const H = 'a'.repeat(64)
const B = 'b'.repeat(64)
const clone = (v) => JSON.parse(JSON.stringify(v))
const context = (extra = {}) => ({ tenantId: 'synthetic-tenant', orgId: 'synthetic-org', projectId: 'p', drawingId: 'solar', drawingVersion: 7, graphRev: 0, checkoutCapability: 'synthetic-checkout', ...extra })
const artifact = (assignment = false, v = 7, bytes = 274972) => ({
  schema: 'leaf.solar-artifact-ref.v1', artifact_id: assignment ? B : H, media_type: 'application/json',
  filename: assignment ? 'PVcaseAssignments.json' : 'pvcase-g33-source.json', byte_length: bytes,
  content_sha256: B, source_version: v, download: `/api/drawings/solar/artifacts/${assignment ? B : H}`,
})
const source = () => ({ schema: 'leaf.pvcase-g33-source.v1', kind: 'pvcase-g33', drawing_id: 'solar', project_id: 'p',
  source_version: 7, graph_rev: 0, graph_sha256: H, source: artifact() })
const receipt = (operation = 'convert', c = context(), jobId = 'job-1') => ({
  schema_version: 'leaf.solar-graph-commit.v1', adapter: 'local-graph-commit', tenant_id: c.tenantId,
  job_id: jobId, tool: `solar-pvcase-${operation}`, project_id: c.projectId, drawing_id: c.drawingId,
  request_sha256: H, new_version: { drawing_id: c.drawingId, version: c.drawingVersion + 6, parent: c.drawingVersion },
  before_graph_sha256: H, graph_sha256: B, intake_sha256: H, before_rev: c.graphRev, after_rev: c.graphRev + 1,
  drawing_changed: true, replayed: false,
})
const runEnvelope = (r) => ({ ok: true, tool: r.tool, version: '1.0.0', result: r, overlay: null, timing_ms: 1, cost: 0, error: null, degraded_mode: false })
const binding = (r = receipt(), c = context()) => ({ context: c, source: source(), tool: r.tool, jobId: r.job_id })
const readReceipt = (c = context()) => ({
  schema_version: 'leaf.solar-graph-read.v1', adapter: 'local-graph-read', tenant_id: c.tenantId, job_id: 'job-1',
  tool: 'solar-pvcase-export', project_id: c.projectId, drawing_id: c.drawingId, request_sha256: H, source_version: c.drawingVersion,
  representation: 'intake', graph_sha256: B, output_sha256: H, output_bytes: 900, drawing_changed: false,
  output: { summary: { schema: 'leaf.pvcase-g33-export.v1', panels: 2345, strings: 88, electrical_sizing: 'not-evaluated',
    source: { artifact_id: H, content_sha256: B } }, artifact: artifact(true, c.drawingVersion, 475691) },
})
const entityId = (kind, n) => `leaf:${kind}:00000000-0000-4000-8000-${n.toString(16).padStart(12, '0')}`
// Synthetic graph projection of the checked-in Roof inventory, not a server conversion fixture.
const roofIntakeText = readFileSync(resolve(process.cwd(), '..', 'docs', 'parity', 'evidence', 'rooftop', 'pvcase', 'intake.json'), 'utf8').trim()
const roofIntake = JSON.parse(roofIntakeText)
function roofGraph(solved = false, rev = 1) {
  const g = { graph_schema_version: 1, rev, parent_rev: rev - 1, project: { id: 'p' },
    frames: [], panels: [], strings: [], inverters: [], electrical_zones: [], routes: [], schedules: [], extra: { synthetic: true } }
  let n = 1
  for (const group of roofIntake.panel_groups) {
    const f = { id: entityId('frame', n++), kind: 'frame', rev, panel_refs: [], panel_assignments: [], matrix: [], sequences: [], electrical_zone_ref: null }
    for (const row of group.rows) for (const cell of row) {
      if (cell?.code !== 1 || !cell.id) continue
      const p = { id: entityId('panel', n++), kind: 'panel', rev, frame_ref: f.id, assignment: { string_ref: null, seq: null }, matrix_cell: { row: 0, col: f.panel_refs.length } }
      g.panels.push(p); f.panel_refs.push(p.id)
    }
    if (f.panel_refs.length) g.frames.push(f)
  }
  if (solved) {
    // Synthetic 88-string membership used only to exercise collection proofs.
    for (let i = 0, at = 0; i < 88; i++) {
      const length = Math.ceil((g.panels.length - at) / (88 - i))
      const refs = g.panels.slice(at, at + length).map((p, seq) => {
        p.assignment = { string_ref: entityId('string', n), seq }; return p.id
      })
      g.strings.push({ id: entityId('string', n++), kind: 'string', rev, module_count: length, ordered_panel_refs: refs, inverter_ref: null })
      at += length
    }
  }
  const panels = new Map(g.panels.map((p) => [p.id, p]))
  for (const f of g.frames) {
    f.panel_assignments = f.panel_refs.map((ref) => ({ panel_ref: ref, ...panels.get(ref).assignment, inverter_id: null, string_input_number: null }))
    f.matrix = [f.panel_assignments.map((a) => ({ ...a }))]
    f.sequences = g.strings.map((s) => ({ string_ref: s.id, ordered_panel_refs: s.ordered_panel_refs.filter((ref) => f.panel_refs.includes(ref)) })).filter((s) => s.ordered_panel_refs.length)
  }
  return g
}
const view = (g = roofGraph(), v = 13) => ({ intake: { solar_design_graph: g }, version: v, head: 99, latest: 100, error: null, degraded_mode: false })
const catalog = (operation) => ({ name: `solar-pvcase-${operation}`, version: '1.0.0', catalog_digest: H, capabilities: [] })
const deferred = () => { let resolve; const promise = new Promise((r) => { resolve = r }); return { promise, resolve } }

const accepted = (r = receipt(), c = context()) => validatePvcaseCommit(runEnvelope(r), binding(r, c)).value
it('PVM01 validates source identity and version', () => {
  const good = validatePvcaseSource(source(), context())
  expect(good.ok).toBe(true); expect(sourceSummary(good.value).value.bytes).toBe(274972)
  for (const change of [{ project_id: 'other' }, { source_version: 8 }, { drawing_id: 'other' }, { unexpected: true }]) expect(validatePvcaseSource({ ...source(), ...change }, context()).ok).toBe(false)
  for (const version of [0, true, 2147483648, 1.5]) {
    const s = source(); s.source_version = version; s.source.source_version = version
    expect(validatePvcaseSource(s, context()).ok).toBe(false)
  }
  for (const field of ['url', 'bytes', 'graph_version', 'sha256']) {
    const s = source(); s.source[field] = H; expect(validatePvcaseSource(s, context()).ok).toBe(false)
  }
})
it('PVM02 builds only declared parameters', () => {
  expect(buildPvcaseParams('convert', context(), source()).value).toEqual({ drawing_id: 'solar', expected_rev: 0, source_artifact_id: H })
  expect(buildPvcaseParams('solve', context(), source()).value).toEqual({ drawing_id: 'solar', expected_rev: 0 })
  expect(buildPvcaseParams('export', context(), source()).value).toEqual({ drawing_id: 'solar' })
  for (const field of ['pvcase_source', 'initialize', 'cancel', 'diagnostics']) expect(buildPvcaseParams('convert', context(), { ...source(), [field]: true }).ok).toBe(false)
  for (const rev of [true, -1, 2147483648, 0.5]) expect(buildPvcaseParams('solve', context({ graphRev: rev }), source()).ok).toBe(false)
})
it('PVM03 distinguishes revision from drawing version', () => {
  expect(accepted().new_version).toEqual({ drawing_id: 'solar', version: 13, parent: 7 })
  for (const change of [{ after_rev: 2 }, { before_rev: 7 }, { replayed: 1 }, { new_version: { drawing_id: 'solar', version: 13, parent: 6 } }, { extra: true }]) expect(validatePvcaseCommit(runEnvelope({ ...receipt(), ...change }), binding()).ok).toBe(false)
})
it('PVM04 requires a bound graph before counting', () => {
  const r = accepted()
  for (const fake of [null, receipt(), source(), { counts: { panels: 2345 } }, { extra: { diagnostics: { written: 2345 } } }]) {
    expect(commitSummary(r, fake).ok).toBe(false); expect(solveSummary(r, fake).ok).toBe(false)
  }
})
it('PVM05 derives conversion counts from graph collections', () => {
  const r = accepted(); const g = roofGraph(); g.extra.diagnostics = { panels: 1 }
  const proof = validatePvcaseCommittedGraph(view(g), { context: context(), receipt: r })
  expect(proof.ok).toBe(true)
  expect(commitSummary(r, proof.value).value).toMatchObject({ frames: 11, panels: 2345, strings: 0, inverters: 0 })
  const smaller = roofGraph(); const removed = smaller.panels.pop()
  const frame = smaller.frames.find((f) => f.id === removed.frame_ref)
  frame.panel_refs.pop(); frame.panel_assignments.pop(); frame.matrix[0].pop()
  const changed = validatePvcaseCommittedGraph(view(smaller), { context: context(), receipt: r })
  expect(commitSummary(r, changed.value).value.panels).toBe(2344)
  expect(proof.value.graph.extra.diagnostics.panels).toBe(1)
})
it('PVM06 derives solve counts from graph collections', () => {
  const c = context({ drawingVersion: 13, graphRev: 1 }); const r = accepted(receipt('solve', c), c)
  const g = roofGraph(true, 2)
  const proof = validatePvcaseCommittedGraph(view(g, 19), { context: c, receipt: r })
  expect(solveSummary(r, proof.value).value).toMatchObject({ strings: 88, assignedPanels: 2345, assignedInverters: 0 })
  expect(solveSummary(r, proof.value).value).not.toHaveProperty('written')
  g.inverters.push({ id: entityId('inverter', 99999), kind: 'inverter', rev: 2, is_l2: true, input_assignments: [], l1_assignments: [] })
  const l2 = validatePvcaseCommittedGraph(view(g, 19), { context: c, receipt: r })
  expect(solveSummary(r, l2.value).value.assignedInverters).toBe(0)
  g.strings[0].inverter_ref = g.inverters[0].id
  g.inverters[0].input_assignments.push({ string_ref: g.strings[0].id, mppt_letter: 'A', input_number: 0 })
  const assignedRefs = new Set(g.strings[0].ordered_panel_refs)
  for (const f of g.frames) for (const a of [...f.panel_assignments, ...f.matrix.flat()]) {
    if (assignedRefs.has(a.panel_ref)) { a.inverter_id = g.inverters[0].id; a.string_input_number = 0 }
  }
  const assigned = validatePvcaseCommittedGraph(view(g, 19), { context: c, receipt: r })
  expect(solveSummary(r, assigned.value).value.assignedInverters).toBe(1)
  for (const mutate of [(x) => x.strings[0].module_count++, (x) => x.panels[0].assignment.seq++, (x) => x.strings[1].ordered_panel_refs.push(x.strings[0].ordered_panel_refs[0])]) {
    const bad = roofGraph(true, 2); mutate(bad)
    expect(validatePvcaseCommittedGraph(view(bad, 19), { context: c, receipt: r }).ok).toBe(false)
  }
})
it('PVM07 validates the export summary and artifact', () => {
  const r = readReceipt(); const b = binding(r)
  expect(validatePvcaseExport(runEnvelope(r), b).ok).toBe(true)
  for (const mutate of [(x) => x.output.summary.source.artifact_id = B, (x) => x.output.summary.source.content_sha256 = H, (x) => x.output.artifact.source_version++, (x) => x.output.artifact.download = '/other', (x) => x.output.artifact.media_type = 'text/csv', (x) => x.output.artifact.filename = 'other.json', (x) => x.drawing_changed = true]) {
    const bad = clone(r); mutate(bad); expect(validatePvcaseExport(runEnvelope(bad), b).ok).toBe(false)
  }
})
it('PVM08 maps only closed refusal sentences', () => {
  const keys = Object.keys(SOLAR_REFUSAL_REASONS).slice(0, 31)
  expect(keys).toHaveLength(31)
  for (const key of [...keys, 'entitlement_required', 'entitlement_policy_unavailable', 'unresolved_units']) {
    expect(pvcaseReason(key)).toBe(solarRunRefusal(key)); expect(pvcaseReason(key.toUpperCase())).toBe(solarRunRefusal(key))
  }
  expect(pvcaseReason('BAD_PARAMS', 409)).toBe(PVCASE_REASONS.request)
  for (const code of ['INTERNAL', 'hostile message', 'Pvg_invalid_json', 'x'.repeat(65), null, {}]) expect(pvcaseReason(code)).toBe(PVCASE_REASONS.refused)
  for (const [code, key] of [['PVS_DRAWING_NOT_FOUND', 'drawing'], ['PVS_PROJECT_MISMATCH', 'project'], ['PVS_SOURCE_CORRUPT', 'sourceInvalid'], ['ARTIFACT_NOT_FOUND', 'artifactAgain'], ['SOLAREDGE_CLIENT_DIGEST_UNAVAILABLE', 'digest'], ['UNAUTHENTICATED', 'signIn'], ['CHECKOUT_REQUIRED', 'checkout'], ['UNKNOWN_TOOL', 'catalog']]) expect(pvcaseReason(code)).toBe(PVCASE_REASONS[key])
})
it('PVM09 refuses read-back version mismatch', () => {
  const r = accepted()
  for (const v of [14, undefined, '13', 13.5, 'head', 'latest']) expect(validatePvcaseCommittedGraph({ ...view(), version: v }, { context: context(), receipt: r }).ok).toBe(false)
  expect(r.new_version.version).toBe(13)
})
it('PVM10 refuses read-back revision mismatch', () => {
  const r = accepted()
  expect(validatePvcaseCommittedGraph(view(roofGraph(false, 2)), { context: context(), receipt: r }).code).toBe('countsUnavailable')
  expect(r.after_rev).toBe(1)
})
it('PVM11 refuses a missing or malformed graph', () => {
  const r = accepted()
  for (const g of [undefined, null, { ...roofGraph(), graph_schema_version: 2 }, { ...roofGraph(), panels: null }, { ...roofGraph(), frames: undefined }]) expect(validatePvcaseCommittedGraph({ ...view(), intake: { solar_design_graph: g } }, { context: context(), receipt: r }).ok).toBe(false)
  const g = roofGraph(); g.panels[0].assignment = {}
  expect(validatePvcaseCommittedGraph(view(g), { context: context(), receipt: r }).ok).toBe(false)
})
it('PVM12 refuses foreign read-back identity', () => {
  const r = accepted()
  const base = view()
  expect(validatePvcaseCommittedGraph(base, { context: context(), receipt: r }).ok).toBe(true)
  expect(validatePvcaseCommittedGraph({ ...base, drawing_id: 'foreign', project_id: 'foreign' }, { context: context(), receipt: r }).ok).toBe(true)
  for (const change of [{ tenant_id: 'foreign' }, { org_id: 'foreign' }, { organization_id: 'foreign' }]) expect(validatePvcaseCommittedGraph({ ...base, ...change }, { context: context(), receipt: r }).ok).toBe(false)
  base.intake.solar_design_graph.project.id = 'foreign'
  expect(validatePvcaseCommittedGraph(base, { context: context(), receipt: r }).ok).toBe(false)
})
it('PVM13 separates server refusal vocabulary from local copy', () => {
  for (const code of ['PVS_SOURCE_INVALID', 'pvs_source_invalid', 'PVS_GRAPH_REQUIRED', 'BAD_PARAMS', 'FORBIDDEN', 'SOLAREDGE_CLIENT_TIMEOUT', 'PVG_INPUT_BYTES_EXCEEDED', 'PVCASE_EMPTY_TARGET_REQUIRED']) {
    expect(pvcaseServerRefusalCode(code)).toBe(code)
    expect(pvcaseReason(code)).not.toBe(PVCASE_REASONS.refused)
  }
  for (const code of Object.keys(SOLAR_REFUSAL_REASONS).filter((key) => solarRunRefusal(key))) expect(pvcaseServerRefusalCode(code)).toBe(code)
  for (const code of Object.keys(PVCASE_REASONS)) {
    // FORBIDDEN is also an existing machine refusal, including its lower case wire form.
    expect(pvcaseServerRefusalCode(code)).toBe(code === 'forbidden' ? code : null)
    expect(pvcaseReason(code)).toBe(PVCASE_REASONS[code])
  }
  for (const code of [null, undefined, {}, 1, '', 'UNKNOWN_CODE', 'bad code', 'x'.repeat(65), 'HTTP']) expect(pvcaseServerRefusalCode(code)).toBeNull()
})
