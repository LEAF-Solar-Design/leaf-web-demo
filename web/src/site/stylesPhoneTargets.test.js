// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, it, expect } from 'vitest'

const css = readFileSync(new URL('../styles.css', import.meta.url), 'utf8')
  .replace(/\r\n/g, '\n')
  .replace(/\/\*[\s\S]*?\*\//g, '')
const baseLegend = /(?:^|\n)\.legend-row\s*\{([^{}]*)\}/.exec(css)
const phoneBlocks = [...css.matchAll(/@media\s*\(\s*max-width\s*:\s*599px\s*\)\s*\{((?:[^{}]|\{[^{}]*\})*)\}/g)]

describe('phone touch targets (L4)', () => {
  it('sets a 44px legend row minimum in a phone block after the base rule', () => {
    expect(baseLegend).not.toBeNull()
    const laterBlocks = phoneBlocks.filter((block) => block.index > baseLegend.index)
    expect(laterBlocks.length).toBeGreaterThan(0)
    const legendRules = laterBlocks.flatMap((block) =>
      [...block[1].matchAll(/([^{}]+)\{([^{}]*)\}/g)]
        .filter((rule) => rule[1].trim() === '.legend-row'),
    )
    expect(legendRules.some((rule) =>
      /(?:^|;)\s*min-height\s*:\s*44px\s*(?:;|$)/.test(rule[2]),
    )).toBe(true)
  })

  it('preserves the desktop legend row padding', () => {
    expect(baseLegend).not.toBeNull()
    expect(baseLegend[1]).toMatch(/(?:^|;)\s*padding\s*:\s*4px\s+2px\s*(?:;|$)/)
  })
})
