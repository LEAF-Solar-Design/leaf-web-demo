// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync(new URL('../styles.css', import.meta.url), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '')
const declarations = [...css.matchAll(/(?:^|[;{}])\s*([\w-]+)\s*:\s*([^;}]+)/g)]
  .map(([, property, value]) => [property.toLowerCase(), value.replace(/\s*!important\s*$/i, '').trim()])

describe('base stylesheet exact-match v1 token lint (LS-T2, LS-T3, LS-T4)', () => {
  it('uses size tokens for the exact 12px, 13px and 15px font sizes (LS-T2)', () => {
    const sizes = declarations.filter(([property]) => property === 'font-size')
    expect(sizes.length).toBeGreaterThan(0)
    for (const [property, value] of sizes) {
      expect(value, `styles.css ${property}: ${value}`).not.toMatch(/^(?:12|13|15)px$/i)
    }
  })

  it('uses radius tokens for every exact 4px and 6px value part (LS-T3)', () => {
    const radii = declarations.filter(([property]) => /^border-(?:[a-z]+-)*radius$/.test(property))
    expect(radii.length).toBeGreaterThan(0)
    for (const [property, value] of radii) {
      const literalParts = value.split(/[\s/]+/).filter((part) => /^(?:4|6)px$/i.test(part))
      expect(literalParts, `styles.css ${property}: ${value}`).toEqual([])
    }
  })

  it('uses size tokens for the remaining rounded pixel font sizes (P6d)', () => {
    const sizes = declarations.filter(([property]) => property === 'font-size')
    expect(sizes.length).toBeGreaterThan(0)
    for (const [property, value] of sizes) {
      expect(value, `styles.css ${property}: ${value}`).not.toMatch(/^(?:12\.5|14|24|30|32)px$/i)
    }
  })

  it('keeps the bar input at the iOS minimum through the body size token (P6d)', () => {
    expect(css).toMatch(/\.bar-input input\s*,\s*\.bar-input \.bar-field\s*\{\s*font-size:\s*calc\(var\(--leaf-size-body\) \+ 1px\)\s*;\s*\}/)
  })

  it('uses radius tokens for every rounded 2px, 3px, 5px, 8px and 9px part (P6d)', () => {
    const radii = declarations.filter(([property]) => /^border-(?:[a-z]+-)*radius$/.test(property))
    expect(radii.length).toBeGreaterThan(0)
    for (const [property, value] of radii) {
      const literalParts = value.split(/[\s/]+/).filter((part) => /^(?:2|3|5|8|9)px$/i.test(part))
      expect(literalParts, `styles.css ${property}: ${value}`).toEqual([])
    }
  })

  it('preserves the small square status markers and resolver bar radius (P6d)', () => {
    const squareMarkers = declarations.filter(([property, value]) => property === 'border-radius' && value === '1.5px')
    expect(squareMarkers).toHaveLength(16)
    expect(css).toMatch(/\.resolver-row\.active \.lbar\s*\{[^{}]*border-radius:\s*1px\s*;/)
  })

  it('uses layer tokens for positive z-index values (LS-T4)', () => {
    const layers = declarations.filter(([property]) => property === 'z-index')
    expect(layers.length).toBeGreaterThan(0)
    for (const [property, value] of layers) {
      const allowed = value === '0' || value === 'auto' || /^-\d+$/.test(value)
        || value.includes('var(--leaf-layer-')
      expect(allowed, `styles.css ${property}: ${value}`).toBe(true)
    }
  })
})
