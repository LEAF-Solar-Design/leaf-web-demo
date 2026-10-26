// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync(new URL('./landing.css', import.meta.url), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '')
const declarations = [...css.matchAll(/(?:^|[;{}])\s*([\w-]+)\s*:\s*([^;}]+)/g)]
  .map(([, property, value]) => [property, value.trim()])

describe('front door v1 token lint (LS-T3, LS-T4)', () => {
  it('uses layer tokens for z-index (LS-T4)', () => {
    const layers = declarations.filter(([property]) => property === 'z-index')
    expect(layers.length).toBeGreaterThan(0)
    for (const [property, value] of layers) {
      const allowed = value === 'auto' || value === '0' || value.includes('var(--leaf-layer-')
      expect(allowed, `landing.css ${property}: ${value}`).toBe(true)
    }
  })

  it('uses radius tokens for literal corners (LS-T3)', () => {
    const radii = declarations.filter(([property]) => /^border-(?:[a-z]+-)*radius$/.test(property))
    expect(radii.length).toBeGreaterThan(0)
    for (const [property, value] of radii) {
      // Values without literal numbers, including custom properties, need no token mapping.
      const allowed = !/\d/.test(value) || value === '0' || value === '50%'
        || (/^\d+(?:\.\d+)?px$/.test(value) && parseFloat(value) >= 999)
        || value.includes('var(--leaf-radius-')
      expect(allowed, `landing.css ${property}: ${value}`).toBe(true)
    }
  })

  it('preserves the phone illustration radius at 26px', () => {
    const phoneRules = [...css.matchAll(/[^{}]+\{([^{}]*)\}/g)]
      .map(([, body]) => body)
      .filter((body) => /(?:^|;)\s*height\s*:\s*310px\s*;/.test(body))
    expect(phoneRules).toHaveLength(1)
    expect(phoneRules[0]).toMatch(/(?:^|;)\s*border-radius\s*:\s*calc\(var\(--leaf-radius-sheet\) \+ 10px\)\s*;/)
  })
})
