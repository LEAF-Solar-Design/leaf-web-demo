import { describe, it, expect, vi } from 'vitest'
import * as model from './solarTrackerRowsModel.js'

const row = () => ({ axis_start: [0, 0], axis_end: [0, 6], cross_axis_width_du: 2, slots: 3 })
const body = () => ({ operation: 'manual-create', rows: [row()], module_power_watts: 450, expected_head: null })
const draft = () => ({ rows: [row()], modulePowerWatts: 450, expectedHead: null, drawingUnits: 'm' })
const typed = (value, units = 'm') => model.validateTrackerRowsRequest(value, { drawingUnits: units })
const error = (suffix, field = null) => ({ ok: false, code: `TRACKER_ROWS_${suffix}`, field })
const good = (value) => ({ ok: true, body: value })

describe('tracker rows model', () => {
  it('m01_exports_and_constants', () => {
    expect(model.TRACKER_ROWS_OPERATION).toBe('manual-create')
    expect(model.TRACKER_ROWS_RESULT_SCHEMA).toBe('leaf.solar-tracker-rows.v1')
    expect([model.TRACKER_ROWS_MAX_ROWS, model.TRACKER_ROWS_MAX_ROW_SLOTS,
      model.TRACKER_ROWS_MAX_TOTAL_SLOTS, model.TRACKER_ROWS_MAX_REQUEST_BYTES]).toEqual([256, 10000, 100000, 262144])
    for (const key of ['buildTrackerRowsRequest', 'validateTrackerRowsRequest', 'validateTrackerRowsResult']) {
      expect(typeof model[key]).toBe('function')
    }
  })

  it('m02_normal_request_four_keys', () => {
    expect(model.buildTrackerRowsRequest(draft())).toEqual(good(body()))
    expect(typed(body())).toEqual(good(body()))
    expect(Object.keys(typed(body()).body)).toEqual(['operation', 'rows', 'module_power_watts', 'expected_head'])
  })

  it('m03_decimal_drafts_and_integral_slots', () => {
    const value = draft()
    value.rows[0] = { axis_start: [' +0e2 ', '-.0'], axis_end: ['.0', '6.'], cross_axis_width_du: '2e0', slots: '3.0' }
    value.modulePowerWatts = ' 4.50e2 '
    expect(model.buildTrackerRowsRequest(value).ok).toBe(true)
    expect(JSON.stringify(model.buildTrackerRowsRequest(value).body)).toBe(JSON.stringify(body()))
    for (const invalid of ['', ' ', '0x1', 'Infinity', 'NaN', true, null, [], {}]) {
      const bad = draft()
      bad.rows[0].axis_start[0] = invalid
      expect(model.buildTrackerRowsRequest(bad)).toEqual(error('ROW_INVALID', 'rows.0.axis_start.0'))
    }
  })

  it('m04_typed_validation_never_coerces', () => {
    for (const [key, value, suffix, field] of [
      ['slots', '3', 'SLOTS_INVALID', 'rows.0.slots'],
      ['cross_axis_width_du', '2', 'WIDTH_INVALID', 'rows.0.cross_axis_width_du'],
      ['axis_start', ['0', 0], 'ROW_INVALID', 'rows.0.axis_start.0'],
    ]) {
      const bad = body()
      bad.rows[0][key] = value
      expect(typed(bad)).toEqual(error(suffix, field))
    }
    expect(typed({ ...body(), module_power_watts: '450' })).toEqual(error('POWER_INVALID', 'module_power_watts'))
  })

  it('m05_closed_body_and_operation', () => {
    for (const extra of ['project_id', 'drawing_units', 'checkout', 'calculated']) {
      expect(typed({ ...body(), [extra]: null })).toEqual(error('REQUEST_INVALID'))
    }
    expect(typed({ ...body(), [Symbol('extra')]: 1 })).toEqual(error('REQUEST_INVALID'))
    expect(typed({ ...body(), operation: 'mesh' })).toEqual(error('OPERATION_UNSUPPORTED', 'operation'))
    expect(model.buildTrackerRowsRequest({ ...draft(), extra: 1 })).toEqual(error('REQUEST_INVALID'))
    for (const value of [null, [], true, 4, new Date()]) expect(typed(value)).toEqual(error('REQUEST_INVALID'))
  })

  it('m06_row_count_boundaries', () => {
    expect(typed({ ...body(), rows: [] })).toEqual(error('ROWS_INVALID', 'rows'))
    for (const value of [null, {}, 'rows']) expect(typed({ ...body(), rows: value })).toEqual(error('ROWS_INVALID', 'rows'))
    const many = (count) => ({ ...body(), rows: Array.from({ length: count }, () => ({ ...row(), slots: 1 })) })
    expect(typed(many(256))).toEqual(good(many(256)))
    expect(typed(many(257))).toEqual(error('LIMIT_EXCEEDED', 'rows'))
  })

  it('m07_exact_row_keys', () => {
    for (const key of Object.keys(row())) {
      const bad = body()
      delete bad.rows[0][key]
      expect(typed(bad)).toEqual(error('ROW_INVALID', 'rows.0'))
    }
    for (const value of [null, [], { ...row(), row_index: 0 }, { ...row(), [Symbol('x')]: 1 }]) {
      expect(typed({ ...body(), rows: [value] })).toEqual(error('ROW_INVALID', 'rows.0'))
    }
    expect(typed({ ...body(), rows: new Array(1) })).toEqual(error('ROW_INVALID', 'rows.0'))
  })

  it('m08_point_shape', () => {
    for (const value of [[], [0], [0, 0, 0], new Array(2), null, { 0: 0, 1: 0, length: 2 }]) {
      const bad = body()
      bad.rows[0].axis_start = value
      expect(typed(bad)).toEqual(error('ROW_INVALID', 'rows.0.axis_start'))
    }
  })

  it('m09_coordinate_inclusive_endpoints', () => {
    for (const x of [-1e9, 1e9]) {
      const value = body()
      value.rows[0].axis_start[0] = x
      value.rows[0].axis_end[0] = x
      expect(typed(value)).toEqual(good(value))
    }
  })

  it('m10_nonfinite_boolean_and_outside_coordinates', () => {
    for (const number of [NaN, Infinity, -Infinity, true, null, 1e9 + 1, -1e9 - 1]) {
      const value = body()
      value.rows[0].axis_start[0] = number
      expect(typed(value)).toEqual(error('ROW_INVALID', 'rows.0.axis_start.0'))
    }
  })

  it('m11_slot_boundaries', () => {
    for (const slots of [1, 10000]) {
      const value = body()
      value.rows[0].slots = slots
      expect(typed(value)).toEqual(good(value))
    }
    for (const slots of [0, 10001, 1.5, true, null, Infinity]) {
      const value = body()
      value.rows[0].slots = slots
      expect(typed(value)).toEqual(error('SLOTS_INVALID', 'rows.0.slots'))
    }
  })

  it('m12_total_slot_boundaries', () => {
    const value = { ...body(), rows: Array.from({ length: 10 }, () => ({ ...row(), slots: 10000 })) }
    expect(typed(value)).toEqual(good(value))
    value.rows.push({ ...row(), slots: 1 })
    expect(typed(value)).toEqual(error('LIMIT_EXCEEDED', 'rows'))
  })

  it('m13_axis_epsilon_boundaries', () => {
    for (const length of [0, 1e-9, 1.0000000000000003e-9]) {
      const value = body()
      value.rows[0].axis_end = [0, length]
      expect(typed(value)).toEqual(length > 1e-9 ? good(value) : error('AXIS_INVALID', 'rows.0.axis_end'))
    }
  })

  it('m14_axis_metre_limits_in_both_units', () => {
    for (const [units, length, accepted] of [['m', 100000, true], ['m', 100000.00000000001, false],
      ['ft', 328083, true], ['ft', 328084, false]]) {
      const value = body()
      value.rows[0].axis_end = [0, length]
      expect(typed(value, units)).toEqual(accepted ? good(value) : error('AXIS_INVALID', 'rows.0.axis_end'))
    }
  })

  it('m15_width_epsilon_boundaries', () => {
    for (const width of [-1, 0, 1e-9, 1.0000000000000003e-9]) {
      const value = body()
      value.rows[0].cross_axis_width_du = width
      expect(typed(value)).toEqual(width > 1e-9 ? good(value) : error('WIDTH_INVALID', 'rows.0.cross_axis_width_du'))
    }
  })

  it('m16_width_metre_limits_in_both_units', () => {
    for (const [units, width, accepted] of [['m', 1000, true], ['m', 1000.0000000000001, false],
      ['ft', 3280, true], ['ft', 3281, false]]) {
      const value = body()
      value.rows[0].cross_axis_width_du = width
      expect(typed(value, units)).toEqual(accepted ? good(value) : error('WIDTH_INVALID', 'rows.0.cross_axis_width_du'))
    }
  })

  it('m17_power_boundaries', () => {
    for (const power of [1e-300, 1e15]) {
      const value = { ...body(), module_power_watts: power }
      expect(typed(value)).toEqual(good(value))
    }
    for (const power of [0, -1, 1e15 + 1, true, null, Infinity]) {
      expect(typed({ ...body(), module_power_watts: power })).toEqual(error('POWER_INVALID', 'module_power_watts'))
    }
  })

  it('m18_required_expected_head', () => {
    for (const expected_head of [null, 'a'.repeat(64)]) {
      const value = { ...body(), expected_head }
      expect(typed(value)).toEqual(good(value))
    }
    for (const expected_head of [undefined, true, 'A'.repeat(64), 'a'.repeat(63)]) {
      expect(typed({ ...body(), expected_head })).toEqual(error('HEAD_INVALID', 'expected_head'))
    }
    const value = body()
    delete value.expected_head
    expect(typed(value)).toEqual(error('HEAD_INVALID', 'expected_head'))
    const input = draft()
    delete input.expectedHead
    expect(model.buildTrackerRowsRequest(input)).toEqual(error('HEAD_INVALID', 'expected_head'))
  })

  it('m19_drawing_units', () => {
    for (const units of ['m', 'ft']) expect(typed(body(), units).ok).toBe(true)
    for (const units of ['in', '', null, undefined, 'M']) {
      expect(typed(body(), units === undefined ? null : units)).toEqual(error('UNITS_UNSUPPORTED', 'drawing_units'))
      expect(model.buildTrackerRowsRequest({ ...draft(), drawingUnits: units })).toEqual(error('UNITS_UNSUPPORTED', 'drawing_units'))
    }
  })

  it('m20_validation_order_and_field_paths', () => {
    expect(typed({ ...body(), module_power_watts: 0, rows: [null] })).toEqual(error('POWER_INVALID', 'module_power_watts'))
    const value = { ...body(), rows: Array.from({ length: 11 }, () => ({ ...row(), slots: 10000 })) }
    value.rows[10].axis_end = [0, 0]
    value.rows[10].cross_axis_width_du = 0
    expect(typed(value)).toEqual(error('LIMIT_EXCEEDED', 'rows'))
    value.rows[10].slots = 0
    expect(typed(value)).toEqual(error('SLOTS_INVALID', 'rows.10.slots'))
    expect(typed({ ...body(), expected_head: true }, 'in')).toEqual(error('HEAD_INVALID', 'expected_head'))
    expect(typed({ ...body(), rows: [], module_power_watts: 0 })).toEqual(error('ROWS_INVALID', 'rows'))
    const bad = body()
    bad.rows[0].axis_end = [0, null]
    bad.rows[0].slots = 0
    expect(typed(bad)).toEqual(error('ROW_INVALID', 'rows.0.axis_end.1'))
  })

  it('m21_order_preserved_and_inputs_unchanged', () => {
    const value = body()
    value.rows.push({ axis_start: [2, 3], axis_end: [2, 5], cross_axis_width_du: 1, slots: 4 })
    const before = structuredClone(value)
    const result = typed(value)
    expect(result).toEqual(good(before))
    result.body.rows[0].axis_start[0] = 7
    expect(value).toEqual(before)
    expect(result.body.rows[1]).toEqual(before.rows[1])
    const input = draft()
    Object.freeze(input.rows[0].axis_start)
    Object.freeze(input.rows[0])
    expect(model.buildTrackerRowsRequest(input).ok).toBe(true)
  })

  it('m22_hostile_inputs_resolve', () => {
    const hostile = new Proxy({}, { ownKeys() { throw new Error('private') } })
    for (const value of [hostile, Object.defineProperty(body(), 'rows', { get() { throw new Error('private') } })]) {
      expect(typed(value)).toEqual(error('REQUEST_INVALID'))
      expect(model.buildTrackerRowsRequest(value)).toEqual(error('REQUEST_INVALID'))
    }
    expect(model.validateTrackerRowsResult(hostile, hostile)).toBeNull()
    const revoked = Proxy.revocable({}, {})
    revoked.revoke()
    expect(typed(revoked.proxy)).toEqual(error('REQUEST_INVALID'))
  })

  it('m23_serialized_request_bound', () => {
    const value = { ...body(), rows: Array.from({ length: 256 }, () => ({ ...row(), slots: 1 })) }
    const result = typed(value)
    expect(result.ok).toBe(true)
    expect(new TextEncoder().encode(JSON.stringify(result.body)).byteLength).toBeLessThanOrEqual(262144)
    const encode = vi.spyOn(TextEncoder.prototype, 'encode').mockReturnValue(new Uint8Array(262145))
    try { expect(typed(body())).toEqual(error('LIMIT_EXCEEDED')) } finally { encode.mockRestore() }
  })

  it('m24_signed_zero_and_json_integral_float_limitation', () => {
    const value = body()
    value.rows[0].axis_start[0] = -0
    value.rows[0].slots = JSON.parse('3.0')
    const result = typed(value)
    expect(result.ok).toBe(true)
    expect(Object.is(result.body.rows[0].axis_start[0], -0)).toBe(true)
    expect(JSON.stringify(result.body)).toBe(JSON.stringify(body()))
    expect(model.buildTrackerRowsRequest({ ...draft(), rows: [{ ...row(), slots: '3.0' }] })).toEqual(good(body()))
  })
})
