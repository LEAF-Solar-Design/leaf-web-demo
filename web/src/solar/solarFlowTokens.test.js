// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, it, expect } from 'vitest'

const read = (file) => readFileSync(new URL(file, import.meta.url), 'utf8')
const stripComments = (css) => css.replace(/\/\*[\s\S]*?\*\//g, '')
const stripUrls = (css) => css.replace(/url\((?:\\.|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\\)'"])*\)/gi, '')
const file = 'solarFlow.css'
const css = stripComments(read('./solarFlow.css'))
const declarations = [...css.matchAll(/(?:^|[;{}])\s*([\w-]+)\s*:\s*([^;}]+)/g)]
  .map(([, property, value]) => [property, value.trim()])

describe('Solar flow v1 token lint (LS-T1, LS-T2, LS-T3, LS-T4)', () => {
  it('uses color tokens outside --leaf-* declarations (LS-T1)', () => {
    const colors = [...stripUrls(css).matchAll(/(?:^|[;{}])\s*([\w-]+)\s*:\s*([^;}]+)/g)]
      .filter(([, property]) => !property.startsWith('--leaf-'))
    expect(colors.length, file).toBeGreaterThan(0)
    for (const [, property, value] of colors) {
      expect(value, `${file} ${property}: ${value.trim()}`)
        .not.toMatch(/#(?:[\da-f]{8}|[\da-f]{6}|[\da-f]{4}|[\da-f]{3})(?![\da-f])|\b(?:rgba?|hsla?)\(/i)
    }
  })

  it('uses size tokens for font-size (LS-T2)', () => {
    const sizes = declarations.filter(([property]) => property === 'font-size')
    expect(sizes.length, file).toBeGreaterThanOrEqual(8)
    for (const [property, value] of sizes) {
      expect(!/\d/.test(value) || value === '100%' || value.includes('var(--leaf-size-'), `${file} ${property}: ${value}`).toBe(true)
    }
  })

  it('uses radius tokens for literal corners (LS-T3)', () => {
    const radii = declarations.filter(([property]) => /^border-(?:[a-z]+-)*radius$/.test(property))
    expect(radii.length, file).toBeGreaterThan(0)
    for (const [property, value] of radii) {
      // Keywords such as inherit carry no literal radius to tokenize.
      const allowed = !/\d/.test(value) || value === '0' || value === '50%'
        || (/^\d+(?:\.\d+)?px$/.test(value) && parseFloat(value) >= 999)
        || value.includes('var(--leaf-radius-')
      expect(allowed, `${file} ${property}: ${value}`).toBe(true)
    }
  })

  it('uses layer tokens for z-index (LS-T4)', () => {
    const layers = declarations.filter(([property]) => property === 'z-index')
    expect(layers.length, file).toBeGreaterThan(0)
    for (const [property, value] of layers) {
      expect(value === 'auto' || value === '0' || value.includes('var(--leaf-layer-'), `${file} ${property}: ${value}`).toBe(true)
    }
  })

  it('uses the caption size for the shared note and reason rule', () => {
    const rule = css.match(/\.solar-flow-note,\r?\n\.solar-flow-reason\s*\{([^{}]*)\}/)
    expect(rule).not.toBeNull()
    expect(rule[1]).toMatch(/(?:^|;)\s*font-size:\s*var\(--leaf-size-caption\)\s*;/)
  })
})
