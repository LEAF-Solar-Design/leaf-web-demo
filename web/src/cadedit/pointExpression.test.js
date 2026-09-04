import { describe, expect, it } from 'vitest'

import {
  MAX_EXPRESSION_CHARS, isPointExpression, parsePointExpression, pointExpressionRefusal, resolvePointExpression,
} from './pointExpression.js'

describe('pointExpression (W4f-8): the command line\'s point grammar', () => {
  it('tells an expression from a plain number', () => {
    expect(isPointExpression('10')).toBe(false)
    expect(isPointExpression('-2.5')).toBe(false)
    expect(isPointExpression('')).toBe(false)
    expect(isPointExpression(undefined)).toBe(false)
    expect(isPointExpression('10,5')).toBe(true)
    expect(isPointExpression('@10,5')).toBe(true)
    expect(isPointExpression('20<45')).toBe(true)
    expect(isPointExpression('@')).toBe(true)
    expect(isPointExpression('1,'.padEnd(MAX_EXPRESSION_CHARS + 1, '1'))).toBe(false)
  })

  it('parses the four forms and refuses everything else', () => {
    expect(parsePointExpression('10,5')).toEqual({ relative: false, polar: false, a: 10, b: 5 })
    expect(parsePointExpression(' @ -10 , 5.25 ')).toEqual({ relative: true, polar: false, a: -10, b: 5.25 })
    expect(parsePointExpression('20<45')).toEqual({ relative: false, polar: true, a: 20, b: 45 })
    expect(parsePointExpression('@20<-90')).toEqual({ relative: true, polar: true, a: 20, b: -90 })
    expect(parsePointExpression('1e2,5')).toEqual({ relative: false, polar: false, a: 100, b: 5 })
    for (const bad of ['@', '10,', ',5', '10,5,6', '20<', '<45', '20<45<3', '10<5,6', '10abc,5', '10,5x', 'ten,five', 'NaN,1', 'Infinity,1']) {
      expect(parsePointExpression(bad), bad).toBeNull()
    }
  })

  it('resolves absolute, relative and polar forms against an anchor, rounded to three decimals', () => {
    expect(resolvePointExpression('10,5')).toEqual([10, 5])
    expect(resolvePointExpression('10,5', [100, 100])).toEqual([10, 5])
    expect(resolvePointExpression('@10,5', [100, 100])).toEqual([110, 105])
    expect(resolvePointExpression('@-10,-5.5', [100, 100])).toEqual([90, 94.5])
    expect(resolvePointExpression('20<90')).toEqual([0, 20])
    expect(resolvePointExpression('20<180')).toEqual([-20, 0])
    expect(resolvePointExpression('10<45')).toEqual([7.071, 7.071])
    expect(resolvePointExpression('@10<0', [5, 5])).toEqual([15, 5])
    expect(resolvePointExpression('@10<270', [5, 5])).toEqual([5, -5])
    // A relative form with no anchor, or a bad anchor, resolves to nothing.
    expect(resolvePointExpression('@10,5')).toBeNull()
    expect(resolvePointExpression('@10,5', null)).toBeNull()
    expect(resolvePointExpression('@10,5', [NaN, 1])).toBeNull()
    expect(resolvePointExpression('@10,5', [1])).toBeNull()
    expect(resolvePointExpression('nope')).toBeNull()
    expect(resolvePointExpression('10')).toBeNull()
  })

  it('names the refusal in the drafter\'s words', () => {
    expect(pointExpressionRefusal('10')).toBe('')
    expect(pointExpressionRefusal('10,5')).toBe('')
    expect(pointExpressionRefusal('@10,5', [1, 1])).toBe('')
    expect(pointExpressionRefusal('@10,5')).toBe('"@" needs a previous point to measure from.')
    expect(pointExpressionRefusal('10,5,6')).toBe('"10,5,6" is not a point: use x,y, @dx,dy, dist<angle or @dist<angle.')
    expect(pointExpressionRefusal('20<')).toBe('"20<" is not a point: use x,y, @dx,dy, dist<angle or @dist<angle.')
  })
})
