// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const files = [
  './sheets.css',
  '../demo/coach.css',
  '../lab/widgets/widgets.css',
  '../components/detailsDrawerSheet.css',
]
const readCss = path => readFileSync(new URL(path, import.meta.url), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '')

function declarations(css) {
  return [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].flatMap(([, selector, body]) =>
    [...body.matchAll(/(?:^|;)\s*([\w-]+)\s*:\s*([^;]+)/g)].map(([, property, value]) => ({
      selector: selector.trim(),
      property: property.toLowerCase(),
      value: value.replace(/\s*!important\s*$/i, '').trim(),
    })),
  )
}

describe('sheets, coach, widgets and bottom sheet v1 scale rounding', () => {
  for (const file of files) {
    const css = readCss(file)
    const rules = declarations(css)
    const sizes = rules.filter(({ property }) => property === 'font-size')

    it(`${file} uses size tokens or relative font sizes`, () => {
      for (const { selector, value } of sizes) {
        expect(value, `${file} ${selector} font-size: ${value}`)
          .toMatch(/^(?:var\(--leaf-size-[\w-]+\)|(?:\d+(?:\.\d+)?|\.\d+)(?:rem|em))$/)
      }
    })

    it(`${file} uses radius tokens apart from the recorded keeps`, () => {
      const radii = rules.filter(({ property }) => /^border-(?:[a-z]+-)*radius$/.test(property))
      expect(radii.length, file).toBeGreaterThan(0)
      for (const { selector, property, value } of radii) {
        for (const part of value.split(/[\s/]+/)) {
          const pill = /^\d+(?:\.\d+)?px$/.test(part) && parseFloat(part) >= 999
          const keptBar = file === './sheets.css'
            && selector === '.sheets-root .sheet-index-row .bar'
            && property === 'border-radius' && value === '1px'
          const keptWidget = file === '../lab/widgets/widgets.css'
            && selector === '.widget-preview-canvas'
            && property === 'border-radius' && value === '22px'
          const allowed = /^(?:0|50%|var\(--leaf-radius-[\w-]+\))$/.test(part)
            || pill || keptBar || keptWidget
          expect(allowed, `${file} ${selector} ${property}: ${value} (${part})`).toBe(true)
        }
      }
    })

    it(`${file} has no pixel font size below the caption floor`, () => {
      for (const { selector, value } of sizes) {
        for (const [, pixels] of value.matchAll(/(-?(?:\d+(?:\.\d+)?|\.\d+))px\b/g)) {
          expect(Number(pixels), `${file} ${selector} font-size: ${value}`).toBeGreaterThanOrEqual(12)
        }
      }
    })
  }

  it('uses sheet radii on the bottom sheet corners', () => {
    expect(declarations(readCss('../components/detailsDrawerSheet.css'))).toContainEqual({
      selector: '.drawer-layer.drawer-sheet-layer > .drawer.drawer-sheet',
      property: 'border-radius',
      value: 'var(--leaf-radius-sheet) var(--leaf-radius-sheet) 0 0',
    })
  })

  it('preserves the narrow index bar and true-size widget preview corners', () => {
    expect(declarations(readCss('./sheets.css'))).toContainEqual({
      selector: '.sheets-root .sheet-index-row .bar', property: 'border-radius', value: '1px',
    })
    expect(declarations(readCss('../lab/widgets/widgets.css'))).toContainEqual({
      selector: '.widget-preview-canvas', property: 'border-radius', value: '22px',
    })
  })
})
