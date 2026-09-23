import { expect, it } from 'vitest'
import { humanizeError, MSG_GENERIC, MSG_NETWORK, MSG_SERVICE } from './errorHumanize.js'

it('SSD1-B2 row1', () => {
  const value = JSON.parse('{"toString":0}')
  expect(() => humanizeError(value)).not.toThrow()
  expect(humanizeError(value)).toBe(MSG_GENERIC)
})

it('SSD1-B2 row2', () => {
  const value = {
    get message() {
      throw new Error('Cannot read message')
    },
  }
  expect(humanizeError(value)).toBe(MSG_GENERIC)
})

it('SSD1-B2 row3', () => {
  const value = {
    get name() {
      throw new Error('Cannot read name')
    },
  }
  expect(humanizeError(value)).toBe(MSG_GENERIC)
})

it('SSD1-B2 row4', () => {
  expect(humanizeError(null)).toBe(MSG_GENERIC)
  expect(humanizeError(new Error('Could not load the drawing.'))).toBe('Could not load the drawing.')
  expect(humanizeError(new TypeError('Failed to fetch'))).toBe(MSG_NETWORK)
  expect(humanizeError('POST /api/run -> 502')).toBe(MSG_SERVICE)
})
