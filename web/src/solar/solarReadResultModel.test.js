// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { validateSolarArtifactRef } from './solarImportClient.js'
import { createSolarProjectArtifactClient } from './solarProjectArtifactClient.js'
import { deriveProjectReadResultState, isSolarProjectReadResult, projectReadReason,
  PROJECT_READ_REASONS, SOLAR_PROJECT_READ_RESULT_SCHEMA, SOLAR_PROJECT_READ_ADAPTER } from './solarReadResultModel.js'
import {
  ARTIFACT_UNVERIFIED, DOWNLOAD_REASONS, MAX_CELL_TEXT, MAX_COLUMNS, MAX_FIELDS, MAX_LABEL, MAX_TABLE_ROWS, MAX_TEXT, PRIVATE_KEYS,
  READ_RESULT_UNREADABLE, SOLAR_READ_ADAPTER, SOLAR_READ_RESULT_SCHEMA,
  downloadReason, formatBytes, formatValue, isSolarReadResult, labelOf, readResultView,
} from './solarReadResultModel.js'

const ampacity = {
  "adapter": "local-graph-read",
  "drawing_changed": false,
  "drawing_id": "solar",
  "graph_sha256": "67ab25efcda14a69da65a4184cf5d362ac19a85dcf266f722a9528f5881737c3",
  "job_id": "job-g6r-1",
  "output": {
    "article": "NEC 310.16",
    "formula": "I_corrected = I_base × temp_factor × conduit_factor = 100 × 0.82 × 0.7",
    "inputs": {
      "I_base": 100,
      "conduit_factor": 0.7,
      "temp_factor": 0.82
    },
    "one_liner": "NEC 310.16 — Conductor ampacity with temperature and conduit-fill correction: I_corrected = I_base × temp_factor × conduit_factor = 100 × 0.82 × 0.7 = 57.4 A",
    "rejected_alternatives": [
      {
        "description": "I_base with no temperature correction",
        "why_rejected": "Required when ambient exceeds 30°C.",
        "would_have_resulted_in": 70
      }
    ],
    "result": 57.4,
    "short_description": "Conductor ampacity with temperature and conduit-fill correction",
    "source_url": "https://www.nfpa.org/codes-and-standards/nfpa-70",
    "units": "A"
  },
  "output_bytes": 743,
  "output_sha256": "8e74a3c4458786952c3d1ef54af45f7716abc16a31708fa0f5476ed518a0a9b2",
  "project_id": "leaf:project:00000000-0000-4000-8000-000000000001",
  "representation": "intake",
  "request_sha256": "ea27a372187309ff800cc8841bf16b4524c4f0b118da101d5f703c1d92897d6e",
  "schema_version": "leaf.solar-graph-read.v1",
  "source_version": 1,
  "tenant_id": "fixture-tenant",
  "tool": "solar-nec-ampacity-correction"
}

const acDrop = {
  "adapter": "local-graph-read",
  "drawing_changed": false,
  "drawing_id": "solar",
  "graph_sha256": "67ab25efcda14a69da65a4184cf5d362ac19a85dcf266f722a9528f5881737c3",
  "job_id": "job-g6r-2",
  "output": {
    "article": "NEC 210.19(A)(4) Informational Note 4 (AC)",
    "formula": "VD% = (2 × I × (R·cosφ + X·sinφ) × L / 1000) / V × 100 = (2 × 10 × (0.5·1 + 0.1·0.000) × 100 / 1000) / 240 × 100",
    "inputs": {
      "I": 10,
      "L_ft": 100,
      "R_ohm_per_1000ft": 0.5,
      "V_source": 240,
      "X_ohm_per_1000ft": 0.1,
      "phase": 1,
      "powerFactor": 1
    },
    "one_liner": "NEC 210.19(A)(4) Informational Note 4 (AC) — AC voltage drop (1-phase, recommended <= 3% for inverter output): VD% = (2 × I × (R·cosφ + X·sinφ) × L / 1000) / V × 100 = (2 × 10 × (0.5·1 + 0.1·0.000) × 100 / 1000) / 240 × 100 = 0.416667 % (3% recommended max)",
    "rejected_alternatives": [],
    "result": 0.4166666666666667,
    "short_description": "AC voltage drop (1-phase, recommended <= 3% for inverter output)",
    "source_url": "https://www.nfpa.org/codes-and-standards/nfpa-70",
    "units": "% (3% recommended max)"
  },
  "output_bytes": 743,
  "output_sha256": "8e74a3c4458786952c3d1ef54af45f7716abc16a31708fa0f5476ed518a0a9b2",
  "project_id": "leaf:project:00000000-0000-4000-8000-000000000001",
  "representation": "intake",
  "request_sha256": "ea27a372187309ff800cc8841bf16b4524c4f0b118da101d5f703c1d92897d6e",
  "schema_version": "leaf.solar-graph-read.v1",
  "source_version": 1,
  "tenant_id": "fixture-tenant",
  "tool": "solar-nec-ac-voltage-drop"
}

const conduit = {
  "adapter": "local-graph-read",
  "drawing_changed": false,
  "drawing_id": "solar",
  "graph_sha256": "67ab25efcda14a69da65a4184cf5d362ac19a85dcf266f722a9528f5881737c3",
  "job_id": "job-g6r-3",
  "output": {
    "conductor_area_sq_in": 0.0528,
    "conductors": [
      {
        "area_sq_in": 0.0211,
        "count": 2,
        "gauge": "10",
        "insulation": "THWN2",
        "role": "current-carrying",
        "unit": "AWG"
      },
      {
        "area_sq_in": 0.0106,
        "count": 1,
        "gauge": "10",
        "insulation": "Bare",
        "role": "egc",
        "unit": "AWG"
      }
    ],
    "conduit_area_sq_in": 0.285,
    "conduit_table": [
      {
        "area_sq_in": 0.285,
        "trade_size": "1/2"
      },
      {
        "area_sq_in": 0.508,
        "trade_size": "3/4"
      },
      {
        "area_sq_in": 0.832,
        "trade_size": "1"
      },
      {
        "area_sq_in": 1.453,
        "trade_size": "1-1/4"
      },
      {
        "area_sq_in": 1.986,
        "trade_size": "1-1/2"
      },
      {
        "area_sq_in": 3.291,
        "trade_size": "2"
      },
      {
        "area_sq_in": 4.695,
        "trade_size": "2-1/2"
      },
      {
        "area_sq_in": 7.268,
        "trade_size": "3"
      },
      {
        "area_sq_in": 9.737,
        "trade_size": "3-1/2"
      },
      {
        "area_sq_in": 12.554,
        "trade_size": "4"
      }
    ],
    "conduit_type": "PvcSch40",
    "conduit_type_label": "PVC Sch 40",
    "failure_reason": null,
    "fill_pct": 18.526315789473685,
    "max_fill_fraction": 0.4,
    "max_fill_pct": 40,
    "note": "NEC Ch9 T1/T4 - 3 conductors in 1/2\" PVC Sch 40: fill 18.5% ≤ 40% max",
    "success": true,
    "total_conductors": 3,
    "trade_size": "1/2"
  },
  "output_bytes": 743,
  "output_sha256": "8e74a3c4458786952c3d1ef54af45f7716abc16a31708fa0f5476ed518a0a9b2",
  "project_id": "leaf:project:00000000-0000-4000-8000-000000000001",
  "representation": "intake",
  "request_sha256": "ea27a372187309ff800cc8841bf16b4524c4f0b118da101d5f703c1d92897d6e",
  "schema_version": "leaf.solar-graph-read.v1",
  "source_version": 1,
  "tenant_id": "fixture-tenant",
  "tool": "solar-nec-conduit-fill"
}

const cable = {
  "adapter": "local-graph-read",
  "drawing_changed": false,
  "drawing_id": "solar",
  "graph_sha256": "67ab25efcda14a69da65a4184cf5d362ac19a85dcf266f722a9528f5881737c3",
  "job_id": "dfe84971-84b5-45de-804e-d4da02c4c787",
  "output": {
    "artifact": {
      "artifact_id": "e3c082e905ff821f63b27ce25902f93396ae1bed958f21257cfd35e06c079e06",
      "byte_length": 4421,
      "content_sha256": "475c7c55ac1ee7eff28cc8d7a0bbf9d63b9d988d94624231f6077e39012be6e2",
      "download": "/api/drawings/solar/artifacts/e3c082e905ff821f63b27ce25902f93396ae1bed958f21257cfd35e06c079e06",
      "filename": "CableExport.xlsx",
      "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      "schema": "leaf.solar-artifact-ref.v1",
      "source_version": 1
    },
    "summary": {
      "circuit_source": "topology",
      "feeders": 0,
      "inverter_record": "caller",
      "module_catalog": "unresolved",
      "modules": 3,
      "rows": [
        4,
        17,
        7,
        8
      ],
      "sheets": [
        "Homeruns",
        "Equipment Schedule",
        "Inverter Schedule",
        "String Schedule"
      ],
      "sizing": "absent",
      "status": "written",
      "strings": 2
    }
  },
  "output_bytes": 743,
  "output_sha256": "8e74a3c4458786952c3d1ef54af45f7716abc16a31708fa0f5476ed518a0a9b2",
  "project_id": "leaf:project:00000000-0000-4000-8000-000000000001",
  "representation": "intake",
  "request_sha256": "ea27a372187309ff800cc8841bf16b4524c4f0b118da101d5f703c1d92897d6e",
  "schema_version": "leaf.solar-graph-read.v1",
  "source_version": 1,
  "tenant_id": "fixture-tenant",
  "tool": "solar-cable-export"
}

const allFixtures = [ampacity, acDrop, conduit, cable]
const withOutput = (output) => ({ ...ampacity, output })
const fieldMap = (fields) => Object.fromEntries(fields.map(({ label, text }) => [label, text]))

describe('Solar read result model', () => {
  it('srr01_detects_read_results', () => {
    for (const fixture of allFixtures) expect(isSolarReadResult(fixture)).toBe(true)
    for (const value of [null, [], 'x', {}, { ...ampacity, schema_version: 'leaf.solar-graph-commit.v1' }, { ...ampacity, adapter: 'local-graph-commit' }]) {
      expect(isSolarReadResult(value)).toBe(false)
    }
    expect(SOLAR_READ_RESULT_SCHEMA).toBe('leaf.solar-graph-read.v1')
    expect(SOLAR_READ_ADAPTER).toBe('local-graph-read')
    expect(isSolarReadResult(Object.assign(Object.create(null), ampacity))).toBe(true)
    expect(isSolarReadResult(new Proxy({}, { getPrototypeOf() { throw new Error('unreadable') } }))).toBe(false)
  })

  it('srr02_ampacity_view', () => {
    const view = readResultView(ampacity)
    expect(view.headline).toBe('57.4 A')
    expect(view.oneLiner).toBe(ampacity.output.one_liner)
    expect(view.fields.some(({ key }) => ['result', 'units', 'one_liner'].includes(key))).toBe(false)
    expect(fieldMap(view.fields).Article).toBe('NEC 310.16')
    expect(fieldMap(view.sections.find(({ label }) => label === 'Inputs').fields)).toEqual({
      'I base': '100', 'Conduit factor': '0.7', 'Temp factor': '0.82',
    })
    const table = view.tables.find(({ label }) => label === 'Rejected alternatives')
    expect(table.columns.map(({ label }) => label)).toEqual(['Description', 'Why rejected', 'Would have resulted in'])
    expect(table.rows).toEqual([['I_base with no temperature correction', 'Required when ambient exceeds 30°C.', '70']])
  })

  it('srr03_ac_drop_view', () => {
    const view = readResultView(acDrop)
    expect(view.headline).toBe('0.416667 % (3% recommended max)')
    expect(fieldMap(view.fields)['Rejected alternatives']).toBe('none')
    expect(view.tables).toEqual([])
  })

  it('srr04_conduit_fill_view', () => {
    const view = readResultView(conduit)
    expect(view.headline).toBeNull()
    expect(fieldMap(view.fields)).toMatchObject({
      Success: 'yes', 'Trade size': '1/2', 'Conduit type label': 'PVC Sch 40',
      'Fill pct': '18.5263', 'Max fill pct': '40', 'Failure reason': 'none', 'Total conductors': '3',
    })
    const conductors = view.tables.find(({ label }) => label === 'Conductors')
    expect(conductors.rows).toHaveLength(2)
    expect(conductors.rows[1]).toContain('Bare')
    expect(conductors.rows[1]).toContain('0.0106')
    expect(view.tables.find(({ label }) => label === 'Conduit table').rows).toHaveLength(10)
  })

  it('srr05_cable_export_view', () => {
    const view = readResultView(cable)
    expect(view.fields).toEqual([])
    expect(fieldMap(view.sections.find(({ label }) => label === 'Summary').fields)).toMatchObject({
      Sheets: 'Homeruns, Equipment Schedule, Inverter Schedule, String Schedule',
      Rows: '4, 17, 7, 8', Status: 'written', Feeders: '0',
    })
    expect(view.artifact).toEqual({ state: 'ready', ref: cable.output.artifact, filename: 'CableExport.xlsx', sizeText: '4.3 KB' })
    expect(formatBytes(10)).toBe('10 bytes')
    expect(formatBytes(1048576)).toBe('1.0 MB')
  })

  it('srr06_artifact_unverified', () => {
    const ref = cable.output.artifact
    for (const artifact of [
      { ...ref, download: ref.download.replace('/solar/', '/other/') }, { ...ref, media_type: 'text/plain' },
      { ...ref, filename: 'CableExport.txt' }, { ...ref, byte_length: 0 }, { ...ref, extra: true }, null, 'x',
    ]) {
      const view = readResultView({ ...cable, output: { ...cable.output, artifact } })
      expect(view.artifact).toEqual({ state: 'unverified' })
      expect(view.drawingId).toBe('solar')
    }
    expect(ARTIFACT_UNVERIFIED).toBe(DOWNLOAD_REASONS.ARTIFACT_ID_INVALID)
  })

  it('srr07_details_and_privacy', () => {
    const view = readResultView(cable)
    expect(view.details.map(({ label }) => label)).toEqual(['Tool', 'Drawing', 'Version', 'Representation', 'Graph', 'Output', 'Request', 'Job'])
    expect(fieldMap(view.details)).toMatchObject({ Tool: 'solar-cable-export', Version: '1' })
    expect(JSON.stringify(view)).not.toContain('fixture-tenant')
    expect(JSON.stringify(view)).not.toContain('leaf:project:00000000-0000-4000-8000-000000000001')
  })

  it('srr08_unreadable_output', () => {
    const removed = { ...ampacity }
    delete removed.output
    for (const fixture of [withOutput(null), withOutput([]), withOutput('x'), removed]) {
      expect(readResultView(fixture)).toMatchObject({
        refusal: 'unreadable', fields: [], sections: [], tables: [], headline: null, oneLiner: null, artifact: null,
      })
      expect(readResultView(fixture).details.length).toBeGreaterThan(0)
    }
    expect(readResultView({ ...ampacity, adapter: 'other' }).refusal).toBe('unreadable')
    expect(READ_RESULT_UNREADABLE).toBe('This read finished, but its result could not be shown.')
  })

  it('srr09_bounds', () => {
    expect([MAX_FIELDS, MAX_TABLE_ROWS, MAX_COLUMNS, MAX_TEXT, MAX_CELL_TEXT]).toEqual([200, 200, 12, 500, 200])
    const scalar = readResultView(withOutput(Object.fromEntries(Array.from({ length: 250 }, (_, i) => ['k' + i, i]))))
    expect(scalar.fields).toHaveLength(200)
    expect(scalar.omittedFields).toBe(50)
    const combined = readResultView(withOutput({ group: Object.fromEntries(Array.from({ length: 150 }, (_, i) => ['a' + i, i])), ...Object.fromEntries(Array.from({ length: 100 }, (_, i) => ['b' + i, i])) }))
    expect(combined.sections[0].fields).toHaveLength(150)
    expect(combined.fields).toHaveLength(50)
    expect(combined.omittedFields).toBe(50)
    const table = readResultView(withOutput({ items: Array.from({ length: 250 }, (_, i) => ({ n: i })) })).tables[0]
    expect(table.rows).toHaveLength(200)
    expect(table.omittedRows).toBe(50)
    const columns = readResultView(withOutput({ items: Array.from({ length: 15 }, (_, i) => ({ ['c' + i]: i })) })).tables[0]
    expect(columns.columns).toHaveLength(12)
    expect(columns.omittedColumns).toBe(3)
    expect(columns.rows[1][0]).toBe('')
    const text = readResultView(withOutput({ long: 'a'.repeat(600), exact: 'b'.repeat(500), items: [{ text: 'c'.repeat(300) }] }))
    expect(text.fields[0].text).toBe('a'.repeat(500) + ' [truncated]')
    expect(text.fields[1].text).toBe('b'.repeat(500))
    expect(text.tables[0].rows[0][0]).toBe('c'.repeat(200) + ' [truncated]')
  })

  it('srr10_format_value', () => {
    for (const [value, text] of [
      [null, 'none'], [undefined, 'none'], [true, 'yes'], [false, 'no'], [57.4, '57.4'],
      [0.4166666666666667, '0.416667'], [18.526315789473685, '18.5263'], [70, '70'],
      [-0.5, '-0.5'], [1234567.891, '1234570'], [Infinity, 'Infinity'], [-Infinity, '-Infinity'],
      [NaN, 'NaN'], ['hello', 'hello'], [[], 'none'], [[1, 'a', true], '1, a, yes'],
      [{ a: 1 }, '{"a":1}'], [[{ a: 1 }], '[{"a":1}]'],
    ]) expect(formatValue(value)).toBe(text)
    expect(formatValue({ toJSON() { throw new Error('no') } })).toBe('unreadable value')
    expect(formatValue({ toJSON() { return undefined } })).toBe('unreadable value')
    expect(formatValue('abc', 2)).toBe('ab [truncated]')
    for (const code of ['toString', '__proto__', 'constructor', 'SAVE_FAILED', 'FALLBACK']) {
      expect(downloadReason(code, 500)).toBe(DOWNLOAD_REASONS.FALLBACK)
    }
    expect(downloadReason('unknown', 403)).toBe(DOWNLOAD_REASONS.FORBIDDEN)
    expect(downloadReason('unknown', 404)).toBe(DOWNLOAD_REASONS.ARTIFACT_NOT_FOUND)
    expect(Object.isFrozen(DOWNLOAD_REASONS)).toBe(true)
  })

  it('srr11_label_of', () => {
    for (const [key, label] of [['fill_pct', 'Fill pct'], ['I_base', 'I base'], ['x', 'X'], ['', '']]) expect(labelOf(key)).toBe(label)
  })

  it('srr12_no_mutation', () => {
    const freeze = (value) => {
      if (value && typeof value === 'object') {
        Object.values(value).forEach(freeze)
        Object.freeze(value)
      }
      return value
    }
    for (const fixture of allFixtures) {
      const copy = freeze(JSON.parse(JSON.stringify(fixture)))
      expect(() => readResultView(copy)).not.toThrow()
      expect(copy).toEqual(fixture)
    }
  })

  it('srr13_private_keys_removed_at_every_depth', () => {
    const id = 'leaf:project:00000000-0000-4000-8000-000000000001'
    const view = readResultView(withOutput({
      tenant_id: 'fixture-tenant',
      project_id: id,
      head: { project_id: id, index: 3, nested: { project_id: id, keep: 1 } },
      rows: [{ project_id: id, n: 1 }, { n: 2, tenant_id: 'fixture-tenant' }],
      cells: [{ value: { project_id: id, kept: 'yes' } }],
      list: [[{ project_id: id }]],
    }))
    const text = JSON.stringify(view)
    for (const hidden of [id, 'fixture-tenant', 'Project id', 'Tenant id', 'project_id', 'tenant_id']) expect(text).not.toContain(hidden)
    const head = view.sections.find((section) => section.key === 'head')
    expect(fieldMap(head.fields)).toEqual({ Index: '3', Nested: '{"keep":1}' })
    const rows = view.tables.find((table) => table.key === 'rows')
    expect(rows.columns.map(({ label }) => label)).toEqual(['N'])
    expect(rows.rows).toEqual([['1'], ['2']])
    expect(view.tables.find((table) => table.key === 'cells').rows).toEqual([['{"kept":"yes"}']])
    expect(PRIVATE_KEYS).toEqual(['tenant_id', 'project_id'])
    expect(Object.isFrozen(PRIVATE_KEYS)).toBe(true)
  })

  it('srr14_private_keys_leave_the_artifact', () => {
    const view = readResultView({ ...cable, output: { ...cable.output, project_id: cable.project_id, tenant_id: cable.tenant_id } })
    expect(view.artifact).toEqual({ state: 'ready', ref: cable.output.artifact, filename: 'CableExport.xlsx', sizeText: '4.3 KB' })
    expect(JSON.stringify(view.fields)).not.toContain(cable.project_id)
    expect(JSON.stringify(view.fields)).not.toContain(cable.tenant_id)
  })

  it('srr15_display_strings_bounded', () => {
    expect(MAX_LABEL).toBe(80)
    const view = readResultView(withOutput({
      result: 1, units: 'u'.repeat(600), one_liner: 'x'.repeat(100000),
      ['k'.repeat(100000)]: 2,
      ['g'.repeat(90)]: { a: 1 },
      items: [{ ['c'.repeat(90)]: 1 }],
    }))
    expect(view.headline).toBe(`1 ${'u'.repeat(500)} [truncated]`)
    expect(view.oneLiner).toBe(`${'x'.repeat(500)} [truncated]`)
    expect(view.fields[0].label).toBe(`K${'k'.repeat(79)} [truncated]`)
    expect(view.sections[0].label).toBe(`G${'g'.repeat(79)} [truncated]`)
    expect(view.tables[0].columns[0].label).toBe(`C${'c'.repeat(79)} [truncated]`)
    expect(labelOf('a'.repeat(80))).toBe(`A${'a'.repeat(79)}`)
    expect(labelOf('a'.repeat(81))).toBe(`A${'a'.repeat(79)} [truncated]`)
    const exact = readResultView(withOutput({ result: 2, units: 'v'.repeat(500), one_liner: 'y'.repeat(500) }))
    expect(exact.headline).toBe(`2 ${'v'.repeat(500)}`)
    expect(exact.oneLiner).toBe('y'.repeat(500))
  })

  it('srr16_parsed_proto_key_stays_data', () => {
    const view = readResultView(withOutput(JSON.parse('{"group":{"__proto__":5,"a":1},"items":[{"__proto__":7,"b":2}]}')))
    expect(view.sections[0].fields.map(({ key, text }) => [key, text])).toEqual([['__proto__', '5'], ['a', '1']])
    expect(view.tables[0].columns.map(({ key }) => key)).toEqual(['__proto__', 'b'])
    expect(view.tables[0].rows).toEqual([['7', '2']])
  })
})

const projectBinding = Object.freeze({
  kind: 'project',
  organization_id: '11111111-1111-4111-8111-111111111111',
  project_id: '22222222-2222-4222-8222-222222222222',
  drawing_id: '33333333-3333-4333-8333-333333333333',
  input_version_id: '44444444-4444-4444-8444-444444444444',
})
const bodyHash = '44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a'
const intakeHash = 'a'.repeat(64)
const graphHash = 'b'.repeat(64)
const catalogDigest = 'sha256:' + 'c'.repeat(64)
function projectRef(overrides = {}) {
  const artifactId = 'd'.repeat(64)
  return { organization_id: projectBinding.organization_id, project_id: projectBinding.project_id,
    drawing_id: projectBinding.drawing_id, source_version_id: projectBinding.input_version_id,
    source_intake_sha256: intakeHash, graph_sha256: graphHash, artifact_id: artifactId,
    media_type: 'application/json', filename: 'ElectricalSchedules.json', byte_length: 2,
    content_sha256: bodyHash, schema: 'leaf.solar-project-artifact-ref.v1',
    download: `/api/projects/${projectBinding.project_id}/drawings/${projectBinding.drawing_id}/solar-artifacts/${artifactId}`,
    ...overrides }
}
function projectResult(overrides = {}) {
  return { schema: 'leaf.solar-project-read.v1', adapter: 'project-local-graph-read',
    organization_id: projectBinding.organization_id, project_id: projectBinding.project_id,
    drawing_id: projectBinding.drawing_id, input_version_id: projectBinding.input_version_id,
    input_intake_sha256: intakeHash, graph_sha256: graphHash, tool: 'solar-electrical-schedules',
    tool_manifest_sha256: catalogDigest, graph_project_id: 'private-graph-project',
    head_version_id: projectBinding.input_version_id, is_head: true, current_required: true,
    request_sha256: 'e'.repeat(64), output_sha256: 'f'.repeat(64), output_bytes: 200,
    drawing_changed: false, output: { result: 57.4, units: 'A', artifact: projectRef() },
    ...overrides }
}

afterEach(() => { cleanup(); vi.restoreAllMocks() })
const projectState = (data = projectResult(), options = {}) => deriveProjectReadResultState({
  binding: projectBinding, data, catalogDigest, ...options,
})

describe('canonical project read presentation', () => {
  it('R12-01 presents a completed result for the exact project version', () => {
    const data = projectResult()
    const state = projectState(data)
    expect(isSolarProjectReadResult(data)).toBe(true)
    expect(isSolarReadResult(data)).toBe(false)
    expect([SOLAR_PROJECT_READ_RESULT_SCHEMA, SOLAR_PROJECT_READ_ADAPTER]).toEqual([
      'leaf.solar-project-read.v1', 'project-local-graph-read'])
    expect(state).toMatchObject({ mode: 'ready', binding: projectBinding, current: true,
      inputIntakeSha256: intakeHash, graphSha256: graphHash })
    expect(state.view.headline).toBe('57.4 A')
    expect(state.view.details.map(({ label }) => label)).toEqual(['Tool', 'Drawing', 'Version', 'Graph', 'Output', 'Request'])
    expect(state.view.details[2].text).toBe(projectBinding.input_version_id)
    expect(state.view.artifact).toMatchObject({ state: 'ready', filename: 'ElectricalSchedules.json', sizeText: '2 bytes' })
    expect(state.view.details.some(({ label }) => ['Job', 'Representation'].includes(label))).toBe(false)
    expect(JSON.stringify(state.view.details)).not.toContain('private-graph-project')
  })

  it('R12-02 keeps canonical references and UUID versions outside the legacy validator', () => {
    expect(validateSolarArtifactRef(projectRef(), projectBinding.drawing_id)).toBe(false)
    expect(validateSolarArtifactRef({ ...cable.output.artifact, source_version: projectBinding.input_version_id }, cable.drawing_id)).toBe(false)
  })

  it('R12-07 leaves mismatched source hashes as an unverified opaque artifact', async () => {
    for (const key of ['source_intake_sha256', 'graph_sha256']) {
      const ref = projectRef({ [key]: '0'.repeat(64) })
      const state = projectState(projectResult({ output: { artifact: ref, count: 4 } }))
      expect(state.mode).toBe('ready')
      expect(state.view.artifact).toEqual({ state: 'unverified' })
      expect(state.view.fields).toEqual([{ key: 'count', label: 'Count', text: '4' }])
      const downloadArtifact = vi.fn()
      const projectClient = { downloadArtifact, getSnapshot: () => ({ binding: projectBinding, authorityKey: 'a' }),
        subscribe: () => () => {}, captureCatalogScope: () => () => true }
      const wrapper = createSolarProjectArtifactClient({ projectClient })
      expect((await wrapper.downloadArtifact({ binding: projectBinding, authorityKey: 'a', ref,
        inputIntakeSha256: intakeHash, graphSha256: graphHash, isCurrent: () => true })).reason).toBe('SIP_R12_ARTIFACT_REF_INVALID')
      expect(downloadArtifact).not.toHaveBeenCalled()
    }
  })

  it('R12-20 refuses invalid canonical metadata JSON and head claims before presentation', () => {
    const bad = [
      { project_id: projectBinding.organization_id }, { input_version_id: projectBinding.organization_id },
      { head_version_id: 'bad' }, { is_head: false }, { is_head: 1 }, { current_required: 'true' },
      { tool_manifest_sha256: 'sha256:' + '0'.repeat(64) }, { adapter: 'local-graph-read' },
      { schema: 'wrong' }, { drawing_changed: true }, { tool: '../tool' }, { graph_project_id: '' },
      { input_intake_sha256: intakeHash.toUpperCase() }, { request_sha256: 'x' },
      { graph_sha256: 'x' }, { output_sha256: 'not-a-hash' },
      // A 64-character value that is not lower-case hex: a length-only check accepts these.
      { request_sha256: 'g'.repeat(64) }, { request_sha256: 'B'.repeat(64) },
      { graph_sha256: 'g'.repeat(64) }, { graph_sha256: 'B'.repeat(64) },
      { output_sha256: 'g'.repeat(64) }, { output_sha256: 'B'.repeat(64) },
      { input_intake_sha256: 'g'.repeat(64) },
      { output_bytes: 0 }, { output_bytes: 1048577 }, { output: null }, { output: [] },
      { output: { value: Infinity } }, { output: { value: undefined } },
      { output: { value: 'x'.repeat(1048577) } }, { output: { value: new Date() } },
    ]
    const cycle = {}; cycle.self = cycle; bad.push({ output: cycle })
    for (const change of bad) expect(projectState(projectResult(change))).toEqual({
      mode: 'refused', reason: 'SIP_R12_RESULT_INVALID' })
    expect(projectState(undefined, { binding: {} }).reason).toBe('SIP_R12_BINDING_INVALID')
    for (const digest of [null, '', 'sha256:ABC']) expect(projectState(undefined, { catalogDigest: digest }).reason).toBe('SIP_R12_CATALOG_DIGEST_REQUIRED')
    const accessor = vi.fn()
    const hostile = projectResult()
    Object.defineProperty(hostile, 'output', { get: accessor })
    expect(projectState(hostile).reason).toBe('SIP_R12_RESULT_INVALID')
    expect(accessor).not.toHaveBeenCalled()
    expect(isSolarProjectReadResult(new Proxy({}, { getPrototypeOf() { throw new Error() } }))).toBe(false)
  })

  it('R12-33 validates and bounds every artifact subtree before presentation', () => {
    for (const output of [
      { count: 4, nested: { artifact: 'x'.repeat(1048577) } },
      { count: 4, nested: { artifact: Infinity } },
      { count: 4, nested: { artifact: { at: new Date() } } },
      { count: 4, artifact: { ...projectRef(), extra: Infinity } },
    ]) expect(projectState(projectResult({ output }))).toEqual({ mode: 'refused', reason: 'SIP_R12_RESULT_INVALID' })
    const nested = projectState(projectResult({ output: { count: 4, nested: { artifact: 'kept-out-of-view' } } }))
    expect(nested.mode).toBe('ready')
    expect(JSON.stringify(nested.view)).not.toContain('kept-out-of-view')
    expect(projectState(projectResult()).mode).toBe('ready')
  })

  it('R12-21 refuses historical results under the default current policy', () => {
    const data = projectResult({ is_head: false, head_version_id: projectBinding.organization_id, current_required: false })
    expect(projectState(data)).toEqual({ mode: 'refused', reason: 'SIP_R12_STALE_CURRENT' })
    expect(projectState(data, { current: false }).mode).toBe('ready')
    expect(projectState(projectResult(), { current: false }).reason).toBe('SIP_R12_RESULT_INVALID')
  })

  it('R12-26 presents successful no-schedules and no-export results without manufacturing a failure', () => {
    for (const [tool, status, reason] of [['solar-electrical-schedules', 'no-schedules', 'no-schedule-data'],
      ['solar-cable-export', 'no-export', 'no-homeruns']]) {
      const state = projectState(projectResult({ tool, output: { status, reason } }))
      expect(state.mode).toBe('ready')
      expect(state.view.artifact).toBeNull()
      expect(state.view.refusal).toBeNull()
      expect(state.view.fields.map(({ text }) => text)).toEqual([status, reason])
    }
  })

  it('R12-27 removes canonical private keys at every presentation depth', () => {
    const privateRow = { organization_id: 'hidden-org', project_id: 'hidden-project',
      tenant_id: 'hidden-tenant', graph_project_id: 'hidden-graph', n: 3 }
    const output = { ...privateRow, group: { ...privateRow, nested: privateRow },
      rows: [privateRow], cells: [{ value: privateRow }], list: [[privateRow]] }
    const frozen = JSON.stringify(output)
    const view = projectState(projectResult({ output })).view
    for (const hidden of ['hidden-org', 'hidden-project', 'hidden-tenant', 'hidden-graph',
      'organization_id', 'project_id', 'tenant_id', 'graph_project_id']) expect(JSON.stringify(view)).not.toContain(hidden)
    expect(view.tables.find(({ key }) => key === 'rows').rows).toEqual([['3']])
    expect(view.tables.find(({ key }) => key === 'cells').rows).toEqual([['{"n":3}']])
    expect(JSON.stringify(output)).toBe(frozen)
    expect(PRIVATE_KEYS).toEqual(['tenant_id', 'project_id'])
  })

  it('R12-30 matches the planner frozen standalone model strings bytes and hashes', () => {
    const raw = readFileSync(process.cwd() + '/src/solar/__fixtures__/solarReadResultStandalone.main-a021056e.json', 'utf8')
    expect(createHash('sha256').update(raw).digest('hex')).toBe('e3c8dafa5a78cd21c7ef21f07e3a17dab34fb8ae852b972a6992302ae5ca3ae9')
    const oracle = JSON.parse(raw)
    expect(oracle.capturedOn).toBe('a021056e68896332d308529f5d1a60665a587fef')
    expect(Object.keys(oracle.cases)).toEqual(['ampacity', 'acDrop', 'conduit', 'cable'])
    for (const entry of Object.values(oracle.cases)) {
      const actual = JSON.stringify(readResultView(entry.input))
      expect(actual).toBe(entry.view)
      expect(Buffer.byteLength(actual, 'utf8')).toBe(entry.viewBytes)
      expect(createHash('sha256').update(actual).digest('hex')).toBe(entry.viewSha256)
    }
  })

  it('R12-32 uses a frozen literal sentence map and safe static classification', () => {
    expect(Object.isFrozen(PROJECT_READ_REASONS)).toBe(true)
    expect(Object.keys(PROJECT_READ_REASONS)).toHaveLength(24)
    expect(PROJECT_READ_REASONS).toEqual({
      binding_invalid: 'Open a project drawing version before viewing this result.',
      result_unreadable: 'This project result does not match the selected drawing version.',
      artifact_unverified: 'This result names a file that could not be verified, so no download is offered.',
      scope_stale: 'The drawing or session changed, so the download was discarded.',
      client_unavailable: 'Project downloads are not available in this view.',
      catalog_digest_required: 'Refresh the Solar tools before viewing this project result.',
      request_invalid: 'This project output request could not be prepared.',
      response_invalid: 'The server did not return a complete verified file.',
      artifact_mismatch: 'The downloaded file did not match its record, so it was discarded.',
      digest_unavailable: 'This browser could not check the downloaded file, so it was discarded.',
      timeout: 'The download took too long, so try again.',
      aborted: 'The download was cancelled.',
      unauthenticated: 'Sign in again to download this file.',
      forbidden: 'You do not have access to this file.',
      not_found: 'This file or its source version is no longer available.',
      stale: 'The drawing changed after this file was made, so run the tool again.',
      unavailable: 'Project output storage is unavailable right now, so try again shortly.',
      disabled: 'Project Solar outputs are not enabled on this server.',
      unsupported: 'This Solar tool has no connected project output adapter.',
      tool_changed: 'The Solar tool changed, so refresh the tools and run it again.',
      entitlement: 'Your plan does not currently permit this project output.',
      output_refused: 'This Solar output is unavailable for the selected drawing version.',
      save_failed: 'The file downloaded but the browser could not save it.',
      fallback: 'The download failed, so try again.',
    })
    for (const code of ['toString', '__proto__', 'constructor', 'unknown', 'credential /path', null, undefined]) {
      expect(projectReadReason(code, 500)).toBe(PROJECT_READ_REASONS.fallback)
      expect(projectReadReason(code, 401)).toBe(PROJECT_READ_REASONS.unauthenticated)
      expect(projectReadReason(code, 403)).toBe(PROJECT_READ_REASONS.forbidden)
      expect(projectReadReason(code, 404)).toBe(PROJECT_READ_REASONS.not_found)
    }
    for (const [code, sentence] of [
      ['SIP_R12_BINDING_INVALID', PROJECT_READ_REASONS.binding_invalid],
      ['SIP_R12_RESULT_INVALID', PROJECT_READ_REASONS.result_unreadable],
      ['SIP_R12_ARTIFACT_REF_INVALID', PROJECT_READ_REASONS.artifact_unverified],
      ['SIP_R12_SCOPE_STALE', PROJECT_READ_REASONS.scope_stale],
      ['SIP_R12_CLIENT_UNAVAILABLE', PROJECT_READ_REASONS.client_unavailable],
      ['SIP_R12_CATALOG_DIGEST_REQUIRED', PROJECT_READ_REASONS.catalog_digest_required],
      ['SIP_R12_REQUEST_INVALID', PROJECT_READ_REASONS.request_invalid],
      ['SIP_R12_RESPONSE_INVALID', PROJECT_READ_REASONS.response_invalid],
      ['SIP_R12_ARTIFACT_MISMATCH', PROJECT_READ_REASONS.artifact_mismatch],
      ['SIP_R12_DIGEST_UNAVAILABLE', PROJECT_READ_REASONS.digest_unavailable],
      ['SIP_R12_TIMEOUT', PROJECT_READ_REASONS.timeout],
      ['SIP_R12_ABORTED', PROJECT_READ_REASONS.aborted],
      ['SIP_R12_STALE_CURRENT', PROJECT_READ_REASONS.stale],
      ['SIP_R12_SAVE_FAILED', PROJECT_READ_REASONS.save_failed],
      ['ARTIFACT_CORRUPT', PROJECT_READ_REASONS.artifact_mismatch],
      ['SIP_R6_ARTIFACT_SOURCE_MISMATCH', PROJECT_READ_REASONS.artifact_mismatch],
      ['ARTIFACT_NOT_FOUND', PROJECT_READ_REASONS.not_found],
      ['project_execution_disabled', PROJECT_READ_REASONS.disabled],
      ['ARTIFACT_STORE_UNAVAILABLE', PROJECT_READ_REASONS.unavailable],
      ['ARTIFACT_WRITES_DRAINED', PROJECT_READ_REASONS.unavailable],
      ['SIP_R6_TOOL_UNSUPPORTED', PROJECT_READ_REASONS.unsupported],
      ['SIP_R6_TOOL_MANIFEST_MISMATCH', PROJECT_READ_REASONS.tool_changed],
      ['ENTITLEMENT_POLICY_UNAVAILABLE', PROJECT_READ_REASONS.entitlement],
      ['SCHEDULES_KERNEL_REFUSED', PROJECT_READ_REASONS.output_refused],
      ['SIP_R7_TRANSPORT_FAILED', PROJECT_READ_REASONS.fallback],
    ]) expect(projectReadReason(code, 500)).toBe(sentence)
    const source = readFileSync(process.cwd() + '/src/solar/solarReadResultModel.js', 'utf8')
    expect(source).not.toContain('PROJECT_READ_REASONS[')
  })
})
