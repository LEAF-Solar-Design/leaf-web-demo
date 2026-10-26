// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync(new URL('./landing.css', import.meta.url), 'utf8')
  .replace(/\r\n/g, '\n')
  .replace(/\/\*[\s\S]*?\*\//g, '')
const phonePattern = /@media\s*\(\s*max-width\s*:\s*599px\s*\)\s*\{((?:[^{}]|\{[^{}]*\})*)\}/g
const rules = (source) => [...source.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
  .map(([, selector, body]) => ({ selector: selector.trim().replace(/\s+/g, ' '), body }))
const slotSelector = '.stage-root[data-scene="tool"] .lp-top-slot'
const phone = [...css.matchAll(phonePattern)].find((block) => rules(block[1])
  .some((rule) => rule.selector === slotSelector))

const ruleBody = (selector) => {
  expect(phone).toBeDefined()
  const rule = rules(phone?.[1] || '').find((candidate) => candidate.selector === selector)
  expect(rule).toBeDefined()
  return rule?.body || ''
}
const declaration = (body, property) => body.match(new RegExp(`(?:^|;)\\s*${property}\\s*:\\s*([^;]+)`))?.[1].trim()
const pixels = (value) => {
  expect(value).toMatch(/^\d+px$/)
  return Number.parseFloat(value)
}

describe('/try phone top cluster fits its 44px controls', () => {
  it('gives the exact top cluster rule a 44px row starting at 28px', () => {
    const cluster = ruleBody('.tc-topcluster')
    expect(declaration(cluster, 'top')).toBe('28px')
    expect(declaration(cluster, 'height')).toBe('44px')
  })

  it('keeps the entire cluster inside the 72px top bar', () => {
    const cluster = ruleBody('.tc-topcluster')
    const topBar = ruleBody('.stage-root[data-scene="tool"] .lp-topbar, .tc-header')
    const barHeight = pixels(declaration(topBar, 'height'))
    expect(barHeight).toBe(72)
    expect(pixels(declaration(cluster, 'top')) + pixels(declaration(cluster, 'height')))
      .toBeLessThanOrEqual(barHeight)
  })

  it('starts below the wordmark row and the top bar padding', () => {
    const topBar = ruleBody('.stage-root[data-scene="tool"] .lp-topbar, .tc-header')
    const slotHeight = pixels(declaration(ruleBody(slotSelector), 'height'))
    const paddingTop = pixels(declaration(topBar, 'padding')?.split(/\s+/)[0])
    expect(slotHeight).toBe(24)
    expect(paddingTop).toBe(4)
    expect(slotHeight + paddingTop).toBeLessThanOrEqual(pixels(declaration(ruleBody('.tc-topcluster'), 'top')))
  })
})
