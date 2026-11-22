// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { getConfig } from '@testing-library/react'
import { parseTestTimeScale, scaleTimeout, TEST_TIME_SCALE } from './testTimeScale.js'

describe('parseTestTimeScale', () => {
  it.each([
    [undefined, 3], ['', 3], ['1', 1], ['10', 10], ['2.5', 2.5],
  ])('parses %j as %s', (raw, expected) => {
    expect(parseTestTimeScale(raw)).toBe(expected)
  })

  it.each(['0', '0.5', '11', 'abc', 'NaN', 'Infinity'])('rejects %s', (raw) => {
    expect(() => parseTestTimeScale(raw)).toThrow('LEAF_TEST_TIME_SCALE must be a number from 1 to 10')
  })
})

describe('scaleTimeout', () => {
  it('scales a ceiling by the environment factor and rounds it', () => {
    expect(scaleTimeout(1000)).toBe(Math.round(1000 * TEST_TIME_SCALE))
  })

  it.each([0, -1, NaN, '5'])('rejects invalid ceiling %j', (ms) => {
    expect(() => scaleTimeout(ms)).toThrow(TypeError)
  })

  it('scales Vitest test and hook defaults', async () => {
    const config = (await import('../vitest.config.js')).default
    expect(config.test.testTimeout).toBe(scaleTimeout(5000))
    expect(config.test.hookTimeout).toBe(scaleTimeout(10000))
  })

  it('scales the Testing Library async default in setup', () => {
    expect(getConfig().asyncUtilTimeout).toBe(scaleTimeout(1000))
  })
})
