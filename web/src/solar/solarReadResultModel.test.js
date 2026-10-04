// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
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
