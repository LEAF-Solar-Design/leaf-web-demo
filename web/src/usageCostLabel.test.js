import { readFileSync } from 'node:fs'
import { expect, it } from 'vitest'
import { orDash, usageCostLabel } from './usage.js'

it('usageCostLabel renders tokens first then the USD estimate', () => {
  expect(usageCostLabel(1250, 0.125)).toBe(`${(1250).toLocaleString()} tokens · ~$0.125 est`)
  expect(usageCostLabel(1250, 0.0004)).toBe(`${(1250).toLocaleString()} tokens · ~$0.0004 est`)
  expect(usageCostLabel('1250', '0.125')).toBe(`${(1250).toLocaleString()} tokens · ~$0.125 est`)
  expect(usageCostLabel(0, 0)).toBe(`${(0).toLocaleString()} tokens · ~$0.000 est`)
})

it('usageCostLabel renders USD alone only as an estimate, never a bare dollar figure', () => {
  expect(usageCostLabel(null, 0.125)).toBe('~$0.125 est')
  expect(usageCostLabel(undefined, 0.0004)).toBe('~$0.0004 est')
  expect(usageCostLabel(-1, 0.01)).toBe('~$0.010 est')
})

it('usageCostLabel renders tokens alone with no dollar figure', () => {
  expect(usageCostLabel(1250, null)).toBe(`${(1250).toLocaleString()} tokens`)
  expect(usageCostLabel(1250, -1)).toBe(`${(1250).toLocaleString()} tokens`)
})

it('usageCostLabel renders the honest dash when both are absent or malformed', () => {
  const absent = [null, undefined, '', true, [], NaN, -1]
  for (const tokens of absent) {
    for (const usd of absent) {
      expect(usageCostLabel(tokens, usd)).toBe(orDash(null))
    }
  }
})

it('ConversePanel and AuthorPanel format cost only through usageCostLabel', () => {
  for (const file of ['ConversePanel.jsx', 'AuthorPanel.jsx']) {
    const source = readFileSync(`${process.cwd()}/src/components/${file}`, 'utf8')
    expect(source).toContain('usageCostLabel(')
    expect(source).not.toContain('~$${')
  }
})
