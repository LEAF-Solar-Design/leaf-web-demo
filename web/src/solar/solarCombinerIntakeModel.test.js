import { describe, expect, it } from 'vitest'
import { MODEL_WHITESPACE, placementParams, validateHardware } from './solarCombinerIntakeModel.js'

const raw = { model: 'Combiner', max_dc_voltage: '480', max_ac_power_kw: '10' }

describe('combiner intake model', () => {
  it('CI7 matches Python model length and whitespace rules without trimming', () => {
    for (const model of ['', ...MODEL_WHITESPACE, MODEL_WHITESPACE.join(''), 'x'.repeat(129)]) {
      expect(validateHardware({ ...raw, model })).toMatchObject({ ok: false, field: 'model', reason: expect.any(String) })
    }
    for (const model of ['😀'.repeat(128), '\u0000', ' Combiner ', '\uFEFF']) {
      expect(validateHardware({ ...raw, model })).toEqual({ ok: true, hardware: { model, max_dc_voltage: 480, max_ac_power_kw: 10 } })
    }
  })

  it('CI8 refuses non-decimal, non-finite and out-of-range ratings', () => {
    for (const field of ['max_dc_voltage', 'max_ac_power_kw']) {
      for (const value of ['0', '-1', '1000000.0000001', 'NaN', 'Infinity', '1e400', '0x10', '', ' 5', '5 ', '5\n', '+5', true, null, NaN, Infinity]) {
        expect(validateHardware({ ...raw, [field]: value })).toMatchObject({ ok: false, field, reason: expect.any(String) })
        expect(placementParams(7, { ...raw, [field]: value })).toBeNull()
      }
    }
  })

  it('CI9 keeps the smallest positive and maximum ratings as numbers with exact parameter keys', () => {
    for (const text of ['5e-324', '1000000', '480']) {
      const hardware = { model: ' Combiner ', max_dc_voltage: Number(text), max_ac_power_kw: Number(text) }
      expect(validateHardware({ ...hardware, max_dc_voltage: text, max_ac_power_kw: text })).toEqual({ ok: true, hardware })
      expect(placementParams(7, { ...hardware, extra: true })).toEqual({ expected_rev: 7, hardware })
      expect(placementParams(7, hardware)).toEqual({ expected_rev: 7, hardware })
    }
    for (const rev of [0, 2_147_483_647]) expect(placementParams(rev, raw)?.expected_rev).toBe(rev)
    for (const rev of [-1, 2_147_483_648, 1.5, '7', true, NaN, Infinity, null]) expect(placementParams(rev, raw)).toBeNull()
    expect(placementParams(7, null)).toBeNull()
  })

  it('CI14 exports exactly the frozen 29 Python whitespace code points', () => {
    const points = [
      0x0009, 0x000A, 0x000B, 0x000C, 0x000D, 0x001C, 0x001D, 0x001E, 0x001F,
      0x0020, 0x0085, 0x00A0, 0x1680, 0x2000, 0x2001, 0x2002, 0x2003, 0x2004,
      0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200A, 0x2028, 0x2029, 0x202F,
      0x205F, 0x3000,
    ]
    expect(MODEL_WHITESPACE.map((point) => point.codePointAt(0))).toEqual(points)
    expect(Object.isFrozen(MODEL_WHITESPACE)).toBe(true)
  })
})
