// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync(new URL('./projectBoard.css', import.meta.url), 'utf8')
  .replace(/\r\n/g, '\n')
  .replace(/\/\*[\s\S]*?\*\//g, '')

function phoneDeclarations(selector) {
  const media = css.match(/@media\s*\(\s*max-width\s*:\s*599px\s*\)\s*\{((?:[^{}]|\{[^{}]*\})*)\}/)
  expect(media, 'phone media block').not.toBeNull()
  const rule = [...media[1].matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .find(([, candidate]) => candidate.trim() === selector)
  expect(rule, selector).toBeDefined()
  return Object.fromEntries([...rule[2].matchAll(/(?:^|;)\s*([\w-]+)\s*:\s*([^;]+)(?=;|$)/g)]
    .map(([, property, value]) => [property, value.trim()]))
}

describe('Browser row targets on phones', () => {
  it('gives row actions a 44 px minimum target in both dimensions', () => {
    expect(phoneDeclarations('.studio-ground-board .ground-row-action')).toMatchObject({
      'min-height': '44px',
      'min-width': '44px',
    })
  })

  it('lets rows wrap without clipping their actions', () => {
    expect(phoneDeclarations('.studio-ground-board .ground-tile li')).toMatchObject({
      'white-space': 'normal',
      overflow: 'visible',
    })
  })

  it('preserves the stacked pane header', () => {
    expect(phoneDeclarations('.studio-ground-board .ground-pane-head')).toMatchObject({
      'flex-direction': 'column',
    })
  })

  it('uses radius tokens instead of nonzero numeric radii', () => {
    const radii = [...css.matchAll(/\bborder-radius\s*:\s*([^;}]+)/g)]
    expect(radii.length).toBeGreaterThan(0)
    for (const [, rawValue] of radii) {
      const value = rawValue.trim()
      expect(value === '0' || !/\d/.test(value) || value.includes('var(--leaf-radius-'), value).toBe(true)
    }
  })
})
