import { readFileSync } from 'node:fs'
import { describe, it, expect } from 'vitest'

const read = (file) => readFileSync(new URL(file, import.meta.url), 'utf8')
const stripComments = (css) => css.replace(/\/\*[\s\S]*?\*\//g, '')
const shells = ['cockpit.css', 'studioShell.css'].map((file) => [file, stripComments(read(`./${file}`))])

describe('shell v1 token lint (LS-T2, LS-T3, LS-T4)', () => {
  for (const [file, css] of shells) {
    const declarations = [...css.matchAll(/(?:^|[;{}])\s*([\w-]+)\s*:\s*([^;}]+)/g)]
      .map(([, property, value]) => [property, value.trim()])

    it(`${file} uses layer tokens for z-index (LS-T4)`, () => {
      const layers = declarations.filter(([property]) => property === 'z-index')
      expect(layers.length, file).toBeGreaterThan(0)
      for (const [property, value] of layers) {
        expect(value === 'auto' || value === '0' || value.includes('var(--leaf-layer-'), `${file} ${property}: ${value}`).toBe(true)
      }
    })

    it(`${file} uses radius tokens for literal corners (LS-T3)`, () => {
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

    it(`${file} uses size tokens for font-size (LS-T2)`, () => {
      const sizes = declarations.filter(([property]) => property === 'font-size')
      expect(sizes.length, file).toBeGreaterThan(0)
      for (const [property, value] of sizes) {
        expect(!/\d/.test(value) || value === '100%' || value.includes('var(--leaf-size-'), `${file} ${property}: ${value}`).toBe(true)
      }
    })
  }

  it('the phone ribbon disclosure hides its text while retaining the pseudo-element label', () => {
    const css = shells.find(([file]) => file === 'cockpit.css')[1]
    const phone = css.split('@media (max-width: 600px) {')[1]?.split('\n@media')[0]
    expect(phone).toBeDefined()
    const selector = '.studio-shell .app[data-studio-shell="cockpit"][data-drawer][data-surface] .ribbon-more'
    const rule = [...phone.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
      .find(([, selectors]) => selectors.trim() === selector)
    expect(rule).toBeDefined()
    expect(rule[2]).toMatch(/(?:^|;)\s*color\s*:\s*transparent\s*;/)
    expect(rule[2]).toMatch(/(?:^|;)\s*font-size\s*:\s*var\(--leaf-size-caption\)\s*;/)
    expect(rule[2]).toMatch(/(?:^|;)\s*line-height\s*:\s*0\s*;/)
  })
})
