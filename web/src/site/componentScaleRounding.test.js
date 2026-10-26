// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const files = [
  '../components/popovers.css',
  '../components/rails.css',
  '../components/panels.css',
  '../demo/demo.css',
  '../components/operator/operator.css',
  '../cadedit/ObjectSnapMenu.css',
  '../components/CostTransparencyPanel.css',
  './drawingCameraControls.css',
  './cadOverview.css',
  '../lab/labPreview.css',
]

describe('component v1 scale rounding and text minimum (P6f)', () => {
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

    it(`${file} uses size tokens or relative font sizes`, () => {
      for (const { property, value } of declarations) {
        if (property !== 'font-size') continue
        expect(value, `${file} ${property}: ${value}`)
          .toMatch(/^(?:var\(--leaf-size-[\w-]+\)|(?:\d*\.)?\d+r?em)$/i)
      }
    })

    it(`${file} uses size tokens or inherit in font shorthands without bare pixel sizes`, () => {
      for (const { property, value } of declarations) {
        if (property !== 'font') continue
        expect(value, `${file} ${property}: ${value}`)
          .toMatch(/^(?:inherit)$|var\(--leaf-size-[\w-]+\)/i)
        expect(value, `${file} ${property}: ${value}`)
          .not.toMatch(/(-?(?:\d*\.)?\d+)px\b/i)
      }
    })

    it(`${file} uses radius tokens with only the specified exceptions`, () => {
      let barExceptions = 0
      for (const { selector, property, value } of declarations) {
        if (!/^border-(?:[a-z]+-)*radius$/.test(property)) continue
        if (file === '../components/popovers.css'
          && selector === '.vh-list .vh-row.active .lbar'
          && property === 'border-radius' && value === '1px') {
          barExceptions += 1
          continue
        }
        for (const part of value.split(/[\s/]+/)) {
          const allowed = /^(?:0|50%|var\(--leaf-radius-[\w-]+\))$/.test(part)
            || (/^\d+(?:\.\d+)?px$/i.test(part) && parseFloat(part) >= 999)
          expect(allowed, `${file} ${selector} ${property}: ${value}`).toBe(true)
        }
      }
      expect(barExceptions, file).toBeLessThanOrEqual(1)
    })

    it(`${file} has no pixel font size below 12px`, () => {
      for (const [, property, value] of css.matchAll(/\b(font(?:-size)?)\s*:\s*([^;}]+)/gi)) {
        for (const [, pixels] of value.matchAll(/(-?(?:\d*\.)?\d+)px\b/gi)) {
          expect(Number(pixels), `${file} ${property}: ${value}`).toBeGreaterThanOrEqual(12)
        }
      }
    })
  }
})
