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
const hasDeclaration = (body, property, value) => new RegExp(`(?:^|;)\\s*${property}\\s*:\\s*${value}\\s*(?:;|$)`).test(body)

describe('/try console phone touch targets (L4)', () => {
  const expectedRules = [
    ['.stage-root .tc-rail [role="tab"]', ['min-height', 'box-sizing']],
    ['.stage-root .tc-back, .stage-root .proj-chip', ['min-height', 'box-sizing']],
    ['.stage-root .bar-add', ['min-width', 'min-height', 'box-sizing']],
    ['.stage-root .bar-scope, .stage-root .tc-run', ['min-height', 'box-sizing']],
    ['.stage-root .session-gate button, .stage-root .drawing-upload button', ['min-height', 'box-sizing']],
  ]

  it.each(expectedRules)('sets the required phone minimums for %s', (selector, properties) => {
    const phone = phoneBlocks.find((block) => rules(block[1])
      .some((rule) => rule.selector === '.lp-cta-row'))
    expect(phone).toBeDefined()
    const target = rules(phone?.[1] || '').find((rule) => rule.selector === selector)
    expect(target).toBeDefined()
    for (const property of properties) {
      const value = property === 'box-sizing' ? 'border-box' : '44px'
      expect(hasDeclaration(target?.body || '', property, value)).toBe(true)
    }
  })

  it.each(expectedRules)('keeps the 44px minimum for %s out of non-phone rules', (selector) => {
    const outsidePhone = css.replace(phonePattern, '')
    const targetRules = rules(outsidePhone).filter((rule) => rule.selector === selector)
    expect(targetRules.some((rule) => hasDeclaration(rule.body, 'min-height', '44px'))).toBe(false)
  })
})
