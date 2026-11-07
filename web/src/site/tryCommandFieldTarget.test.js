// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, it, expect } from 'vitest'

const css = readFileSync(new URL('./landing.css', import.meta.url), 'utf8')
  .replace(/\r\n/g, '\n')
  .replace(/\/\*[\s\S]*?\*\//g, '')
const phonePattern = /@media\s*\(\s*max-width\s*:\s*599px\s*\)\s*\{((?:[^{}]|\{[^{}]*\})*)\}/g
const rules = (source) => [...source.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
  .map(([, selector, body]) => ({ selector: selector.trim().replace(/\s+/g, ' '), body }))
const hasDeclaration = (body, property, value) => new RegExp(`(?:^|;)\\s*${property}\\s*:\\s*${value}\\s*(?:;|$)`).test(body)
const fieldSelector = '.tc-bar .bar-input .tc-bar-input'

describe('/try command field phone touch target (L4)', () => {
  const phone = [...css.matchAll(phonePattern)].find((block) => rules(block[1])
    .some((rule) => rule.selector === '.tc-bar-wrap'))

  it('puts the 44px touch target and vertical padding on the field', () => {
    expect(phone).toBeDefined()
    const fields = rules(phone?.[1] || '').filter((rule) => rule.selector === fieldSelector)
    expect(fields.length).toBeGreaterThan(0)
    for (const [property, value] of [
      ['min-width', '0'],
      ['min-height', '44px'],
      ['padding', '12px 0'],
      ['box-sizing', 'border-box'],
    ]) {
      expect(fields.some((rule) => hasDeclaration(rule.body, property, value))).toBe(true)
    }
    const widthIndex = fields.findIndex((rule) => hasDeclaration(rule.body, 'min-width', '0'))
    const heightIndex = fields.findIndex((rule) => hasDeclaration(rule.body, 'min-height', '44px'))
    expect(heightIndex).toBeGreaterThan(widthIndex)
  })

  it('removes the row padding above and below the field', () => {
    expect(phone).toBeDefined()
    const row = rules(phone?.[1] || '').find((rule) => rule.selector === '.tc-bar .tc-bar-input-row')
    expect(row).toBeDefined()
    expect(hasDeclaration(row?.body || '', 'padding', '0 12px')).toBe(true)
  })

  it('keeps the 44px field minimum out of non-phone rules', () => {
    const outsidePhone = css.replace(phonePattern, '')
    const fields = rules(outsidePhone).filter((rule) => rule.selector === fieldSelector)
    expect(fields.some((rule) => hasDeclaration(rule.body, 'min-height', '44px'))).toBe(false)
  })
})
