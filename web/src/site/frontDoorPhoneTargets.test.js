// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, it, expect } from 'vitest'

const css = readFileSync(new URL('./landing.css', import.meta.url), 'utf8')
  .replace(/\r\n/g, '\n')
  .replace(/\/\*[\s\S]*?\*\//g, '')
const phonePattern = /@media\s*\(\s*max-width\s*:\s*599px\s*\)\s*\{((?:[^{}]|\{[^{}]*\})*)\}/g
const phoneBlocks = [...css.matchAll(phonePattern)]
const rules = (source) => [...source.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
  .map(([, selector, body]) => ({ selector: selector.trim().replace(/\s+/g, ' '), body }))
const hasTouchMinimum = (body) => /(?:^|;)\s*min-height\s*:\s*44px\s*(?:;|$)/.test(body)

describe('front door phone touch targets (L4)', () => {
  const selectors = [
    '.lp-stmt .lp-cta-row button',
    '.lp-stmt form input, .lp-stmt form button',
    '.lp-index .lp-tab',
    '.tc-product-tabs button',
  ]

  it.each(selectors)('sets a 44px minimum for %s in the front door phone block', (selector) => {
    const phone = phoneBlocks.find((block) => rules(block[1])
      .some((rule) => rule.selector === '.lp-cta-row'))
    expect(phone).toBeDefined()
    const targetRules = rules(phone?.[1] || '').filter((rule) => rule.selector === selector)
    expect(targetRules.some((rule) => hasTouchMinimum(rule.body))).toBe(true)
  })

  it('keeps the product tab 44px minimum out of non-phone rules', () => {
    const outsidePhone = css.replace(phonePattern, '')
    const productRules = rules(outsidePhone).filter((rule) => rule.selector
      .split(/\s*,\s*/).includes('.tc-product-tabs button'))
    expect(productRules.length).toBeGreaterThan(0)
    expect(productRules.filter((rule) => hasTouchMinimum(rule.body))).toEqual([])
  })
})
