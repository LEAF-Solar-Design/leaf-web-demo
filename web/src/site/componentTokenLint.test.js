// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const files = [
  '../components/operator/operator.css',
  './sheets.css',
  '../demo/demo.css',
  '../campaigns/campaigns.css',
  '../components/popovers.css',
  '../demo/coach.css',
]

describe('component exact-match v1 token lint (LS-T2, LS-T3, LS-T4)', () => {
  for (const file of files) {
    const css = readFileSync(new URL(file, import.meta.url), 'utf8')
      .replace(/\/\*[\s\S]*?\*\//g, '')
    const declarations = [...css.matchAll(/(?:^|[;{}])\s*([\w-]+)\s*:\s*([^;}]+)/g)]
      .map(([, property, value]) => [property, value.trim().replace(/\s*!important\s*$/i, '').trim()])

    it(`${file} uses tokens for exact font-size matches (LS-T2)`, () => {
      const sizes = declarations.filter(([property]) => property === 'font-size')
      expect(sizes.length, file).toBeGreaterThan(0)
      for (const [property, value] of sizes) {
        expect(value, `${file} ${property}: ${value}`)
          .not.toMatch(/^(?:12|13|15|18|22|28|48)px$/)
      }
    })

    it(`${file} uses tokens for exact radius parts (LS-T3)`, () => {
      const radii = declarations.filter(([property]) => /^border-(?:[a-z]+-)*radius$/.test(property))
      expect(radii.length, file).toBeGreaterThan(0)
      for (const [property, value] of radii) {
        const parts = value.split(/[\s/]+/)
        for (const part of parts) {
          expect(part, `${file} ${property}: ${value}`).not.toMatch(/^(?:4|6|10|16)px$/)
        }
      }
    })

    it(`${file} uses layer tokens for positive z-index (LS-T4)`, () => {
      const layers = declarations.filter(([property]) => property === 'z-index')
      for (const [property, value] of layers) {
        const allowed = value === '0' || value === 'auto' || /^-\d+$/.test(value)
          || value.includes('var(--leaf-layer-')
        expect(allowed, `${file} ${property}: ${value}`).toBe(true)
      }
    })
  }
})
