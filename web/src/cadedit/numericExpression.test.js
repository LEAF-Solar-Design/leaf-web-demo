import { describe, expect, it } from 'vitest'

import { isRelativeDimension, parseDimension } from './numericExpression.js'

const options = { current: 10, unit: 'mm' }

function expectValue(raw, value, overrides = {}) {
  const result = parseDimension(raw, { ...options, ...overrides })
  expect(result.ok, raw).toBe(true)
  expect(result.value, raw).toBeCloseTo(value, 10)
  expect(result.canonical, raw).toBe(String(result.value))
  expect(Object.keys(result).sort()).toEqual(['canonical', 'ok', 'value'])
}

function expectRefusal(raw, overrides = {}) {
  const result = parseDimension(raw, { ...options, ...overrides })
  expect(result.ok, String(raw)).toBe(false)
  expect(result.reason).toMatch(/^[A-Z].*\.$/)
  expect(Object.keys(result).sort()).toEqual(['ok', 'reason'])
}

describe('numericExpression: bounded CAD dimensions', () => {
  it.each([
    ['5', 5], ['-5', -5], ['+5', 5], ['5.25', 5.25],
    ['-0.5', -0.5], ['+.5', 0.5], ['.5', 0.5], ['5.', 5],
    ['  +005.250  ', 5.25], ['-0', 0], ['+0', 0],
  ])('treats %s as an absolute decimal regardless of current', (raw, value) => {
    expect(parseDimension(raw, options)).toEqual({ ok: true, value, canonical: String(value) })
    expect(parseDimension(raw, { unit: 'mm' })).toEqual({ ok: true, value, canonical: String(value) })
    expect(isRelativeDimension(raw)).toBe(false)
  })

  it.each([
    ['1mm', 1], ['1cm', 10], ['1m', 1000], ['1in', 25.4], ['1ft', 304.8],
    ["1'", 304.8], ['1"', 25.4], ['1′', 304.8], ['1″', 25.4],
    ['-2cm', -20], ['+2.5 cm', 25], [' .5 IN ', 12.7],
  ])('converts %s into millimetres', (raw, value) => {
    expectValue(raw, value)
  })

  it.each([
    ['mm', 1000], ['cm', 100], ['m', 1], ['in', 1000 / 25.4], ['ft', 1000 / 304.8],
    ['"', 1000 / 25.4], ["'", 1000 / 304.8], ['′', 1000 / 304.8], ['″', 1000 / 25.4],
  ])('converts metres into drawing unit %s', (unit, value) => {
    expectValue('1m', value, { unit })
  })

  it('preserves unitless field values and canonicalizes equivalent units', () => {
    for (const unit of ['mm', 'cm', 'm', 'in', 'ft']) expectValue('5', 5, { unit })
    expect(parseDimension('1ft', { unit: 'in' })).toEqual({ ok: true, value: 12, canonical: '12' })
    expect(parseDimension('10mm', { unit: 'cm' })).toEqual({ ok: true, value: 1, canonical: '1' })
  })

  it.each([['@-2', 8], ['@+2', 12], ['@*4', 40], ['@/2', 5]])(
    'resolves %s against current', (raw, value) => {
      expect(parseDimension(raw, options)).toEqual({ ok: true, value, canonical: String(value) })
      expect(isRelativeDimension(raw)).toBe(true)
    },
  )

  it('converts relative distances and accepts decimal factors and spacing', () => {
    expectValue('@+2cm', 30)
    expectValue('@-0.5in', -2.7)
    expectValue(' @ + .5 cm ', 15)
    expectValue('@*0.5', 5)
    expectValue('@/0.5', 20)
    expectValue('@+2', -8, { current: -10 })
    expectValue('@*0', 0, { current: -10 })
  })

  it('detects the marker without mistaking bare signs for relative arithmetic', () => {
    for (const raw of ['@', '@nope', ' @/2 ']) expect(isRelativeDimension(raw)).toBe(true)
    for (const raw of [undefined, null, 5, '', '-5', '+5', '2@+3', '@'.padEnd(65, ' ')]) {
      expect(isRelativeDimension(raw)).toBe(false)
    }
  })

  it('accepts exactly 64 characters and refuses 65, including outer whitespace', () => {
    expectValue('0'.repeat(63) + '5', 5)
    expectRefusal('0'.repeat(64) + '5')
    expectRefusal('5'.padEnd(65, ' '))
  })

  it.each([
    '10abc', '2**3', '@/0', '@/0.0', '1yd', '', ' ', '.', '+', '-', '@', '@2',
    '@+2+3', '@--2', '@*4mm', '@/2cm', '1 2', '1.2.3', '5cm mm', '1e3',
    'NaN', 'Infinity', '0x10', '1/2', '(2)', 'Math.random()', '2;alert(1)',
    undefined, null, 5,
  ])('refuses malformed or unsafe input %s with a sentence', (raw) => {
    expectRefusal(raw)
  })

  it('refuses unknown drawing units even for a bare number or unitless factor', () => {
    for (const unit of ['yd', 'constructor', '', undefined, null, 1]) {
      expectRefusal('5', { unit })
      expectRefusal('1mm', { unit })
      expectRefusal('@*2', { unit })
      expect(parseDimension('5', { unit }).reason).toContain('drawing unit is unknown')
    }
  })

  it('requires a finite current only for relative input and refuses overflow', () => {
    for (const current of [undefined, null, NaN, Infinity, -Infinity, '10']) {
      for (const raw of ['@+2', '@-2', '@*4', '@/2']) expectRefusal(raw, { current })
      expectValue('-5', -5, { current })
    }
    expectRefusal('@*4', { current: Number.MAX_VALUE })
    expectRefusal('@/0.1', { current: Number.MAX_VALUE })
  })
})
