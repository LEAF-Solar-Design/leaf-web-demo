// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const files = [
  '../cadedit/drawingObjectList.css',
  '../cadedit/ObjectSnapMenu.css',
  '../components/panels.css',
  '../leafPlatform/leafPlatform.css',
  './drawingNavigation.css',
  '../structural.css',
  '../components/rails.css',
  '../lab/widgets/widgets.css',
  './cadOverview.css',
  '../components/ArloProposalReview.css',
  '../components/CostTransparencyPanel.css',
  '../components/resultObjectLinks.css',
  '../lab/labPreview.css',
  './drawingCameraControls.css',
]

describe('remaining exact-match v1 token lint (LS-T2, LS-T3, LS-T4)', () => {
  for (const file of files) {
    const css = readFileSync(new URL(file, import.meta.url), 'utf8')
      .replace(/\/\*[\s\S]*?\*\//g, '')
    const declarations = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
      .flatMap(([, selector, body]) => [...body.matchAll(/(?:^|;)\s*([\w-]+)\s*:\s*([^;]+)/g)]
        .map(([, property, value]) => ({
          selector: selector.trim(),
          property: property.toLowerCase(),
          value: value.replace(/\s*!important\s*$/i, '').trim(),
        })))

    it(`${file} uses tokens for exact font-size matches (LS-T2)`, () => {
      expect(declarations.length, file).toBeGreaterThan(0)
      for (const { property, value } of declarations.filter(({ property }) => property === 'font-size')) {
        expect(value, `${file} ${property}: ${value}`)
          .not.toMatch(/^(?:12|13|15|18|22|28|48)px$/i)
      }
    })

    it(`${file} uses tokens for exact radius parts (LS-T3)`, () => {
      for (const { property, value } of declarations.filter(({ property }) => /^border-(?:[a-z]+-)*radius$/.test(property))) {
        for (const part of value.split(/[\s/]+/)) {
          expect(part, `${file} ${property}: ${value}`).not.toMatch(/^(?:4|6|10|16)px$/i)
        }
      }
    })

    it(`${file} uses layer tokens except for the pinned cad-overview layer (LS-T4)`, () => {
      for (const { selector, property, value } of declarations.filter(({ property }) => property === 'z-index')) {
        const pinned = file === './cadOverview.css' && selector === '.cad-overview' && value === '24'
        const allowed = value === '0' || value === 'auto' || /^-\d+$/.test(value)
          || value.includes('var(--leaf-layer-') || pinned
        expect(allowed, `${file} ${selector} ${property}: ${value}`).toBe(true)
      }
    })
  }
})
